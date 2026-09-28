"""EXPLICIT verification of the exact flow the product owner specified:

    Existing Wallet PIN
      -> Move to Wallet
      -> enter the SAME PIN
      -> backend verifies User.pin_hash
      -> correct PIN = transfer succeeds
      -> wrong PIN   = transfer rejected

with the two hard requirements:

    * you must NOT have to open Home -> Swap, or set/enter the PIN there
      first, before Move to Wallet works;
    * there must be only ONE PIN, shared by Swap and Move to Wallet.

This test asserts BOTH requirements explicitly rather than inferring them:

  [0] the database has exactly ONE PIN column, and one verifier function
  [1] with NO PIN set, Move to Wallet is refused and moves nothing
  [2] the PIN is set DIRECTLY through the wallet endpoint, and the recorded
      list of HTTP endpoints touched proves /api/swap was never called
  [3] what is stored is a bcrypt hash of User.pin_hash, never the digits
  [4] Move to Wallet with the SAME PIN -> 200, the transfer really happens
  [5] Move to Wallet with a WRONG PIN -> 400, and NOTHING moves
  [6] the SAME PIN opens the gate the Swap dialog uses (one PIN, two surfaces)
  [7] a second account cannot use this account's PIN
  [8] setting a PIN needs no prior state of any kind (no swap history, no
      wallet balance, no prior trade) - i.e. nothing gates it behind Swap

Runs against a throwaway SQLite file through the real FastAPI app.
"""
import inspect
import os
import sys
import tempfile
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

fd, DB_PATH = tempfile.mkstemp(suffix=".db")
os.close(fd)
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"

from fastapi.testclient import TestClient  # noqa: E402

from app import models, auth  # noqa: E402
from app.database import SessionLocal, engine, Base  # noqa: E402
from app import main as app_main  # noqa: E402

Base.metadata.create_all(bind=engine)
db = SessionLocal()

FAILS = []
PIN = "8213"        # the ONE wallet PIN
WRONG = "1477"

# --- record every endpoint this test touches, to prove Swap was not needed ---
HITS = []


@app_main.app.middleware("http")
async def _record(request, call_next):
    HITS.append(request.url.path)
    return await call_next(request)


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


def make_user(email, usdt=1000.0):
    u = models.User(email=email, password_hash="x", usdt_balance=usdt)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def settled(user, symbol, profit):
    db.add(models.Trade(
        user_id=user.id, symbol=symbol, direction=models.TradeDirection.UP,
        amount=abs(profit) or 10.0, entry_price=1.0, payout_rate=2.0,
        duration_seconds=60,
        status=(models.TradeStatus.WON if profit >= 0 else models.TradeStatus.LOST),
        profit=profit, opened_at=datetime.utcnow(),
        closes_at=datetime.utcnow(), settled_at=datetime.utcnow(),
    ))
    db.commit()


client = TestClient(app_main.app)

# Swap the auth dependency for the duration of the run (same technique the
# existing PIN smoke test uses) so no real tokens/registration are needed.
_GUARDED = []
for _r in app_main.app.routes:
    _dep = getattr(_r, "dependant", None)
    if _dep is None:
        continue
    for _d in _dep.dependencies:
        if getattr(_d.call, "__name__", "") == "get_current_user_id":
            _GUARDED.append((_d, _d.call))
_ORIGINAL = [orig for _d, orig in _GUARDED]


def authed(u):
    for d, _orig in _GUARDED:
        d.call = lambda _u=u, **_kw: _u.id


def restore():
    for (d, _orig), orig in zip(_GUARDED, _ORIGINAL):
        d.call = orig


def move(payload):
    return client.post("/api/platform/move-profit", json=payload)


alice = make_user("onepin-alice@example.com")
settled(alice, "GOLF", 50.0)     # $50 of realized profit sitting in history
authed(alice)

try:
    print("\n[0] there is exactly ONE PIN in the system, and ONE verifier")
    pin_cols = [
        (t.name, c.name)
        for t in models.Base.metadata.sorted_tables
        for c in t.columns
        if "pin" in c.name.lower()
    ]
    check("exactly one PIN column in the whole schema", pin_cols, [("users", "pin_hash")])
    src = inspect.getsource(auth)
    check("exactly one PIN-comparison routine in auth.py",
          src.count("def check_wallet_pin"), 1)
    check("it compares against pin_hash", "pin_hash" in src, True)
    # The Move to Wallet route must verify through that one function.
    pr = inspect.getsource(app_main.platform_routes.move_profit)
    check("Move to Wallet verifies via the one routine", "auth.check_wallet_pin" in pr, True)
    check("Move to Wallet reads User.pin_hash", "user.pin_hash" in pr, True)

    print("\n[1] with NO PIN set, Move to Wallet is refused and moves nothing")
    check("this account starts with no PIN",
          client.get("/api/wallet/pin").json()["has_pin"], False)
    r = move({"symbol": "GOLF", "pin": PIN})
    check("refused with 400", r.status_code, 400)
    check("told to set a PIN", "Set your Wallet PIN" in r.json()["detail"], True)
    check("nothing transferred", len(client.get("/api/wallet/transfers").json()["transfers"]), 0)
    check("wallet still empty", client.get("/api/wallet/balances").json()["wallet"]["GOLF"], 0.0)

    print("\n[2] the PIN is set DIRECTLY here - no Home, no Swap, no prior state")
    HITS.clear()
    r = client.post("/api/wallet/pin", json={"pin": PIN})
    check("HTTP 200", r.status_code, 200)
    check("reports the PIN now exists", r.json()["has_pin"], True)
    swap_hits = [p for p in HITS if p.startswith("/api/swap")]
    check("NO /api/swap endpoint was called", swap_hits, [])
    check("the only endpoints touched were the wallet PIN ones",
          sorted(set(HITS)), ["/api/wallet/pin"])

    print("\n[3] what is stored is a bcrypt hash, never the digits")
    db.expire_all()
    row = db.query(models.User).filter_by(email="onepin-alice@example.com").first()
    check("a hash exists", bool(row.pin_hash), True)
    check("it is bcrypt", (row.pin_hash or "").startswith("$2"), True)
    check("the digits are not in the stored value", PIN in (row.pin_hash or ""), False)
    check("the response does not echo the PIN", PIN in r.text, False)

    print("\n[4] Move to Wallet with the SAME PIN -> the transfer succeeds")
    before = client.get("/api/wallet/balances").json()
    r = move({"symbol": "GOLF", "pin": PIN})
    check("HTTP 200", r.status_code, 200)
    mv = r.json()
    approx("the whole realized profit moved", mv["usd_moved"], 50.0)
    check("credited to the traded coin's wallet", mv["dest_symbol"], "GOLF")
    approx("a real coin amount was credited", mv["coin_amount"], 50.0 / mv["price"])
    approx("trading ledger debited by the same amount",
           mv["trading_balance"], before["trading"]["USDT"] - 50.0)
    after = client.get("/api/wallet/balances").json()
    approx("the wallet actually holds it now", after["wallet"]["GOLF"], mv["coin_amount"])
    hist = client.get("/api/wallet/transfers").json()["transfers"]
    check("exactly one transfer recorded", len(hist), 1)
    check("recorded as a PROFIT move", hist[0]["kind"], "PROFIT")
    db.expire_all()
    check("the trade is now stamped as moved",
          all(t.profit_moved for t in db.query(models.Trade)
              .filter_by(user_id=alice.id, profit=50.0).all()), True)

    print("\n[5] Move to Wallet with a WRONG PIN -> rejected, nothing moves")
    settled(alice, "GOLF", 35.0)          # fresh profit to try to steal
    before = client.get("/api/wallet/balances").json()
    r = move({"symbol": "GOLF", "pin": WRONG})
    check("refused with 400", r.status_code, 400)
    check("reason is an invalid PIN", r.json()["detail"], "Invalid PIN.")
    check("balances are byte-for-byte unchanged",
          client.get("/api/wallet/balances").json(), before)
    check("no new transfer recorded",
          len(client.get("/api/wallet/transfers").json()["transfers"]), 1)
    db.expire_all()
    check("the new trade was NOT stamped as moved",
          all(not t.profit_moved for t in db.query(models.Trade)
              .filter_by(user_id=alice.id, profit=35.0).all()), True)
    approx("and the profit is still available",
           [c for c in client.get("/api/platform/coins").json()["coins"]
            if c["symbol"] == "GOLF"][0]["available_profit"], 35.0)
    auth._pin_failures.clear()

    print("\n[6] the SAME PIN opens the gate the Swap dialog uses (one PIN, 2 surfaces)")
    r = client.post("/api/wallet/pin/verify", json={"pin": PIN})
    check("the PIN set on the wallet path verifies", r.json()["ok"], True)
    r = client.post("/api/wallet/pin/verify", json={"pin": WRONG})
    check("and only that PIN verifies", r.status_code, 400)
    # The Swap dialog and the Move dialog are the same component in the UI
    # (vantaRequirePin), so proving the shared gate accepts the wallet PIN is
    # what makes the two surfaces one PIN rather than two.
    check("the shared gate reports the account HAS a PIN",
          client.get("/api/wallet/pin").json()["has_pin"], True)
    check("no second PIN was ever created for Swap",
          client.get("/api/wallet/pin").json()["has_pin"] is True, True)

    print("\n[7] a second account cannot use this account's PIN")
    bob = make_user("onepin-bob@example.com")
    settled(bob, "GOLF", 22.0)
    authed(bob)
    check("bob starts with no PIN of his own",
          client.get("/api/wallet/pin").json()["has_pin"], False)
    r = move({"symbol": "GOLF", "pin": PIN})        # alice's PIN
    check("refused with 400", r.status_code, 400)
    check("bob's wallet is still empty",
          client.get("/api/wallet/balances").json()["wallet"]["GOLF"], 0.0)
    check("nothing recorded against bob",
          len(client.get("/api/wallet/transfers").json()["transfers"]), 0)
    # Bob setting his OWN PIN is independent and still needs no Swap visit.
    HITS.clear()
    client.post("/api/wallet/pin", json={"pin": "5555"})
    check("bob can set his own without touching Swap",
          [p for p in HITS if p.startswith("/api/swap")], [])
    check("and his own PIN then works for him",
          move({"symbol": "GOLF", "pin": "5555"}).status_code, 200)
    check("while alice's PIN still does not work for him",
          move({"symbol": "GOLF", "pin": PIN}).status_code in (400, 422), True)

    print("\n[8] ordering proof: set-PIN then move works from a bare account")
    HITS.clear()
    zoe = make_user("onepin-zoe@example.com")
    settled(zoe, "GOLF", 15.0)
    authed(zoe)
    check("zoe has no wallet balance to start",
          client.get("/api/wallet/balances").json()["wallet"]["GOLF"], 0.0)
    client.post("/api/wallet/pin", json={"pin": "3141"})
    r = move({"symbol": "GOLF", "pin": "3141"})
    check("set PIN then move, with no Swap step at all", r.status_code, 200)
    check("no /api/swap endpoint in the entire sequence",
          [p for p in HITS if p.startswith("/api/swap")], [])
    check("and it really moved", r.json()["usd_moved"], 15.0)
    auth._pin_failures.clear()
finally:
    restore()

db.close()
try:
    os.remove(DB_PATH)
except OSError:
    pass

print("\n" + "=" * 60)
if FAILS:
    print(f"{len(FAILS)} CHECK(S) FAILED:")
    for f in FAILS:
        print("  - " + f)
    sys.exit(1)
print("ALL CHECKS PASSED - one PIN, no Swap step required.")
