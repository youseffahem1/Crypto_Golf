from datetime import datetime, timedelta
import time

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


# --- Wallet PIN ---------------------------------------------------------------
# The PIN is a second factor on the SAME account, not a second identity system:
# it is hashed and verified with the identical passlib/bcrypt context used for
# password_hash, and it is always checked against the row belonging to the
# currently authenticated user (never a client-supplied user id, never a PIN
# held in the browser).

PIN_MIN, PIN_MAX = 4, 5

# A 4–5 digit PIN is only 10,000 combinations, so the hash being strong is not
# enough on its own — the endpoint also has to refuse to be used as an oracle.
# In-process, per user: five wrong attempts locks that account out for a minute.
_PIN_MAX_ATTEMPTS = 5
_PIN_LOCKOUT_SECONDS = 60
_pin_failures: dict[str, list[float]] = {}


def valid_pin_format(pin: str) -> bool:
    """A PIN is exactly PIN_MIN..PIN_MAX digits, and nothing else."""
    return isinstance(pin, str) and pin.isdigit() and PIN_MIN <= len(pin) <= PIN_MAX


def _pin_locked_until(user_id: str) -> int:
    """Seconds left on the lockout, or 0."""
    now = time.time()
    hits = [t for t in _pin_failures.get(user_id, []) if now - t < _PIN_LOCKOUT_SECONDS]
    if hits:
        _pin_failures[user_id] = hits
        if len(hits) >= _PIN_MAX_ATTEMPTS:
            return int(_PIN_LOCKOUT_SECONDS - (now - hits[0])) + 1
    else:
        _pin_failures.pop(user_id, None)
    return 0


def record_pin_failure(user_id: str) -> None:
    _pin_failures.setdefault(user_id, []).append(time.time())


def clear_pin_failures(user_id: str) -> None:
    _pin_failures.pop(user_id, None)


def hash_pin(pin: str) -> str:
    """Hash a Wallet PIN with the very same bcrypt context as the password."""
    return hash_password(pin)


def verify_pin(pin: str, pin_hash: str) -> bool:
    """True only for the exact PIN that produced this hash."""
    if not pin_hash or not valid_pin_format(pin):
        return False
    return verify_password(pin, pin_hash)


def check_wallet_pin(user_id: str, pin: str, pin_hash: str) -> str | None:
    """Verify a submitted Wallet PIN for `user_id`. Returns an error message, or
    None when the PIN is correct.

    Call this BEFORE touching any balance, so a wrong PIN can never leave a
    partial transfer behind."""
    locked = _pin_locked_until(user_id)
    if locked:
        return f"Too many incorrect PIN attempts. Try again in {locked}s."
    if not pin_hash:
        return "Set your Wallet PIN first."
    if verify_pin(pin, pin_hash):
        clear_pin_failures(user_id)
        return None
    record_pin_failure(user_id)
    return "Invalid PIN."


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
