from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from .. import schemas, transfer_service
from ..database import get_db
from ..auth import get_current_user_id

router = APIRouter(prefix="/api/users", tags=["users"])


@router.get("/search", response_model=list[schemas.UserSearchOut])
def search_users(q: str = "", db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    return transfer_service.search_users(db, q)