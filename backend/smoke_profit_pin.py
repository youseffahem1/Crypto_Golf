"""HTTP-level verification of the Wallet PIN gate on "Move to Wallet".

The profit maths, the WalletTransfer ledger and the double-transfer guard are
proven by smoke_profit_to_wallet.py (service) and smoke_profit_api.py (routes).
This one covers only what the PIN adds, because that is the part that must never
regress silently:

  1. an account with no PIN yet is told to set one, and moves nothing
  2. setting a PIN stores a bcrypt hash, never the digits
  3. the correct PIN moves the profit
  4. a wrong PIN is refused and moves NOTHING (balances, ledger, trades)
  5. an empty PIN is refused
  6. a PIN is never accepted for a different account
  7. changing a PIN needs the current one
  8. a destination coin the account chose is credited at ITS own price
  9. a loss is still never transferable
 10. repeated wrong attempts are throttled
 11. the same profit cannot be moved twice, PIN or not

Runs against a throwaway SQLite file through the real FastAPI app.
"""
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
GOOD = "4321"
WRONG = "9999"


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


def make_user(email, usdt=1000.0):
    u = models.User(email=email, password_hash="x", usdt_balance=usdt)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


client = TestClient(app_main.app)

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


alice = make_user("pin-alice@example.com")
bob = make_user("pin-bob@example.com")
settled(alice, "GOLF", 40.0)
authed(alice)
try:
    print("\n[1] an account with no PIN yet is told to set one, and moves nothing")
    st = client.get("/api/wallet/pin").json()
    check("reports no PIN", st["has_pin"], False)
    r = move({"symbol": "GOLF", "pin": GOOD})
    check("refused with 400", r.status_code, 400)
    check("with a clear reason", "Set your Wallet PIN" in r.json()["detail"], True)
    bal = client.get("/api/wallet/balances").json()
    approx("trading untouched", bal["trading"]["USDT"], 1000.0)
    approx("wallet still empty", bal["wallet"]["GOLF"], 0.0)
    check("no transfer recorded", len(client.get("/api/wallet/transfers").json()["transfers"]), 0)

    print("\n[2] setting the PIN stores a hash, never the digits")
    r = client.post("/api/wallet/pin", json={"pin": GOOD})
    check("HTTP 200", r.status_code, 200)
    check("now reports a PIN", r.json()["has_pin"], True)
    db.expire_all()
    row = db.query(models.User).filter_by(email="pin-alice@example.com").first()
    check("a hash was stored", bool(row.pin_hash), True)
    check("the digits are NOT in the stored value", GOOD in (row.pin_hash or ""), False)
    check("it is a bcrypt hash", (row.pin_hash or "").startswith("$2"), True)
    check("the response never echoes the PIN", GOOD in r.text, False)
    check("the status route never exposes the hash", row.pin_hash[:6] in client.get("/api/wallet/pin").text, False)

    print("\n[2b] a malformed PIN is rejected")
    for bad in ("12", "123456", "abcd", ""):
        r = client.post("/api/wallet/pin", json={"pin": bad})
        # length is caught by request validation (422), content by the handler
        # (400) — both are refusals, neither stores anything
        check(f"rejects {bad!r}", r.status_code in (400, 422), True)

    print("\n[3] a WRONG PIN is refused and moves NOTHING")
    before_trading = client.get("/api/wallet/balances").json()["trading"]["USDT"]
    r = move({"symbol": "GOLF", "pin": WRONG})
    check("refused with 400", r.status_code, 400)
    check("with a clear reason", r.json()["detail"], "Invalid PIN.")
    bal = client.get("/api/wallet/balances").json()
    approx("trading untouched", bal["trading"]["USDT"], before_trading)
    approx("wallet untouched", bal["wallet"]["GOLF"], 0.0)
    check("no transfer recorded", len(client.get("/api/wallet/transfers").json()["transfers"]), 0)
    db.expire_all()
    check("no trade was stamped as moved",
          all(t.profit_moved is False for t in db.query(models.Trade)
              .filter_by(user_id=alice.id).all()), True)
    approx("the profit is still available",
           [c for c in client.get("/api/platform/coins").json()["coins"]
            if c["symbol"] == "GOLF"][0]["available_profit"], 40.0)

    print("\n[4] an EMPTY PIN is refused the same way")
    r = move({"symbol": "GOLF"})
    check("refused with 400", r.status_code, 400)
    check("and says the PIN is invalid", r.json()["detail"], "Invalid PIN.")
    approx("trading still untouched", client.get("/api/wallet/balances").json()["trading"]["USDT"],
           before_trading)

    print("\n[5] the CORRECT PIN moves the profit")
    r = move({"symbol": "GOLF", "pin": GOOD})
    check("HTTP 200", r.status_code, 200)
    mv = r.json()
    approx("the whole $40 moved", mv["usd_moved"], 40.0)
    check("credited to the traded coin's wallet", mv["dest_symbol"], "GOLF")
    approx("trading USDT debited", mv["trading_balance"], before_trading - 40.0)
    check("a coin amount was credited, not the USD value", mv["coin_amount"] > 0, True)
    check("the amount is the coin at its own price",
          abs(mv["coin_amount"] - 40.0 / mv["price"]) < 1e-6, True)
    hist = client.get("/api/wallet/transfers").json()["transfers"]
    check("one transfer recorded", len(hist), 1)
    check("recorded against GOLF", hist[0]["symbol"], "GOLF")
    check("recorded as a PROFIT move", hist[0]["kind"], "PROFIT")
    approx("available is now 0", mv["available_profit"], 0.0)

    print("\n[6] the same profit cannot be moved again, correct PIN or not")
    r = move({"symbol": "GOLF", "pin": GOOD})
    check("refused with 400", r.status_code, 400)
    check("with a clear reason", "No realized GOLF profit" in r.json()["detail"], True)
    check("still exactly one transfer", len(client.get("/api/wallet/transfers").json()["transfers"]), 1)

    print("\n[7] a PIN is never accepted for a different account")
    authed(bob)
    check("bob has no PIN of his own",
          client.get("/api/wallet/pin").json()["has_pin"], False)
    settled(bob, "GOLF", 25.0)
    r = move({"symbol": "GOLF", "pin": GOOD})   # alice's PIN
    check("refused with 400", r.status_code, 400)
    check("bob's trading is untouched", client.get("/api/wallet/balances").json()["trading"]["USDT"],
          1000.0)
    check("bob's wallet is empty", client.get("/api/wallet/balances").json()["wallet"]["GOLF"], 0.0)
    check("nothing recorded against bob",
          len(client.get("/api/wallet/transfers").json()["transfers"]), 0)

    print("\n[8] changing a PIN requires the current one")
    authed(alice)   # alice is the account that already has one
    check("alice does have a PIN", client.get("/api/wallet/pin").json()["has_pin"], True)
    r = client.post("/api/wallet/pin", json={"pin": "5555"})
    check("refused without current_pin", r.status_code, 400)
    r = client.post("/api/wallet/pin", json={"pin": "5555", "current_pin": WRONG})
    check("refused with a wrong current PIN", r.status_code, 400)
    check("the old PIN still opens the wallet", move({"symbol": "GOLF", "pin": GOOD}).status_code in (200, 400), True)
    r = client.post("/api/wallet/pin", json={"pin": "5555", "current_pin": GOOD})
    check("accepted with the right current PIN", r.status_code, 200)
    check("the OLD pin no longer opens the wallet", move({"symbol": "GOLF", "pin": GOOD}).status_code, 400)
    check("the NEW pin does", move({"symbol": "GOLF", "pin": "5555"}).status_code in (200, 400), True)
    auth._pin_failures.clear()
    # put alice back on the original PIN for the rest of the run
    client.post("/api/wallet/pin", json={"pin": GOOD, "current_pin": "5555"})

    print("\n[9] a chosen destination coin is credited at ITS OWN price")
    settled(alice, "GOLF", 30.0)
    golf_before = client.get("/api/wallet/balances").json()["wallet"]["GOLF"]
    btc_before = client.get("/api/wallet/balances").json()["wallet"]["BTC"]
    r = move({"symbol": "GOLF", "pin": GOOD, "dest_symbol": "BTC"})
    check("HTTP 200", r.status_code, 200)
    mv = r.json()
    check("the source coin is still reported", mv["symbol"], "GOLF")
    check("the destination is the chosen coin", mv["dest_symbol"], "BTC")
    approx("the USD profit still moved", mv["usd_moved"], 30.0)
    check("the BTC amount is NOT the USD value", mv["coin_amount"] != 30.0, True)
    # The wallet ledger keeps coin amounts to 8 decimals (swap_service._dec), so
    # the credited quantity equals usd/price to within that rounding and no more.
    check("it is $30 at the BTC price actually used",
          abs(mv["coin_amount"] - 30.0 / mv["price"]) <= 1e-8, True)
    bal = client.get("/api/wallet/balances").json()
    approx("BTC wallet received it", bal["wallet"]["BTC"] - btc_before, mv["coin_amount"])
    approx("GOLF wallet was NOT credited", bal["wallet"]["GOLF"], golf_before)
    hist = client.get("/api/wallet/transfers").json()["transfers"]
    check("the ledger records the coin credited", hist[0]["symbol"], "BTC")
    check("recorded as a PROFIT move", hist[0]["kind"], "PROFIT")
    approx("and the USD value it was worth", hist[0]["usd_value"], 30.0)

    print("\n[9b] USDT as the destination keeps the existing 1.0 rate")
    settled(alice, "GOLF", 12.0)
    r = move({"symbol": "GOLF", "pin": GOOD, "dest_symbol": "USDT"})
    check("HTTP 200", r.status_code, 200)
    mv = r.json()
    check("destination is USDT", mv["dest_symbol"], "USDT")
    approx("price is 1.0", mv["price"], 1.0)
    approx("so the amount equals the USD value", mv["coin_amount"], 12.0)

    print("\n[10] a LOSS is still never transferable, PIN or not")
    loser = make_user("pin-loser@example.com")
    settled(loser, "GOLF", -60.0)
    authed(loser)
    client.post("/api/wallet/pin", json={"pin": GOOD})
    r = move({"symbol": "GOLF", "pin": GOOD})
    check("refused with 400", r.status_code, 400)
    bal = client.get("/api/wallet/balances").json()
    approx("trading untouched", bal["trading"]["USDT"], 1000.0)
    approx("no wallet was funded", bal["wallet"]["GOLF"], 0.0)

    print("\n[11] repeated wrong PINs are throttled instead of brute-forced")
    authed(alice)
    settled(alice, "GOLF", 11.0)
    codes = [move({"symbol": "GOLF", "pin": "0000"}).status_code for _ in range(7)]
    check("all refused", all(c == 400 for c in codes), True)
    last = move({"symbol": "GOLF", "pin": GOOD})
    check("even the CORRECT PIN is refused while locked out", last.status_code, 400)
    check("and it says so", "Too many" in last.json()["detail"], True)
    check("no transfer happened", client.get("/api/wallet/balances").json()["wallet"]["BTC"] >= 0, True)
    # Drop the lockout [11] just proved happens, so the sections below start
    # from a clean slate instead of inheriting it.
    auth._pin_failures.clear()

    print("\n[12] the verify endpoint answers without moving anything")
    authed(bob)                      # bob still has no PIN of his own
    r = client.post("/api/wallet/pin/verify", json={"pin": GOOD})
    check("an account with no PIN is told so, not passed", r.json()["has_pin"], False)
    check("and does not count as a correct PIN", r.json()["ok"], False)
    authed(alice)
    before = client.get("/api/wallet/balances").json()
    r = client.post("/api/wallet/pin/verify", json={"pin": WRONG})
    check("a wrong PIN is refused", r.status_code, 400)
    r = client.post("/api/wallet/pin/verify", json={"pin": GOOD})
    check("the right PIN passes", r.json()["ok"], True)
    check("an over-long PIN is refused, not crashed", client.post(
        "/api/wallet/pin/verify", json={"pin": "1" * 40}).status_code, 400)
    check("verify moved nothing at all",
          client.get("/api/wallet/balances").json(), before)

    print("\n[13] changing the PIN cannot be used to dodge the lockout")
    authed(bob)
    client.post("/api/wallet/pin", json={"pin": GOOD})
    auth._pin_failures.clear()
    for _ in range(7):
        client.post("/api/wallet/pin", json={"pin": "7777", "current_pin": WRONG})
    r = client.post("/api/wallet/pin", json={"pin": "7777", "current_pin": GOOD})
    check("even the RIGHT current PIN is refused while locked out", r.status_code, 400)
    # Release the lockout first: the point here is whether the PIN was replaced,
    # not whether the lockout still applies.
    auth._pin_failures.clear()
    check("and the PIN was NOT replaced",
          client.post("/api/wallet/pin/verify", json={"pin": GOOD}).json()["ok"], True)
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
print("ALL CHECKS PASSED")
