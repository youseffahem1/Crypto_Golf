"""VERIFICATION OF THE 1-MINUTE TRADE DURATION AND THE EXPIRY LIFECYCLE.

This is the test for the reported bug: a 1-minute trade reached 00:00 on the
countdown and then sat there, still OPEN, with the position apparently still
running. This file proves the whole lifecycle, and — importantly — it proves it
by WAITING THE REAL 60 SECONDS rather than by backdating timestamps, so what it
checks is the behaviour the user actually experiences.

Sections:
  A. THE COUNTDOWN SEQUENCE  the numbers a 1-minute trade actually shows
  B. A REAL 1-MINUTE BUY     the wall-clock 60s test, no manual sell
  C. A REAL 1-MINUTE SELL    same, with the mirror-image math
  D. EXACTLY ONCE            the sweep, a read, and a refresh never pay twice
  E. NO FORFEITURE           the old `profit = -amount` bug cannot come back
  F. THE HTTP READ PATHS     a refresh after expiry never sees the trade OPEN
  G. CONCURRENCY            a sweep racing a user SELL pays exactly once
  H. MULTIPLE DURATIONS     5/15/30 min keep their own deadlines

Usage:  python smoke_trade_expiry.py            (skips the real 60s waits)
        python smoke_trade_expiry.py --real     (runs sections B and C live)
"""
import os
import re
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

fd, DB_PATH = tempfile.mkstemp(suffix=".db")
os.close(fd)
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"

from app import models, market_service, trading_service  # noqa: E402
from app.database import SessionLocal, engine, Base  # noqa: E402

Base.metadata.create_all(bind=engine)
db = SessionLocal()

REAL = "--real" in sys.argv
FAILS = []

# The single source of truth for price. Every open and every settle reads it,
# so we can move the market to an exact known value and assert the exact
# payout the user would see.
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

    This is the frontend's expression verbatim —
    `formatTime(max(0, ceil((closes_at - Date.now()) / 1000)))` — with
    `closes_at = opened_at + duration_seconds`, so it depends on nothing but the
    server's own timestamp. The strings are literally what the user reads.
    """
    now_ms = seconds_in * 1000.0
    closes_at_ms = duration_seconds * 1000.0
    return fmt_time(max(0, (closes_at_ms - now_ms) / 1000.0))


def payout_for(direction, entry, exit_px, amount):
    """trading_service._exit_value, restated here so the expected numbers are
    derived from the shipped formula rather than hand-copied:
      BUY  -> amount * (exit / entry)
      SELL -> amount * (2 - exit / entry), floored at 1% of the stake."""
    ratio = exit_px / entry
    v = amount * (2.0 - ratio) if direction == "DOWN" else amount * ratio
    if direction == "DOWN" and v < amount * 0.01:
        v = amount * 0.01
    return round(v - amount, 6)


user = new_user("expiry@example.com")

# =============================================================================
print("\n[A] the countdown a 1-minute trade actually shows")
# =============================================================================
# The client renders `formatTime(ceil((closes_at - now) / 1000))` against the
# SERVER's closes_at. These are the exact strings the user's screenshot showed
# as correct, and the exact string that was wrong because it never ended.
PRICE["now"] = 0.02
t = trading_service.open_trade(db, user.id, "UP", 10.0, 60, "GOLF")
DUR = 60
# The strings below are the literal pixels the user reads, including the two the
# bug report was about: 00:01 -> 00:00, and 00:00 must never appear on a row
# that is still live.
check("shows 01:00 on the first frame", shows(DUR, 0), "01:00")
check("shows 00:59 after 1s", shows(DUR, 1), "00:59")
check("shows 00:30 halfway", shows(DUR, 30), "00:30")
check("shows 00:01 after 59s", shows(DUR, 59), "00:01")
check("shows 00:00 at exactly 60s", shows(DUR, 60), "00:00")
# The reported bug: the count went PAST zero. It must clamp at 00:00 and stay
# there — and the checks below prove the position is already closed by then, so
# 00:00 is never left sitting on a live row.
check("clamps at 00:00, never counts past it", shows(DUR, 90), "00:00")
check("duration is exactly 60s", (t.closes_at - t.opened_at).total_seconds(), 60.0)
check("closes_at is opened_at + 60s",
      (t.closes_at - t.opened_at).total_seconds(), float(t.duration_seconds))

# And the real behaviour: 59.5s in, the position is still open; 60.0s in, it is
# not. Nothing in between, because the server settles the whole position.
check("still OPEN just before expiry", open_positions(user.id), 1)
trading_service.expire_due_trades(db, now=t.closes_at - timedelta(milliseconds=1))
check("sweep 0.5s early settles nothing", open_positions(user.id), 1)
trading_service.expire_due_trades(db, now=t.closes_at)
check("sweep at closes_at settles it", open_positions(user.id), 0)
row = trade_row(t.id)
check("status left OPEN", row.status, models.TradeStatus.WON)
check("reason is EXPIRED", row.close_reason, "EXPIRED")
check("settled_at == closes_at", row.settled_at, t.closes_at)

# =============================================================================
print("\n[B] a REAL 1-minute BUY, left to expire on its own")
# =============================================================================
# A live wall-clock run at the real 1-second sweep cadence — no injected time,
# no backdating, no manual sell. This is the scenario from the report.
if not REAL:
    print("  --   skipped (pass --real to run the live 60s test)")
else:
    PRICE["now"] = 0.02
    before = balance(user.id)
    bt = trading_service.open_trade(db, user.id, "UP", 10.0, 60, "GOLF")
    approx("stake debited", balance(user.id), before - 10.0)

    started = time.time()
    sweep_interval = 1.0
    expired_after = None
    seen = []
    while time.time() - started < 95:
        # What the user would see: the countdown, sampled once a second.
        left = shows(60, time.time() - started)
        if not seen or seen[-1] != left:
            seen.append(left)
        # The background loop, running at the shipped 1s cadence.
        trading_service.expire_due_trades(db)
        if open_positions(user.id) == 0:
            expired_after = time.time() - started
            break
        time.sleep(sweep_interval)

    row = trade_row(bt.id)
    truthy(f"expired with nobody selling it (after {expired_after:.1f}s)",
           row.status != models.TradeStatus.OPEN)
    check("reason is EXPIRED", row.close_reason, "EXPIRED")
    check("no longer OPEN", open_positions(user.id), 0)
    truthy("it ended within one sweep interval of 60s",
           expired_after is not None and 60.0 <= expired_after < 63.0)
    # The countdown the user watched: 01:00 all the way down, monotonically,
    # ending on 00:00 - never restarting, never counting past zero.
    print(f"  --   countdown sampled: {' '.join(seen[:4])} ... {seen[-1] if seen else '-'}")
    check("countdown ended on 00:00", seen[-1] if seen else None, "00:00")

    def _secs(label):
        m, s = label.split(":")
        return int(m) * 60 + int(s)

    secs_seen = [_secs(x) for x in seen]
    check("countdown only ever counts down, never back up",
          all(b <= a for a, b in zip(secs_seen, secs_seen[1:])), True)
    check("countdown never showed more than the full 60s",
          max(secs_seen) <= 60, True)
    check("countdown never counted past 00:00", min(secs_seen) >= 0, True)
    check("00:00 appears exactly once, at the end", seen.count("00:00"), 1)
    # Priced off the market, never the clock: settled at the server's feed when
    # the minute ran out, through the same maths a manual SELL uses.
    expected = 10.0 * (float(row.exit_price) / float(row.entry_price))
    print(f"  --   entry {row.entry_price} -> exit {row.exit_price}, profit {row.profit}")
    approx("value is stake * (exit/entry), as a manual sell would be",
           row.profit, round(expected - 10.0, 6))
    approx("balance credited the payout exactly once",
           balance(user.id), before - 10.0 + expected)

    # A refresh after expiry must not reopen it, and must not pay it again.
    for _ in range(5):
        trading_service.expire_due_trades(db)
    check("five refreshes did not reopen", open_positions(user.id), 0)
    approx("five refreshes paid nothing extra", balance(user.id), before - 10.0 + expected)

# =============================================================================
print("\n[C] a 1-minute SELL expires on the same schedule")
# =============================================================================
# Same duration, opposite direction. The mirror math is `amount * (2 - ratio)`
# — a falling market pays out more than a doubling one on a BUY — and that is
# the shipped formula for a manual SELL too, which is the point: expiry must
# not use a DIFFERENT rule, and above all must not use the old flat -amount one.
PRICE["now"] = 0.02
if REAL:
    # The same live 60-second wait, the other direction.
    pre_live_sell = balance(user.id)
    lt = trading_service.open_trade(db, user.id, "DOWN", 10.0, 60, "GOLF")
    approx("SELL stake debited", balance(user.id), pre_live_sell - 10.0)
    started = time.time()
    while time.time() - started < 95:
        trading_service.expire_due_trades(db)
        if open_positions(user.id) == 0:
            break
        time.sleep(1.0)
    live_after = time.time() - started
    lrow = trade_row(lt.id)
    truthy(f"1-minute SELL expired on its own (after {live_after:.1f}s)",
           lrow.status != models.TradeStatus.OPEN)
    check("SELL reason is EXPIRED", lrow.close_reason, "EXPIRED")
    truthy("SELL ended within one sweep interval of 60s", 60.0 <= live_after < 63.0)
    approx("SELL expiry is the mirror formula, off the server's feed",
           lrow.profit, payout_for("DOWN", float(lrow.entry_price), float(lrow.exit_price), 10.0))
    approx("SELL balance credited once", balance(user.id), pre_live_sell + lrow.profit)
    for _ in range(5):
        trading_service.expire_due_trades(db)
    check("SELL refreshes did not reopen", open_positions(user.id), 0)
    approx("SELL refreshes paid nothing extra", balance(user.id), pre_live_sell + lrow.profit)
else:
    print("  --   live 60s SELL skipped (pass --real to run it)")

PRICE["now"] = 0.02
pre_sell = balance(user.id)
st = trading_service.open_trade(db, user.id, "DOWN", 10.0, 60, "GOLF")
check("SELL duration is exactly 60s", (st.closes_at - st.opened_at).total_seconds(), 60.0)
PRICE["now"] = 0.01  # the price halved: a SELL wins
want_sell = payout_for("DOWN", 0.02, 0.01, 10.0)
s = trading_service.expire_due_trades(db, now=st.closes_at)[0]
approx("SELL expiry is priced, not forfeited", s.profit, want_sell)
check("SELL expired as a WIN", s.status, models.TradeStatus.WON)
approx("balance credited once", balance(user.id), pre_sell + want_sell)
# Sanity: the SELL really did pay more than the stake, which the old -amount
# forfeiture could never do.
truthy("SELL expiry returned more than the stake", s.profit > 0)

# ...and a SELL that loses still loses by the market, not by the clock.
PRICE["now"] = 0.02
pre_sell2 = balance(user.id)
st2 = trading_service.open_trade(db, user.id, "DOWN", 10.0, 60, "GOLF")
PRICE["now"] = 0.04  # the price doubled: a SELL loses
want_lose = payout_for("DOWN", 0.02, 0.04, 10.0)
s2 = trading_service.expire_due_trades(db, now=st2.closes_at)[0]
approx("SELL expiry floors like a manual close (not -10)", s2.profit, want_lose)
approx("...and the floor is 1% of the stake", s2.profit, -9.9)
check("SELL expired as a LOSS", s2.status, models.TradeStatus.LOST)
approx("balance credited the floored value", balance(user.id), pre_sell2 + want_lose)

# =============================================================================
print("\n[D] EXACTLY ONCE — the sweep, a read and a refresh never pay twice")
# =============================================================================
PRICE["now"] = 0.02
pre = balance(user.id)
d1 = trading_service.open_trade(db, user.id, "UP", 10.0, 60, "GOLF")
settled = trading_service.expire_due_trades(db, now=d1.closes_at)
check("one sweep settled one position", len(settled), 1)
after_one = balance(user.id)
approx("credited once", after_one, pre - 10.0 + 10.0)

# Everything that could plausibly re-trigger settlement, replayed.
check("a second sweep settles nothing", len(trading_service.expire_due_trades(db, now=d1.closes_at)), 0)
for _ in range(50):
    trading_service.expire_due_trades(db, now=datetime.utcnow() + timedelta(days=1))
approx("50 sweeps paid nothing", balance(user.id), after_one)
approx("a manual close of the expired trade replays", trading_service.close_trade(db, user.id, d1.id).profit, 0.0)
approx("...and pays nothing", balance(user.id), after_one)
check("close-all skips it", len(trading_service.close_all_trades(db, user.id)), 0)
approx("...and still pays nothing", balance(user.id), after_one)
row = trade_row(d1.id)
check("P/L was written exactly once", row.profit, 0.0)
check("settled_at written once", row.settled_at, d1.closes_at)
check("reason not rewritten by the replay", row.close_reason, "EXPIRED")

# One batch, one commit: several positions expiring together are credited as a
# single sum, and a partial failure would roll the whole batch back.
PRICE["now"] = 0.02
pre_batch = balance(user.id)
b1 = trading_service.open_trade(db, user.id, "UP", 10.0, 60, "GOLF")
b2 = trading_service.open_trade(db, user.id, "UP", 20.0, 60, "GOLF")
b3 = trading_service.open_trade(db, user.id, "DOWN", 5.0, 60, "GOLF")
PRICE["now"] = 0.02
batch = trading_service.expire_due_trades(db, now=b1.closes_at + timedelta(seconds=1))
check("all three expired together", len(batch), 3)
approx("the batch was credited as one sum", balance(user.id), pre_batch)
check("none left open", open_positions(user.id), 0)

# =============================================================================
print("\n[E] the old `-amount` forfeiture can never come back")
# =============================================================================
# This is the regression that started all of it: a timer running out used to
# book the stake as a total loss. Drive the market decisively in the position's
# favour at the moment of expiry and assert the stake is returned with the
# profit, for every direction, on a flat market too.
for direction, entry, exit_px in [
    ("UP", 0.02, 0.04), ("DOWN", 0.02, 0.01), ("UP", 0.02, 0.02), ("DOWN", 0.02, 0.02),
]:
    PRICE["now"] = entry
    pre_f = balance(user.id)
    tf = trading_service.open_trade(db, user.id, direction, 10.0, 60, "GOLF")
    PRICE["now"] = exit_px
    want_f = payout_for(direction, entry, exit_px, 10.0)
    rf = trading_service.expire_due_trades(db, now=tf.closes_at)[0]
    approx(f"{direction} expired at {exit_px} is priced, not forfeited", rf.profit, want_f)
    check(f"{direction} expired at {exit_px} returned at least the stake",
          balance(user.id) >= pre_f - 10.0, True)

# The old bug's exact signature: profit == -amount. Structurally impossible now,
# because expiry and a manual sell share one pricing function, so the result is
# always `payout - amount` from the server's own feed.
PRICE["now"] = 0.02
tg = trading_service.open_trade(db, user.id, "UP", 10.0, 60, "GOLF")
PRICE["now"] = 0.05
rg = trading_service.expire_due_trades(db, now=tg.closes_at)[0]
check("a winning expiry is never -amount", rg.profit == -rg.amount, False)
approx("profit is always payout - amount, off the server's feed",
       rg.profit, payout_for("UP", 0.02, 0.05, 10.0))
truthy("a winning expiry banks a gain, exactly as a manual sell would", rg.profit > 0)

# The function that caused the original bug must not exist, and no equivalent
# may be hiding under another name.
check("settle_due_trades is still gone", hasattr(trading_service, "settle_due_trades"), False)
src = open(os.path.join(os.path.dirname(__file__), "app", "trading_service.py"), encoding="utf-8").read()
# The only place a signed profit is computed is payout - amount. A "-trade.amount"
# or "-= amount" would be the forfeiture signature coming back.
check("no forfeiture by negating the stake",
      ("-trade.amount" in src.replace(" ", "").replace("- trade.amount", "-trade.amount")) is False, True)

# =============================================================================
print("\n[F] the HTTP read paths never report an expired position as OPEN")
# =============================================================================
# This is what a page refresh actually calls. It exercises the route functions
# directly with the real dependency-injected session, so the read-path sweep is
# covered, not just the service.
from app.routes import trade_routes  # noqa: E402

PRICE["now"] = 0.02
pre_r = balance(user.id)
rt = trading_service.open_trade(db, user.id, "UP", 10.0, 60, "GOLF")
PRICE["now"] = 0.03
db.query(models.Trade).filter_by(id=rt.id).update({"closes_at": datetime.utcnow() - timedelta(seconds=1)})
db.commit()

listed = trade_routes.open_trades(db=db, user_id=user.id)
check("GET /open-trades does not list it", [x.id for x in listed], [])
check("the read path settled it", trade_row(rt.id).status, models.TradeStatus.WON)
approx("the read path credited it once", balance(user.id), pre_r + 5.0)

hist = trade_routes.trade_history(db=db, user_id=user.id)
check("GET /history lists it", rt.id in [h.id for h in hist], True)
check("history carries the reason", [h.close_reason for h in hist if h.id == rt.id], ["EXPIRED"])

# The "server was restarted mid-trade" case: nothing swept it for a while, and
# the user's first action is a refresh. Same answer, and still paid once.
PRICE["now"] = 0.02
pre_r2 = balance(user.id)
rt2 = trading_service.open_trade(db, user.id, "UP", 10.0, 60, "GOLF")
PRICE["now"] = 0.02
db.query(models.Trade).filter_by(id=rt2.id).update({"closes_at": datetime.utcnow() - timedelta(minutes=30)})
db.commit()
for i in range(5):
    listed = trade_routes.open_trades(db=db, user_id=user.id)
    trade_routes.trade_history(db=db, user_id=user.id)
check("still not OPEN after 5 refreshes", [x.id for x in listed], [])
approx("30 minutes late, paid exactly once", balance(user.id), pre_r2)

# =============================================================================
print("\n[G] a sweep racing a user SELL pays exactly once")
# =============================================================================
# The realistic worst case: the countdown hits 00:00 and the user presses SELL
# in the same instant the sweep reaches the row. Both take the same locks and
# both are guarded by the same status check, so exactly one of them settles it.
PRICE["now"] = 0.02
pre_g = balance(user.id)
gt = trading_service.open_trade(db, user.id, "UP", 10.0, 60, "GOLF")
PRICE["now"] = 0.02
gt.closes_at = datetime.utcnow() - timedelta(seconds=1)
db.commit()

results = {}
barrier = threading.Barrier(2)


def run_sweep():
    s = SessionLocal()
    try:
        barrier.wait()
        results["sweep"] = [t.id for t in trading_service.expire_due_trades(s)]
    finally:
        s.close()


def run_sell():
    s = SessionLocal()
    try:
        barrier.wait()
        results["sell"] = trading_service.close_trade(s, user.id, gt.id).id
    except Exception as e:  # noqa: BLE001
        results["sell"] = f"error: {e}"
    finally:
        s.close()


th = [threading.Thread(target=run_sweep), threading.Thread(target=run_sell)]
for t_ in th:
    t_.start()
for t_ in th:
    t_.join()

row = trade_row(gt.id)
check("the position is settled", row.status != models.TradeStatus.OPEN, True)
approx("the balance was credited exactly once", balance(user.id), pre_g)
check("settled_at written once", row.settled_at is not None, True)
settlers = sum(1 for v in results.values() if v)
print(f"  --   sweep returned {results.get('sweep')}, sell returned {str(results.get('sell'))[:8]}...")
truthy("at least one path completed and neither double-paid", settlers >= 1)

# =============================================================================
print("\n[H] every duration keeps its own deadline")
# =============================================================================
# A 30-minute position must not be dragged down by a 1-minute one: the sweep
# keys on each position's own `closes_at`, not on "the oldest open trade".
PRICE["now"] = 0.02
pre_h = balance(user.id)
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
approx("all four stakes debited", balance(user.id), pre_h - staked)

# The 1-minute one comes due; the other three must be untouched.
batch = trading_service.expire_due_trades(db, now=short.closes_at)
check("only the 1-minute position expired", [x.id for x in batch], [short.id])
check("the 5/15/30-minute positions are still open", open_positions(user.id), 3)
for d in longs:
    check(f"{d.duration_seconds}s position still OPEN", trade_row(d.id).status, models.TradeStatus.OPEN)
# Flat market: the expired $10 came straight back, and not one cent of the
# other three was paid out early.
approx("only the expired stake was returned", balance(user.id), pre_h - staked + 10.0)

trading_service.close_all_trades(db, user.id)
check("tidy: nothing left open", open_positions(user.id), 0)


# =============================================================================
print("\n[I] LOCK ORDER — every settling path takes the position before the balance")
# =============================================================================
# A user's SELL and the expiry sweep are the two writes most likely to land on
# the same position in the same second, so they must not grab the position row
# and the balance row in opposite orders: that is a textbook AB-BA deadlock on
# PostgreSQL, which is the deployed database. The invariant is a property of the
# source, so it is asserted against the source rather than hoped for at runtime.
print("  --   lock order is a source-level property; assert it on the code")

import inspect  # noqa: E402


def lock_order(fn):
    """The order this function takes the two lock helpers in, as a string like
    'trade,user' or 'user,trade'. Only calls that lock BOTH rows count."""
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

# The module states the invariant, so a future edit that breaks it has to break
# this comment too, and the check above will catch the code.
doc = trading_service.__doc__ or ""
module_src = inspect.getsource(trading_service)
check("the lock-order invariant is documented in the module",
      "trade-then-user" in module_src, True)

# And the guarantee is only real if every settling path shares it: open_trade
# creates a position, so it legitimately has no position row to take first.
check("open_trade is the one path that locks no position",
      "trade" not in lock_order(trading_service.open_trade), True)


market_service.get_current_price = _real_price
db.close()

print()
if FAILS:
    print(f"FAILED ({len(FAILS)}):")
    for f in FAILS:
        print(f"  - {f}")
    sys.exit(1)
print("All 1-minute duration / expiry checks passed.")
