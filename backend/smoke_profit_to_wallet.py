"""End-to-end verification of the realized PROFIT / LOSS split and the
"Move to Wallet" bridge that transfers positive realized profit into the SAME
coin's wallet.

Runs against a throwaway SQLite file. Asserts, in order:
  1. a winning trade reports a positive profit and no loss
  2. a losing trade reports $0.00 profit and a negative loss  (never the reverse)
  3. wins and losses are NOT netted - profit stays +$20 beside a -$60 loss
  4. only positive realized profit is transferable
  5. the profit lands in the TRADED coin's wallet, not USDT
  6. the trading ledger is debited and the two ledgers stay independent
  7. the same profit cannot be transferred twice
  8. a loss is never transferable and never produces a negative transfer
  9. a later win is transferable again
 10. only the traded coin's profit moves to that coin's wallet
 11. a partial balance moves what is left and never goes negative
 12. every move is recorded in the WalletTransfer ledger
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

fd, DB_PATH = tempfile.mkstemp(suffix=".db")
os.close(fd)
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"

from app import models, market_service, swap_service, trading_service  # noqa: E402
from app.database import SessionLocal, engine, Base  # noqa: E402

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


def raises(label, exc, fn, *a, **kw):
    try:
        fn(*a, **kw)
    except exc as e:
        print(f"  ok   {label} rejected: {e}")
        return
    FAILS.append(f"{label}: expected {exc.__name__}")
    print(f"  FAIL {label}: expected {exc.__name__}")


def settled(user, symbol, profit, when=None):
    """One already-closed trade. profit is the signed realized result."""
    t = models.Trade(
        user_id=user.id, symbol=symbol, direction=models.TradeDirection.UP,
        amount=abs(profit) or 10.0, entry_price=1.0, payout_rate=2.0,
        duration_seconds=60,
        status=(models.TradeStatus.WON if profit >= 0 else models.TradeStatus.LOST),
        profit=profit,
        opened_at=when or datetime.utcnow(),
        closes_at=when or datetime.utcnow(),
        settled_at=when or datetime.utcnow(),
    )
    db.add(t)
    db.commit()
    return t


user = models.User(email="profit@example.com", password_hash="x",
                   usdt_balance=1000.0, golf_balance=0.0, golf_wallet_balance=0.0)
db.add(user)
db.commit()
db.refresh(user)

PRICE = 0.02  # GOLF, matching the seeded market

print("\n[1] a winning trade is profit, and only profit")
settled(user, "GOLF", 20.0)
s = trading_service.realized_split(db, user.id, "GOLF")
approx("profit", s["profit"], 20.0)
approx("loss", s["loss"], 0.0)
check("profit is never negative", s["profit"] >= 0, True)
approx("available", s["available"], 20.0)

print("\n[2] a losing trade is a loss, and only a loss")
lossy = settled(user, "GOLF", -60.0)
s = trading_service.realized_split(db, user.id, "GOLF")
approx("profit is unchanged by a loss", s["profit"], 20.0)
approx("loss", s["loss"], -60.0)
check("loss is never positive", s["loss"] <= 0, True)
approx("net (never shown as PROFIT)", s["net"], -40.0)

print("\n[3] wins and losses are never netted into the profit figure")
check("profit is +20, not -40", s["profit"], 20.0)
check("a loss does not cancel a win", s["profit"] > 0, True)
check("'net' is never what the card shows", s["profit"] != s["net"], True)

print("\n[4] only positive realized profit is transferable")
approx("available excludes the loss", s["available"], 20.0)
check("available is never negative", s["available"] >= 0, True)
raises("a negative amount can never be moved", swap_service.SwapError,
       swap_service.move_profit_to_wallet, db, user.id, "GOLF", -60.0, PRICE)
raises("a zero amount can never be moved", swap_service.SwapError,
       swap_service.move_profit_to_wallet, db, user.id, "GOLF", 0.0, PRICE)

print("\n[5] the profit lands in the TRADED coin's wallet, not USDT")
r = swap_service.move_profit_to_wallet(db, user.id, "GOLF", s["available"], PRICE)
check("credited symbol is the traded coin", r["symbol"], "GOLF")
approx("coin amount credited to the GOLF wallet", r["wallet_balance"], 20.0 / PRICE)
approx("GOLF wallet balance", swap_service.get_wallet_balance(db, user, "GOLF"), 1000.0)
approx("USDT wallet is untouched", swap_service.get_wallet_balance(db, user, "USDT"), 0.0)
check("row kind is PROFIT", r["kind"], "PROFIT")
check("direction", r["direction"], "TO_WALLET")

print("\n[6] trading is debited and the two ledgers stay independent")
approx("trading USDT debited by the profit", r["trading_balance"], 980.0)
approx("trading GOLF untouched", swap_service.get_balance(db, user, "GOLF"), 0.0)
check("wallet GOLF != trading GOLF",
      swap_service.get_wallet_balance(db, user, "GOLF") != swap_service.get_balance(db, user, "GOLF"), True)

print("\n[7] the same profit cannot be transferred again")
s2 = trading_service.realized_split(db, user.id, "GOLF")
approx("profit total is unchanged (history)", s2["profit"], 20.0)
approx("available is now zero", s2["available"], 0.0)
raises("second transfer is refused", swap_service.SwapError,
       swap_service.move_profit_to_wallet, db, user.id, "GOLF", 20.0, PRICE)
approx("GOLF wallet unchanged by the refusal",
       swap_service.get_wallet_balance(db, user, "GOLF"), 1000.0)
approx("trading USDT unchanged by the refusal",
       swap_service.get_balance(db, user, "USDT"), 980.0)

print("\n[8] a loss alone is never transferable")
solo = models.User(email="loser@example.com", password_hash="x", usdt_balance=1000.0)
db.add(solo)
db.commit()
db.refresh(solo)
settled(solo, "GOLF", -60.0)
ls = trading_service.realized_split(db, solo.id, "GOLF")
approx("profit", ls["profit"], 0.0)
approx("loss", ls["loss"], -60.0)
approx("available", ls["available"], 0.0)
raises("no loss can be transferred", swap_service.SwapError,
       swap_service.move_profit_to_wallet, db, solo.id, "GOLF", 60.0, PRICE)
approx("trading USDT untouched", swap_service.get_balance(db, solo, "USDT"), 1000.0)
approx("wallet still empty", swap_service.get_wallet_balance(db, solo, "GOLF"), 0.0)

print("\n[9] a later win is transferable again")
check("a losing trade is never stamped", bool(lossy.profit_moved), False)
settled(user, "GOLF", 5.0)
s3 = trading_service.realized_split(db, user.id, "GOLF")
approx("profit total", s3["profit"], 25.0)
approx("only the NEW win is available", s3["available"], 5.0)
r = swap_service.move_profit_to_wallet(db, user.id, "GOLF", s3["available"], PRICE)
approx("trading USDT debited again", r["trading_balance"], 975.0)
approx("GOLF wallet grew again", r["wallet_balance"], 1000.0 + 5.0 / PRICE)
approx("available back to zero", trading_service.realized_split(db, user.id, "GOLF")["available"], 0.0)

print("\n[10] only the traded coin's profit moves to that coin's wallet")
nova = models.User(email="nova@example.com", password_hash="x", usdt_balance=1000.0)
db.add(nova)
db.commit()
db.refresh(nova)
settled(nova, "GOLF", 30.0)
settled(nova, "NOVA", 70.0)
all_syms = trading_service.realized_split(db, nova.id)
approx("total profit", all_syms["profit"], 100.0)
approx("GOLF profit", all_syms["by_symbol"]["GOLF"]["profit"], 30.0)
approx("NOVA profit", all_syms["by_symbol"]["NOVA"]["profit"], 70.0)
r = swap_service.move_profit_to_wallet(db, nova.id, "GOLF", 30.0, PRICE)
approx("GOLF wallet funded", swap_service.get_wallet_balance(db, nova, "GOLF"), 30.0 / PRICE)
approx("NOVA wallet still empty", swap_service.get_wallet_balance(db, nova, "NOVA"), 0.0)
approx("NOVA profit still available",
       trading_service.realized_split(db, nova.id, "NOVA")["available"], 70.0)
raises("unsupported coin", swap_service.SwapError,
       swap_service.move_profit_to_wallet, db, nova.id, "DOGE2", 10.0, 1.0)
raises("no price", swap_service.SwapError,
       swap_service.move_profit_to_wallet, db, nova.id, "GOLF", 10.0, 0.0)

print("\n[11] a partially spent trading balance moves what is left, never negative")
broke = models.User(email="broke@example.com", password_hash="x", usdt_balance=12.0)
db.add(broke)
db.commit()
db.refresh(broke)
settled(broke, "GOLF", 100.0)
approx("profit is still 100", trading_service.realized_split(db, broke.id, "GOLF")["profit"], 100.0)
r = swap_service.move_profit_to_wallet(db, broke.id, "GOLF", 100.0, PRICE)
approx("only the 12 USDT that is still there moved", r["usd_moved"], 12.0)
approx("trading USDT lands exactly on zero, never below", r["trading_balance"], 0.0)
check("trading USDT is not negative", r["trading_balance"] >= 0, True)
approx("GOLF wallet got 12 USDT of coin", r["wallet_balance"], 12.0 / PRICE)
raises("nothing left to move", swap_service.SwapError,
       swap_service.move_profit_to_wallet, db, broke.id, "GOLF", 100.0, PRICE)
approx("trading USDT stays at zero", swap_service.get_balance(db, broke, "USDT"), 0.0)

print("\n[12] every move is recorded in the WalletTransfer ledger")
rows = db.query(models.WalletTransfer).filter_by(user_id=user.id).all()
check("two profit moves recorded", len(rows), 2)
check("all are PROFIT kind", sorted(r.kind for r in rows), ["PROFIT", "PROFIT"])
check("all credited GOLF", sorted(r.symbol for r in rows), ["GOLF", "GOLF"])
check("every amount is positive", all(float(r.amount) > 0 for r in rows), True)
check("usd values recorded", sorted(float(r.usd_value) for r in rows), [5.0, 20.0])
check("first move wallet-after is 1000 GOLF", float(rows[0].wallet_balance_after), 1000.0)
check("first move trading-after is 980 USDT", float(rows[0].trading_balance_after), 980.0)
check("no ordinary BALANCE rows were created here",
      db.query(models.WalletTransfer).filter(
          models.WalletTransfer.user_id == user.id,
          models.WalletTransfer.kind == "BALANCE").count(), 0)

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
