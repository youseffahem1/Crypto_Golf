"""
Opens and settles UP/DOWN binary trades against the server's OWN
authoritative price (market_service.get_current_price) — the client never
supplies entry_price or exit_price, and settlement never trusts anything
the client sends at close time. This is the piece that makes
"UP + price rises = WIN" an enforced rule instead of a suggestion.
"""
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from . import models, market_service
from .config import TRADE_PAYOUT_RATE, MIN_TRADE_AMOUNT, MAX_TRADE_AMOUNT


class TradingError(Exception):
    pass


def open_trade(db: Session, user_id: str, direction: str, amount: float, duration_seconds: int) -> models.Trade:
    if direction not in ("UP", "DOWN"):
        raise TradingError("Invalid direction")
    if amount < MIN_TRADE_AMOUNT or amount > MAX_TRADE_AMOUNT:
        raise TradingError(f"Amount must be between {MIN_TRADE_AMOUNT} and {MAX_TRADE_AMOUNT}")
    if duration_seconds not in (60, 300, 900, 1800):
        raise TradingError("Invalid duration")

    try:
        user = db.query(models.User).filter_by(id=user_id).with_for_update().first()
    except Exception:
        db.rollback()
        user = db.query(models.User).filter_by(id=user_id).first()  # SQLite fallback

    if not user:
        raise TradingError("User not found")
    if user.usdt_balance < amount:
        raise TradingError("Insufficient virtual USDT balance")

    entry_price = market_service.get_current_price(db)
    now = datetime.utcnow()

    user.usdt_balance = float(user.usdt_balance) - float(amount)
    trade = models.Trade(
        user_id=user_id, direction=direction, amount=amount, entry_price=entry_price,
        payout_rate=TRADE_PAYOUT_RATE, duration_seconds=duration_seconds,
        status=models.TradeStatus.OPEN, opened_at=now, closes_at=now + timedelta(seconds=duration_seconds),
    )
    db.add(trade)
    db.commit()
    db.refresh(trade)
    return trade


def settle_due_trades(db: Session):
    """Called periodically by the background loop — settles every OPEN
    trade whose closes_at has passed, using the server's own current price
    as the exit price. Never called with client-supplied data."""
    now = datetime.utcnow()
    due = db.query(models.Trade).filter(
        models.Trade.status == models.TradeStatus.OPEN,
        models.Trade.closes_at <= now,
    ).all()
    if not due:
        return

    exit_price = market_service.get_current_price(db)
    for trade in due:
        won = (
            (trade.direction == models.TradeDirection.UP and exit_price > trade.entry_price)
            or (trade.direction == models.TradeDirection.DOWN and exit_price < trade.entry_price)
        )
        trade.exit_price = exit_price
        trade.settled_at = now

        user = db.query(models.User).filter_by(id=trade.user_id).first()
        if won:
            profit = trade.amount * trade.payout_rate
            trade.status = models.TradeStatus.WON
            trade.profit = profit
            if user:
                user.usdt_balance = float(user.usdt_balance) + float(trade.amount) + float(profit)
        else:
            trade.status = models.TradeStatus.LOST
            trade.profit = -trade.amount
        db.commit()
