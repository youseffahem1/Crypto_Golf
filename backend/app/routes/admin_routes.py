"""
VANTA TRADE — Admin controls.

Everything here is behind require_admin, so only users whose accounts have
is_admin=True can see the account list, the transaction feed or move
balances. Every number the frontend shows is computed from PostgreSQL here —
there are no mock arrays anywhere on the admin side.

Capabilities:
  - GET  /api/admin/stats                        real aggregate statistics
  - GET  /api/admin/users                        paginated, searchable, sortable list
  - GET  /api/admin/users/{user_id}              full single-user detail
  - POST /api/admin/users/{user_id}/balance      decimal-safe, atomic balance credit
  - POST /api/admin/users/{user_id}/set-admin    promote / demote an admin
  - GET  /api/admin/users/{user_id}/transactions full activity for one user
  - GET  /api/admin/transactions                 merged, filterable, paginated feed
  - GET  /api/admin/users/{user_id}/activity     (legacy timeline alias)
  - DELETE /api/admin/users/{user_id}            permanent removal with FK-safe cleanup

The first admin account is bootstrapped from env vars at startup
(email_admin + password_admin, see config.py).
"""
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

from fastapi import APIRouter, Depends, HTTPException, Query
import sqlalchemy
from sqlalchemy import or_, func
from sqlalchemy.orm import Session

from .. import schemas, models, swap_service, market_service, tron_service
from ..database import SessionLocal, get_db
from ..auth import require_admin, hash_password, get_current_user_id
from ..config import ADMIN_BOOTSTRAP_EMAIL, ADMIN_BOOTSTRAP_PASSWORD

router = APIRouter(prefix="/api/admin", tags=["admin"])

_MONEY_QUANT = Decimal("0.00000001")
_MAX_ADJUST = Decimal("1000000000000")  # 1e12 hard cap per adjustment


# =============================================================================
# Small helpers
# =============================================================================

def _money(value):
    """Safe float -> Decimal with the shared 8-dp money precision."""
    try:
        return Decimal(str(value)).quantize(_MONEY_QUANT, rounding=ROUND_HALF_UP)
    except Exception:
        return Decimal("0")


def _symbol_list(balances: dict) -> list[str]:
    out = set(market_service.SUPPORTED_SYMBOLS)
    out.update((balances or {}).keys())
    return sorted(out, key=lambda s: market_service.SUPPORTED_SYMBOLS.index(s) if s in market_service.SUPPORTED_SYMBOLS else 999)


def _balances_map(db: Session, user_ids: list[str]) -> dict:
    """{user_id: {SYMBOL: balance}} for every listed coin, one DB query."""
    ids = list(dict.fromkeys(user_ids))
    out: dict = {}
    balances: dict = {i: {} for i in ids}
    usdt = {i: {} for i in ids}
    if ids:
        for uid, val in db.query(models.User.id, models.User.usdt_balance).filter(models.User.id.in_(ids)).all():
            usdt[uid] = {"USDT": float(val or 0.0)}
        for uid, val in db.query(models.User.id, models.User.golf_balance).filter(models.User.id.in_(ids)).all():
            usdt[uid]["GOLF"] = float(val or 0.0)
        for uid, sym, val in db.query(
            models.CoinBalance.user_id, models.CoinBalance.symbol, models.CoinBalance.balance
        ).filter(models.CoinBalance.user_id.in_(ids)).all():
            balances[uid][sym] = float(val or 0.0)
    for uid in out.keys():
        pass
    for uid in ids:
        joined = dict(usdt[uid])
        for sym, val in balances[uid].items():
            joined[sym] = val
        out[uid] = joined
    return out


def _counters_map(db: Session, user_ids: list[str]) -> dict:
    """{user_id: {trades, deposits, last}} built with grouped queries for a
    page of users (avoids N+1 at the cost of 5 grouped SELECTs)."""
    ids = list(dict.fromkeys(user_ids))
    base: dict = {i: {"trades": 0, "deposits": 0, "last": None} for i in ids}
    if not ids:
        return base
    for uid, n in db.query(models.Trade.user_id, func.count(models.Trade.id)).filter(
        models.Trade.user_id.in_(ids)
    ).group_by(models.Trade.user_id).all():
        base[uid]["trades"] = n
    for uid, n in db.query(models.Deposit.user_id, func.count(models.Deposit.id)).filter(
        models.Deposit.user_id.in_(ids)
    ).group_by(models.Deposit.user_id).all():
        base[uid]["deposits"] = n
    latest: dict = {}
    for uid, ts in db.query(models.Trade.user_id, func.max(models.Trade.opened_at)).filter(
        models.Trade.user_id.in_(ids)
    ).group_by(models.Trade.user_id).all():
        latest[uid] = ts
    for uid, ts in db.query(models.Deposit.user_id, func.max(models.Deposit.created_at)).filter(
        models.Deposit.user_id.in_(ids)
    ).group_by(models.Deposit.user_id).all():
        latest[uid] = max(latest.get(uid) or ts, ts) if latest.get(uid) else ts
    for uid, ts in db.query(models.SwapTx.user_id, func.max(models.SwapTx.created_at)).filter(
        models.SwapTx.user_id.in_(ids)
    ).group_by(models.SwapTx.user_id).all():
        latest[uid] = max(latest.get(uid) or ts, ts) if latest.get(uid) else ts
    for uid, ts in db.query(models.BalanceTransaction.user_id, func.max(models.BalanceTransaction.created_at)).filter(
        models.BalanceTransaction.user_id.in_(ids)
    ).group_by(models.BalanceTransaction.user_id).all():
        latest[uid] = max(latest.get(uid) or ts, ts) if latest.get(uid) else ts
    for uid, ts in db.query(models.Transfer.sender_id, func.max(models.Transfer.created_at)).filter(
        models.Transfer.sender_id.in_(ids)
    ).group_by(models.Transfer.sender_id).all():
        latest[uid] = max(latest.get(uid) or ts, ts) if latest.get(uid) else ts
    for uid, ts in db.query(models.Transfer.recipient_id, func.max(models.Transfer.created_at)).filter(
        models.Transfer.recipient_id.in_(ids)
    ).group_by(models.Transfer.recipient_id).all():
        latest[uid] = max(latest.get(uid) or ts, ts) if latest.get(uid) else ts
    for uid in ids:
        if latest.get(uid):
            base[uid]["last"] = latest[uid]
    return base


def _user_email_map(db: Session, user_ids: list[str]) -> dict:
    ids = list(dict.fromkeys(user_ids))
    if not ids:
        return {}
    return {
        uid: email
        for uid, email in db.query(models.User.id, models.User.email)
        .filter(models.User.id.in_(ids)).all()
    }


# =============================================================================
# Statistics
# =============================================================================

@router.get("/stats", response_model=schemas.AdminStatsOut)
def admin_stats(db: Session = Depends(get_db), _: bool = Depends(require_admin)):
    users = db.query(models.User).count()
    admins = db.query(models.User).filter_by(is_admin=True).count()
    usdt_total = float(db.query(func.coalesce(func.sum(models.User.usdt_balance), 0)).scalar() or 0)
    golf_total = float(db.query(func.coalesce(func.sum(models.User.golf_balance), 0)).scalar() or 0)

    coin_totals = {
        sym: float(val or 0.0)
        for sym, val in db.query(models.CoinBalance.symbol, func.sum(models.CoinBalance.balance))
        .group_by(models.CoinBalance.symbol).all()
    }
    total_balance_usd = usdt_total
    for sym, val in coin_totals.items():
        price = market_service.get_usd_price(db, sym)
        total_balance_usd += val * (price or 0.0)
    total_balance_usd += golf_total * (market_service.get_usd_price(db, "GOLF") or 0.0)

    deposits_total = db.query(models.Deposit).filter_by(status=models.DepositStatus.CONFIRMED).count()
    deposit_amount = float(
        db.query(func.coalesce(func.sum(models.Deposit.amount), 0))
        .filter(models.Deposit.status == models.DepositStatus.CONFIRMED).scalar() or 0
    )
    trades = db.query(models.Trade).count()
    swaps = db.query(models.SwapTx).count()
    transfers = db.query(models.Transfer).count()
    adjustments = db.query(models.BalanceTransaction).count()
    transactions = trades + deposits_total + swaps + transfers + adjustments

    week_ago = datetime.utcnow() - timedelta(days=7)
    new_users_7d = db.query(models.User).filter(models.User.created_at >= week_ago).count()

    recent = _build_transactions(db, limit=8)
    emails = _user_email_map(db, [i.user_id for i in recent])
    for it in recent:
        it.user_email = emails.get(it.user_id, "—")

    return schemas.AdminStatsOut(
        users=users,
        admins=admins,
        usdt_total=usdt_total,
        golf_total=golf_total,
        trades=trades,
        deposits=deposits_total,
        deposit_total=deposit_amount,
        balance_adjustments=adjustments,
        transactions=transactions,
        new_users_7d=new_users_7d,
        total_balance_usd=round(total_balance_usd, 2),
        recent_transactions=recent,
    )


# =============================================================================
# Users list / detail
# =============================================================================

@router.get("/users", response_model=schemas.AdminUserPageOut)
def admin_users(
    q: str = "",
    role: str = Query("all", pattern="^(all|admin|user)$"),
    sort: str = Query("created_at", pattern="^(created_at|email|label|usdt_balance|golf_balance)$"),
    order: str = Query("desc", pattern="^(asc|desc)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=200),
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
    if role == "admin":
        rows = rows.filter(models.User.is_admin.is_(True))
    elif role == "user":
        rows = rows.filter(models.User.is_admin.is_(False))

    col = getattr(models.User, sort, models.User.created_at)
    rows = rows.order_by(col.desc() if order == "desc" else col.asc())

    total = rows.count()
    paged = rows.offset((page - 1) * page_size).limit(page_size).all()
    ids = [u.id for u in paged]
    balances = _balances_map(db, ids)
    counters = _counters_map(db, ids)

    items = []
    for u in paged:
        items.append(schemas.AdminUserListItem(
            id=u.id,
            email=u.email,
            label=u.label,
            is_admin=bool(u.is_admin),
            usdt_balance=float(u.usdt_balance or 0.0),
            golf_balance=float(u.golf_balance or 0.0),
            created_at=u.created_at,
            balances=balances.get(u.id) or {},
            trades=counters.get(u.id, {}).get("trades", 0),
            deposits=counters.get(u.id, {}).get("deposits", 0),
            last_activity=counters.get(u.id, {}).get("last"),
        ))

    return schemas.AdminUserPageOut(items=items, total=total, page=page, page_size=page_size)


@router.get("/users/{user_id}", response_model=schemas.AdminUserDetailOut)
def admin_user_detail(
    user_id: str,
    db: Session = Depends(get_db),
    _: bool = Depends(require_admin),
):
    user = db.query(models.User).filter_by(id=user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    balances = swap_service.user_balances(db, user.id)
    counters = _counters_map(db, [user.id])[user.id]
    depos = db.query(models.DepositAddress).filter_by(user_id=user.id).first()

    usd_total = 0.0
    for sym, val in balances.items():
        price = market_service.get_usd_price(db, sym)
        usd_total += val * (price or 0.0)

    return schemas.AdminUserDetailOut(
        id=user.id,
        email=user.email,
        label=user.label,
        is_admin=bool(user.is_admin),
        usdt_balance=float(user.usdt_balance or 0.0),
        golf_balance=float(user.golf_balance or 0.0),
        created_at=user.created_at,
        balances=balances,
        trades=counters["trades"],
        deposits=counters["deposits"],
        last_activity=counters["last"],
        swaps=db.query(models.SwapTx).filter_by(user_id=user.id).count(),
        transfers_sent=db.query(models.Transfer).filter_by(sender_id=user.id).count(),
        transfers_received=db.query(models.Transfer).filter_by(recipient_id=user.id).count(),
        balance_transactions=db.query(models.BalanceTransaction).filter_by(user_id=user.id).count(),
        messages_sent=db.query(models.DirectMessage).filter_by(sender_id=user.id).count(),
        messages_received=db.query(models.DirectMessage).filter_by(recipient_id=user.id).count(),
        deposit_address=depos.address if depos else None,
        usd_total=round(usd_total, 2),
    )


# =============================================================================
# Balance management (decimal-safe, atomic)
# =============================================================================

def _apply_balance_change(
    db: Session,
    user: models.User,
    symbol: str,
    amount: Decimal,
    tx_type: str = "ADMIN_CREDIT",
    note: str = None,
) -> models.BalanceTransaction:
    """Credit/debit a user's live balance AND write the audit ledger row in a
    single database transaction. On any failure both changes are rolled back —
    a balance can never be updated without its ledger record."""
    symbol = (symbol or "").strip().upper()
    if symbol not in market_service.SUPPORTED_SYMBOLS:
        raise HTTPException(status_code=400, detail="Unsupported coin")
    if not isinstance(amount, Decimal):
        try:
            amount = Decimal(str(amount))
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid amount")
    try:
        amount = amount.quantize(_MONEY_QUANT, rounding=ROUND_HALF_UP)
    except Exception:
        raise HTTPException(status_code=400, detail="Amount is too large or malformed")
    if not amount.is_finite() or amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be greater than zero")
    if amount > _MAX_ADJUST:
        raise HTTPException(status_code=400, detail="Amount is too large")

    # Read the current balance as an exact Decimal (column is float in the
    # legacy schema; str() conversion keeps full decimal fidelity).
    current = _money(swap_service.get_balance(db, user, symbol))
    delta = amount if tx_type == "ADMIN_CREDIT" else -amount
    new_balance = max(Decimal("0"), (current + delta).quantize(_MONEY_QUANT, rounding=ROUND_HALF_UP))

    try:
        swap_service.set_balance(db, user, symbol, float(new_balance))
        ledger = models.BalanceTransaction(
            user_id=user.id,
            symbol=symbol,
            tx_type=tx_type,
            amount=amount.quantize(_MONEY_QUANT, rounding=ROUND_HALF_UP),
            balance_after=new_balance,
            note=(note or "").strip()[:200] or None,
        )
        db.add(ledger)
        db.commit()
        db.refresh(ledger)
        return ledger
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Could not update balance: {e}")


@router.post("/users/{user_id}/balance", response_model=schemas.BalanceAddOut)
def admin_add_balance(
    user_id: str,
    payload: schemas.BalanceAddRequest,
    db: Session = Depends(get_db),
    _: bool = Depends(require_admin),
):
    """The primary admin credit endpoint. Validates a Decimal amount (rejects
    0 / negatives / non-numeric / absurdly large), applies it atomically with
    a ledger row, and returns the new live balances."""
    user = db.query(models.User).filter_by(id=user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    raw = payload.amount
    try:
        amount = (raw if isinstance(raw, Decimal) else Decimal(str(raw))).quantize(
            _MONEY_QUANT, rounding=ROUND_HALF_UP
        )
    except Exception:
        raise HTTPException(status_code=400, detail="Amount is too large or malformed")
    if not amount.is_finite() or amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be greater than zero")

    ledger = _apply_balance_change(db, user, payload.symbol, amount, tx_type="ADMIN_CREDIT")

    user = db.query(models.User).filter_by(id=user_id).first()
    balances = swap_service.user_balances(db, user.id)
    counters = _counters_map(db, [user.id])[user.id]
    depos = db.query(models.DepositAddress).filter_by(user_id=user.id).first()
    usd_total = 0.0
    for sym, val in balances.items():
        price = market_service.get_usd_price(db, sym)
        usd_total += val * (price or 0.0)
    detail = schemas.AdminUserDetailOut(
        id=user.id, email=user.email, label=user.label, is_admin=bool(user.is_admin),
        usdt_balance=float(user.usdt_balance or 0.0), golf_balance=float(user.golf_balance or 0.0),
        created_at=user.created_at, balances=balances,
        trades=counters["trades"], deposits=counters["deposits"], last_activity=counters["last"],
        swaps=db.query(models.SwapTx).filter_by(user_id=user.id).count(),
        transfers_sent=db.query(models.Transfer).filter_by(sender_id=user.id).count(),
        transfers_received=db.query(models.Transfer).filter_by(recipient_id=user.id).count(),
        balance_transactions=db.query(models.BalanceTransaction).filter_by(user_id=user.id).count(),
        messages_sent=db.query(models.DirectMessage).filter_by(sender_id=user.id).count(),
        messages_received=db.query(models.DirectMessage).filter_by(recipient_id=user.id).count(),
        deposit_address=depos.address if depos else None,
        usd_total=round(usd_total, 2),
    )
    return schemas.BalanceAddOut(
        tx_id=ledger.id,
        symbol=(payload.symbol or "USDT").strip().upper(),
        amount=str(amount),
        balance_after=detail.balances,
        user=detail,
    )


@router.post("/users/credit", response_model=schemas.AdminUserOut)
def admin_credit(
    payload: schemas.AdminCreditRequest,
    db: Session = Depends(get_db),
    _: bool = Depends(require_admin),
):
    """Backward-compatible alias for the old credit endpoint. The frontend now
    uses POST /users/{user_id}/balance, but this keeps any existing callers
    working."""
    user = None
    if payload.email:
        user = db.query(models.User).filter_by(email=payload.email.strip().lower()).first()
    elif payload.user_id:
        user = db.query(models.User).filter_by(id=payload.user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    amount = _money(payload.amount)
    if amount <= 0 or amount > _MAX_ADJUST:
        raise HTTPException(status_code=400, detail="Amount must be greater than zero")
    _apply_balance_change(db, user, payload.symbol, amount, tx_type="ADMIN_CREDIT")
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
    if not payload.is_admin and user.is_admin:
        admins_left = db.query(models.User).filter_by(is_admin=True).count()
        if admins_left <= 1:
            raise HTTPException(status_code=400, detail="You can't demote the last admin")
    user.is_admin = bool(payload.is_admin)
    db.commit()
    db.refresh(user)
    return _to_admin_user(user, db)


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


# =============================================================================
# Merged transaction feed
# =============================================================================

def _build_transactions(
    db: Session,
    user_id: str = None,
    tx_type: str = None,
    status: str = None,
    q: str = None,
    date_from: datetime = None,
    date_to: datetime = None,
    limit: int = None,
) -> list[schemas.AdminTransactionItem]:
    """Flattens every money-moving table (Trade, Deposit, SwapTx, Transfer,
    BalanceTransaction) into one shape. Returns newest-first by default; the
    caller applies its own sorting/pagination when it needs paging."""
    items: list[schemas.AdminTransactionItem] = []

    # -- TRADE -----------------------------------------------------------------
    tq = db.query(models.Trade)
    if user_id:
        tq = tq.filter(models.Trade.user_id == user_id)
    if status:
        tq = tq.filter(models.Trade.status == status)
    if date_from:
        tq = tq.filter(models.Trade.opened_at >= date_from)
    if date_to:
        tq = tq.filter(models.Trade.opened_at <= date_to)
    for t in tq.order_by(models.Trade.opened_at.desc()).all():
        items.append(schemas.AdminTransactionItem(
            id=t.id, user_id=t.user_id,
            type="TRADE", symbol="USDT", amount=t.amount,
            direction=t.direction.value if hasattr(t.direction, "value") else t.direction,
            status=t.status.value if hasattr(t.status, "value") else t.status,
            created_at=t.opened_at,
            note=f"entry {t.entry_price}",
            meta={"entry": t.entry_price, "exit": t.exit_price, "profit": t.profit,
                  "duration": t.duration_seconds},
        ))

    # -- DEPOSIT ----------------------------------------------------------------
    dq = db.query(models.Deposit)
    if user_id:
        dq = dq.filter(models.Deposit.user_id == user_id)
    if status:
        dq = dq.filter(models.Deposit.status == status)
    if date_from:
        dq = dq.filter(models.Deposit.created_at >= date_from)
    if date_to:
        dq = dq.filter(models.Deposit.created_at <= date_to)
    for d in dq.order_by(models.Deposit.created_at.desc()).all():
        items.append(schemas.AdminTransactionItem(
            id=d.id, user_id=d.user_id,
            type="DEPOSIT", symbol=d.token, amount=d.amount,
            direction=None,
            status=d.status.value if hasattr(d.status, "value") else d.status,
            created_at=d.confirmed_at or d.created_at,
            note=d.tx_hash,
            meta={"tx": d.tx_hash, "network": d.network, "from": d.from_address, "confirmations": d.confirmations},
        ))

    # -- SWAP -------------------------------------------------------------------
    sq = db.query(models.SwapTx)
    if user_id:
        sq = sq.filter(models.SwapTx.user_id == user_id)
    if date_from:
        sq = sq.filter(models.SwapTx.created_at >= date_from)
    if date_to:
        sq = sq.filter(models.SwapTx.created_at <= date_to)
    if status:
        sq = sq.filter(sqlalchemy.false())  # swaps only ever have status COMPLETED
    for s in sq.order_by(models.SwapTx.created_at.desc()).all():
        items.append(schemas.AdminTransactionItem(
            id=s.id, user_id=s.user_id,
            type="SWAP", symbol=f"{s.from_symbol}→{s.to_symbol}", amount=s.from_amount,
            direction=None, status="COMPLETED", created_at=s.created_at,
            note=f"{s.from_amount} {s.from_symbol} → {s.to_amount} {s.to_symbol}",
            meta={"from": s.from_symbol, "to": s.to_symbol,
                  "from_amount": s.from_amount, "to_amount": s.to_amount, "rate": s.rate},
        ))

    # -- TRANSFER ----------------------------------------------------------------
    mq = db.query(models.Transfer)
    if user_id:
        mq = mq.filter(or_(models.Transfer.sender_id == user_id, models.Transfer.recipient_id == user_id))
    if status:
        mq = mq.filter(models.Transfer.status == status)
    if date_from:
        mq = mq.filter(models.Transfer.created_at >= date_from)
    if date_to:
        mq = mq.filter(models.Transfer.created_at <= date_to)
    for tr in mq.order_by(models.Transfer.created_at.desc()).all():
        sent = tr.sender_id == user_id if user_id else None
        counterpart = tr.recipient if sent else tr.sender
        items.append(schemas.AdminTransactionItem(
            id=tr.id, user_id=tr.sender_id,
            type="TRANSFER", symbol=tr.symbol, amount=tr.amount,
            direction=("SENT" if sent else "RECEIVED") if sent is not None else None,
            status=tr.status.value if hasattr(tr.status, "value") else tr.status,
            created_at=tr.created_at,
            note=tr.note,
            meta={"sender": tr.sender_id, "recipient": tr.recipient_id,
                  "with": counterpart.email if counterpart else ""},
        ))

    # -- BALANCE TRANSACTION (admin adjustments) ---------------------------------
    bq = db.query(models.BalanceTransaction)
    if user_id:
        bq = bq.filter(models.BalanceTransaction.user_id == user_id)
    if status:
        bq = bq.filter(sqlalchemy.false())  # ledger rows only ever have status COMPLETED
    if date_from:
        bq = bq.filter(models.BalanceTransaction.created_at >= date_from)
    if date_to:
        bq = bq.filter(models.BalanceTransaction.created_at <= date_to)
    for b in bq.order_by(models.BalanceTransaction.created_at.desc()).all():
        items.append(schemas.AdminTransactionItem(
            id=b.id, user_id=b.user_id,
            type="CREDIT", symbol=b.symbol, amount=float(b.amount or 0),
            direction="IN", status="COMPLETED", created_at=b.created_at,
            note=b.note or "Admin balance credit",
            meta={"tx_type": b.tx_type, "balance_after": float(b.balance_after or 0)},
        ))

    if tx_type:
        items = [i for i in items if i.type == tx_type.upper()]
    if q:
        needle = q.strip().lower()
        uids = {
            uid for uid, email in _user_email_map(db, [i.user_id for i in items]).items()
            if needle in email.lower()
        }
        items = [i for i in items if i.user_id in uids]

    items.sort(key=lambda i: i.created_at, reverse=True)
    if limit is not None:
        items = items[:limit]
    return items


@router.get("/transactions", response_model=schemas.AdminTransactionPageOut)
def admin_transactions(
    q: str = "",
    type: str = Query("", description="TRADE | DEPOSIT | SWAP | TRANSFER | CREDIT"),
    status: str = "",
    from_date: str = Query("", description="ISO date/datetime"),
    to_date: str = Query("", description="ISO date/datetime"),
    user_id: str = "",
    sort: str = Query("newest", pattern="^(newest|oldest)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=200),
    db: Session = Depends(get_db),
    _: bool = Depends(require_admin),
):
    def _parse_dt(value: str) -> datetime | None:
        if not value:
            return None
        try:
            v = value.strip()
            if len(v) == 10:
                return datetime.strptime(v, "%Y-%m-%d")
            return datetime.fromisoformat(v.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            raise HTTPException(status_code=422, detail=f"Invalid date: {value}")

    date_f = _parse_dt(from_date)
    date_t = _parse_dt(to_date)

    items = _build_transactions(
        db, user_id=user_id or None, tx_type=type or None, status=status or None,
        q=q, date_from=date_f, date_to=date_t,
    )

    total = len(items)
    if sort == "oldest":
        items = sorted(items, key=lambda i: i.created_at)
    else:
        items = sorted(items, key=lambda i: i.created_at, reverse=True)

    users = _user_email_map(db, [i.user_id for i in items])
    start = (page - 1) * page_size
    paged = items[start:start + page_size]
    for it in paged:
        it.user_email = users.get(it.user_id, "—")

    return schemas.AdminTransactionPageOut(items=paged, total=total, page=page, page_size=page_size)


@router.get("/users/{user_id}/transactions", response_model=schemas.AdminTransactionPageOut)
def admin_user_transactions(
    user_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    _: bool = Depends(require_admin),
):
    user = db.query(models.User).filter_by(id=user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    items = _build_transactions(db, user_id=user_id)
    total = len(items)
    start = (page - 1) * page_size
    paged = items[start:start + page_size]
    for it in paged:
        it.user_email = user.email
    return schemas.AdminTransactionPageOut(items=paged, total=total, page=page, page_size=page_size)


# =============================================================================
# Legacy per-user activity timeline (still used by some frontend flows)
# =============================================================================

@router.get("/users/{user_id}/activity", response_model=list[schemas.AdminActivityItem])
def admin_activity(
    user_id: str,
    db: Session = Depends(get_db),
    _: bool = Depends(require_admin),
):
    user = db.query(models.User).filter_by(id=user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    items: list[schemas.AdminActivityItem] = []

    for t in db.query(models.Trade).filter_by(user_id=user_id).all():
        items.append(schemas.AdminActivityItem(
            type="TRADE", symbol="USDT", amount=t.amount,
            direction=str(t.direction.value), status=str(t.status.value),
            created_at=t.opened_at,
            meta={"profit": t.profit, "entry": t.entry_price, "exit": t.exit_price},
        ))

    for d in db.query(models.Deposit).filter_by(user_id=user_id).all():
        items.append(schemas.AdminActivityItem(
            type="DEPOSIT", symbol=d.token, amount=d.amount,
            status=str(d.status.value), created_at=d.confirmed_at or d.created_at,
            meta={"tx": d.tx_hash, "network": d.network, "from": d.from_address},
        ))

    for s in db.query(models.SwapTx).filter_by(user_id=user_id).all():
        items.append(schemas.AdminActivityItem(
            type="SWAP", symbol=f"{s.from_symbol}→{s.to_symbol}", amount=s.from_amount,
            status="COMPLETED", created_at=s.created_at,
            meta={"to_amount": s.to_amount, "rate": s.rate},
        ))

    for tr in db.query(models.Transfer).filter(
        or_(models.Transfer.sender_id == user_id, models.Transfer.recipient_id == user_id)
    ).all():
        sent = tr.sender_id == user_id
        counterpart = tr.recipient if sent else tr.sender
        items.append(schemas.AdminActivityItem(
            type="TRANSFER", symbol=tr.symbol, amount=tr.amount,
            direction="SENT" if sent else "RECEIVED",
            status=str(tr.status.value), created_at=tr.created_at,
            meta={"with": counterpart.email if counterpart else "", "note": tr.note},
        ))

    for b in db.query(models.BalanceTransaction).filter_by(user_id=user_id).all():
        items.append(schemas.AdminActivityItem(
            type="CREDIT", symbol=b.symbol, amount=float(b.amount or 0),
            direction="IN", status="COMPLETED", created_at=b.created_at,
            meta={"note": b.note, "balance_after": float(b.balance_after or 0)},
        ))

    items.sort(key=lambda x: x.created_at, reverse=True)
    return items[:300]


# =============================================================================
# Delete user
# =============================================================================

@router.delete("/users/{user_id}")
def admin_delete_user(
    user_id: str,
    db: Session = Depends(get_db),
    _: bool = Depends(require_admin),
    acting_admin_id: str = Depends(get_current_user_id),
):
    """Permanently remove an account and everything attached to it — trades,
    deposits, swaps, transfers, messages, blocks, reports, coin balances and
    the admin balance ledger. Wrapped in a single transaction so a partial
    foreign-key failure can never leave orphaned rows. Guards: the acting admin
    can't delete their own account, and the last remaining admin never goes."""
    user = db.query(models.User).filter_by(id=user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.id == acting_admin_id:
        raise HTTPException(status_code=400, detail="You can't delete your own account")
    if user.is_admin and db.query(models.User).filter_by(is_admin=True).count() <= 1:
        raise HTTPException(status_code=400, detail="You can't delete the last admin")

    email = user.email
    uid = user.id
    try:
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
        db.query(models.BalanceTransaction).filter_by(user_id=uid).delete(synchronize_session=False)
        db.query(models.DepositAddress).filter_by(user_id=uid).delete(synchronize_session=False)
        db.query(models.Deposit).filter_by(user_id=uid).delete(synchronize_session=False)
        db.query(models.Trade).filter_by(user_id=uid).delete(synchronize_session=False)
        db.query(models.SwapTx).filter_by(user_id=uid).delete(synchronize_session=False)
        db.query(models.CoinBalance).filter_by(user_id=uid).delete(synchronize_session=False)
        db.query(models.User).filter_by(id=uid).delete(synchronize_session=False)
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Could not delete user: {e}")
    return {"ok": True, "email": email}


def bootstrap_admin():
    """Ensure an admin account exists at startup. Comes ONLY from env vars
    (email_admin + password_admin) — never hardcoded."""
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