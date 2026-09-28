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
    """The positions still OPEN right now — including the FINISHED ones.

    A position whose duration has elapsed is NOT closed by this endpoint. It is
    FROZEN: the server records the price at the deadline and returns it as
    `frozen_profit` / `frozen_exit_price`, so the client can show the user a
    figure that will not move. The row stays OPEN, the balance reserved for it
    stays reserved, and it stays out of the top PROFIT / LOSS cards and out of
    closed history until the user closes it or presses CLOSE ALL.

    That is why this endpoint returns finished positions at all: they are still
    the user's to collect, and a client that dropped them here would lose money
    the server has already promised."""
    trading_service.freeze_due_trades(db)
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
    """Closed positions, newest first. Runs the freeze sweep for the same
    reason as /open-trades, so a position that ended on its own is already
    priced and waiting to be collected rather than looking live."""
    trading_service.freeze_due_trades(db)
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
    """Collect every FINISHED position in ONE atomic transaction.

    Supply `symbol` to collect a single instrument, or omit it to collect the
    user's whole finished book — which is what the "CLOSE ALL" button sends.
    Either way the balance is credited once for the whole batch, so a failure
    part-way through cannot leave some positions collected and others not.

    "Finished" means the duration has ended and the server has recorded its
    price. A position still counting down is NOT touched: it has no guaranteed
    figure to collect, and selling it here would be a sale the user did not ask
    for, at a price they never saw settle. Those are closed with their own CLOSE
    button, priced live, whenever the user chooses.

    Already-collected positions are excluded, so pressing CLOSE ALL twice moves
    money only once. Having nothing finished is not an error: the response is
    simply an empty list."""
    try:
        closed = trading_service.close_all_trades(
            db, user_id, payload.symbol if payload else None
        )
    except trading_service.TradingError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return closed
