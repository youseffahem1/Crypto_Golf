"""Verify a trading swap credits its destination to the wallet balance shown in cards."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
fd, db_path = tempfile.mkstemp(suffix=".db")
os.close(fd)
os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"

from app import market_service, models, swap_service  # noqa: E402
from app.database import Base, SessionLocal, engine  # noqa: E402

Base.metadata.create_all(bind=engine)
db = SessionLocal()
try:
    user = models.User(email="swap-card@example.com", password_hash="test", usdt_balance=290.0)
    db.add(user)
    db.commit()
    db.refresh(user)

    price = market_service.get_usd_price(db, "GOLF")
    tx = swap_service.execute_trading_swap_to_wallet(db, user.id, "USDT", "GOLF", 290.0)
    trading = swap_service.user_balances(db, user.id)
    wallet = swap_service.user_wallet_balances(db, user.id)
    card_total = trading["GOLF"] + wallet["GOLF"]
    expected = 290.0 / price

    assert tx.from_symbol == "USDT" and tx.to_symbol == "GOLF"
    assert abs(trading["USDT"]) < 1e-8
    assert abs(trading["GOLF"]) < 1e-8
    assert abs(wallet["GOLF"] - expected) < 1e-8
    assert abs(card_total - expected) < 1e-8
    assert db.query(models.WalletSwapTx).filter_by(user_id=user.id).count() == 1
    print(f"PASS: 290 USDT converted to {card_total:.8f} GOLF; GOLF card total matches wallet holdings.")
finally:
    db.close()
    Base.metadata.drop_all(bind=engine)
    engine.dispose()
    try:
        os.remove(db_path)
    except OSError:
        pass
