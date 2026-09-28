"""End-to-end verification of the trade LIFECYCLE and its accounting.

Runs against a throwaway SQLite file. Asserts, in order:
   1. a position survives its `closes_at` and is never force-settled
   2. waiting for days changes nothing about the position or the balance
   3. settlement is SERVER-AUTHORITATIVE - a client-supplied value is ignored,
      so a $10 position cannot claim $99,999
   4. a close is IDEMPOTENT - closing twice pays once
   5. settlement is DIRECTION-AWARE - a SELL is the mirror of a BUY
   6. Close All closes the whole book in one transaction and pays the sum
   7. Close All is idempotent - a second press pays nothing
   8. Close All can be scoped to a single instrument
   9. one account can never close another account's position
  10. realized PROFIT / LOSS still split without netting
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

fd, DB_PATH = tempfile.mkstemp(suffix=".db")
os.close(fd)
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"

from app import models, market_service, trading_service  # noqa: E402
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

# --- 1. a position survives its closes_at and is never force-settled -------
print("\n[1] a position is not destroyed by the clock")
PRICE["now"] = 0.02
t = trading_service.open_trade(db, user.id, "UP", 10.0, 60, "GOLF")
check("opened", t.status, models.TradeStatus.OPEN)
check("stake debited", balance(user.id), 990.0)

# Backdate the position's closes_at far into the past - exactly the state the
# removed background settlement loop existed to exploit.
stale = t.id
db.query(models.Trade).filter_by(id=t.id).update({"closes_at": datetime.utcnow() - timedelta(days=7)})
db.commit()

# There is no settle_due_trades() any more, so the sweep that used to run every
# two seconds has nothing to call. Assert the function is genuinely gone rather
# than merely unused, and that the stale position is still open.
check("settle_due_trades removed", hasattr(trading_service, "settle_due_trades"), False)
check("still OPEN 7 days past closes_at", status_of(stale), models.TradeStatus.OPEN)
check("balance untouched while waiting", balance(user.id), 990.0)
check("unrealized did not touch the balance", balance(user.id), 990.0)

# --- 2. closing long after opening still settles normally -------------------
print("\n[2] a position opened long ago still closes normally")
PRICE["now"] = 0.02
closed = trading_service.close_trade(db, user.id, stale)
approx("stake returned", closed.profit, 0.0)
approx("balance restored", balance(user.id), 1000.0)
check("settled as history", closed.status, models.TradeStatus.WON)

# --- 3. settlement is server-authoritative ---------------------------------
print("\n[3] a client cannot name the figure it is paid")
PRICE["now"] = 0.02
t2 = trading_service.open_trade(db, user.id, "UP", 10.0, 60, "GOLF")
check("balance after open", balance(user.id), 990.0)
PRICE["now"] = 0.04  # the market doubles; a BUY is therefore worth 2x

# A hostile client claims a huge value. It must be ignored entirely: the
# server pays exactly the mark-to-market figure for its own feed.
c2 = trading_service.close_trade(db, user.id, t2.id, value=99999.0)
approx("profit is server-priced, not claimed", c2.profit, 10.0)
approx("balance credited 20, not 99999", balance(user.id), 1010.0)

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
