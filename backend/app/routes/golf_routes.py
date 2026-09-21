from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from .. import schemas, models, market_service
from ..database import get_db

router = APIRouter(prefix="/api/golf", tags=["golf"])


@router.get("/stats", response_model=schemas.GolfStatsOut)
def golf_stats(db: Session = Depends(get_db)):
    row = db.query(models.GolfStat).first()
    if not row:
        row = models.GolfStat()
        db.add(row)
        db.commit()
        db.refresh(row)
    # Price always mirrors the same authoritative GOLF/USDT price the
    # trading engine and swap use — never a second, inconsistent number.
    row.price_usdt = market_service.get_current_price(db)
    return row
