"""
Internal peer-to-peer coin transfers between platform accounts.

Per spec there are NO external transfers in this project: a user can never
send assets to a wallet address outside the platform, and nothing outside
the platform can ever credit a user. The only way coins move between humans
here is sender -> recipient where BOTH are Vanta accounts, moved entirely
inside the balances ledger (the same local tables swap/trading already use).

Security notes:
  - recipient is resolved by exact account email (normalized lowercase),
    never by a user-supplied external address.
  - sender identity comes from the JWT, never from the request body.
  - balances are read/modified through swap_service.get_balance/set_balance
    so USDT/GOLF columns and CoinBalance rows stay consistent everywhere.
"""
from sqlalchemy import or_, and_
from sqlalchemy.orm import Session

from . import models, swap_service, market_service


class TransferError(Exception):
    pass


def find_user_by_email(db: Session, email: str):
    normalized = (email or "").strip().lower()
    if not normalized:
        return None
    return db.query(models.User).filter_by(email=normalized).first()


def search_users(db: Session, query: str, limit: int = 8) -> list:
    """Public lookup for sending coins / starting a chat. Only ever returns
    identity fields (id, email, label) — never balances. Matches email
    prefixes (before the @ is typed too) and label fragments."""
    if not query:
        return []
    term = query.strip().lower()
    q = db.query(models.User).filter(
        or_(
            models.User.email.like(f"%{term}%"),
            models.User.label.ilike(f"%{term}%"),
        )
    )
    rows = q.limit(limit).all()
    return [
        {"id": u.id, "email": u.email, "label": u.label}
        for u in rows
    ]


def _account_full(db: Session, user_id: str) -> models.User:
    user = db.query(models.User).filter_by(id=user_id).first()
    if not user:
        raise TransferError("Your account could not be found")
    return user


def execute_transfer(
    db: Session,
    sender_id: str,
    recipient_email: str,
    symbol: str,
    amount: float,
    note: str = None,
) -> models.Transfer:
    symbol = (symbol or "").upper()
    if symbol not in market_service.SUPPORTED_SYMBOLS:
        raise TransferError("Unsupported coin for transfer")

    if amount <= 0:
        raise TransferError("Amount must be greater than zero")

    sender = _account_full(db, sender_id)
    recipient = find_user_by_email(db, recipient_email)
    if not recipient or recipient.id == sender.id:
        raise TransferError("Recipient account not found")

    # Blocked users may not send each other coins.
    blocked = db.query(models.UserBlock).filter_by(
        blocker_id=sender.id, blocked_id=recipient.id
    ).first()
    if blocked:
        raise TransferError("You have blocked this user — unblock them before sending")

    balance = swap_service.get_balance(db, sender, symbol)
    if balance < amount:
        raise TransferError(f"Insufficient {symbol} balance")

    swap_service.set_balance(db, sender, symbol, balance - amount)
    swap_service.set_balance(db, recipient, symbol, swap_service.get_balance(db, recipient, symbol) + amount)

    tx = models.Transfer(
        sender_id=sender.id,
        recipient_id=recipient.id,
        symbol=symbol,
        amount=amount,
        note=(note or "").strip() or None,
        status=models.TransferStatus.COMPLETED,
    )
    db.add(tx)
    db.commit()
    db.refresh(tx)
    return tx


def transfer_history(db: Session, user_id: str) -> dict:
    user = db.query(models.User).filter_by(id=user_id).first()
    if not user:
        raise TransferError("Your account could not be found")

    sent = (
        db.query(models.Transfer).filter_by(sender_id=user_id)
        .order_by(models.Transfer.created_at.desc()).limit(50).all()
    )
    received = (
        db.query(models.Transfer).filter_by(recipient_id=user_id)
        .order_by(models.Transfer.created_at.desc()).limit(50).all()
    )

    return {
        "sent": [_to_out(t, t.recipient.email) for t in sent],
        "received": [_to_out(t, t.sender.email) for t in received],
    }


def _to_out(t: models.Transfer, other_email: str) -> dict:
    return {
        "id": t.id,
        "symbol": t.symbol,
        "amount": t.amount,
        "note": t.note,
        "status": t.status.value if hasattr(t.status, "value") else t.status,
        "created_at": t.created_at,
        "other_email": other_email,
    }