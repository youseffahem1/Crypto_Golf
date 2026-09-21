"""
Virtual swap between balances only — NEVER creates a real blockchain
transaction. Any listed currency can be converted into any other (USDT,
GOLF, BTC, ETH, SOL, TRX, BNB, XRP, DOGE, ADA, LINK) using the same
server-authoritative USD prices the rest of the app uses, so quoting is
internally consistent everywhere. Rates are always expressed as:
    rate(from, to) = price(from, USD) / price(to, USD)
i.e. how many "to" tokens 1 "from" token is worth.
"""
from sqlalchemy.orm import Session

from . import models, market_service

SUPPORTED_SYMBOLS = market_service.SUPPORTED_SYMBOLS


class SwapError(Exception):
    pass


def _validate_pair(from_symbol: str, to_symbol: str):
    if from_symbol not in SUPPORTED_SYMBOLS or to_symbol not in SUPPORTED_SYMBOLS:
        raise SwapError("Unsupported swap pair")
    if from_symbol == to_symbol:
        raise SwapError("Cannot convert a coin into itself")


def _usd_price(db: Session, symbol: str) -> float:
    price = market_service.get_usd_price(db, symbol)
    if not price or price <= 0:
        raise SwapError(f"Price unavailable for {symbol} right now — try again shortly")
    return price


def get_rate(db: Session, from_symbol: str, to_symbol: str) -> float:
    """How many `to_symbol` one unit of `from_symbol` buys."""
    _validate_pair(from_symbol, to_symbol)
    pf = _usd_price(db, from_symbol)
    pt = _usd_price(db, to_symbol)
    return pf / pt


def get_balance(db: Session, user: models.User, symbol: str) -> float:
    if symbol == "USDT":
        return float(user.usdt_balance or 0.0)
    if symbol == "GOLF":
        return float(user.golf_balance or 0.0)
    row = db.query(models.CoinBalance).filter_by(user_id=user.id, symbol=symbol).first()
    return float(row.balance) if row else 0.0


def set_balance(db: Session, user: models.User, symbol: str, value: float):
    if symbol == "USDT":
        user.usdt_balance = float(value)
        return
    if symbol == "GOLF":
        user.golf_balance = float(value)
        return
    row = db.query(models.CoinBalance).filter_by(user_id=user.id, symbol=symbol).first()
    if row:
        row.balance = float(value)
    else:
        db.add(models.CoinBalance(user_id=user.id, symbol=symbol, balance=float(value)))


def user_balances(db: Session, user_id: str) -> dict:
    """{SYMBOL: balance} for every listed currency, including zero rows."""
    user = db.query(models.User).filter_by(id=user_id).first()
    if not user:
        return {}
    out = {s: 0.0 for s in SUPPORTED_SYMBOLS}
    out["USDT"] = float(user.usdt_balance or 0.0)
    out["GOLF"] = float(user.golf_balance or 0.0)
    for row in db.query(models.CoinBalance).filter_by(user_id=user_id).all():
        if row.symbol in out:
            out[row.symbol] = float(row.balance)
    return out


def execute_swap(db: Session, user_id: str, from_symbol: str, to_symbol: str, from_amount: float) -> models.SwapTx:
    _validate_pair(from_symbol, to_symbol)
    if from_amount <= 0:
        raise SwapError("Amount must be greater than zero")

    try:
        user = db.query(models.User).filter_by(id=user_id).with_for_update().first()
    except Exception:
        db.rollback()
        user = db.query(models.User).filter_by(id=user_id).first()  # SQLite fallback
    if not user:
        raise SwapError("User not found")

    balance = get_balance(db, user, from_symbol)
    if balance < from_amount:
        raise SwapError(f"Insufficient {from_symbol} balance")

    rate = get_rate(db, from_symbol, to_symbol)
    to_amount = from_amount * rate

    set_balance(db, user, from_symbol, balance - from_amount)
    set_balance(db, user, to_symbol, get_balance(db, user, to_symbol) + to_amount)

    tx = models.SwapTx(
        user_id=user_id, from_symbol=from_symbol, to_symbol=to_symbol,
        from_amount=from_amount, to_amount=to_amount, rate=rate,
    )
    db.add(tx)
    db.commit()
    db.refresh(tx)
    return tx