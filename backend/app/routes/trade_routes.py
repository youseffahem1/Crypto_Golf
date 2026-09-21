from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import schemas, models, trading_service
from ..database import get_db
from ..auth import get_current_user_id

router = APIRouter(prefix="/api/trade", tags=["trade"])


@router.post("/open", response_model=schemas.TradeOut)
def open_trade(payload: schemas.TradeOpenRequest, db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    try:
        trade = trading_service.open_trade(
            db, user_id, payload.direction, payload.amount, payload.duration_seconds, payload.symbol
        )
    except trading_service.TradingError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return trade


@router.get("/open-trades", response_model=list[schemas.TradeOut])
def open_trades(db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    return (
        db.query(models.Trade)
        .filter_by(user_id=user_id, status=models.TradeStatus.OPEN)
        .order_by(models.Trade.opened_at.desc()).all()
    )


@router.get("/history", response_model=list[schemas.TradeOut])
def trade_history(
    symbol: str | None = None,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    q = db.query(models.Trade).filter(
        models.Trade.user_id == user_id, models.Trade.status != models.TradeStatus.OPEN
    )
    if symbol:
        q = q.filter(models.Trade.symbol == symbol.strip().upper())
    return q.order_by(models.Trade.settled_at.desc()).limit(100).all()
