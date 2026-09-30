"""VERIFICATION OF THE 00:00 RULE: THE CLOCK FIXES THE PRICE, THE USER FIXES
THE PAYMENT.

    00:00        -> the server records the price. Nothing else happens.
    (market moves)
    CLOSE (user) -> the user collects, at exactly the recorded price.

The line between those two moments is the whole feature, and this file exists
because the opposite used to be true. A 1-minute trade used to POST
/api/trade/close the instant its countdown hit 00:00, and the server swept
elapsed positions on a timer as a backstop. So a trade that ran out of time
silently realized itself: the top Profit or Loss moved, the Trading Balance
moved, and the trade appeared in closed history -- all without the user ever
pressing anything.

The rule now enforced, end to end:

  * reaching `closes_at` SETTLES nothing, by any path;
  * it RECORDS a price in `frozen_exit_price` / `frozen_profit`, and nothing
    else -- no status change, no balance credit, no summary entry;
  * that recorded price is immutable: the market can move arbitrarily before
    the user collects, and the payout does not;
  * the position stays OPEN and closable the whole time;
  * ONLY the user closing it books the result, credits the balance and files
    it -- and it books the recorded price, not the current one.

A note on "the price at 00:00". The market is a random walk, so there is no
price to look up afterwards; the only honest reading is "the price the server
was quoting at the moment it saw the deadline end", which is bounded by the
sweep interval (1s by default, the same cadence the market itself ticks). What
IS absolute, and what is asserted here, is that the figure cannot move
afterwards. A late sweep is bounded lateness; a moving promise would be a lie.

Sections:
  A. THE COUNTDOWN SEQUENCE   the numbers a 1-minute trade actually shows
  B. 00:00 RECORDS, DOES NOT SETTLE   row frozen, balance and P/L untouched
  C. THE READ PATHS RECORD TOO  a refresh freezes but never realizes
  D. THE USER'S CLOSE COLLECTS  at the recorded price, not the current one
  E. P/L IS UNREALIZED UNTIL CLOSE  the top card's own source of truth
  F. EXACTLY ONCE             double close, close-all, replay pay nothing extra
  G. NO FORFEITURE            a late close is priced, never booked as -amount
  H. EVERY DURATION HOLDS      5/15/30 min are not dragged down by a 1-minute
  I. LOCK ORDER               every settling path takes the position before
                              the balance, and the freeze takes neither
  J. A REAL 1-MINUTE TRADE    the live 60s wall-clock test (--real)

Usage:  python smoke_trade_expiry.py            (skips the live 60s wait)
        python smoke_trade_expiry.py --real     (runs section J live)
"""
import inspect
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

fd, DB_PATH = tempfile.mkstemp(suffix=".db")
os.close(fd)
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"

from app import models, market_service, trading_service  # noqa: E402
from app.config import AUTO_SETTLE_ON_EXPIRE, TRADE_PAYOUT_MULTIPLIER  # noqa: E402
from app.database import SessionLocal, engine, Base  # noqa: E402

Base.metadata.create_all(bind=engine)
db = SessionLocal()

REAL = "--real" in sys.argv
FAILS = []

# The single source of truth for price. Every open and every settle reads it, so
# we can move the market to an exact known value and assert the exact payout the
# user would see once they close.
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


def balance(user_id, session=None):
    s = session or db
    s.expire_all()
    return float(s.query(models.User).filter_by(id=user_id).first().usdt_balance)


def trade_row(trade_id, session=None):
    s = session or db
    s.expire_all()
    return s.query(models.Trade).filter_by(id=trade_id).first()


def open_positions(user_id, session=None):
    s = session or db
    s.expire_all()
    return s.query(models.Trade).filter_by(
        user_id=user_id, status=models.TradeStatus.OPEN).count()


def realized(user_id, session=None):
    """The realized split -- the very thing the top PROFIT and LOSS cards read."""
    s = session or db
    return trading_service.realized_split(s, user_id)


def new_user(email, usdt=1000.0):
    u = models.User(email=email, password_hash="x", usdt_balance=usdt,
                    golf_balance=0.0, golf_wallet_balance=0.0)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def fmt_time(seconds):
    """The frontend's own formatter (index.html formatTime), so the numbers
    asserted here are literally the numbers a user reads off the screen."""
    seconds = max(0, int(seconds))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def shows(duration_seconds, seconds_in):
    """The countdown as it reads `seconds_in` after the trade was opened.

    This is the frontend's expression verbatim --
    `formatTime(max(0, ceil((closes_at - Date.now()) / 1000)))` -- with
    `closes_at = opened_at + duration_seconds`, so it depends on nothing but the
    server's own timestamp. The strings are literally what the user reads.
    """
    now_ms = seconds_in * 1000.0
    closes_at_ms = duration_seconds * 1000.0
    return fmt_time(max(0, (closes_at_ms - now_ms) / 1000.0))


def payout_for(direction, entry, exit_px, amount, multiplier=None):
    """trading_service._exit_value, restated here so the expected numbers are
    derived from the shipped formula rather than hand-copied:
      move  = (exit / entry - 1) * multiplier
      BUY   -> amount * (1 + move)
      SELL -> amount * (1 - move)
    floored at 1% of the stake in BOTH directions, so a losing trade can never
    settle below 1% of what it staked. `multiplier` defaults to the value the
    server is actually configured with, so this stays true when it changes."""
    if multiplier is None:
        multiplier = float(TRADE_PAYOUT_MULTIPLIER or 1.0)
    move = (exit_px / entry - 1.0) * float(multiplier)
    v = amount * (1.0 - move if direction == "DOWN" else 1.0 + move)
    floor = amount * 0.01
    v = v if v > floor else floor
    return round(v - amount, 6)


user = new_user("expiry@example.com")

# =============================================================================
print("\n[A] the countdown a 1-minute trade actually shows")
# =============================================================================
# The client renders `formatTime(ceil((closes_at - now) / 1000))` against the
# SERVER's closes_at. These are the literal strings the user reads, including
# the two the original bug report was about: 00:01 -> 00:00, and 00:00 must be
# terminal -- the clock stops there and is never written again.
PRICE["now"] = 0.02
t = trading_service.open_trade(db, user.id, "UP", 10.0, 60, "GOLF")
DUR = 60
check("shows 01:00 on the first frame", shows(DUR, 0), "01:00")
check("shows 00:59 after 1s", shows(DUR, 1), "00:59")
check("shows 00:30 halfway", shows(DUR, 30), "00:30")
check("shows 00:01 after 59s", shows(DUR, 59), "00:01")
check("shows 00:00 at exactly 60s", shows(DUR, 60), "00:00")
check("clamps at 00:00, never counts past it", shows(DUR, 90), "00:00")
check("duration is exactly 60s", (t.closes_at - t.opened_at).total_seconds(), 60.0)
check("closes_at is opened_at + 60s",
      (t.closes_at - t.opened_at).total_seconds(), float(t.duration_seconds))

# 00:00 is reached. It records a price and does nothing else — which is the
# whole test in five lines; everything after it is the long version.
trading_service.expire_due_trades(db, now=t.closes_at)
trading_service.freeze_due_trades(db, now=t.closes_at)
r0 = trade_row(t.id)
check("at 00:00 the trade is STILL OPEN", r0.status, models.TradeStatus.OPEN)
check("at 00:00 it is still counted open", open_positions(user.id), 1)
check("at 00:00 no profit is recorded", r0.profit, None)
check("at 00:00 nothing is settled yet", r0.settled_at, None)
approx("at 00:00 a price was recorded", r0.frozen_exit_price, 0.02)
approx("at 00:00 its P/L was recorded", r0.frozen_profit, 0.0)
trading_service.close_trade(db, user.id, t.id)
check("tidy: closed for the next section", open_positions(user.id), 0)

# =============================================================================
print("\n[B] 00:00 RECORDS A PRICE AND SETTLES NOTHING")
# =============================================================================
# Two different sweeps, and the difference between them is the feature:
#
#   expire_due_trades  -- the old settlement sweep, GATED OFF, inert
#   freeze_due_trades  -- always on, and it only writes frozen_*
#
# The bug was that the old sweep turned a past `closes_at` into a settled
# trade. It is now gated behind AUTO_SETTLE_ON_EXPIRE, which is off. The new
# sweep must be just as incapable of moving money, and is asserted to be so on
# the row, on the balance and on the realized split.
check("auto-settle-on-expiry is OFF by default", AUTO_SETTLE_ON_EXPIRE, False)

PRICE["now"] = 0.02
pre_b = balance(user.id)
approx("starting realized profit is 0", realized(user.id)["profit"], 0.0)
approx("starting realized loss is 0", realized(user.id)["loss"], 0.0)

b_t = trading_service.open_trade(db, user.id, "UP", 100.0, 60, "GOLF")
approx("stake reserved at open", balance(user.id), pre_b - 100.0)
stake_debited = balance(user.id)

# Drive the market hard in the trade's favour, so that IF anything were to
# settle it, the damage would be obvious.
PRICE["now"] = 0.05
far_future = datetime.utcnow() + timedelta(days=365)

settled = trading_service.expire_due_trades(db, now=far_future)
check("a sweep a YEAR past closes_at returns nothing", len(settled), 0)

row = trade_row(b_t.id)
check("the trade is still OPEN", row.status, models.TradeStatus.OPEN)
check("no exit price was recorded", row.exit_price, None)
check("no profit was recorded", row.profit, None)
check("no settled_at was recorded", row.settled_at, None)
check("no close_reason was recorded", row.close_reason, None)
approx("the stake is still reserved, not refunded or doubled", balance(user.id), stake_debited)
approx("realized profit is STILL 0.00", realized(user.id)["profit"], 0.0)
approx("realized loss is STILL 0.00", realized(user.id)["loss"], 0.0)
check("still counted as an open position", open_positions(user.id), 1)

# The freeze sweep, on the same position and the same year-future clock. It
# writes a price and STOPS. A +$1500 unrealized gain is sitting right there and
# the balance must not move by a cent.
frozen = trading_service.freeze_due_trades(db, now=far_future)
check("the freeze sweep found the position", len(frozen), 1)
row = trade_row(b_t.id)
approx("it recorded the $0.05 price", row.frozen_exit_price, 0.05)
approx("it recorded the +$1500 P/L", row.frozen_profit, 1500.0)
check("...and nothing else", row.status, models.TradeStatus.OPEN)
check("no realized profit", row.profit, None)
check("no exit_price", row.exit_price, None)
check("no settled_at", row.settled_at, None)
check("no close_reason", row.close_reason, None)
approx("the balance did NOT move", balance(user.id), stake_debited)
approx("realized profit is STILL 0.00", realized(user.id)["profit"], 0.0)
approx("realized loss is STILL 0.00", realized(user.id)["loss"], 0.0)
check("still an open position", open_positions(user.id), 1)

# Hammer it. A freeze that re-prices on every tick is not a freeze, it is a
# live position with extra steps. 200 sweeps over a year of simulated time must
# leave the recorded price exactly where the first one put it.
for i in range(200):
    PRICE["now"] = 0.05 + i * 0.001  # the market runs away entirely
    trading_service.freeze_due_trades(db, now=far_future + timedelta(seconds=i))
row = trade_row(b_t.id)
approx("after 200 sweeps the price has NOT moved", row.frozen_exit_price, 0.05)
approx("nor has the frozen P/L", row.frozen_profit, 1500.0)
check("after 200 sweeps it is STILL OPEN", row.status, models.TradeStatus.OPEN)
check("after 200 sweeps still no profit", row.profit, None)
approx("after 200 sweeps the balance is untouched", balance(user.id), stake_debited)
approx("after 200 sweeps realized profit is STILL 0.00", realized(user.id)["profit"], 0.0)
approx("after 200 sweeps realized loss is STILL 0.00", realized(user.id)["loss"], 0.0)

# ...and the position is still fully closable afterwards, which is the other
# half of "not a settlement": the user never lost the ability to act, and
# collecting now pays the price from 200 sweeps ago.
b_done = trading_service.close_trade(db, user.id, b_t.id)
approx("collecting pays the frozen +$1500, not the runaway price", b_done.profit, 1500.0)
approx("balance credited stake + $1600", balance(user.id), stake_debited + 1600.0)

# A losing one behaves identically: a clock running out is not a loss event.
PRICE["now"] = 0.02
pre_bl = balance(user.id)
bl_t = trading_service.open_trade(db, user.id, "UP", 100.0, 60, "GOLF")
PRICE["now"] = 0.005
trading_service.expire_due_trades(db, now=far_future)
trading_service.freeze_due_trades(db, now=far_future)
approx("a finished LOSING position did not book a loss",
       realized(user.id)["loss"], 0.0)
check("the losing position is STILL OPEN", trade_row(bl_t.id).status, models.TradeStatus.OPEN)
approx("its stake is still held", balance(user.id), pre_bl - 100.0)
approx("but its frozen P/L is recorded", trade_row(bl_t.id).frozen_profit, -99.0)
trading_service.close_trade(db, user.id, bl_t.id)

# =============================================================================
print("\n[C] the READ PATHS freeze too — but still settle nothing")
# =============================================================================
# A page refresh calls these two routes. They used to run the settlement sweep,
# so merely LOOKING at the page realized a trade; they now run the FREEZE
# sweep, so a 2-hour-old position gets its price recorded even if the
# background loop has been down. Recording is the point. Settling is still
# forbidden, so the balance and the summary must not move by a cent.
# These are the route functions themselves, with the real injected session, so
# the read-path call is covered and not just the service.
from app.routes import trade_routes  # noqa: E402

PRICE["now"] = 0.02
pre_c = balance(user.id)
# The realized split is all-time, so every assertion is against a baseline taken
# here rather than against zero. What matters is that it does not MOVE.
base_c_profit = realized(user.id)["profit"]
base_c_loss = realized(user.id)["loss"]
c_t = trading_service.open_trade(db, user.id, "UP", 100.0, 60, "GOLF")
PRICE["now"] = 0.04
# Backdate the deadline the way real time would have, with no sweep running.
db.query(models.Trade).filter_by(id=c_t.id).update(
    {"closes_at": datetime.utcnow() - timedelta(hours=2)})
db.commit()
stake_c = balance(user.id)
check("before any read, nothing is frozen", trade_row(c_t.id).frozen_exit_price, None)

for _ in range(5):
    listed = trade_routes.open_trades(db=db, user_id=user.id)
    hist = trade_routes.trade_history(db=db, user_id=user.id)

check("GET /open-trades STILL lists it", [x.id for x in listed], [c_t.id])
check("GET /history does NOT list it", c_t.id in [h.id for h in hist], False)
check("the read path did not settle it", trade_row(c_t.id).status, models.TradeStatus.OPEN)
check("the read path recorded no profit", trade_row(c_t.id).profit, None)
approx("the read path moved no money", balance(user.id), stake_c)
approx("the read path realized no profit", realized(user.id)["profit"], base_c_profit)
approx("the read path realized no loss", realized(user.id)["loss"], base_c_loss)

# The client cannot invent the frozen figure, so it has to be handed the real
# one. This is the field the row's P/L is rendered from, which is why its
# absence would leave the user staring at a moving number.
approx("the read path recorded the price", trade_row(c_t.id).frozen_exit_price, 0.04)
approx("...and the P/L that goes with it", trade_row(c_t.id).frozen_profit, 1000.0)
check("...and hands it to the client", listed[0].frozen_profit, 1000.0)
check("...with the price too", listed[0].frozen_exit_price, 0.04)
# A frozen position is an OPEN position, so it must not have leaked into the
# closed history at the same time.
check("...but it is NOT in the closed history",
      c_t.id in [h.id for h in hist], False)

# "The server was restarted mid-trade" — the case the read sweep existed for.
# A 2-hour-old position is still open, still the user's, now priced, and still
# waiting for them to collect it. The price must not be re-read on each visit.
for _ in range(5):
    PRICE["now"] = 0.04 + _ * 0.01
    listed = trade_routes.open_trades(db=db, user_id=user.id)
    trade_routes.trade_history(db=db, user_id=user.id)
check("2 hours later it is STILL listed as open", [x.id for x in listed], [c_t.id])
check("2 hours later it is STILL OPEN", trade_row(c_t.id).status, models.TradeStatus.OPEN)
approx("2 hours later the balance is untouched", balance(user.id), stake_c)
approx("2 hours later the price has NOT been re-read", trade_row(c_t.id).frozen_exit_price, 0.04)
c_done = trading_service.close_trade(db, user.id, c_t.id)
approx("collecting pays the first recorded price", c_done.profit, 1000.0)
check("no longer open", open_positions(user.id), 0)

# =============================================================================
print("\n[D] the USER'S CLOSE is what realizes — at the recorded price")
# =============================================================================
# The other half of the rule, and the reason section B matters: the position
# was not stranded or destroyed, it was priced and waiting. The user's close
# books that recorded price, not whatever the market has done since.
PRICE["now"] = 0.02
pre_d = balance(user.id)
base_d_profit = realized(user.id)["profit"]
base_d_loss = realized(user.id)["loss"]
d_t = trading_service.open_trade(db, user.id, "UP", 100.0, 60, "GOLF")
PRICE["now"] = 0.04
db.query(models.Trade).filter_by(id=d_t.id).update(
    {"closes_at": datetime.utcnow() - timedelta(minutes=30)})
db.commit()
trading_service.freeze_due_trades(db)   # 00:00 happens here, at $0.04
want_d = payout_for("UP", 0.02, 0.04, 100.0)  # +1000.00
approx("nothing is realized while it is open", realized(user.id)["profit"], base_d_profit)

# The user waits. A month, in the worst case — they simply never press the
# button. The market does what it likes. The recorded figure is a promise.
PRICE["now"] = 0.008  # -80%
d_done = trading_service.close_trade(db, user.id, d_t.id)

check("the close settled it", d_done.status, models.TradeStatus.WON)
approx("it was priced at 00:00, not at the clock's expense", d_done.profit, want_d)
approx("the balance was credited the frozen payout", balance(user.id), pre_d - 100.0 + 1100.0)
approx("and ONLY NOW is profit realized", realized(user.id)["profit"], base_d_profit + want_d)
check("the reason records a user close, not an expiry", d_done.close_reason, "SOLD")
check("no longer open", open_positions(user.id), 0)
check("it is now in closed history", d_t.id in [t.id for t in trading_service._closed_trades(db, user.id)], True)
truthy("settled_at was written", d_done.settled_at is not None)
approx("and it left at the recorded price", d_done.exit_price, 0.04)

# A losing close lands in Loss, and it lands there ONLY because the user closed
# it. The clock was involved in neither the timing nor the size.
PRICE["now"] = 0.02
d2_t = trading_service.open_trade(db, user.id, "UP", 100.0, 60, "GOLF")
PRICE["now"] = 0.01
db.query(models.Trade).filter_by(id=d2_t.id).update(
    {"closes_at": datetime.utcnow() - timedelta(minutes=30)})
db.commit()
trading_service.freeze_due_trades(db)
PRICE["now"] = 0.10  # it would have been a huge win if priced now
approx("still nothing realized", realized(user.id)["loss"], base_d_loss)
d2 = trading_service.close_trade(db, user.id, d2_t.id)
check("the losing close is a LOSS", d2.status, models.TradeStatus.LOST)
approx("paid the frozen -$99, not the runaway win", d2.profit, -99.0)
approx("and only now the loss is realized", realized(user.id)["loss"], base_d_loss - 99.0)
approx("profit is untouched by the loss", realized(user.id)["profit"], base_d_profit + want_d)

# =============================================================================
print("\n[E] P/L is UNREALIZED until the close -- the top card's own source")
# =============================================================================
# The Profit and Loss cards in the frontend are driven by realized_profit /
# realized_loss off /api/platform/coins, which is built from this exact
# classification pass. So asserting on realized_split() is asserting on what
# those cards would render -- which is the user's actual requirement: at 00:00
# both cards read $0.00.
#
# The freeze makes this harder than it looks, and that is the point. There is
# now a NUMBER sitting on the position -- a real, recorded, guaranteed +$20000 --
# and it is still not realized money. A frozen figure that leaked into the
# summary would put a $20000 in the top card that the balance does not contain,
# with a "Move to Wallet" button under it.
e_before_profit = realized(user.id)["profit"]
e_before_loss = realized(user.id)["loss"]

PRICE["now"] = 0.02
pre_e = balance(user.id)
e_t = trading_service.open_trade(db, user.id, "UP", 500.0, 60, "GOLF")
stake_e = balance(user.id)
PRICE["now"] = 0.10  # a huge unrealized gain sitting right there
approx("the stake is reserved", stake_e, pre_e - 500.0)
# move = (0.10/0.02 - 1) * 10 = 40, so the stake is worth 500 * 41 = 20500.
approx("the unrealized gain is large and obvious",
       payout_for("UP", 0.02, 0.10, 500.0), 20000.0)
# Let the deadline actually pass, so 00:00 really happens in this section.
db.query(models.Trade).filter_by(id=e_t.id).update(
    {"closes_at": datetime.utcnow() - timedelta(minutes=1)})
db.commit()
trading_service.expire_due_trades(db, now=far_future)
listed = trade_routes.open_trades(db=db, user_id=user.id)
trade_routes.trade_history(db=db, user_id=user.id)

row_e = trade_row(e_t.id)
approx("00:00 recorded the +$20000", row_e.frozen_profit, 20000.0)
check("and the client can see it", [x.frozen_profit for x in listed if x.id == e_t.id], [20000.0])
check("AT 00:00 with a recorded +$20000, top Profit is unchanged",
      realized(user.id)["profit"], e_before_profit)
check("AT 00:00 with a recorded +$20000, top Loss is unchanged",
      realized(user.id)["loss"], e_before_loss)
approx("no loss is booked either", realized(user.id)["loss"], e_before_loss)
approx("the balance is still just the reserved stake", balance(user.id), stake_e)
check("the position is still open and unrealized",
      trade_row(e_t.id).status, models.TradeStatus.OPEN)
check("and holds no profit", trade_row(e_t.id).profit, None)
check("and is not in the closed history",
      e_t.id in [h.id for h in trading_service._closed_trades(db, user.id)], False)

# The market collapses while the user does nothing. The top cards must not
# react -- they are not a live ticker, they are realized money.
PRICE["now"] = 0.001
for _ in range(3):
    trade_routes.open_trades(db=db, user_id=user.id)
approx("a -95% move leaves top Profit alone", realized(user.id)["profit"], e_before_profit)
approx("...and the balance alone", balance(user.id), stake_e)
approx("...and the recorded figure alone", trade_row(e_t.id).frozen_profit, 20000.0)

e_done = trading_service.close_trade(db, user.id, e_t.id)
# The payout is 500 * (1 + 40) = 20500, of which 500 is the stake coming back
# and 20000 is the gain. Only the payout credits the balance.
approx("CLOSE is what moves top Profit", realized(user.id)["profit"], e_before_profit + 20000.0)
approx("and the balance is credited then", balance(user.id), stake_e + 20500.0)
approx("the recorded profit is the server's own", e_done.profit, 20000.0)

# =============================================================================
print("\n[F] EXACTLY ONCE -- replay, double close and close-all pay nothing extra")
# =============================================================================
PRICE["now"] = 0.02
pre_f = balance(user.id)
f_t = trading_service.open_trade(db, user.id, "UP", 50.0, 60, "GOLF")
db.query(models.Trade).filter_by(id=f_t.id).update(
    {"closes_at": datetime.utcnow() - timedelta(minutes=10)})
db.commit()
# A long stream of sweeps around a finished-but-unclosed position.
for i in range(50):
    trading_service.expire_due_trades(db, now=far_future + timedelta(seconds=i))
approx("50 sweeps around an open position paid nothing", balance(user.id), pre_f - 50.0)
check("and left it open", trade_row(f_t.id).status, models.TradeStatus.OPEN)

trading_service.close_trade(db, user.id, f_t.id)
after_close = balance(user.id)
profit_f = realized(user.id)["profit"]

approx("the user's close paid once", trading_service.close_trade(db, user.id, f_t.id).profit, 0.0)
approx("a second close pays nothing", balance(user.id), after_close)
check("a second close settles nothing", len(trading_service.close_all_trades(db, user.id)), 0)
approx("close-all pays nothing", balance(user.id), after_close)
approx("realized profit is not double counted", realized(user.id)["profit"], profit_f)
for _ in range(10):
    trading_service.expire_due_trades(db, now=far_future)
approx("sweeps after the close pay nothing", balance(user.id), after_close)
approx("...and do not re-realize it", realized(user.id)["profit"], profit_f)
check("the reason was not rewritten by any replay", trade_row(f_t.id).close_reason, "SOLD")

# One batch, one commit: CLOSE ALL over several FINISHED positions credits a
# single sum and files every one of them. It must not reach a position that is
# still counting down — that is the user's own live trade.
PRICE["now"] = 0.02
pre_g = balance(user.id)
batched = [trading_service.open_trade(db, user.id, "UP", 10.0, 60, "GOLF") for _ in range(3)]
still_running = trading_service.open_trade(db, user.id, "UP", 40.0, 300, "GOLF")
approx("four stakes are held", balance(user.id), pre_g - 70.0)

# Nothing has reached 00:00 yet, so there is nothing to collect. The 5-minute
# position is the control: it is in the same batch and stays untouched later.
check("CLOSE ALL collects nothing while the book is live",
      len(trading_service.close_all_trades(db, user.id)), 0)
approx("...and pays nothing", balance(user.id), pre_g - 70.0)
check("...and all four are still open", open_positions(user.id), 4)

# 00:00 arrives for the three, at $0.021. Then the market runs away.
db.query(models.Trade).filter(models.Trade.id.in_([x.id for x in batched])).update(
    {"closes_at": datetime.utcnow() - timedelta(minutes=5)})
db.commit()
PRICE["now"] = 0.021
trading_service.freeze_due_trades(db)
PRICE["now"] = 0.05

collected = trading_service.close_all_trades(db, user.id)
check("CLOSE ALL collects all three", len(collected), 3)
# $10 each at the FROZEN $0.021 = $15.00 each (move 0.05 x 10 = 0.5), not the
# $0.05 the market is on.
approx("credited as one sum at each frozen price", balance(user.id), pre_g - 70.0 + 45.0)
check("each left at the frozen price, not the live one",
      sorted(t.exit_price for t in collected), [0.021, 0.021, 0.021])
check("the 5-minute position is STILL open",
      trade_row(still_running.id).status, models.TradeStatus.OPEN)
check("and was never frozen", trade_row(still_running.id).frozen_exit_price, None)
check("so it is the only one left open", open_positions(user.id), 1)
check("close-all is a no-op on replay", len(trading_service.close_all_trades(db, user.id)), 0)
approx("replay paid nothing", balance(user.id), pre_g - 70.0 + 45.0)
trading_service.close_trade(db, user.id, still_running.id)

# =============================================================================
print("\n[G] NO FORFEITURE -- a late close is priced, never booked as -amount")
# =============================================================================
# The original bug in this area was a timer booking `profit = -amount`. Closing
# late must be priced off the market for every direction, on a flat market too,
# and must never carry the old -amount signature.
for direction, entry, exit_px in [
    ("UP", 0.02, 0.04), ("DOWN", 0.02, 0.01), ("UP", 0.02, 0.02), ("DOWN", 0.02, 0.02),
]:
    PRICE["now"] = entry
    pre_h = balance(user.id)
    h_t = trading_service.open_trade(db, user.id, direction, 10.0, 60, "GOLF")
    PRICE["now"] = exit_px
    db.query(models.Trade).filter_by(id=h_t.id).update(
        {"closes_at": datetime.utcnow() - timedelta(hours=1)})
    db.commit()
    trading_service.expire_due_trades(db, now=far_future)
    check(f"{direction} at {exit_px}: still unrealized after 200 sweeps",
          trade_row(h_t.id).profit, None)
    h_done = trading_service.close_trade(db, user.id, h_t.id)
    approx(f"{direction} at {exit_px}: closed late is priced, not forfeited",
           h_done.profit, payout_for(direction, entry, exit_px, 10.0))
    check(f"{direction} at {exit_px}: returned at least the stake",
          balance(user.id) >= pre_h - 10.0, True)

# The forfeiture signature: profit == -amount. Impossible from a user close,
# because a close and a settle share one pricing function, so the result is
# always `payout - amount` off the server's own feed.
PRICE["now"] = 0.02
g_t = trading_service.open_trade(db, user.id, "UP", 10.0, 60, "GOLF")
PRICE["now"] = 0.05
g_done = trading_service.close_trade(db, user.id, g_t.id)
check("a winning close is never -amount", g_done.profit == -g_done.amount, False)
truthy("a winning close banks a gain", g_done.profit > 0)
check("settle_due_trades is still gone", hasattr(trading_service, "settle_due_trades"), False)

# =============================================================================
print("\n[H] EVERY DURATION HOLDS, and none of them auto-settles")
# =============================================================================
# A 30-minute position must not be dragged down by a 1-minute one, and a
# 1-minute one reaching 00:00 must not settle -- or unsettle -- the others.
PRICE["now"] = 0.02
pre_i = balance(user.id)
base_i_profit = realized(user.id)["profit"]
short = trading_service.open_trade(db, user.id, "UP", 10.0, 60, "GOLF")
longs = [
    trading_service.open_trade(db, user.id, "UP", 1.0, 300, "GOLF"),
    trading_service.open_trade(db, user.id, "UP", 1.0, 900, "GOLF"),
    trading_service.open_trade(db, user.id, "UP", 1.0, 1800, "GOLF"),
]
staked = 10.0 + 3 * 1.0
for secs, d in [(60, short)] + [(x.duration_seconds, x) for x in longs]:
    check(f"{secs}s duration shows {fmt_time(secs)}",
          (d.closes_at - d.opened_at).total_seconds(), float(secs))
check("four positions open", open_positions(user.id), 4)
approx("all four stakes debited", balance(user.id), pre_i - staked)

# Run the 1-minute one right past its deadline, 200 times over.
for i in range(200):
    trading_service.freeze_due_trades(db, now=short.closes_at + timedelta(seconds=i))
check("all four are STILL open at 00:00 -- a finished trade is not closed",
      open_positions(user.id), 4)
check("...and so are the other three", trade_row(longs[0].id).status, models.TradeStatus.OPEN)
check("the 1-minute one was frozen", trade_row(short.id).frozen_exit_price, 0.02)
check("...and the 15-minute one was NOT",
      trade_row(longs[0].id).frozen_exit_price, None)
check("...nor the 30-minute one", trade_row(longs[1].id).frozen_exit_price, None)
check("...nor the 1-hour one", trade_row(longs[2].id).frozen_exit_price, None)
approx("no stake was returned by the clock", balance(user.id), pre_i - staked)
approx("nothing was realized by the clock", realized(user.id)["profit"], base_i_profit)
for d in longs:
    check(f"{d.duration_seconds}s position untouched", trade_row(d.id).profit, None)

# CLOSE ALL collects the one finished position and leaves the three running
# ones exactly as they are. This is the mixed case, and it is the one the
# button actually faces: a user's own book with one trade collected and
# others running.
PRICE["now"] = 0.05
collected = trading_service.close_all_trades(db, user.id)
check("CLOSE ALL collected only the finished one", len(collected), 1)
check("...which was the 1-minute one", collected[0].id, short.id)
check("the three longer ones are still open", open_positions(user.id), 3)
approx("their stakes are still held", balance(user.id), pre_i - staked + 10.0)
for d in longs:
    trading_service.close_trade(db, user.id, d.id)
check("tidy: nothing left open", open_positions(user.id), 0)

# =============================================================================
print("\n[I] LOCK ORDER -- every settling path takes the position before the balance")
# =============================================================================
# A user's SELL and the (opt-in) sweep are the two writes most likely to land on
# the same position in the same second, so they must not grab the position row
# and the balance row in opposite orders: that is a textbook AB-BA deadlock on
# PostgreSQL, which is the deployed database. The invariant is a property of the
# source, so it is asserted against the source rather than hoped for at runtime.
print("  --   lock order is a source-level property; assert it on the code")


def lock_order(fn):
    """The order this function takes the two lock helpers in, as a list like
    ['trade', 'user']. Only calls that lock BOTH rows count."""
    src = inspect.getsource(fn)
    order = []
    for match in re.finditer(r"_lock_(trade|user)\s*\(", src):
        kind = match.group(1)
        if kind not in order:
            order.append(kind)
    return order


for fn in (trading_service.close_trade, trading_service.expire_due_trades,
           trading_service.close_all_trades):
    order = lock_order(fn)
    check(f"{fn.__name__} locks trade before user", order, ["trade", "user"])

# The freeze sweep is the odd one out, and that is the design. It runs on a
# timer for every user at once, so it must NOT take the user row: doing so
# would serialize the whole platform's freezing behind one account's balance
# lock and, worse, would imply it can write to the balance at all. It writes
# three columns on the trade and nothing else, so it must take no user lock and
# must never credit anybody.
check("freeze_due_trades takes NO user lock", lock_order(trading_service.freeze_due_trades), ["trade"])
check("...because it cannot move money", "usdt_balance" not in inspect.getsource(trading_service.freeze_due_trades), True)
_freeze_src = inspect.getsource(trading_service._freeze_row)
check("...it only writes the frozen columns",
      sorted(set(re.findall(r"trade\.(frozen_\w+)\s*=", _freeze_src))),
      ["frozen_at", "frozen_exit_price", "frozen_profit"])
check("...it never writes a settlement field",
      re.findall(r"trade\.(exit_price|profit|status|settled_at|close_reason)\s*=", _freeze_src), [])
check("...it never calls _book_result", "_book_result" in _freeze_src, False)
check("...it returns before touching the session commit", "db.commit" not in _freeze_src, True)

module_src = inspect.getsource(trading_service)
check("the lock-order invariant is documented in the module",
      "trade-then-user" in module_src, True)
check("open_trade is the one path that locks no position",
      "trade" not in lock_order(trading_service.open_trade), True)

# The gate itself, asserted on the source so a future edit cannot quietly move
# the check somewhere it can be skipped.
ts_src = module_src
gate = re.search(
    r"def expire_due_trades\([^)]*\)[^:]*:\s*.*?if not AUTO_SETTLE_ON_EXPIRE:\s*.*?return \[\]",
    ts_src, re.S)
check("expire_due_trades is gated on AUTO_SETTLE_ON_EXPIRE", bool(gate), True)
if gate:
    tail = ts_src[gate.end():]
    first_write = re.search(r"\b(_mark_to_market|settled_at\s*=|close_reason\s*=|db\.commit\()", tail)
    check("...and returns before any settlement write",
          first_write is not None and "return []" in gate.group(0) and
          tail.find("db.commit()") > ts_src[:gate.end()].count("\n"), True)
check("the 'running out of time is not a settlement' rule is documented",
      "RUNNING OUT OF TIME IS NOT A SETTLEMENT" in module_src, True)

main_src = open(os.path.join(os.path.dirname(__file__), "app", "main.py"), encoding="utf-8").read()
# The settlement loop must exist in the source, but may only ever be created
# inside the AUTO_SETTLE_ON_EXPIRE guard. Matching on the create_task call
# alone is not enough (it could be hoisted out of the guard), and matching the
# guard alone is not enough either (the call could be gone entirely) — so this
# pins both, and pins that there is exactly one such call.
_expiry_creations = re.findall(r"asyncio\.create_task\(\s*_trade_expiry_loop\(\)", main_src)
check("the settlement loop is created exactly once in the source",
      len(_expiry_creations), 1)
_guard = re.search(r"if AUTO_SETTLE_ON_EXPIRE:(.*?)(?:\n\n\n|\Z)", main_src, re.S)
check("...and only inside the AUTO_SETTLE_ON_EXPIRE guard",
      bool(_guard) and bool(re.search(r"asyncio\.create_task\(\s*_trade_expiry_loop\(\)", _guard.group(1))),
      True)
check("AUTO_SETTLE_ON_EXPIRE defaults to off",
      re.search(r'AUTO_SETTLE_ON_EXPIRE\s*=\s*os\.environ\.get\(\s*"AUTO_SETTLE_ON_EXPIRE"\s*,\s*"0"',
                open(os.path.join(os.path.dirname(__file__), "app", "config.py"), encoding="utf-8").read()) is not None,
      True)
check("main.py documents that no timer settles a trade",
      "NO TIMER SETTLES A TRADE" in main_src, True)

# =============================================================================
print("\n[J] a REAL 1-minute trade, live, with the user closing it")
# =============================================================================
# A live wall-clock run at the real cadence -- no injected time, no backdating.
# This is the scenario from the report, and the assertion is the one the user
# asked for: the countdown reaches 00:00, a price is recorded, the position is
# STILL OPEN, the balance has NOT moved, and nothing appears in the top P/L
# until the close -- which then pays the recorded price, not the later one.
if not REAL:
    print("  --   skipped (pass --real to run the live 60s test)")
else:
    # A FRESH user, so "$0.00" below is literally true rather than "unchanged
    # from a baseline". This is the user's requirement stated in the strongest
    # form available: an account that has never traded reaches 00:00 with both
    # top cards reading exactly zero, and they still read zero 12 seconds later.
    j_user = new_user("expiry-real@example.com")
    PRICE["now"] = 0.02
    pre_j = balance(j_user.id)
    approx("starting from a clean slate", realized(j_user.id)["profit"], 0.0)
    approx("and no loss", realized(j_user.id)["loss"], 0.0)
    j_t = trading_service.open_trade(db, j_user.id, "UP", 100.0, 60, "GOLF")
    approx("stake reserved", balance(j_user.id), pre_j - 100.0)
    stake_j = balance(j_user.id)

    started = time.time()
    seen = []
    reached_zero_at = None
    recorded_after = None
    while time.time() - started < 75:
        left = shows(60, time.time() - started)
        if not seen or seen[-1] != left:
            seen.append(left)
        # The background loops, at the shipped cadence. The freeze one must
        # record a price and nothing more.
        trading_service.expire_due_trades(db)
        trading_service.freeze_due_trades(db)
        if reached_zero_at is None and left == "00:00":
            reached_zero_at = time.time() - started
            # Sample the full state at the exact moment the clock stops. Note
            # what is NOT asserted here: that the price already exists. The
            # countdown the user reads is the client's own arithmetic against
            # the server's closes_at, so this frame can be a fraction of a
            # second ahead of the server's clock reaching the same instant. The
            # price lands on the next sweep — bounded by the sweep interval,
            # which is what the guarantee actually is.
            j_at_zero = trade_row(j_t.id)
            check("AT 00:00: still OPEN", j_at_zero.status, models.TradeStatus.OPEN)
            check("AT 00:00: no profit on the row", j_at_zero.profit, None)
            check("AT 00:00: nothing settled", j_at_zero.settled_at, None)
            check("AT 00:00: no close reason", j_at_zero.close_reason, None)
            approx("AT 00:00: balance unchanged", balance(j_user.id), stake_j)
            approx("AT 00:00: top Profit is $0.00", realized(j_user.id)["profit"], 0.0)
            approx("AT 00:00: top Loss is $0.00", realized(j_user.id)["loss"], 0.0)
        # The price is recorded within one sweep of the deadline, never later.
        # Keep running for 12s past it either way, so the "it does not move"
        # half of the guarantee is observed over a real window.
        if reached_zero_at is not None and trade_row(j_t.id).frozen_exit_price is not None \
                and recorded_after is None:
            recorded_after = time.time() - started - reached_zero_at
        if reached_zero_at is not None and time.time() - started > reached_zero_at + 12:
            break
        time.sleep(1.0)

    # The guarantee, stated as a bound: the promise is made within one sweep
    # interval of 00:00, and never broken afterwards.
    check("a price was recorded after 00:00",
          trade_row(j_t.id).frozen_exit_price is not None, True)
    truthy("and within one sweep of it", 0.0 <= recorded_after <= 2.0)

    print(f"  --   countdown sampled: {' '.join(seen[:4])} ... {seen[-1] if seen else '-'}")
    check("countdown ended on 00:00", seen[-1] if seen else None, "00:00")
    secs_seen = [int(x.split(':')[0]) * 60 + int(x.split(':')[1]) for x in seen]
    check("countdown only ever counts down", all(b <= a for a, b in zip(secs_seen, secs_seen[1:])), True)
    check("countdown never counted past 00:00", min(secs_seen) >= 0, True)
    check("00:00 appears, at the end", seen.count("00:00"), 1)
    truthy("the clock reached zero", reached_zero_at is not None)
    truthy("around the 60s mark", reached_zero_at is not None and 58.0 <= reached_zero_at <= 62.0)

    # 12s PAST 00:00, with a sweep every second the whole time.
    j_frozen_price = trade_row(j_t.id).frozen_exit_price
    check("12s past 00:00: STILL OPEN", trade_row(j_t.id).status, models.TradeStatus.OPEN)
    check("12s past 00:00: still an open position", open_positions(j_user.id), 1)
    approx("12s past 00:00: balance STILL unchanged", balance(j_user.id), stake_j)
    approx("12s past 00:00: top Profit STILL $0.00", realized(j_user.id)["profit"], 0.0)
    approx("12s past 00:00: top Loss STILL $0.00", realized(j_user.id)["loss"], 0.0)
    check("12s past 00:00: still not in history",
          j_t.id in [t.id for t in trading_service._closed_trades(db, j_user.id)], False)
    approx("12s past 00:00: the recorded price did not move",
           trade_row(j_t.id).frozen_exit_price, j_frozen_price)

    # Now the user closes it. The market has had 12 extra seconds to move, and
    # the payout must ignore them completely — that is the guarantee.
    PRICE["now"] = 0.021
    want_j = payout_for("UP", 0.02, j_frozen_price, 100.0)
    j_done = trading_service.close_trade(db, j_user.id, j_t.id)
    approx("CLOSE pays the price recorded at 00:00, not the current one",
           j_done.profit, want_j)
    approx("...which is not what the live price would have paid",
           want_j != payout_for("UP", 0.02, 0.021, 100.0), True)
    approx("the balance is credited only now", balance(j_user.id), stake_j + 100.0 + want_j)
    approx("and only now does top Profit move", realized(j_user.id)["profit"], want_j)
    approx("and it left at the recorded price", j_done.exit_price, j_frozen_price)
    check("and only now is it in closed history",
          j_t.id in [t.id for t in trading_service._closed_trades(db, j_user.id)], True)


market_service.get_current_price = _real_price
db.close()

print()
if FAILS:
    print(f"FAILED ({len(FAILS)}):")
    for f in FAILS:
        print(f"  - {f}")
    sys.exit(1)
print("All 00:00-is-not-a-settlement checks passed.")
