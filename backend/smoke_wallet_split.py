"""End-to-end verification of the trading <-> wallet separation.

Runs against a throwaway SQLite file. Asserts, in order:
  1. both ledgers exist and are independent
  2. a wallet starts empty
  3. the transfer cannot exceed the source balance
  4. a full "move max" (exact balance) succeeds
  5. the destination coin path works (BTC -> CoinBalance row)
  6. the reverse direction works
  7. every move is recorded in the WalletTransfer ledger
  8. a P2P send spends WALLET, not trading, and credits the peer's wallet
  9. a P2P send is rejected when the sender has only a trading balance
 10. an unsupported symbol / bad direction / zero amount are all rejected
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

fd, DB_PATH = tempfile.mkstemp(suffix=".db")
os.close(fd)
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"

from app import models, swap_service, transfer_service  # noqa: E402
from app.database import SessionLocal, engine  # noqa: E402
from app.database import Base  # noqa: E402

Base.metadata.create_all(bind=engine)
db = SessionLocal()

FAILS = []


def check(label, got, want):
    if got != want:
        FAILS.append(f"{label}: got {got!r}, want {want!r}")
        print(f"  FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"  ok   {label} = {got!r}")


def approx(label, got, want, tol=1e-8):
    if abs(float(got) - float(want)) > tol:
        FAILS.append(f"{label}: got {got!r}, want {want!r}")
        print(f"  FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"  ok   {label} = {got!r}")


def raises(label, exc, fn, *a, **kw):
    try:
        fn(*a, **kw)
    except exc as e:
        print(f"  ok   {label} rejected: {e}")
        return
    FAILS.append(f"{label}: expected {exc.__name__}")
    print(f"  FAIL {label}: expected {exc.__name__}")


alice = models.User(email="alice@example.com", password_hash="x",
                    usdt_balance=1000.0, golf_balance=0.0)
bob = models.User(email="bob@example.com", password_hash="x",
                  usdt_balance=500.0, golf_balance=0.0)
# Carol holds a TRADING balance but has never moved anything into her wallet,
# which is exactly the case that must be refused by a P2P send.
carol = models.User(email="carol@example.com", password_hash="x",
                    usdt_balance=750.0, golf_balance=0.0)
db.add_all([alice, bob, carol])
db.commit()
db.refresh(alice)
db.refresh(bob)
db.refresh(carol)
db.add(models.CoinBalance(user_id=alice.id, symbol="BTC", balance=2.0, wallet_balance=0.0))
db.commit()

print("\n[1] both ledgers exist and are independent")
t = swap_service.user_balances(db, alice.id)
w = swap_service.user_wallet_balances(db, alice.id)
check("trading USDT", t["USDT"], 1000.0)
check("trading BTC", t["BTC"], 2.0)
check("wallet USDT", w["USDT"], 0.0)
check("wallet BTC", w["BTC"], 0.0)
check("wallet GOLF", w["GOLF"], 0.0)

print("\n[2] a wallet starts empty")
check("alice wallet total is 0", sum(w.values()), 0.0)

print("\n[3] the source balance cannot be exceeded")
raises("move 1001 USDT", swap_service.SwapError, swap_service.move_between_accounts,
       db, alice.id, "USDT", 1001.0, "TO_WALLET")
raises("move 2.5 BTC (has 2)", swap_service.SwapError, swap_service.move_between_accounts,
       db, alice.id, "BTC", 2.5, "TO_WALLET")
raises("move negative", swap_service.SwapError, swap_service.move_between_accounts,
       db, alice.id, "USDT", -5, "TO_WALLET")
raises("move zero", swap_service.SwapError, swap_service.move_between_accounts,
       db, alice.id, "USDT", 0, "TO_WALLET")
raises("unsupported symbol", swap_service.SwapError, swap_service.move_between_accounts,
       db, alice.id, "DOGE2", 1.0, "TO_WALLET")
raises("bad direction", swap_service.SwapError, swap_service.move_between_accounts,
       db, alice.id, "USDT", 1.0, "SIDEWAYS")
check("nothing moved after rejections", swap_service.user_balances(db, alice.id)["USDT"], 1000.0)

print("\n[4] a partial move debits one ledger and credits the other")
r = swap_service.move_between_accounts(db, alice.id, "USDT", 250.5, "TO_WALLET")
approx("trading USDT", swap_service.user_balances(db, alice.id)["USDT"], 749.5)
approx("wallet USDT", swap_service.user_wallet_balances(db, alice.id)["USDT"], 250.5)
approx("response trading", r["trading_balance"], 749.5)
approx("response wallet", r["wallet_balance"], 250.5)
check("response direction", r["direction"], "TO_WALLET")

print("\n[5] the destination-coin path (CoinBalance row) works")
r = swap_service.move_between_accounts(db, alice.id, "BTC", 0.75, "TO_WALLET")
approx("trading BTC", swap_service.user_balances(db, alice.id)["BTC"], 1.25)
approx("wallet BTC", swap_service.user_wallet_balances(db, alice.id)["BTC"], 0.75)

print("\n[6] move max - the exact balance, no rounding error")
r = swap_service.move_between_accounts(db, alice.id, "USDT", 749.5, "TO_WALLET")
approx("trading USDT", swap_service.user_balances(db, alice.id)["USDT"], 0.0)
approx("wallet USDT", swap_service.user_wallet_balances(db, alice.id)["USDT"], 1000.0)
check("wallet equals original trading", r["wallet_balance"], 1000.0)

print("\n[7] the reverse direction moves funds back")
r = swap_service.move_between_accounts(db, alice.id, "USDT", 400.0, "TO_TRADING")
approx("trading USDT", swap_service.user_balances(db, alice.id)["USDT"], 400.0)
approx("wallet USDT", swap_service.user_wallet_balances(db, alice.id)["USDT"], 600.0)
check("response direction", r["direction"], "TO_TRADING")
raises("reverse cannot exceed wallet", swap_service.SwapError,
       swap_service.move_between_accounts, db, alice.id, "USDT", 600.01, "TO_TRADING")

print("\n[8] the ledger recorded every move")
rows = db.query(models.WalletTransfer).filter_by(user_id=alice.id).all()
check("ledger row count", len(rows), 4)
# id is a random UUID, so it carries no chronological meaning — compare the
# contents as multisets rather than pretending the rows come back in order.
check("ledger dirs", sorted(x.direction.value for x in rows),
      sorted(["TO_WALLET", "TO_WALLET", "TO_WALLET", "TO_TRADING"]))
check("ledger symbols", sorted(x.symbol for x in rows),
      sorted(["USDT", "BTC", "USDT", "USDT"]))
check("every amount is positive", all(float(x.amount) > 0 for x in rows), True)
check("bob has no wallet rows", db.query(models.WalletTransfer).filter_by(user_id=bob.id).count(), 0)

print("\n[9] a P2P send spends WALLET and credits the peer's wallet")
tx = transfer_service.execute_transfer(db, alice.id, "bob@example.com", "USDT", 100.0)
check("transfer completed", tx.status.value, "COMPLETED")
approx("alice wallet after send", swap_service.user_wallet_balances(db, alice.id)["USDT"], 500.0)
approx("bob wallet after receive", swap_service.user_wallet_balances(db, bob.id)["USDT"], 100.0)
approx("alice TRADING untouched by send", swap_service.user_balances(db, alice.id)["USDT"], 400.0)
approx("bob TRADING untouched by receive", swap_service.user_balances(db, bob.id)["USDT"], 500.0)

print("\n[10] a P2P send is rejected on a trading-only balance")
raises("carol has trading USDT but no wallet USDT", transfer_service.TransferError,
       transfer_service.execute_transfer, db, carol.id, "alice@example.com", "USDT", 1.0)
check("carol trading untouched", swap_service.user_balances(db, carol.id)["USDT"], 750.0)
check("carol wallet still empty", swap_service.user_wallet_balances(db, carol.id)["USDT"], 0.0)
raises("send to self", transfer_service.TransferError,
       transfer_service.execute_transfer, db, alice.id, "alice@example.com", "USDT", 1.0)
raises("send to unknown", transfer_service.TransferError,
       transfer_service.execute_transfer, db, alice.id, "nobody@example.com", "USDT", 1.0)

db.close()

try:
    os.remove(DB_PATH)
except OSError:
    pass

print("\n" + ("=" * 60))
if FAILS:
    print(f"{len(FAILS)} CHECK(S) FAILED:")
    for f in FAILS:
        print("  - " + f)
    sys.exit(1)
print("ALL CHECKS PASSED")
