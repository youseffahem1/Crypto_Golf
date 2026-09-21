from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from .. import schemas, market_service
from ..database import get_db

router = APIRouter(prefix="/api/market", tags=["market"])


@router.get("/price")
def price(db: Session = Depends(get_db)):
    return {"symbol": market_service.SYMBOL, "price": market_service.get_current_price(db)}


@router.get("/symbol-price")
def symbol_price(symbol: str = "GOLF", db: Session = Depends(get_db)):
    """Current authoritative price of any platform coin (GOLF/NOVA/ABC…).
    Public — charts and the trading UI read it from here."""
    symbol = symbol.strip().upper()
    return {
        "symbol": symbol,
        "price": round(market_service.get_usd_price(db, symbol), 8),
        "change_pct": market_service.get_change_pct(db, symbol),
        "launch_price": market_service.platform_launch_price(symbol),
    }


@router.get("/prices")
def all_prices(db: Session = Depends(get_db)):
    """USD price of every listed currency (USDT = 1.0) — the same table
    Convert/Invest quoting uses server-side."""
    return market_service.get_all_usd_prices(db)


@router.get("/candles", response_model=list[schemas.CandleOut])
def candles(symbol: str = "GOLFUSDT", limit: int = 150, db: Session = Depends(get_db)):
    sym = symbol.strip().upper()
    if sym == "GOLF":
        sym = market_service.SYMBOL
    elif not sym.endswith("USDT"):
        sym = sym + "USDT"
    return market_service.get_candles(db, sym, limit)
