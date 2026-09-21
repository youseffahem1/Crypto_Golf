from datetime import datetime, timedelta

from fastapi import Header, HTTPException
from jose import jwt, JWTError
from passlib.context import CryptContext

from . import models
from .database import SessionLocal
from .config import APP_SECRET_KEY, JWT_ALGORITHM, JWT_EXPIRE_MINUTES

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(plain: str) -> str:
    return pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return pwd_context.verify(plain, hashed)
    except Exception:
        return False


def create_access_token(user_id: str) -> str:
    expire = datetime.utcnow() + timedelta(minutes=JWT_EXPIRE_MINUTES)
    return jwt.encode({"sub": user_id, "exp": expire}, APP_SECRET_KEY, algorithm=JWT_ALGORITHM)


def _decode_token(token: str) -> str:
    try:
        payload = jwt.decode(token, APP_SECRET_KEY, algorithms=[JWT_ALGORITHM])
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token")
    return user_id


def _extract_bearer_token(authorization: str) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    return authorization[len("Bearer "):].strip()


def get_current_user_id(authorization: str = Header(None)) -> str:
    token = _extract_bearer_token(authorization)
    user_id = _decode_token(token)
    db = SessionLocal()
    try:
        user = db.query(models.User).filter_by(id=user_id).first()
        if not user:
            raise HTTPException(status_code=401, detail="User no longer exists")
        return user_id
    finally:
        db.close()


def require_admin(authorization: str = Header(None)) -> bool:
    token = _extract_bearer_token(authorization)
    user_id = _decode_token(token)
    db = SessionLocal()
    try:
        user = db.query(models.User).filter_by(id=user_id).first()
        if not user or not user.is_admin:
            raise HTTPException(status_code=403, detail="Admin access required")
        return True
    finally:
        db.close()
