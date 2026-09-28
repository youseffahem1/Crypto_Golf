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


@router.post("/close", response_model=schemas.TradeOut)
def close_trade_route(
    payload: schemas.TradeCloseRequest,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Close one position at the server's own price and credit the result.

    Idempotent: closing an already-closed position returns the recorded
    result instead of erroring or paying twice, so a double-click or a retry
    after a dropped response is safe."""
    try:
        trade = trading_service.close_trade(db, user_id, payload.trade_id, payload.value)
    except trading_service.TradingError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return trade


@router.post("/close-all", response_model=list[schemas.TradeOut])
def close_all_trades_route(
    payload: schemas.TradeCloseAllRequest | None = None,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Close every open position in ONE atomic transaction.

    Supply `symbol` to close a single instrument, or omit it to close the
    whole book — which is what the "CLOSE ALL" button sends. Either way the
    balance is credited once for the whole batch, so a failure part-way
    through cannot leave some positions closed and others not.

    Positions that are already closed are excluded, so pressing CLOSE ALL
    twice moves money only once. Having nothing open is not an error: the
    response is simply an empty list."""
    try:
        closed = trading_service.close_all_trades(
            db, user_id, payload.symbol if payload else None
        )
    except trading_service.TradingError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return closed
