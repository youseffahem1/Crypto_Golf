"""
VANTA TRADE — Admin controls.

Everything here is behind require_admin, so only users whose accounts have
is_admin=True can see the account list or move balances. Intentionally thin:
monitor every registered account, credit a user's REAL (live-wallet) balance,
and promote/demote admins.

The first admin account is bootstrapped from env vars at startup
(ADMIN_BOOTSTRAP_EMAIL / ADMIN_BOOTSTRAP_PASSWORD, see config.py + the
bootstrap_admin() function in this module).
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import or_, func
from sqlalchemy.orm import Session

from .. import schemas, models, swap_service, market_service, tron_service
from ..database import SessionLocal, get_db
from ..auth import require_admin, hash_password, get_current_user_id
from ..config import ADMIN_BOOTSTRAP_EMAIL, ADMIN_BOOTSTRAP_PASSWORD

router = APIRouter(prefix="/api/admin", tags=["admin"])


def _to_admin_user(u: models.User, db: Session) -> schemas.AdminUserOut:
    return schemas.AdminUserOut(
        id=u.id,
        email=u.email,
        label=u.label,
        is_admin=bool(u.is_admin),
        usdt_balance=float(u.usdt_balance or 0.0),
        golf_balance=float(u.golf_balance or 0.0),
        created_at=u.created_at,
        balances=swap_service.user_balances(db, u.id),
    )


@router.get("/stats", response_model=schemas.AdminStatsOut)
def admin_stats(db: Session = Depends(get_db), _: bool = Depends(require_admin)):
    return schemas.AdminStatsOut(
        users=db.query(models.User).count(),
        admins=db.query(models.User).filter_by(is_admin=True).count(),
        usdt_total=float(db.query(func.coalesce(func.sum(models.User.usdt_balance), 0)).scalar() or 0),
        golf_total=float(db.query(func.coalesce(func.sum(models.User.golf_balance), 0)).scalar() or 0),
        trades=db.query(models.Trade).count(),
    )


@router.get("/users", response_model=list[schemas.AdminUserOut])
def admin_users(
    q: str = "",
    db: Session = Depends(get_db),
    _: bool = Depends(require_admin),
):
    rows = db.query(models.User)
    term = q.strip().lower() if q else ""
    if term:
        rows = rows.filter(
            or_(
                models.User.email.like(f"%{term}%"),
                models.User.label.ilike(f"%{term}%"),
            )
        )
    return [
        _to_admin_user(u, db)
        for u in rows.order_by(models.User.created_at.desc()).all()
    ]


@router.post("/users/credit", response_model=schemas.AdminUserOut)
def admin_credit(
    payload: schemas.AdminCreditRequest,
    db: Session = Depends(get_db),
    _: bool = Depends(require_admin),
):
    """Add a virtual balance to a user's REAL (live) account. Works for any
    supported symbol — USDT/GOLF columns or CoinBalance rows, exactly the
    same ledger swap/trading use, so the credited amount is immediately
    usable in the app."""
    user = None
    if payload.email:
        mail = payload.email.strip().lower()
        user = db.query(models.User).filter_by(email=mail).first()
    elif payload.user_id:
        user = db.query(models.User).filter_by(id=payload.user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    symbol = (payload.symbol or "USDT").strip().upper()
    if symbol not in market_service.SUPPORTED_SYMBOLS:
        raise HTTPException(status_code=400, detail="Unsupported coin for credit")

    current = swap_service.get_balance(db, user, symbol)
    swap_service.set_balance(db, user, symbol, current + payload.amount)
    db.commit()
    db.refresh(user)
    return _to_admin_user(user, db)


@router.post("/users/{user_id}/set-admin", response_model=schemas.AdminUserOut)
def admin_set_role(
    user_id: str,
    payload: schemas.AdminSetAdminRequest,
    db: Session = Depends(get_db),
    _: bool = Depends(require_admin),
):
    user = db.query(models.User).filter_by(id=user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user.is_admin = bool(payload.is_admin)
    db.commit()
    db.refresh(user)
    return _to_admin_user(user, db)


@router.get("/users/{user_id}/activity", response_model=list[schemas.AdminActivityItem])
def admin_activity(
    user_id: str,
    db: Session = Depends(get_db),
    _: bool = Depends(require_admin),
):
    """Every transaction for one user, merged into a single newest-first
    timeline: trades, verified deposits, swaps and internal transfers
    (both sides). Used by the admin panel's per-user activity view."""
    user = db.query(models.User).filter_by(id=user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    items: list[schemas.AdminActivityItem] = []

    for t in db.query(models.Trade).filter_by(user_id=user_id).all():
        items.append(schemas.AdminActivityItem(
            type="TRADE",
            symbol="USDT",
            amount=t.amount,
            direction=str(t.direction.value),
            status=str(t.status.value),
            created_at=t.opened_at,
            meta={"profit": t.profit, "entry": t.entry_price, "exit": t.exit_price},
        ))

    for d in db.query(models.Deposit).filter_by(user_id=user_id).all():
        items.append(schemas.AdminActivityItem(
            type="DEPOSIT",
            symbol=d.token,
            amount=d.amount,
            status=str(d.status.value),
            created_at=d.confirmed_at or d.created_at,
            meta={"tx": d.tx_hash, "network": d.network, "from": d.from_address},
        ))

    for s in db.query(models.SwapTx).filter_by(user_id=user_id).all():
        items.append(schemas.AdminActivityItem(
            type="SWAP",
            symbol=f"{s.from_symbol}→{s.to_symbol}",
            amount=s.from_amount,
            status="COMPLETED",
            created_at=s.created_at,
            meta={"to_amount": s.to_amount, "rate": s.rate},
        ))

    for tr in db.query(models.Transfer).filter(
        or_(models.Transfer.sender_id == user_id, models.Transfer.recipient_id == user_id)
    ).all():
        sent = tr.sender_id == user_id
        counterpart = tr.recipient if sent else tr.sender
        items.append(schemas.AdminActivityItem(
            type="TRANSFER",
            symbol=tr.symbol,
            amount=tr.amount,
            direction="SENT" if sent else "RECEIVED",
            status=str(tr.status.value),
            created_at=tr.created_at,
            meta={"with": counterpart.email if counterpart else "", "note": tr.note},
        ))

    items.sort(key=lambda x: x.created_at, reverse=True)
    return items[:300]


@router.delete("/users/{user_id}")
def admin_delete_user(
    user_id: str,
    db: Session = Depends(get_db),
    _: bool = Depends(require_admin),
    acting_admin_id: str = Depends(get_current_user_id),
):
    """Permanently remove an account and everything attached to it (trades,
    deposits, swaps, transfer/message history involving them, messages,
    blocks and reports). Guards: the acting admin can't delete their own
    account, and the last remaining admin can never be deleted."""
    user = db.query(models.User).filter_by(id=user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.id == acting_admin_id:
        raise HTTPException(status_code=400, detail="You can't delete your own account")
    if user.is_admin and db.query(models.User).filter_by(is_admin=True).count() <= 1:
        raise HTTPException(status_code=400, detail="You can't delete the last admin")

    email = user.email
    uid = user.id
    db.query(models.UserBlock).filter(
        or_(models.UserBlock.blocker_id == uid, models.UserBlock.blocked_id == uid)
    ).delete(synchronize_session=False)
    db.query(models.Report).filter(
        or_(models.Report.reporter_id == uid, models.Report.reported_user_id == uid)
    ).delete(synchronize_session=False)
    db.query(models.DirectMessage).filter(
        or_(models.DirectMessage.sender_id == uid, models.DirectMessage.recipient_id == uid)
    ).delete(synchronize_session=False)
    db.query(models.Transfer).filter(
        or_(models.Transfer.sender_id == uid, models.Transfer.recipient_id == uid)
    ).delete(synchronize_session=False)
    db.query(models.DepositAddress).filter_by(user_id=uid).delete(synchronize_session=False)
    db.query(models.Deposit).filter_by(user_id=uid).delete(synchronize_session=False)
    db.query(models.Trade).filter_by(user_id=uid).delete(synchronize_session=False)
    db.query(models.SwapTx).filter_by(user_id=uid).delete(synchronize_session=False)
    db.query(models.CoinBalance).filter_by(user_id=uid).delete(synchronize_session=False)
    db.query(models.User).filter_by(id=uid).delete(synchronize_session=False)
    db.commit()
    return {"ok": True, "email": email}


def bootstrap_admin():
    """Ensure an admin account exists at startup. The admin identity comes
    ONLY from environment variables (email_admin + password_admin) — never
    hardcoded. If the email is already a user it is promoted, otherwise a new
    admin account is created (with a watch-only Nile deposit address, same as
    signup). When either variable is missing, nothing is bootstrapped."""
    email = (ADMIN_BOOTSTRAP_EMAIL or "").strip().lower()
    password = ADMIN_BOOTSTRAP_PASSWORD or ""
    if not email or not password:
        if email and not password:
            print("[bootstrap_admin] email_admin set but password_admin missing — skipping admin bootstrap")
        return
    db = SessionLocal()
    try:
        user = db.query(models.User).filter_by(email=email).first()
        if user:
            if not user.is_admin:
                user.is_admin = True
                db.commit()
                print(f"[bootstrap_admin] promoted existing user {email} to admin")
            return
        user = models.User(
            email=email,
            password_hash=hash_password(password),
            label="Administrator",
            is_admin=True,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        addr = tron_service.generate_deposit_address()
        db.add(models.DepositAddress(user_id=user.id, address=addr["address"], private_key_hex=addr["private_key_hex"]))
        db.commit()
        print(f"[bootstrap_admin] created admin account {email}")
    finally:
        db.close()