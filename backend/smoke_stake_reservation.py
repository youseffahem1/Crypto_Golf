"""VERIFICATION OF STAKE RESERVATION AND REALIZED PROFIT/LOSS.

This is the test for the reported bug: the user opened a $100 trade and the
available trading balance still read $500 instead of $400, so the stake was
never visibly reserved. It also pins the rule the client clarified alongside
it — an OPEN position's P/L is UNREALIZED and must not move the account-level
Profit/Loss summary, while a CLOSED position's must.

The whole point is that these are SERVER facts, so the assertions run against
the real service functions on a real (temporary) database. Nothing here is
computed the way the browser computes it, because the browser is the thing
under suspicion.

Sections:
  A. THE $500 -> $400 EXAMPLE      open a $100 BUY, balance drops to $400
  B. THE STAY-OPEN BASES          the balance is $400 the whole minute
  C. CLOSING A WINNER              $500 - $100 then +$120 payout = $520
  D. CLOSING A LOSER               $500 - $100 then +$80 payout  = $480
  E. PROFIT AND LOSS STAY SEPARATE  +$20 and -$60 never become -$40
  F. MULTIPLE OPEN TRADES          $100 + $50 reserves to $350
  G. CLOSING ONE OF MANY           only that trade's stake is released
  H. CLOSE ALL                     every position settles exactly once
  I. REALIZED P/L IS NOT LIVE      an open position changes no summary
  J. IDEMPOTENCE                   a second close moves no money
  K. THE WALLET IS A SEPARATE LEDGER  trading is untouched by wallet moves
  L. THE READ PATHS AGREE          balances endpoint reports $400, not $500

Usage:  python smoke_stake_reservation.py
"""
import os
import sys
import tempfile
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

fd, DB_PATH = tempfile.mkstemp(suffix=".db")
os.close(fd)
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"

from app import models, market_service, trading_service  # noqa: E402
from app.database import SessionLocal, engine, Base  # noqa: E402

Base.metadata.create_all(bind=engine)
db = SessionLocal()

FAILS = []

# One controllable price, so a "the price doubled" is an exact number.
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


def truthy(label, cond):
    check(label, bool(cond), True)


def make_user(usdt=500.0, email=None):
    """A user whose TRADING balance starts at `usdt`. The wallet ledger starts
    at zero, which is the real default — a wallet is only funded by an explicit
    move."""
    email = email or f"stake{models.datetime.utcnow().timestamp()}@test.local"
    u = models.User(email=email, password_hash="x", usdt_balance=usdt,
                    golf_balance=0.0, golf_wallet_balance=0.0)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def trading_balance(user_id, session=None):
    s = session or db
    s.expire_all()
    u = s.query(models.User).filter_by(id=user_id).first()
    return float(u.usdt_balance or 0.0)


def wallet_balance(user_id, session=None):
    s = session or db
    s.expire_all()
    u = s.query(models.User).filter_by(id=user_id).first()
    return float(u.usdt_wallet_balance or 0.0)


def realized(user_id, symbol=None):
    return trading_service.realized_split(db, user_id, symbol)


def open_positions(user_id):
    db.expire_all()
    return db.query(models.Trade).filter_by(
        user_id=user_id, status=models.TradeStatus.OPEN
    ).count()


def open_staked(user_id):
    """The total currently reserved in OPEN positions."""
    db.expire_all()
    rows = db.query(models.Trade).filter_by(
        user_id=user_id, status=models.TradeStatus.OPEN
    ).all()
    return sum(float(t.amount or 0.0) for t in rows)


def row(trade_id):
    db.expire_all()
    return db.query(models.Trade).filter_by(id=trade_id).first()


# =============================================================================
print("\n[A] THE $500 -> $400 EXAMPLE — the stake is reserved the moment it opens")
# =============================================================================
user = make_user(500.0, "stake-a@test.local")
approx("starting trading balance", trading_balance(user.id), 500.0)
approx("starting wallet balance", wallet_balance(user.id), 0.0)
check("nothing is reserved before the trade", open_staked(user.id), 0.0)

t = trading_service.open_trade(db, user.id, "UP", 100.0, 60, "GOLF")

# The single most important assertion in this file.
approx("available balance is $400 with the $100 committed", trading_balance(user.id), 400.0)
check("the position is OPEN", t.status, models.TradeStatus.OPEN)
approx("the reserved amount is the $100 stake", open_staked(user.id), 100.0)
approx("balance + reserved == the $500 it started as", trading_balance(user.id) + open_staked(user.id), 500.0)
approx("the wallet was never involved", wallet_balance(user.id), 0.0)

# =============================================================================
print("\n[B] the stake stays reserved for the whole minute")
# =============================================================================
# Nothing settles it, so a sweep that finds nothing due must not have released
# the stake. A read of the open list is the other thing that could.
for _ in range(5):
    trading_service.expire_due_trades(db, now=t.opened_at + timedelta(seconds=30))
approx("30s in, the balance is still $400", trading_balance(user.id), 400.0)
check("still exactly one open position", open_positions(user.id), 1)
approx("still exactly $100 reserved", open_staked(user.id), 100.0)
check("no profit is booked while the position is OPEN", row(t.id).profit, None)
check("no settled_at written while OPEN", row(t.id).settled_at, None)

# =============================================================================
print("\n[C] closing a WINNER releases the stake AND banks the gain ($500 -> $520)")
# =============================================================================
# Priced off the server's own price, not anything the client supplied.
PRICE["now"] = 0.024  # +20%, so the $100 BUY is worth $120
expect_payout = 100.0 * (0.024 / 0.02)
approx("the server's payout is $120", expect_payout, 120.0)

closed = trading_service.close_trade(db, user.id, t.id)
approx("the realized profit is +$20", closed.profit, 20.0)
check("it closed as a WIN", closed.status, models.TradeStatus.WON)
check("the reason is a sale", closed.close_reason, "SOLD")
approx("balance is $520 = $400 + the $120 stake returned", trading_balance(user.id), 520.0)
check("nothing is reserved any more", open_staked(user.id), 0.0)
check("no positions left", open_positions(user.id), 1 - 1)

r = realized(user.id)
approx("the summary's Profit is +$20", r["profit"], 20.0)
approx("the summary's Loss is $0", r["loss"], 0.0)
check("exactly one closed trade is counted", r["count"], 1)

# =============================================================================
print("\n[D] closing a LOSER returns what is left ($500 -> $496)")
# =============================================================================
user = make_user(500.0, "stake-d@test.local")
PRICE["now"] = 0.02
t = trading_service.open_trade(db, user.id, "UP", 100.0, 60, "GOLF")
approx("stake reserved", trading_balance(user.id), 400.0)

PRICE["now"] = 0.0192  # -4%, so the $100 BUY is worth $96
closed = trading_service.close_trade(db, user.id, t.id)
approx("the realized loss is -$4", closed.profit, -4.0)
check("it closed as a LOSS", closed.status, models.TradeStatus.LOST)
approx("balance is $496 = $400 + the $96 actually worth", trading_balance(user.id), 496.0)

r = realized(user.id)
approx("summary Profit stays $0", r["profit"], 0.0)
approx("summary Loss is -$4", r["loss"], -4.0)

# The two directions floor differently, and that asymmetry is the EXISTING
# documented pricing, which this change must not alter:
#   SELL -> floored at 1% of the stake, so it always returns something;
#   BUY  -> no floor, so a total wipeout returns nothing.
# The point of the assertions is that a wipeout can never take the balance
# below zero, whichever direction it was.
user = make_user(500.0, "stake-d2@test.local")
PRICE["now"] = 0.02
t = trading_service.open_trade(db, user.id, "UP", 100.0, 60, "GOLF")
PRICE["now"] = 0.0000001  # effectively total loss
closed = trading_service.close_trade(db, user.id, t.id)
approx("a wiped-out BUY loses essentially the whole stake", closed.profit, -99.9995)
approx("balance is $400.0005, never negative", trading_balance(user.id), 400.0005)
truthy("the balance can never be driven negative", trading_balance(user.id) >= 0)

user = make_user(500.0, "stake-d3@test.local")
PRICE["now"] = 0.02
t = trading_service.open_trade(db, user.id, "DOWN", 100.0, 60, "GOLF")
PRICE["now"] = 10.0  # a SELL that goes completely the wrong way
closed = trading_service.close_trade(db, user.id, t.id)
approx("a wiped-out SELL keeps its 1% floor", closed.profit, -99.0)
approx("balance is $401 — only the $1 floor came back", trading_balance(user.id), 401.0)
truthy("and that is still not negative", trading_balance(user.id) >= 0)

# =============================================================================
print("\n[E] PROFIT AND LOSS STAY SEPARATE — never netted into one number")
# =============================================================================
user = make_user(500.0, "stake-e@test.local")
PRICE["now"] = 0.02

t1 = trading_service.open_trade(db, user.id, "UP", 100.0, 60, "GOLF")
PRICE["now"] = 0.024  # +20%
c1 = trading_service.close_trade(db, user.id, t1.id)

PRICE["now"] = 0.02   # trade 2 enters at the same $0.02
t2 = trading_service.open_trade(db, user.id, "UP", 100.0, 60, "GOLF")
PRICE["now"] = 0.008  # -60% -> -$60
c2 = trading_service.close_trade(db, user.id, t2.id)

approx("trade A realized +$20", c1.profit, 20.0)
approx("trade B realized -$60", c2.profit, -60.0)

r = realized(user.id)
approx("Profit is +$20 on its own", r["profit"], 20.0)
approx("Loss is -$60 on its own", r["loss"], -60.0)
# The client's explicit instruction: do NOT collapse these to -$40.
truthy("the two are NOT netted into a single -$40", r["profit"] != -40.0 and r["loss"] != -40.0)
approx("the net is reported separately, only as a derived figure", r["net"], -40.0)
approx("balance reflects BOTH results", trading_balance(user.id), 500.0 + 20.0 - 60.0)

# =============================================================================
print("\n[F] MULTIPLE OPEN TRADES reserve each stake separately")
# =============================================================================
user = make_user(500.0, "stake-f@test.local")
PRICE["now"] = 0.02

t1 = trading_service.open_trade(db, user.id, "UP", 100.0, 60, "GOLF")
approx("after a $100 trade", trading_balance(user.id), 400.0)
t2 = trading_service.open_trade(db, user.id, "UP", 50.0, 60, "GOLF")
approx("after a $50 trade the balance is $350", trading_balance(user.id), 350.0)
t3 = trading_service.open_trade(db, user.id, "UP", 25.0, 60, "GOLF")
approx("after a $25 trade the balance is $325", trading_balance(user.id), 325.0)
approx("total reserved is $175", open_staked(user.id), 175.0)
check("three positions are open", open_positions(user.id), 3)

# The balance guard must use the RESERVED figure, not the raw one.
over = None
try:
    trading_service.open_trade(db, user.id, "UP", 400.0, 60, "GOLF")
except trading_service.TradingError as e:
    over = str(e)
truthy("a trade larger than the available $325 is rejected", over is not None)
approx("a rejected trade reserves nothing", trading_balance(user.id), 325.0)
approx("still exactly $175 reserved", open_staked(user.id), 175.0)

# =============================================================================
print("\n[G] closing ONE of several releases only that trade's stake")
# =============================================================================
# $325 available, three positions open. Settle the $100 one and the other two
# must stay reserved — this is the "do not accidentally release a still-open
# position's stake" case.
PRICE["now"] = 0.022  # +10%: the $100 is worth $110
c1 = trading_service.close_trade(db, user.id, t1.id)
approx("the $100 position realized +$10", c1.profit, 10.0)
approx("balance is $435 = $325 + the $110 it was worth", trading_balance(user.id), 435.0)
approx("the other two are STILL reserved ($75)", open_staked(user.id), 75.0)
check("two positions remain open", open_positions(user.id), 2)
check("the $50 position is still OPEN", row(t2.id).status, models.TradeStatus.OPEN)
check("the $25 position is still OPEN", row(t3.id).status, models.TradeStatus.OPEN)
approx("the realized total so far is +$10", realized(user.id)["profit"], 10.0)

# =============================================================================
print("\n[H] CLOSE ALL settles every open position exactly once")
# =============================================================================
before = trading_balance(user.id)
PRICE["now"] = 0.03
batch = trading_service.close_all_trades(db, user.id)
check("both remaining positions were returned", len(batch), 2)
check("nothing is open any more", open_positions(user.id), 0)
approx("nothing is reserved", open_staked(user.id), 0.0)

expected = before
for t_ in (t2, t3):
    ratio = 0.03 / float(row(t_.id).entry_price)
    expected += float(t_.amount) * ratio
approx("the balance is credited each stake at the server's price", trading_balance(user.id), expected)
r = realized(user.id)
check("all three trades are in the summary", r["count"], 3)

# A second CLOSE ALL must move nothing.
settled = trading_balance(user.id)
again = trading_service.close_all_trades(db, user.id)
check("a second CLOSE ALL returns nothing", len(again), 0)
approx("...and pays nothing", trading_balance(user.id), settled)

# =============================================================================
print("\n[I] an OPEN position moves NO realized summary")
# =============================================================================
user = make_user(500.0, "stake-i@test.local")
PRICE["now"] = 0.02
t = trading_service.open_trade(db, user.id, "UP", 100.0, 60, "GOLF")

r0 = realized(user.id)
approx("with nothing closed, Profit is $0", r0["profit"], 0.0)
approx("with nothing closed, Loss is $0", r0["loss"], 0.0)
check("no closed trades counted", r0["count"], 0)

# Drive the price hard UP. The position is deeply in profit, but nothing is
# realized, so the account summary must not move by a cent.
PRICE["now"] = 0.05
for _ in range(3):
    trading_service.expire_due_trades(db, now=t.opened_at + timedelta(seconds=30))
r1 = realized(user.id)
approx("a +150% move leaves Profit at $0", r1["profit"], 0.0)
approx("a +150% move leaves Loss at $0", r1["loss"], 0.0)
check("still no closed trades", r1["count"], 0)
approx("the balance is untouched by the move", trading_balance(user.id), 400.0)

# ...and the same to the downside.
PRICE["now"] = 0.0000001
for _ in range(3):
    trading_service.expire_due_trades(db, now=t.opened_at + timedelta(seconds=30))
r2 = realized(user.id)
approx("a -99.999% move still leaves Loss at $0", r2["loss"], 0.0)
approx("a -99.999% move still leaves Profit at $0", r2["profit"], 0.0)
approx("the balance is STILL untouched", trading_balance(user.id), 400.0)

# Only the close realizes it — and it realizes what the server would actually
# pay, which is not the raw -$99.9995 the price implied.
c = trading_service.close_trade(db, user.id, t.id)
approx("the close realizes the wiped-out result", c.profit, -99.9995)
r3 = realized(user.id)
approx("now, and only now, Loss appears", r3["loss"], -99.9995)
approx("Profit is still $0", r3["profit"], 0.0)
approx("and the balance is $400.0005", trading_balance(user.id), 400.0005)

# =============================================================================
print("\n[J] IDEMPOTENCE — a repeat close moves no money")
# =============================================================================
user = make_user(500.0, "stake-j@test.local")
PRICE["now"] = 0.02
t = trading_service.open_trade(db, user.id, "UP", 100.0, 60, "GOLF")
PRICE["now"] = 0.024

first = trading_service.close_trade(db, user.id, t.id)
after_first = trading_balance(user.id)
for _ in range(5):
    again = trading_service.close_trade(db, user.id, t.id)
approx("the balance is unchanged by five repeat closes", trading_balance(user.id), after_first)
approx("the recorded profit is the same every time", again.profit, first.profit)
approx("the summary counted it once", realized(user.id)["count"], 1)
approx("...profit is $20, not $100", realized(user.id)["profit"], 20.0)

# =============================================================================
print("\n[K] THE WALLET IS A SEPARATE LEDGER")
# =============================================================================
user = make_user(500.0, "stake-k@test.local")
PRICE["now"] = 0.02
t = trading_service.open_trade(db, user.id, "UP", 100.0, 60, "GOLF")
PRICE["now"] = 0.024
trading_service.close_trade(db, user.id, t.id)

approx("trading is $520 after the winner", trading_balance(user.id), 520.0)
approx("the wallet is still $0 — nothing was transferred", wallet_balance(user.id), 0.0)

# Trading further must not leak into the wallet, either.
PRICE["now"] = 0.02
t2 = trading_service.open_trade(db, user.id, "UP", 100.0, 60, "GOLF")
approx("a further stake leaves the wallet at $0", wallet_balance(user.id), 0.0)
PRICE["now"] = 0.024
trading_service.close_all_trades(db, user.id)
approx("closing everything still leaves the wallet at $0", wallet_balance(user.id), 0.0)
approx("...while trading moved on its own: $520 - $100 staked + $120 back",
       trading_balance(user.id), 540.0)

# =============================================================================
print("\n[L] THE READ PATHS REPORT THE RESERVED BALANCE, NOT THE RAW ONE")
# =============================================================================
# What the browser actually calls. If this said $500, the UI would show $500
# even though the row was debited — which is the reported symptom.
user = make_user(500.0, "stake-l@test.local")
PRICE["now"] = 0.02
t = trading_service.open_trade(db, user.id, "UP", 100.0, 60, "GOLF")
db.expire_all()

from app import swap_service  # noqa: E402

split = swap_service.split_balances(db, user.id)
approx("the balances endpoint reports $400 as TRADING", float(split["trading"]["USDT"]), 400.0)
approx("...and the wallet side is untouched", float(split["wallet"]["USDT"]), 0.0)
truthy("the two ledgers are distinct keys", "trading" in split and "wallet" in split)

PRICE["now"] = 0.024
trading_service.close_trade(db, user.id, t.id)
split2 = swap_service.split_balances(db, user.id)
approx("after closing, the endpoint reports $520", float(split2["trading"]["USDT"]), 520.0)
approx("the wallet is still $0", float(split2["wallet"]["USDT"]), 0.0)

# ...and it must be the SAME number after a fresh session, or a page refresh
# would show a different figure from the one the open produced.
db.close()
db2 = SessionLocal()
split3 = swap_service.split_balances(db2, user.id)
approx("a fresh session still reports $520", float(split3["trading"]["USDT"]), 520.0)
approx("the realized P/L persisted", trading_service.realized_split(db2, user.id)["profit"], 20.0)
db2.close()

market_service.get_current_price = _real_price
db.close()

print()
if FAILS:
    print(f"FAILED ({len(FAILS)}):")
    for f in FAILS:
        print(f"  - {f}")
    sys.exit(1)
print("All stake-reservation / realized P-L checks passed.")
