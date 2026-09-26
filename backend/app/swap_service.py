"""
Virtual swap between balances only — NEVER creates a real blockchain
transaction. Any listed currency can be converted into any other (USDT,
GOLF, BTC, ETH, SOL, TRX, BNB, XRP, DOGE, ADA, LINK) using the same
server-authoritative USD prices the rest of the app uses, so quoting is
internally consistent everywhere. Rates are always expressed as:
    rate(from, to) = price(from, USD) / price(to, USD)
i.e. how many "to" tokens 1 "from" token is worth.

=============================================================================
TWO INDEPENDENT LEDGERS PER COIN — TRADING vs WALLET
=============================================================================
Every coin has two separate, separately persisted balances:

  * TRADING balance  (User.usdt_balance / User.golf_balance /
                      CoinBalance.balance)
    Deposits land here, trades are staked and settled here, and the
    platform-coin investment book is valued here. Home/Trading shows this.

  * WALLET balance   (User.usdt_wallet_balance / User.golf_wallet_balance /
                      CoinBalance.wallet_balance)
    Starts at 0 for every account. The ONLY thing that ever changes it is an
    explicit user transfer — move_between_accounts() below, exposed as
    POST /api/wallet/transfer. The Wallet page shows this.

They are never derived from one another, never synchronised, and a wallet
balance is NEVER the trading balance, the total account balance, or a
computed value of any kind. This module is the single gateway every feature
reads and writes balances through, so there is exactly one balance system —
not two competing ones.
"""
from decimal import Decimal, ROUND_DOWN

from sqlalchemy.orm import Session

from . import models, market_service

SUPPORTED_SYMBOLS = market_service.SUPPORTED_SYMBOLS

# Money is stored as binary Float everywhere, so every comparison and every
# write goes through an 8dp decimal quantisation. Without this, 94.116155
# stored as float reads back as 94.11615499999999 and a legitimate
# "move everything" transfer is rejected as insufficient funds.
_QUANT = Decimal("0.00000001")


class SwapError(Exception):
    pass


def _validate_pair(from_symbol: str, to_symbol: str):
    if from_symbol not in SUPPORTED_SYMBOLS or to_symbol not in SUPPORTED_SYMBOLS:
        raise SwapError("Unsupported swap pair")
    if from_symbol == to_symbol:
        raise SwapError("Cannot convert a coin into itself")


def _usd_price(db: Session, symbol: str) -> float:
    price = market_service.get_usd_price(db, symbol)
    if not price or price <= 0:
        raise SwapError(f"Price unavailable for {symbol} right now — try again shortly")
    return price


def get_rate(db: Session, from_symbol: str, to_symbol: str) -> float:
    """How many `to_symbol` one unit of `from_symbol` buys."""
    _validate_pair(from_symbol, to_symbol)
    pf = _usd_price(db, from_symbol)
    pt = _usd_price(db, to_symbol)
    return pf / pt


# =============================================================================
# TRADING LEDGER — deposits, trade settlement, swaps, liquidation
# =============================================================================

def get_balance(db: Session, user: models.User, symbol: str) -> float:
    """The user's TRADING balance for `symbol`."""
    if symbol == "USDT":
        return float(user.usdt_balance or 0.0)
    if symbol == "GOLF":
        return float(user.golf_balance or 0.0)
    row = db.query(models.CoinBalance).filter_by(user_id=user.id, symbol=symbol).first()
    return float(row.balance) if row else 0.0


def set_balance(db: Session, user: models.User, symbol: str, value: float):
    """Writes the user's TRADING balance for `symbol`."""
    if symbol == "USDT":
        user.usdt_balance = float(value)
        return
    if symbol == "GOLF":
        user.golf_balance = float(value)
        return
    row = db.query(models.CoinBalance).filter_by(user_id=user.id, symbol=symbol).first()
    if row:
        row.balance = float(value)
    else:
        db.add(models.CoinBalance(user_id=user.id, symbol=symbol, balance=float(value)))


# =============================================================================
# WALLET LEDGER — funded only by an explicit transfer
# =============================================================================

def get_wallet_balance(db: Session, user: models.User, symbol: str) -> float:
    """The user's WALLET balance for `symbol`. 0 until they move funds in."""
    if symbol == "USDT":
        return float(user.usdt_wallet_balance or 0.0)
    if symbol == "GOLF":
        return float(user.golf_wallet_balance or 0.0)
    row = db.query(models.CoinBalance).filter_by(user_id=user.id, symbol=symbol).first()
    return float(row.wallet_balance or 0.0) if row else 0.0


def set_wallet_balance(db: Session, user: models.User, symbol: str, value: float):
    """Writes the user's WALLET balance for `symbol`."""
    value = max(0.0, float(value))
    if symbol == "USDT":
        user.usdt_wallet_balance = value
        return
    if symbol == "GOLF":
        user.golf_wallet_balance = value
        return
    row = db.query(models.CoinBalance).filter_by(user_id=user.id, symbol=symbol).first()
    if row:
        row.wallet_balance = value
    else:
        db.add(models.CoinBalance(
            user_id=user.id, symbol=symbol, balance=0.0, wallet_balance=value,
        ))


# =============================================================================
# Full balance tables
# =============================================================================

def user_balances(db: Session, user_id: str) -> dict:
    """{SYMBOL: TRADING balance} for every listed currency, including zeros.

    Kept as the flat trading table because trading, swaps, platform valuation
    and the admin panel all read exactly this shape."""
    user = db.query(models.User).filter_by(id=user_id).first()
    if not user:
        return {}
    out = {s: 0.0 for s in SUPPORTED_SYMBOLS}
    out["USDT"] = float(user.usdt_balance or 0.0)
    out["GOLF"] = float(user.golf_balance or 0.0)
    for row in db.query(models.CoinBalance).filter_by(user_id=user_id).all():
        if row.symbol in out:
            out[row.symbol] = float(row.balance or 0.0)
    return out


def user_wallet_balances(db: Session, user_id: str) -> dict:
    """{SYMBOL: WALLET balance} for every listed currency, including zeros.

    Every account is 0 here until it explicitly transfers funds in."""
    user = db.query(models.User).filter_by(id=user_id).first()
    if not user:
        return {}
    out = {s: 0.0 for s in SUPPORTED_SYMBOLS}
    out["USDT"] = float(user.usdt_wallet_balance or 0.0)
    out["GOLF"] = float(user.golf_wallet_balance or 0.0)
    for row in db.query(models.CoinBalance).filter_by(user_id=user_id).all():
        if row.symbol in out:
            out[row.symbol] = float(row.wallet_balance or 0.0)
    return out


def split_balances(db: Session, user_id: str) -> dict:
    """{"trading": {...}, "wallet": {...}} — the two ledgers side by side.

    This is the ONLY place the two are presented together, and even here the
    wallet table is read from its own columns, never derived from trading."""
    return {
        "trading": user_balances(db, user_id),
        "wallet": user_wallet_balances(db, user_id),
    }


# =============================================================================
# The explicit trading <-> wallet move
# =============================================================================

def _dec(value) -> Decimal:
    return Decimal(str(float(value or 0.0))).quantize(_QUANT, rounding=ROUND_DOWN)


def move_between_accounts(
    db: Session, user_id: str, symbol: str, amount: float, direction: str = "TO_WALLET",
) -> dict:
    """Moves `amount` of `symbol` between the trading account and the wallet.

    direction "TO_WALLET"   (default) — deduct from TRADING, credit WALLET.
    direction "TO_TRADING"               — deduct from WALLET, credit TRADING.

    The amount is debited from the source ledger and credited to the other
    ledger in the same transaction, and a WalletTransfer audit row is written
    alongside them. Validation is exact-decimal, so "move everything" always
    works and "move more than you have" is always rejected — the source
    balance can never go negative."""
    symbol = (symbol or "").strip().upper()
    if symbol not in SUPPORTED_SYMBOLS:
        raise SwapError("Unsupported coin for transfer")

    direction = (direction or "TO_WALLET").strip().upper()
    if direction not in (models.WalletTransferDirection.TO_WALLET.value,
                         models.WalletTransferDirection.TO_TRADING.value):
        raise SwapError("Invalid transfer direction")

    amount_dec = Decimal(str(float(amount))).quantize(_QUANT, rounding=ROUND_DOWN)
    if amount_dec <= 0:
        raise SwapError("Amount must be greater than zero")

    try:
        user = db.query(models.User).filter_by(id=user_id).with_for_update().first()
    except Exception:
        db.rollback()
        user = db.query(models.User).filter_by(id=user_id).first()  # SQLite fallback
    if not user:
        raise SwapError("User not found")

    to_wallet = direction == models.WalletTransferDirection.TO_WALLET.value
    source = (get_balance if to_wallet else get_wallet_balance)(db, user, symbol)
    source_name = "trading" if to_wallet else "wallet"

    if _dec(source) < amount_dec:
        raise SwapError(
            f"Insufficient {source_name} balance — you can move at most "
            f"{_dec(source).normalize():f} {symbol}"
        )

    source_after = _dec(source) - amount_dec
    target_after = _dec(get_wallet_balance(db, user, symbol) if to_wallet
                        else get_balance(db, user, symbol)) + amount_dec

    if to_wallet:
        set_balance(db, user, symbol, float(source_after))
        set_wallet_balance(db, user, symbol, float(target_after))
    else:
        set_wallet_balance(db, user, symbol, float(source_after))
        set_balance(db, user, symbol, float(target_after))

    tx = models.WalletTransfer(
        user_id=user_id,
        symbol=symbol,
        amount=amount_dec,
        direction=(models.WalletTransferDirection.TO_WALLET if to_wallet
                   else models.WalletTransferDirection.TO_TRADING),
        trading_balance_after=_dec(get_balance(db, user, symbol)),
        wallet_balance_after=_dec(get_wallet_balance(db, user, symbol)),
    )
    db.add(tx)
    db.commit()
    db.refresh(tx)
    return {
        "id": tx.id,
        "symbol": symbol,
        "amount": float(amount_dec),
        "direction": tx.direction.value,
        "trading_balance": float(get_balance(db, user, symbol)),
        "wallet_balance": float(get_wallet_balance(db, user, symbol)),
    }


def execute_swap(db: Session, user_id: str, from_symbol: str, to_symbol: str, from_amount: float) -> models.SwapTx:
    _validate_pair(from_symbol, to_symbol)
    if from_amount <= 0:
        raise SwapError("Amount must be greater than zero")

    try:
        user = db.query(models.User).filter_by(id=user_id).with_for_update().first()
    except Exception:
        db.rollback()
        user = db.query(models.User).filter_by(id=user_id).first()  # SQLite fallback
    if not user:
        raise SwapError("User not found")

    balance = get_balance(db, user, from_symbol)
    if balance < from_amount:
        raise SwapError(f"Insufficient {from_symbol} balance")

    rate = get_rate(db, from_symbol, to_symbol)
    to_amount = from_amount * rate

    set_balance(db, user, from_symbol, balance - from_amount)
    set_balance(db, user, to_symbol, get_balance(db, user, to_symbol) + to_amount)

    tx = models.SwapTx(
        user_id=user_id, from_symbol=from_symbol, to_symbol=to_symbol,
        from_amount=from_amount, to_amount=to_amount, rate=rate,
    )
    db.add(tx)
    db.commit()
    db.refresh(tx)
    return tx