from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import schemas, models, swap_service
from ..database import get_db
from ..auth import get_current_user_id

router = APIRouter(prefix="/api/swap", tags=["swap"])


@router.get("/quote")
def quote(from_symbol: str, to_symbol: str, amount: float, db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    try:
        rate = swap_service.get_rate(db, from_symbol, to_symbol)
    except swap_service.SwapError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"rate": rate, "to_amount": amount * rate}


@router.post("", response_model=schemas.SwapOut)
def execute(payload: schemas.SwapRequest, db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    try:
        tx = swap_service.execute_swap(db, user_id, payload.from_symbol, payload.to_symbol, payload.amount)
    except swap_service.SwapError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return tx
