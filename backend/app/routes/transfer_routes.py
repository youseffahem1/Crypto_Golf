from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import schemas, transfer_service
from ..database import get_db
from ..auth import get_current_user_id

router = APIRouter(prefix="/api/transfer", tags=["transfer"])


@router.post("", response_model=schemas.TransferOut)
def create_transfer(
    payload: schemas.TransferRequest,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    try:
        tx = transfer_service.execute_transfer(
            db, user_id, payload.recipient_email, payload.symbol, payload.amount, payload.note
        )
    except transfer_service.TransferError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return schemas.TransferOut(
        id=tx.id, symbol=tx.symbol, amount=tx.amount, note=tx.note,
        status=tx.status.value if hasattr(tx.status, "value") else tx.status,
        created_at=tx.created_at,
    )


@router.get("/history")
def history(db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    return transfer_service.transfer_history(db, user_id)