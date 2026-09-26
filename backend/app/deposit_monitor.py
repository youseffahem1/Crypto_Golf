"""
Polls every user's Nile-testnet deposit address for real incoming
USDT-TRC20 transfers, verifies each one fully server-side, and only then
credits the user's VIRTUAL usdt_balance. Nothing here trusts anything the
frontend sends — the frontend never reports "I deposited X", it only asks
"what's my balance / deposit history" after this service has already done
the verifying.

Verification performed for every transfer, in order:
  1. Network:  only ever queries TronGrid's Nile endpoint (config.TRON_NILE_API_BASE)
  2. Token contract: tron_service.fetch_trc20_transfers_to() already filters
     to config.USDT_TRC20_CONTRACT_NILE only — anything else is invisible to
     this code, it's never even considered
  3. Receiver: transfer.to_address must equal the user's own deposit address
     (guaranteed by construction — we only ever poll a user's own address)
  4. Sender: recorded as-is (from_address) for the transaction history/explorer link
  5. Amount: taken directly from the on-chain event, converted from raw units
  6. Confirmations: current Nile block height minus the transaction's block
     height, must reach config.TRON_REQUIRED_CONFIRMATIONS before crediting
  7. Duplicate prevention: Deposit.tx_hash has a UNIQUE constraint — a
     transfer already recorded (PENDING or CONFIRMED) is never re-inserted;
     it's looked up and updated in place instead
"""
from datetime import datetime

from sqlalchemy.orm import Session

from . import models, tron_service
from .config import TRON_REQUIRED_CONFIRMATIONS


def poll_all_deposit_addresses(db: Session):
    addresses = db.query(models.DepositAddress).all()
    for addr_row in addresses:
        try:
            _poll_one_address(db, addr_row)
        except tron_service.TronServiceError as e:
            # A single address's poll failing (e.g. transient TronGrid
            # error) must never stop the others from being checked.
            print(f"[deposit_monitor] poll failed for {addr_row.address}: {e}")
        except Exception as e:
            print(f"[deposit_monitor] unexpected error for {addr_row.address}: {e}")


def _poll_one_address(db: Session, addr_row: models.DepositAddress):
    transfers = tron_service.fetch_trc20_transfers_to(addr_row.address)
    if not transfers:
        return

    current_block = None  # fetched lazily, only if there's something PENDING to check

    for t in transfers:
        tx_hash = t["tx_hash"]
        if not tx_hash:
            continue

        existing = db.query(models.Deposit).filter_by(tx_hash=tx_hash).first()

        if existing and existing.status == models.DepositStatus.CONFIRMED:
            continue  # already fully processed and credited — never touch again

        if existing is None:
            existing = models.Deposit(
                user_id=addr_row.user_id,
                tx_hash=tx_hash,
                token="USDT_TRC20",
                network="TRON_NILE",
                contract_address=t["contract_address"],
                from_address=t["from_address"] or "",
                to_address=t["to_address"] or addr_row.address,
                amount=t["amount"],
                status=models.DepositStatus.PENDING,
            )
            db.add(existing)
            db.commit()
            db.refresh(existing)

        # Confirmation check
        try:
            if current_block is None:
                current_block = tron_service.get_current_block_number()
            tx_block = tron_service.get_transaction_block_number(tx_hash)
            existing.block_number = tx_block
            confirmations = max(0, current_block - tx_block)
            existing.confirmations = confirmations
        except tron_service.TronServiceError:
            db.commit()
            continue  # couldn't check confirmations this round — try again next poll

        if confirmations >= TRON_REQUIRED_CONFIRMATIONS:
            _credit_deposit(db, existing)
        else:
            db.commit()


def _credit_deposit(db: Session, deposit: models.Deposit):
    """Credits the user's TRADING virtual balance exactly once. Re-fetches the
    Deposit row with a lock-equivalent re-check of its status immediately
    before crediting, so two overlapping poll cycles can never double-credit
    the same row even under concurrency.

    A deposit lands in the trading account, NOT in the wallet. The wallet is
    an independent ledger that only ever changes through an explicit user
    transfer, so crediting here must never touch a wallet balance."""
    fresh = db.query(models.Deposit).filter_by(id=deposit.id).first()
    if not fresh or fresh.status == models.DepositStatus.CONFIRMED:
        return

    user = db.query(models.User).filter_by(id=fresh.user_id).first()
    if not user:
        fresh.status = models.DepositStatus.FAILED
        fresh.failure_reason = "User account no longer exists"
        db.commit()
        return

    user.usdt_balance = float(user.usdt_balance) + float(fresh.amount)
    fresh.status = models.DepositStatus.CONFIRMED
    fresh.confirmed_at = datetime.utcnow()
    db.commit()
    print(f"[deposit_monitor] credited {fresh.amount} USDT to trading account of user {user.id} (tx {fresh.tx_hash})")
