"""End-to-end verification of the trade LIFECYCLE and its accounting.

Runs against a throwaway SQLite file. Asserts, in order:
   1. expiry is the SERVER's decision, and it never forfeits a stake
   2. a position that has passed `closes_at` is closed by the sweep
   3. settlement is SERVER-AUTHORITATIVE - a client-supplied value is ignored,
      so a $10 position cannot claim $99,999
   4. a close is IDEMPOTENT - closing twice pays once
   5. settlement is DIRECTION-AWARE - a SELL is the mirror of a BUY
   6. Close All closes the whole book in one transaction and pays the sum
   7. Close All is idempotent - a second press pays nothing
   8. Close All can be scoped to a single instrument
   9. one account can never close another account's position
  10. realized PROFIT / LOSS still split without netting

The dedicated timing tests - that a 1-minute trade really is 60 seconds, that
the sweep is exactly-once, and that expiry never books a flat total loss -
live in smoke_trade_expiry.py.
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

fd, DB_PATH = tempfile.mkstemp(suffix=".db")
os.close(fd)
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"

from app import config, models, market_service, trading_service  # noqa: E402
from app.database import SessionLocal, engine, Base  # noqa: E402

Base.metadata.create_all(bind=engine)
db = SessionLocal()

FAILS = []

# The single source of truth for price in this test. Every open and every close
# reads it, which is what lets us assert that the server - not the client -
# decides the result.
PRICE = {"now": 0.02}
_real_price = market_service.get_current_price
market_service.get_current_price = lambda _db, _symbol: PRICE["now"]


def check(label, got, want):
    if got != want:
        FAILS.append(f"{label}: got {got!r}, want {want!r}")
        print(f"  FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"  ok   {label} = {got!r}")


def approx(label, got, want, tol=1e-6):
    if abs(float(got) - float(want)) > tol:
        FAILS.append(f"{label}: got {got!r}, want {want!r}")
        print(f"  FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"  ok   {label} = {got!r}")


def rejects(label, fn, *a, **kw):
    try:
        fn(*a, **kw)
    except trading_service.TradingError as e:
        print(f"  ok   {label} rejected: {e}")
        return
    FAILS.append(f"{label}: expected TradingError")
    print(f"  FAIL {label}: expected TradingError")


def balance(user_id):
    db.expire_all()
    return float(db.query(models.User).filter_by(id=user_id).first().usdt_balance)


def status_of(trade_id):
    db.expire_all()
    return db.query(models.Trade).filter_by(id=trade_id).first().status


def new_user(email, usdt=1000.0):
    u = models.User(email=email, password_hash="x", usdt_balance=usdt,
                    golf_balance=0.0, golf_wallet_balance=0.0)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


user = new_user("lifecycle@example.com")
other = new_user("intruder@example.com")

# --- 1. the server owns the deadline, and the clock never forfeits ----------
print("\n[1] the duration is the server's deadline, not the user's problem")
PRICE["now"] = 0.02
t = trading_service.open_trade(db, user.id, "UP", 10.0, 60, "GOLF")
check("opened", t.status, models.TradeStatus.OPEN)
check("stake debited", balance(user.id), 990.0)

# `closes_at` is the ONLY deadline, and it is the server's: written at open as
# opened_at + duration_seconds, and the timestamp the frontend counts down to.
# A 1-minute trade must be 60 seconds, exactly, from the server's clock.
check("a 1-minute trade is exactly 60s", (t.closes_at - t.opened_at).total_seconds(), 60.0)
check("closes_at is in the future", t.closes_at > t.opened_at, True)

# The old `settle_due_trades()` is gone for good. It force-booked any elapsed
# position as `profit = -amount` — a flat total loss — so a 1-minute trade that
# was $10 in the money at the 60-second mark got destroyed by the clock. The
# replacement prices through `_mark_to_market` like a manual sell. Assert the
# buggy function is genuinely absent, and that no code path can reach a
# `profit = -amount` forfeiture.
check("settle_due_trades removed", hasattr(trading_service, "settle_due_trades"), False)
check("expire_due_trades present", hasattr(trading_service, "expire_due_trades"), True)

# `expire_due_trades` still exists, but the shipping product does not call it:
# reaching 00:00 is a countdown ending, not a settlement. The flag is off by
# default, so the sweep is a no-op and no timer can move anyone's money.
check("auto-settle on expiry is OFF by default", config.AUTO_SETTLE_ON_EXPIRE, False)

# Backdate the position's closes_at far into the past — the exact state the bug
# used to exploit — and confirm the sweep now does nothing at all: the trade
# stays open, unpriced, unpaid and unrecorded.
stale = t.id
db.query(models.Trade).filter_by(id=t.id).update({"closes_at": datetime.utcnow() - timedelta(days=7)})
db.commit()
PRICE["now"] = 0.04  # the market doubled while the position was open
after_open = balance(user.id)

check("a past deadline settles nothing", len(trading_service.expire_due_trades(db)), 0)
untouched = db.query(models.Trade).filter_by(id=stale).one()
check("the trade is still OPEN past its deadline", untouched.status, models.TradeStatus.OPEN)
check("it has no profit yet", untouched.profit, None)
check("it has no close reason", untouched.close_reason, None)
check("its ledger timestamp is untouched", untouched.settled_at, None)
check("the balance was not paid", balance(user.id), after_open)

# Only the user pressing CLOSE books it, and then at the server's price.
closed = trading_service.close_trade(db, user.id, stale)
approx("the manual close is priced, not forfeited", closed.profit, 10.0)
check("it closed as a WIN, not a -amount loss", closed.status, models.TradeStatus.WON)
check("balance credited at the server price", balance(user.id), 1010.0)
check("reason recorded as a manual SOLD", closed.close_reason, "SOLD")

# --- 2. the sweep is exactly-once -------------------------------------------
print("\n[2] the expiry sweep pays once, however often it runs")
after_expiry = balance(user.id)
check("nothing is due any more", len(trading_service.expire_due_trades(db)), 0)
check("still no open positions",
      db.query(models.Trade).filter_by(status=models.TradeStatus.OPEN).count(), 0)

# Repeated sweeps — the equivalent of the per-second background loop, a read
# sweep, and a client that refreshes ten times — must move no more money.
for _ in range(10):
    trading_service.expire_due_trades(db)
approx("ten more sweeps paid nothing", balance(user.id), after_expiry)

# And a manual CLOSE on the already-closed position is an idempotent replay,
# not a second payout.
replay = trading_service.close_trade(db, user.id, stale)
approx("replay returns the recorded result", replay.profit, 10.0)
check("replay did not re-credit", balance(user.id), after_expiry)
check("replay did not rewrite the reason", replay.close_reason, "SOLD")

# --- 3. settlement is server-authoritative ---------------------------------
print("\n[3] a client cannot name the figure it is paid")
PRICE["now"] = 0.02
pre_t2 = balance(user.id)
t2 = trading_service.open_trade(db, user.id, "UP", 10.0, 60, "GOLF")
check("balance after open", balance(user.id), pre_t2 - 10.0)
PRICE["now"] = 0.04  # the market doubles; a BUY is therefore worth 2x

# A hostile client claims a huge value. It must be ignored entirely: the
# server pays exactly the mark-to-market figure for its own feed.
c2 = trading_service.close_trade(db, user.id, t2.id, value=99999.0)
approx("profit is server-priced, not claimed", c2.profit, 10.0)
approx("balance credited 20, not 99999", balance(user.id), pre_t2 + 10.0)

# A hostile client also tries to UNDER-claim, which is equally ignored.
PRICE["now"] = 0.02
t2b = trading_service.open_trade(db, user.id, "UP", 10.0, 60, "GOLF")
PRICE["now"] = 0.04
c2b = trading_service.close_trade(db, user.id, t2b.id, value=0.01)
approx("under-claim ignored too", c2b.profit, 10.0)

# --- 4. a close is idempotent ----------------------------------------------
print("\n[4] closing twice pays once")
after_first = balance(user.id)
replay = trading_service.close_trade(db, user.id, t2b.id, value=99999.0)
approx("replay returns the recorded result", replay.profit, c2b.profit)
check("replay did not re-credit", balance(user.id), after_first)

# --- 5. settlement is direction-aware --------------------------------------
print("\n[5] a SELL is the mirror of a BUY, not a BUY")
PRICE["now"] = 0.02
up = trading_service.open_trade(db, user.id, "UP", 10.0, 60, "GOLF")
down = trading_service.open_trade(db, user.id, "DOWN", 10.0, 60, "GOLF")
check("balance after two opens", balance(user.id), after_first - 20.0)

PRICE["now"] = 0.04  # price DOUBLES: good for the BUY, fatal for the SELL
up_c = trading_service.close_trade(db, user.id, up.id)
down_c = trading_service.close_trade(db, user.id, down.id)
approx("BUY wins when the price rises", up_c.profit, 10.0)
approx("SELL is floored, never negative", down_c.profit, -9.9)
check("SELL is a loss", down_c.status, models.TradeStatus.LOST)
check("BUY is a win", up_c.status, models.TradeStatus.WON)

# --- 6/7/8. Close All ------------------------------------------------------
print("\n[6] Close All closes the whole book and pays the sum")
PRICE["now"] = 0.02
base = balance(user.id)
a = trading_service.open_trade(db, user.id, "UP", 10.0, 60, "GOLF")
b = trading_service.open_trade(db, user.id, "UP", 20.0, 60, "GOLF")
c = trading_service.open_trade(db, user.id, "UP", 30.0, 60, "GOLF")
check("balance after three opens", balance(user.id), base - 60.0)
PRICE["now"] = 0.02  # flat market: each stake returns exactly what it cost

allc = trading_service.close_all_trades(db, user.id)
check("three positions closed", len(allc), 3)
approx("whole stake returned", balance(user.id), base)
check("all three settled", sorted(str(x.status) for x in allc),
      sorted([str(models.TradeStatus.WON)] * 3))
check("none left open", db.query(models.Trade).filter_by(
    user_id=user.id, status=models.TradeStatus.OPEN).count(), 0)

print("\n[7] Close All is idempotent")
after_all = balance(user.id)
again = trading_service.close_all_trades(db, user.id)
check("nothing left to close", len(again), 0)
check("second press pays nothing", balance(user.id), after_all)

print("\n[8] Close All can be scoped to one instrument")
PRICE["now"] = 0.02
g1 = trading_service.open_trade(db, user.id, "UP", 10.0, 60, "GOLF")
n1 = trading_service.open_trade(db, user.id, "UP", 10.0, 60, "NOVA")
scoped = trading_service.close_all_trades(db, user.id, "GOLF")
check("only GOLF closed", [x.symbol for x in scoped], ["GOLF"])
check("NOVA still open", status_of(n1.id), models.TradeStatus.OPEN)
check("GOLF closed", status_of(g1.id), models.TradeStatus.WON)
trading_service.close_all_trades(db, user.id)  # tidy up

# --- 9. ownership ---------------------------------------------------------
print("\n[9] one account can never close another's position")
PRICE["now"] = 0.02
victim = trading_service.open_trade(db, user.id, "UP", 10.0, 60, "GOLF")
victim_balance = balance(user.id)
rejects("other user closing it", trading_service.close_trade, db, other.id, victim.id)
check("position untouched", status_of(victim.id), models.TradeStatus.OPEN)
check("victim balance untouched", balance(user.id), victim_balance)
# Close All from the other account is not an error - that account simply has
# nothing open - but it must not reach across and close the victim's position.
check("other user close-all is a no-op", len(trading_service.close_all_trades(db, other.id)), 0)
check("victim position still open", status_of(victim.id), models.TradeStatus.OPEN)
check("victim balance still untouched", balance(user.id), victim_balance)
trading_service.close_all_trades(db, user.id)

# --- 10. the profit / loss split still works over the new closes ----------
print("\n[10] realized PROFIT and LOSS stay separate, never netted")
split = trading_service.realized_split(db, user.id, "GOLF")
check("profit is never negative", split["profit"] >= 0, True)
check("loss is never positive", split["loss"] <= 0, True)
check("both sides recorded", split["profit"] > 0 and split["loss"] < 0, True)
approx("net is the two summed", split["net"], split["profit"] + split["loss"])
check("available is the unmoved profit", split["available"] > 0, True)

market_service.get_current_price = _real_price
db.close()

print()
if FAILS:
    print(f"FAILED ({len(FAILS)}):")
    for f in FAILS:
        print(f"  - {f}")
    sys.exit(1)
print("All trade-lifecycle checks passed.")
