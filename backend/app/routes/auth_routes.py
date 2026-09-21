import re

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import schemas, models, tron_service
from ..database import get_db
from ..auth import hash_password, verify_password, create_access_token, get_current_user_id

router = APIRouter(prefix="/api/auth", tags=["auth"])

_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


@router.post("/signup", response_model=schemas.TokenOut)
def signup(payload: schemas.SignupRequest, db: Session = Depends(get_db)):
    email = payload.email.strip().lower()
    if not _EMAIL_RE.match(email):
        raise HTTPException(status_code=400, detail="Enter a valid email address")

    existing = db.query(models.User).filter_by(email=email).first()
    if existing:
        raise HTTPException(status_code=400, detail="An account with this email already exists")

    user = models.User(email=email, password_hash=hash_password(payload.password), label=payload.label)
    db.add(user)
    db.commit()
    db.refresh(user)

    # Every user gets a real Nile-testnet deposit address the moment they sign up.
    addr = tron_service.generate_deposit_address()
    db.add(models.DepositAddress(user_id=user.id, address=addr["address"], private_key_hex=addr["private_key_hex"]))
    db.commit()

    token = create_access_token(user.id)
    return schemas.TokenOut(access_token=token, user=user)


@router.post("/login", response_model=schemas.TokenOut)
def login(payload: schemas.LoginRequest, db: Session = Depends(get_db)):
    email = payload.email.strip().lower()
    user = db.query(models.User).filter_by(email=email).first()
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Incorrect email or password")

    token = create_access_token(user.id)
    return schemas.TokenOut(access_token=token, user=user)


@router.get("/me", response_model=schemas.UserOut)
def me(db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    return db.query(models.User).filter_by(id=user_id).first()
