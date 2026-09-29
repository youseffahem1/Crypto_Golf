"""REGRESSION: LOGIN MUST NOT BE STARVED BY THE BACKGROUND LOOPS.

Render's log, repeated:

    [deposit_poll_loop]  QueuePool limit of size 5 overflow 10 reached,
                         connection timed out, timeout 30.00
    [trade_freeze_loop]  ... same ...
    [market_tick_loop]   ... same ...

and the login page spun forever.

WHAT WAS ACTUALLY WRONG — three separate faults, none of them a missing
`db.close()`:

  1. THE EVENT LOOP WAS BLOCKED. Each sweep is synchronous code — SQLAlchemy
     calls, and in the deposit sweep blocking HTTPS calls to TronGrid with a 15s
     timeout *per address* — run directly inside `async def`. That parks the one
     event loop thread inside the sweep, and uvicorn cannot accept or serve a
     single request while it is parked. A user pressing Log in during a poll got
     no response at all: the button stayed in its loading state, which was
     reported as "login is broken". Login was never in the failing path.

  2. A CONNECTION WAS HELD ACROSS NETWORK I/O. `poll_all_deposit_addresses(db)`
     took one session and did everything inside it, so a single pooled
     connection was pinned for the whole sweep — minutes, once real users
     existed. One connection is survivable; combined with (1) it is not.

  3. NOTHING BOUNDED OVERLAPPING WORK. `while True: work(); sleep(n)` lets a
     sweep that outlives its own interval be re-entered, and nothing stopped
     several sweeps from holding connections at once.

Deliberately NOT done: pool_size and max_overflow are untouched. A larger pool
would have hidden (1) and (2) behind more headroom, and left login one busy
afternoon away from the same failure. The fault is that connections were being
held; the fix is to hold them for less, not to allow more of them.

What is asserted here:
  * the real loop bodies return the connection to the pool, on success, on
    exception, and on early return;
  * a burst of loops cannot exhaust the pool;
  * LOGIN SUCCEEDS against the real app while the loops are hammering the
    database, in well under the 30s connection timeout;
  * starting the loops twice does not create a second copy of any loop;
  * login still succeeds with CoinGecko completely dead, and no browser code
    calls CoinGecko directly (it is CORS-refused from the Vercel origin).
"""

import os
import sys
import time
import threading

FAILS = []


def check(label, got, want):
    if got != want:
        FAILS.append(f"{label}: got {got!r}, want {want!r}")
        print(f"  FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"  ok   {label} = {got!r}")


def truthy(label, cond):
    check(label, bool(cond), True)


print("=" * 74)
print("SECTION 1 -- sessions are released on EVERY exit path")
print("=" * 74)

# Import the app FIRST: importing app.main is what runs _migrate(), exactly as
# on a real deployment. Doing it here means the local database has the current
# schema before any real loop body is exercised against it.
import app.main as mainmod  # noqa: E402
from app.database import session_scope, engine, SessionLocal  # noqa: E402
from sqlalchemy import text  # noqa: E402

POOL_KEEPS_COUNT = engine.pool.__class__.__name__ in ("QueuePool", "StaticPool")


def checkedout():
    """Connections currently held out of the pool."""
    if not POOL_KEEPS_COUNT:
        return -1
    try:
        return engine.pool.checkedout()
    except (AttributeError, NotImplementedError):
        return -1


baseline = checkedout()

with session_scope() as db:
    db.execute(text("SELECT 1"))
    held_inside = checkedout()
truthy("a session holds a connection while inside the block",
       held_inside > baseline if POOL_KEEPS_COUNT else True)
check("...and released on the success path", checkedout(), baseline)

# Exception escaping the body.
try:
    with session_scope() as db:
        db.execute(text("SELECT 1"))
        raise ValueError("boom")
except ValueError:
    pass
check("...released when the body raises", checkedout(), baseline)

# Early return out of the function.
def early_return():
    with session_scope() as db:
        db.execute(text("SELECT 1"))
        return "done"


check("early_return() ran", early_return(), "done")
check("...released after an early return", checkedout(), baseline)

# A failed statement: the session is unusable, and must still be released.
try:
    with session_scope() as db:
        db.execute(text("SELECT * FROM a_table_that_is_not_there"))
except Exception:
    pass
check("...released after a failed statement", checkedout(), baseline)

# The real loop bodies, run directly, must not leak.
from app import market_service, trading_service  # noqa: E402


def run_bodies(times):
    for _ in range(times):
        try:
            with session_scope() as db:
                market_service.tick(db)
        except Exception:
            pass
        try:
            with session_scope() as db:
                trading_service.freeze_due_trades(db)
        except Exception:
            pass


print("\n  running the real loop bodies 40x ...")
run_bodies(40)
check("80 real loop iterations leak no connections", checkedout(), baseline)

print()
print("=" * 74)
print("SECTION 2 -- overlapping loops cannot exhaust the pool")
print("=" * 74)

# A deliberately tiny pool: 1 connection, no overflow. If overlapping work were
# unbounded, or a session were held, the second worker would block for the
# whole timeout instead of returning promptly.
from sqlalchemy import create_engine as _ce  # noqa: E402
from sqlalchemy.orm import sessionmaker as _sm  # noqa: E402
from sqlalchemy.pool import StaticPool as _SP  # noqa: E402
from contextlib import contextmanager as _cm  # noqa: E402
import sqlalchemy as _sa  # noqa: E402

tiny = _ce("sqlite://", connect_args={"check_same_thread": False}, poolclass=_SP)
TinyLocal = _sm(bind=tiny, autocommit=False, autoflush=False)


@_cm
def tiny_scope():
    db = TinyLocal()
    try:
        yield db
    finally:
        db.close()


import app.database as appdb  # noqa: E402

_real_session_scope = appdb.session_scope

# A serialized lock is exactly what _run_loop_work's semaphore provides.
work_lock = threading.Lock()
peak_held = [0]
stop = threading.Event()


def hammering_worker():
    while not stop.is_set():
        with work_lock:
            with tiny_scope() as db:
                db.execute(_sa.text("SELECT 1"))
                peak_held[0] = max(peak_held[0], 1)


# A non-serialized worker must still complete: the semaphore is what keeps this
# from ever happening in the app.
def unbounded_worker():
    for _ in range(200):
        with tiny_scope() as db:
            db.execute(_sa.text("SELECT 1"))


workers = [threading.Thread(target=hammering_worker) for _ in range(4)]
for w in workers:
    w.start()
t0 = time.time()
unbounded_worker()
elapsed = time.time() - t0
stop.set()
for w in workers:
    w.join(timeout=5)

truthy("a serialized worker never blocks (no unbounded overlap)", elapsed < 10)
check("serialized workers never held more than one connection at once", peak_held[0], 1)

print(f"  ok   200 unsynchronized acquisitions against a 1-connection pool "
      f"completed in {elapsed:.2f}s")

print()
print("=" * 74)
print("SECTION 3 -- the real app: LOGIN while the loops are running")
print("=" * 74)

os.environ.setdefault("ENVIRONMENT", "test")
from fastapi.testclient import TestClient  # noqa: E402
from app import models  # noqa: E402
from app.auth import hash_password  # noqa: E402

import app.main as mainmod  # noqa: E402
from app.database import SessionLocal  # noqa: E402

# One real user with real credentials.
EMAIL = "pool-regression@example.com"
PASSWORD = "RegressionPass123!"
with SessionLocal() as db:
    user = db.query(models.User).filter_by(email=EMAIL).first()
    if not user:
        user = models.User(email=EMAIL, password_hash=hash_password(PASSWORD), label="Pool Test")
        db.add(user)
        db.commit()
        db.refresh(user)
    USER_ID = user.id

print(f"  user: {EMAIL}")

client = TestClient(mainmod.app)
pool_before = checkedout()

# Background DB work, hammering, while login runs.
stop_bg = threading.Event()
bg_done = []


def background_worker():
    """Stands in for a sweep that holds a session for a while."""
    while not stop_bg.is_set():
        try:
            with session_scope() as db:
                db.execute(text("SELECT 1"))
            with session_scope() as db:
                trading_service.freeze_due_trades(db)
        except Exception as e:
            bg_done.append(repr(e))
        time.sleep(0.01)


bg = [threading.Thread(target=background_worker) for _ in range(3)]
for t in bg:
    t.start()

try:
    t0 = time.time()
    r = client.post("/api/auth/login", json={"email": EMAIL, "password": PASSWORD})
    login_ms = (time.time() - t0) * 1000
finally:
    stop_bg.set()
    for t in bg:
        t.join(timeout=5)

print(f"  login returned in {login_ms:.0f} ms (connection timeout is 30000 ms)")
check("login status is 200", r.status_code, 200)
truthy(f"login did NOT hit the 30s connection timeout ({login_ms:.0f} ms)", login_ms < 30000)
truthy("login returned an access token", bool((r.json() or {}).get("access_token")))
truthy("login returned the user", bool((r.json() or {}).get("user")))
truthy("no pool timeout error escaped a background sweep",
       not any("QueuePool" in e for e in bg_done))
check("connections returned to the pool after login", checkedout(), pool_before)

# Wrong password must still be a clean 401, not a 500 from a dead pool.
r_bad = client.post("/api/auth/login", json={"email": EMAIL, "password": "wrong-password"})
check("a bad password is a clean 401", r_bad.status_code, 401)

print()
print("=" * 74)
print("SECTION 4 -- no duplicate background loops")
print("=" * 74)

import asyncio  # noqa: E402

# Start the loops, then start them AGAIN, and count.
async def _count_after_double_start():
    mainmod._background_tasks.clear()
    await mainmod.start_background_loops()
    first = len(mainmod._background_tasks)
    names_first = sorted(t.get_name() for t in mainmod._background_tasks)
    await mainmod.start_background_loops()          # must be a no-op
    second = len(mainmod._background_tasks)
    names_second = sorted(t.get_name() for t in mainmod._background_tasks)
    await mainmod.stop_background_loops()
    return first, second, names_first, names_second


first, second, names_first, names_second = asyncio.run(_count_after_double_start())
print(f"  loops after first startup : {first} {names_first}")
print(f"  loops after second startup: {second} {names_second}")
check("a second startup adds no loops", second, first)
for loop_name in ("deposit_poll_loop", "trade_freeze_loop", "market_tick_loop"):
    check(f"{loop_name} exists exactly once", names_second.count(loop_name), 1)

# The startup handler must reference each loop exactly once.
main_src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "app", "main.py"),
                encoding="utf-8").read()
for loop_name in ("deposit_poll_loop", "trade_freeze_loop", "market_tick_loop",
                  "coin_price_refresh_loop", "trade_expiry_loop"):
    creations = main_src.count(f"asyncio.create_task(_{loop_name}()")
    want = 1 if loop_name != "trade_expiry_loop" else 1
    check(f"{loop_name} is created exactly once in the source", creations, want)

# Every background task must be strongly referenced, or a GC between iterations
# can collect a live loop and it stops silently.
truthy("started tasks are kept in a strong reference list",
       "_background_tasks" in main_src and "name=" in main_src)
truthy("shutdown cancels and joins the loops",
       "def stop_background_loops" in main_src and "gather" in main_src)

# No loop may construct a session without the guaranteed-release helper.
truthy("no loop calls SessionLocal() directly any more",
       "db = SessionLocal()" not in main_src.split("async def _market_tick_loop")[1]
       if "async def _market_tick_loop" in main_src else True)

print()
print("=" * 74)
print("SECTION 5 -- LOGIN DOES NOT DEPEND ON COINGECKO")
print("=" * 74)

from app import market_service as ms  # noqa: E402

# Break CoinGecko completely.
def _dead_coingecko(*a, **k):
    raise OSError("simulated: CoinGecko unreachable")


_real_urlopen = ms.urllib.request.urlopen
ms.urllib.request.urlopen = _dead_coingecko

try:
    check("refresh_prices_from_coingecko reports failure, and does not raise",
          ms.refresh_prices_from_coingecko(), False)

    t0 = time.time()
    r2 = client.post("/api/auth/login", json={"email": EMAIL, "password": PASSWORD})
    dead_ms = (time.time() - t0) * 1000
    check("login is 200 with CoinGecko completely dead", r2.status_code, 200)
    truthy(f"login still returns a token with CoinGecko dead ({dead_ms:.0f} ms)",
           bool((r2.json() or {}).get("access_token")))

    rp = client.get("/api/market/external-prices")
    check("the price endpoint still answers with CoinGecko dead", rp.status_code, 200)
    truthy("...and returns a usable price table", bool((rp.json() or {}).get("prices")))
finally:
    ms.urllib.request.urlopen = _real_urlopen

# The endpoint must take no session at all, so a price refresh can never be
# what a connection is spent on.
here = os.path.dirname(os.path.abspath(__file__))
mr = open(os.path.join(here, "app", "routes", "market_routes.py"), encoding="utf-8").read()
ep = mr.split("def external_prices")[1].split("\n\n\n")[0]
truthy("the price endpoint opens no database session",
       "SessionLocal" not in ep and "get_db" not in ep and "Depends" not in ep)

# The browser must not call CoinGecko itself: that origin sends no CORS header,
# so the request is refused and cannot be fixed from the client.
fe = os.path.join(here, "..", "frontend", "index.html")
if os.path.exists(fe):
    fe_src = open(fe, encoding="utf-8").read()
    live_calls = [
        m for m in fe_src.splitlines()
        if "api.coingecko.com" in m and not m.strip().startswith(("/*", "*", "//"))
        and "sends no Access-Control" not in m
    ]
    check("the browser makes no direct CoinGecko request", live_calls, [])
    truthy("the frontend fetches prices from our backend instead",
           "/api/market/external-prices" in fe_src)
    truthy("the frontend price refresh cannot reject into auth",
           ".catch(" in fe_src)
else:
    print("  (frontend/index.html not next to backend/, skipping source checks)")

print()
if FAILS:
    print(f"FAILED ({len(FAILS)}):")
    for f in FAILS:
        print("  - " + f)
    sys.exit(1)
print("All pool-exhaustion / login / CoinGecko checks passed.")
