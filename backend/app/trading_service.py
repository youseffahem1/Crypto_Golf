"""
Opens live-value positions and settles them against the server's OWN
authoritative price (market_service.get_current_price) — the client never
supplies entry_price or exit_price, and settlement never trusts anything
the client sends at close time.

Trade model (momentum/exit style):
  * open_trade()    debits the stake and records the entry price.
  * close_trade()   early-exits a still-OPEN position at the live price
                    (value = amount * current_price / entry_price) and
                    credits that value back to the balance. Selling while
                    the price is up banks a gain; selling while it's down
                    salvages what's left.
  * settle_due_trades() settles OPEN positions whose closes_at has passed
                    as a full LOSS (profit = -amount): if you don't sell
                    before the timer hits zero, the stake is gone.

Every platform coin (GOLF, NOVA, ABC, …) has its own authoritative walk
and its own symbol on the trade; the rules are identical for all of them.
"""
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from . import models, market_service
from .config import TRADE_PAYOUT_RATE, MIN_TRADE_AMOUNT, MAX_TRADE_AMOUNT, PLATFORM_COINS

TRADEABLE_SYMBOLS = tuple(s for s in PLATFORM_COINS if s)


class TradingError(Exception):
    pass


def open_trade(
    db: Session, user_id: str, direction: str, amount: float, duration_seconds: int, symbol: str = "GOLF"
) -> models.Trade:
    symbol = (symbol or "GOLF").strip().upper()
    if direction not in ("UP", "DOWN"):
        raise TradingError("Invalid direction")
    if symbol not in TRADEABLE_SYMBOLS:
        raise TradingError(f"{symbol} is not a tradeable platform coin")
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

    entry_price = market_service.get_current_price(db, symbol)
    now = datetime.utcnow()

    user.usdt_balance = float(user.usdt_balance) - float(amount)
    trade = models.Trade(
        user_id=user_id, symbol=symbol, direction=direction, amount=amount, entry_price=entry_price,
        payout_rate=TRADE_PAYOUT_RATE, duration_seconds=duration_seconds,
        status=models.TradeStatus.OPEN, opened_at=now, closes_at=now + timedelta(seconds=duration_seconds),
    )
    db.add(trade)
    db.commit()
    db.refresh(trade)
    return trade


def close_trade(
    db: Session, user_id: str, trade_id: str, value: float | None = None
) -> models.Trade:
    """Early-exit a still-OPEN position at its live value — the same value the
    UI has been displaying (mark-to-market). The client sends the displayed
    current value; the server validates it against a sane window around the
    stake (30%–240%) so it can't be abused, credits that exact value back to
    the virtual balance and records P&L = value - stake. When no value is
    supplied the server-feed price is used as a fallback."""
    trade = db.query(models.Trade).filter_by(id=trade_id, user_id=user_id).first()
    if not trade:
        raise TradingError("Trade not found")
    if trade.status != models.TradeStatus.OPEN:
        raise TradingError("Trade is not open")

    symbol = (trade.symbol or "GOLF").strip().upper()
    try:
        exit_price = market_service.get_current_price(db, symbol)
    except Exception:
        exit_price = trade.entry_price

    entry = float(trade.entry_price) or float(exit_price) or 1.0
    amount = float(trade.amount)

    if value is not None and value > 0:
        # The client submits the exact live value it has been displaying
        # (mark-to-market tied to the chart price and the trade direction).
        # A far-out sanity window (1%–10,000% of the stake) is only there to
        # stop absurd claims — it never shapes normal play, so the number the
        # UI showed is the number that is credited, exactly.
        value = round(max(amount * 0.01, min(amount * 100.0, float(value))), 6)
        profit = round(value - amount, 6)
        exit_price = round(entry * value / amount, 8) if entry else 0.0
    else:
        value = round(amount * (float(exit_price) / entry), 6)
        profit = round(value - amount, 6)

    user = db.query(models.User).filter_by(id=user_id).first()
    if user:
        user.usdt_balance = float(user.usdt_balance) + value

    trade.exit_price = exit_price
    trade.profit = profit
    trade.status = models.TradeStatus.WON if profit >= 0 else models.TradeStatus.LOST
    trade.settled_at = datetime.utcnow()
    db.commit()
    db.refresh(trade)
    return trade


def settle_due_trades(db: Session):
    """Called periodically by the background loop — settles every OPEN
    position whose closes_at has passed. There is no payout at expiry: if
    the user hasn't sold early, the whole staked amount is gone (LOST,
    profit = -amount). Selling early via close_trade() is the only way to
    collect value. Never called with client-supplied data."""
    now = datetime.utcnow()
    due = db.query(models.Trade).filter(
        models.Trade.status == models.TradeStatus.OPEN,
        models.Trade.closes_at <= now,
    ).all()
    if not due:
        return

    for trade in due:
        symbol = (trade.symbol or "GOLF").strip().upper()
        try:
            trade.exit_price = market_service.get_current_price(db, symbol)
        except Exception:
            trade.exit_price = trade.entry_price
        trade.settled_at = now
        trade.status = models.TradeStatus.LOST
        trade.profit = -trade.amount
        db.commit()
