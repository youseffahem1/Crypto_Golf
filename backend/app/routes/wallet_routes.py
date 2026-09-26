from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import schemas, models, deposit_monitor, swap_service
from ..config import (
    PLATFORM_COINS, NORMAL_WALLET_COINS, DEPOSIT_ADDRESSES, WITHDRAW_ADDRESSES,
)
from ..database import get_db
from ..auth import get_current_user_id

router = APIRouter(prefix="/api/wallet", tags=["wallet"])


def _explorer_url(tx_hash: str) -> str:
    return f"https://nile.tronscan.org/#/transaction/{tx_hash}"


@router.get("/layout", response_model=schemas.WalletLayoutOut)
def wallet_layout(db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    """Describes the two-wallet structure the UI renders: which coins are
    platform-issued (Trading / Investment wallet) vs established currencies
    (Normal wallet), plus BOTH balance ledgers and every coin's (optionally
    configured) deposit/withdraw delivery address.

    trading_balances and wallet_balances are independent numbers for the same
    coins. wallet_balances is all zeros until the user moves funds in."""
    deposit = dict(DEPOSIT_ADDRESSES)
    withdraw = dict(WITHDRAW_ADDRESSES)

    # Every user already has a real TRON Nile deposit address — surface it
    # as USDT's deposit address so the wallet flow is honest instead of
    # inventing one. Empty otherwise (the frontend shows "Not Provided Yet").
    usdt_addr = db.query(models.DepositAddress).filter_by(user_id=user_id).first()
    if usdt_addr is not None:
        deposit.setdefault("USDT", usdt_addr.address)

    trading = swap_service.user_balances(db, user_id)
    return schemas.WalletLayoutOut(
        platform_coins=list(PLATFORM_COINS),
        normal_wallet_coins=list(NORMAL_WALLET_COINS),
        balances=trading,
        trading_balances=trading,
        wallet_balances=swap_service.user_wallet_balances(db, user_id),
        deposit_addresses=deposit,
        withdraw_addresses=withdraw,
    )


@router.get("/deposit-address", response_model=schemas.DepositAddressOut)
def get_deposit_address(db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    row = db.query(models.DepositAddress).filter_by(user_id=user_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="No deposit address found for this account")
    return schemas.DepositAddressOut(address=row.address, network="TRON Nile Testnet")


@router.get("/balances", response_model=schemas.BalancesOut)
def get_balances(db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    """Both ledgers, returned separately.

    `trading` is what Home/Trading renders. `wallet` is what the Wallet page
    renders, and it is read from its own columns — it is never the trading
    balance, the account total, or anything computed from them."""
    return schemas.BalancesOut(**swap_service.split_balances(db, user_id))


@router.post("/transfer", response_model=schemas.WalletTransferOut)
def transfer_between_accounts(
    payload: schemas.WalletTransferRequest,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Move funds between the trading account and the wallet.

    This is the ONLY endpoint in the platform that can change a wallet
    balance. Deposits, trade settlement and trade profit all land in the
    trading account and leave the wallet untouched; nothing here runs
    automatically.

    Rejects any amount above the available source balance, so the source
    ledger can never be driven negative."""
    try:
        result = swap_service.move_between_accounts(
            db, user_id, payload.symbol, payload.amount, payload.direction,
        )
    except swap_service.SwapError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return schemas.WalletTransferOut(**result)


@router.get("/transfers", response_model=schemas.WalletTransferHistoryOut)
def wallet_transfer_history(db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    """The complete history of how this account's wallet was funded. Because
    the wallet starts at 0 and nothing auto-synchronises it, this list is the
    authoritative record of every coin that entered or left it."""
    rows = (
        db.query(models.WalletTransfer).filter_by(user_id=user_id)
        .order_by(models.WalletTransfer.created_at.desc()).limit(100).all()
    )
    return schemas.WalletTransferHistoryOut(transfers=[
        schemas.WalletTransferItem(
            id=r.id, symbol=r.symbol, amount=float(r.amount or 0.0),
            direction=r.direction.value if hasattr(r.direction, "value") else r.direction,
            created_at=r.created_at,
        )
        for r in rows
    ])


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
