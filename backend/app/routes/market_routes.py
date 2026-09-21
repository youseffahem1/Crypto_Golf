from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from .. import schemas, market_service
from ..database import get_db

router = APIRouter(prefix="/api/market", tags=["market"])


@router.get("/price")
def price(db: Session = Depends(get_db)):
    return {"symbol": market_service.SYMBOL, "price": market_service.get_current_price(db)}


@router.get("/prices")
def all_prices(db: Session = Depends(get_db)):
    """USD price of every listed currency (USDT = 1.0) — the same table
    Convert/Invest quoting uses server-side."""
    return market_service.get_all_usd_prices(db)


@router.get("/candles", response_model=list[schemas.CandleOut])
def candles(limit: int = 150, db: Session = Depends(get_db)):
    return market_service.get_candles(db, limit)
