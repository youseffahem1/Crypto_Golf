from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import schemas, models, deposit_monitor, swap_service
from ..config import PLATFORM_COINS, NORMAL_WALLET_COINS
from ..database import get_db
from ..auth import get_current_user_id

router = APIRouter(prefix="/api/wallet", tags=["wallet"])


def _explorer_url(tx_hash: str) -> str:
    return f"https://nile.tronscan.org/#/transaction/{tx_hash}"


@router.get("/layout", response_model=schemas.WalletLayoutOut)
def wallet_layout(db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    """Describes the two-wallet structure the UI renders: which coins are
    platform-issued (Trading / Investment wallet) vs established currencies
    (Normal wallet), plus the user's full balance table."""
    return schemas.WalletLayoutOut(
        platform_coins=list(PLATFORM_COINS),
        normal_wallet_coins=list(NORMAL_WALLET_COINS),
        balances=swap_service.user_balances(db, user_id),
    )


@router.get("/deposit-address", response_model=schemas.DepositAddressOut)
def get_deposit_address(db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    row = db.query(models.DepositAddress).filter_by(user_id=user_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="No deposit address found for this account")
    return schemas.DepositAddressOut(address=row.address, network="TRON Nile Testnet")


@router.get("/balances")
def get_balances(db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    return swap_service.user_balances(db, user_id)


@router.get("/deposits", response_model=list[schemas.DepositOut])
def list_deposits(db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    rows = (
        db.query(models.Deposit).filter_by(user_id=user_id)
        .order_by(models.Deposit.created_at.desc()).limit(100).all()
    )
    return [
        schemas.DepositOut(
            id=d.id, tx_hash=d.tx_hash, token=d.token, network=d.network,
            from_address=d.from_address, to_address=d.to_address, amount=d.amount,
            confirmations=d.confirmations,
            status=d.status.value if hasattr(d.status, "value") else d.status,
            failure_reason=d.failure_reason, created_at=d.created_at, confirmed_at=d.confirmed_at,
            explorer_url=_explorer_url(d.tx_hash),
        )
        for d in rows
    ]


@router.post("/deposits/poll-now")
def poll_now(db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    """Lets the user (or the frontend, right after they send a deposit)
    trigger an immediate check instead of waiting for the next scheduled
    poll — still goes through the exact same real on-chain verification,
    just on demand."""
    addr = db.query(models.DepositAddress).filter_by(user_id=user_id).first()
    if not addr:
        raise HTTPException(status_code=404, detail="No deposit address found for this account")
    try:
        deposit_monitor._poll_one_address(db, addr)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Could not check Nile testnet right now: {e}")
    return {"success": True}
