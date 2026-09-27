"""HTTP-level verification of the realized PROFIT / LOSS split and the
"Move to Wallet" bridge, exercised through the real FastAPI app against a
throwaway SQLite file. The service layer is proven by
smoke_profit_to_wallet.py; this proves the routes, the auth boundary, the
response schema and the error status the button actually receives.
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

from app import models  # noqa: E402
from app import auth  # noqa: E402
from app.database import SessionLocal, engine, Base  # noqa: E402
from app.config import PLATFORM_COINS  # noqa: E402
from app import main as app_main  # noqa: E402

Base.metadata.create_all(bind=engine)
db = SessionLocal()

FAILS = []


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
    t = models.Trade(
        user_id=user.id, symbol=symbol, direction=models.TradeDirection.UP,
        amount=abs(profit) or 10.0, entry_price=1.0, payout_rate=2.0,
        duration_seconds=60,
        status=(models.TradeStatus.WON if profit >= 0 else models.TradeStatus.LOST),
        profit=profit, opened_at=datetime.utcnow(),
        closes_at=datetime.utcnow(), settled_at=datetime.utcnow(),
    )
    db.add(t)
    db.commit()


user = models.User(email="api@example.com", password_hash="x",
                   usdt_balance=1000.0, golf_balance=0.0, golf_wallet_balance=0.0)
other = models.User(email="other@example.com", password_hash="x", usdt_balance=1000.0)
db.add_all([user, other])
db.commit()
db.refresh(user)
db.refresh(other)

# Every account that moves profit needs a stored Wallet PIN: the move endpoint
# verifies it before it touches a balance. The digits are never stored, so the
# hash is written here exactly as the setup endpoint writes it.
PIN = "4321"


def set_pin(u):
    u.pin_hash = auth.hash_pin(PIN)
    db.commit()
    db.refresh(u)
    return u


for _u in (user, other):
    set_pin(_u)

client = TestClient(app_main.app)

# Resolve the guarded dependencies once, BEFORE any swap, so the original
# callable is captured and later swaps are not confused by their own lambda.
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
    """Act as `u` for every route guarded by get_current_user_id.

    Only the dependency *call* is swapped: FastAPI already resolved the
    header/cookie sub-dependencies at registration time, so this stays a test of
    the profit routes rather than a re-test of the auth layer. Accepts **kwargs
    because the real dependency is declared with Header()/Cookie() arguments."""
    for d, _orig in _GUARDED:
        d.call = lambda _u=u, **_kw: _u.id


def restore():
    for (d, _orig), orig in zip(_GUARDED, _ORIGINAL):
        d.call = orig


def move(payload=None):
    """POST /move-profit as the authenticated user, carrying that account's PIN.

    The amount is never part of the body — that is the point of the endpoint —
    so this helper only ever forwards a symbol and an optional destination."""
    return client.post("/api/platform/move-profit", json=dict(payload or {}, pin=PIN))


print("\n[1] the move endpoint requires authentication")
r = client.post("/api/platform/move-profit", json={"symbol": "GOLF"})
check("unauthenticated is rejected", r.status_code in (401, 403), True)

authed(user)
try:
    print("\n[2] CASE 1 â€” a profit: PROFIT +$20.00, LOSS $0.00")
    settled(user, "GOLF", 20.0)
    r = client.get("/api/platform/coins")
    check("HTTP 200", r.status_code, 200)
    body = r.json()
    approx("realized_profit", body["realized_profit"], 20.0)
    approx("realized_loss", body["realized_loss"], 0.0)
    approx("available_profit", body["available_profit"], 20.0)
    check("realized_profit is never negative", body["realized_profit"] >= 0, True)
    golf = [c for c in body["coins"] if c["symbol"] == "GOLF"][0]
    approx("GOLF coin profit", golf["realized_profit"], 20.0)
    approx("GOLF coin available", golf["available_profit"], 20.0)
    check("the signed net is still reported separately", "total_trade_profit" in body, True)

    print("\n[3] CASE 2 â€” a loss only: PROFIT $0.00 and nothing is transferable")
    settled(user, "GOLF", -60.0)
    body = client.get("/api/platform/coins").json()
    approx("PROFIT stays positive", body["realized_profit"], 20.0)
    approx("LOSS", body["realized_loss"], -60.0)
    check("net is not what PROFIT reports", body["total_trade_profit"] != body["realized_profit"], True)

    print("\n[4] CASE 3 â€” profit and loss are never netted for the transfer")
    r = move({"symbol": "GOLF"})
    check("HTTP 200", r.status_code, 200)
    mv = r.json()
    approx("exactly the +$20 moved, not the -$40 net", mv["usd_moved"], 20.0)
    check("the credited coin is the traded coin", mv["symbol"], "GOLF")
    check("the destination defaults to the traded coin", mv["dest_symbol"], "GOLF")
    check("kind", mv["kind"], "PROFIT")
    check("direction", mv["direction"], "TO_WALLET")
    approx("trading USDT debited", mv["trading_balance"], 980.0)
    check("a coin amount was credited", mv["coin_amount"] > 0, True)
    check("the amount is the coin, not the USD value", mv["coin_amount"] != mv["usd_moved"], True)

    print("\n[5] CASE 4 â€” the same profit cannot be transferred again")
    approx("available is now 0", mv["available_profit"], 0.0)
    r2 = move({"symbol": "GOLF"})
    check("the second attempt is refused", r2.status_code, 400)
    check("with a clear reason", "No realized GOLF profit" in r2.json()["detail"], True)
    bal = client.get("/api/wallet/balances").json()
    approx("trading USDT unchanged by the refusal", bal["trading"]["USDT"], 980.0)
    approx("GOLF wallet holds the first transfer only", bal["wallet"]["GOLF"], mv["coin_amount"])

    print("\n[6] the wallet received the profit in the TRADED coin only")
    approx("USDT wallet untouched", bal["wallet"]["USDT"], 0.0)
    check("wallet != trading for the same coin",
          bal["wallet"]["GOLF"] != bal["trading"]["GOLF"], True)
    hist = client.get("/api/wallet/transfers").json()["transfers"]
    check("one transfer recorded", len(hist), 1)
    check("recorded against GOLF", hist[0]["symbol"], "GOLF")
    check("recorded as TO_WALLET", hist[0]["direction"], "TO_WALLET")

    print("\n[7] a loss alone yields 400 and moves nothing")
    solo = models.User(email="solo@example.com", password_hash="x", usdt_balance=1000.0)
    db.add(solo)
    db.commit()
    db.refresh(solo)
    set_pin(solo)
    settled(solo, "GOLF", -60.0)
    authed(solo)
    r = move({"symbol": "GOLF"})
    check("refused with 400", r.status_code, 400)
    bal = client.get("/api/wallet/balances").json()
    approx("trading untouched", bal["trading"]["USDT"], 1000.0)
    approx("wallet still empty", bal["wallet"]["GOLF"], 0.0)
    body = client.get("/api/platform/coins").json()
    approx("PROFIT is $0.00", body["realized_profit"], 0.0)
    approx("LOSS is the full amount", body["realized_loss"], -60.0)

    print("\n[8] the destination really is the coin being traded")
    settled(solo, "GOLF", 30.0)
    settled(solo, "NOVA", 70.0)
    r = move({"symbol": "GOLF"})
    mv = r.json()
    approx("only GOLF's own profit moved", mv["usd_moved"], 30.0)
    bal = client.get("/api/wallet/balances").json()
    check("GOLF wallet funded", bal["wallet"]["GOLF"] > 0, True)
    approx("NOVA wallet still empty", bal["wallet"]["NOVA"], 0.0)
    approx("NOVA profit still available",
           [c for c in client.get("/api/platform/coins").json()["coins"]
            if c["symbol"] == "NOVA"][0]["available_profit"], 70.0)

    print("\n[9] the endpoint never accepts a client-chosen amount")
    r = move({"symbol": "NOVA", "amount": 999999})
    mv = r.json()
    check("the amount field is ignored", mv["usd_moved"], 70.0)
    check("only the recorded profit moved", mv["usd_moved"] != 999999, True)

    print("\n[10] an untradeable coin is refused, not silently zeroed")
    r = move({"symbol": "DOGE2"})
    check("refused with 400", r.status_code, 400)

    print("\n[10b] an untradeable DESTINATION is refused too")
    before = client.get("/api/wallet/balances").json()
    n_before = len(client.get("/api/wallet/transfers").json()["transfers"])
    r = move({"symbol": "GOLF", "dest_symbol": "DOGE2"})
    check("refused with 400", r.status_code, 400)
    after = client.get("/api/wallet/balances").json()
    check("trading unchanged by the refusal", after["trading"], before["trading"])
    check("wallets unchanged by the refusal", after["wallet"], before["wallet"])
    check("no extra transfer recorded",
          len(client.get("/api/wallet/transfers").json()["transfers"]), n_before)

    print("\n[11] the default symbol is the coin the terminal trades")
    # A fresh account: every earlier case deliberately drained its GOLF profit,
    # so a 200 here proves the default resolved, not that a refund happened.
    dflt = models.User(email="default@example.com", password_hash="x", usdt_balance=1000.0)
    db.add(dflt)
    db.commit()
    db.refresh(dflt)
    set_pin(dflt)
    settled(dflt, "NOVA", 90.0)
    settled(dflt, "GOLF", 5.0)
    authed(dflt)
    r = move({})
    check("HTTP 200", r.status_code, 200)
    mv = r.json()
    check("defaults to GOLF", mv["symbol"], "GOLF")
    check("and credits the GOLF wallet by default", mv["dest_symbol"], "GOLF")
    approx("and moves GOLF's profit, not NOVA's", mv["usd_moved"], 5.0)
    bal = client.get("/api/wallet/balances").json()
    approx("GOLF wallet funded", bal["wallet"]["GOLF"], mv["coin_amount"])
    approx("NOVA wallet untouched", bal["wallet"]["NOVA"], 0.0)

    print("\n[12] one user's profit is never another user's")
    authed(other)
    body = client.get("/api/platform/coins").json()
    approx("a fresh account has no profit", body["realized_profit"], 0.0)
    r = move({"symbol": "GOLF"})
    check("and nothing to move", r.status_code, 400)
finally:
    restore()

print("\n[13] the platform coin list the endpoint validates against is real")
check("the tested symbols are platform coins", {"GOLF", "NOVA"} <= set(PLATFORM_COINS), True)

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
