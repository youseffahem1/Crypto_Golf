import uuid
import enum
from datetime import datetime

from sqlalchemy import Column, String, Float, Boolean, DateTime, ForeignKey, Enum, Integer, Text, Numeric, UniqueConstraint
from sqlalchemy.orm import relationship

from .database import Base


def gen_id():
    return str(uuid.uuid4())


class User(Base):
    __tablename__ = "users"

    id = Column(String, primary_key=True, default=gen_id)
    email = Column(String, nullable=False, unique=True, index=True)
    password_hash = Column(String, nullable=False)
    label = Column(String, nullable=True)
    is_admin = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Virtual balances only — never represent real, spendable funds. USDT
    # here is credited ONLY after a real Nile-testnet deposit is verified
    # (see DepositMonitor) or from trade settlement / virtual swap.
    usdt_balance = Column(Float, default=0.0, nullable=False)
    golf_balance = Column(Float, default=0.0, nullable=False)

    deposit_address = relationship("DepositAddress", back_populates="user", uselist=False)
    deposits = relationship("Deposit", back_populates="user")
    trades = relationship("Trade", back_populates="user")


class DepositAddress(Base):
    """One real TRON Nile-testnet address per user, generated locally
    (no private key ever leaves the server, never used to SEND — deposit
    monitoring only reads incoming transfers to it)."""
    __tablename__ = "deposit_addresses"

    id = Column(String, primary_key=True, default=gen_id)
    user_id = Column(String, ForeignKey("users.id"), nullable=False, unique=True)
    address = Column(String, nullable=False, unique=True, index=True)
    private_key_hex = Column(String, nullable=False)  # testnet-only; see README warning in tron_service.py
    network = Column(String, default="TRON_NILE", nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="deposit_address")


class DepositStatus(str, enum.Enum):
    PENDING = "PENDING"       # seen on-chain, waiting for confirmations
    CONFIRMED = "CONFIRMED"   # confirmations met, virtual balance credited
    FAILED = "FAILED"         # failed validation (wrong contract/receiver/etc.) — never credited


class Deposit(Base):
    """One row per verified on-chain transfer event. tx_hash is UNIQUE —
    this is the mechanism that makes double-crediting the same transaction
    structurally impossible, not just a convention."""
    __tablename__ = "deposits"

    id = Column(String, primary_key=True, default=gen_id)
    user_id = Column(String, ForeignKey("users.id"), nullable=False)
    tx_hash = Column(String, nullable=False, unique=True, index=True)
    token = Column(String, default="USDT_TRC20", nullable=False)
    network = Column(String, default="TRON_NILE", nullable=False)
    contract_address = Column(String, nullable=False)
    from_address = Column(String, nullable=False)
    to_address = Column(String, nullable=False)
    amount = Column(Float, nullable=False)
    confirmations = Column(Integer, default=0, nullable=False)
    block_number = Column(Integer, nullable=True)
    status = Column(Enum(DepositStatus), default=DepositStatus.PENDING, nullable=False)
    failure_reason = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    confirmed_at = Column(DateTime, nullable=True)

    user = relationship("User", back_populates="deposits")


class TradeDirection(str, enum.Enum):
    UP = "UP"
    DOWN = "DOWN"


class TradeStatus(str, enum.Enum):
    OPEN = "OPEN"
    WON = "WON"
    LOST = "LOST"


class Trade(Base):
    """A binary UP/DOWN prediction against the server's own authoritative
    demo price feed (see market_service.py) — never settled from anything
    the client sends. amount is virtual USDT only."""
    __tablename__ = "trades"

    id = Column(String, primary_key=True, default=gen_id)
    user_id = Column(String, ForeignKey("users.id"), nullable=False)
    direction = Column(Enum(TradeDirection), nullable=False)
    amount = Column(Float, nullable=False)
    entry_price = Column(Float, nullable=False)
    exit_price = Column(Float, nullable=True)
    payout_rate = Column(Float, nullable=False)
    duration_seconds = Column(Integer, nullable=False)
    status = Column(Enum(TradeStatus), default=TradeStatus.OPEN, nullable=False)
    profit = Column(Float, nullable=True)  # signed: positive on WON, negative (=-amount) on LOST
    opened_at = Column(DateTime, default=datetime.utcnow)
    closes_at = Column(DateTime, nullable=False)
    settled_at = Column(DateTime, nullable=True)

    user = relationship("User", back_populates="trades")


class SwapTx(Base):
    """Virtual swap between demo balances — never creates a real blockchain
    transaction, per spec."""
    __tablename__ = "swap_txs"

    id = Column(String, primary_key=True, default=gen_id)
    user_id = Column(String, ForeignKey("users.id"), nullable=False)
    from_symbol = Column(String, nullable=False)
    to_symbol = Column(String, nullable=False)
    from_amount = Column(Float, nullable=False)
    to_amount = Column(Float, nullable=False)
    rate = Column(Float, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class GolfStat(Base):
    """Single-row table of GOLF token dashboard stats. is_demo=True always
    right now (no real DEX Screener integration wired up yet, per spec:
    'prepare for future integration, with a clear separation between LIVE
    and DEMO data') — flip is_demo to False only once a real data source
    actually feeds this table."""
    __tablename__ = "golf_stats"

    id = Column(String, primary_key=True, default=gen_id)
    price_usdt = Column(Float, default=0.02, nullable=False)
    market_cap_usdt = Column(Float, default=2_000_000.0, nullable=False)
    liquidity_usdt = Column(Float, default=450_000.0, nullable=False)
    volume_24h_usdt = Column(Float, default=180_000.0, nullable=False)
    holders = Column(Integer, default=3200, nullable=False)
    buyers_24h = Column(Integer, default=140, nullable=False)
    likes = Column(Integer, default=980, nullable=False)
    change_24h_pct = Column(Float, default=6.4, nullable=False)
    is_demo = Column(Boolean, default=True, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow)


class CoinBalance(Base):
    """Virtual balance for a single (user, coin) pair — one row per symbol a
    user holds. USDT and GOLF stay on the User row (historical columns all
    existing code reads), every other tradeable coin lives here so users can
    hold/convert as many currencies as the platform lists."""
    __tablename__ = "coin_balances"

    id = Column(String, primary_key=True, default=gen_id)
    user_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    symbol = Column(String, nullable=False, index=True)
    balance = Column(Float, default=0.0, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (UniqueConstraint("user_id", "symbol", name="uq_user_symbol"),)


class BalanceTransaction(Base):
    """Permanent, server-side audit ledger for every admin balance adjustment
    (ADMIN_CREDIT / ADMIN_DEBIT / ADJUSTMENT). Money here is stored as
    Numeric(24,8) so fractional amounts never round-trip through binary float.
    The balance change and its ledger row are written in ONE database
    transaction — if the ledger insert fails, the balance is never updated."""

    __tablename__ = "balance_transactions"

    id = Column(String, primary_key=True, default=gen_id)
    user_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    symbol = Column(String, nullable=False)
    tx_type = Column(String, default="ADMIN_CREDIT", nullable=False)
    amount = Column(Numeric(24, 8), nullable=False)       # always positive; sign lives in tx_type
    balance_after = Column(Numeric(24, 8), nullable=False)
    note = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class Candle(Base):
    """The server's own authoritative market candle history — what
    actually determines trade settlement. Separate from whatever the
    frontend's own chart-rendering code independently draws."""
    __tablename__ = "candles"

    id = Column(String, primary_key=True, default=gen_id)
    symbol = Column(String, default="GOLFUSDT", nullable=False, index=True)
    open = Column(Float, nullable=False)
    high = Column(Float, nullable=False)
    low = Column(Float, nullable=False)
    close = Column(Float, nullable=False)
    open_time = Column(DateTime, nullable=False, index=True)


class TransferStatus(str, enum.Enum):
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class Transfer(Base):
    """Internal peer-to-peer coin transfer between two platform accounts.
    Strictly in-app: there is no on-chain transaction, no external
    address, and no wallet outside the platform can ever receive or send
    assets. This is the ONLY way coins move from one user to another."""
    __tablename__ = "transfers"

    id = Column(String, primary_key=True, default=gen_id)
    sender_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    recipient_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    symbol = Column(String, nullable=False)
    amount = Column(Float, nullable=False)
    note = Column(String, nullable=True)
    status = Column(Enum(TransferStatus), default=TransferStatus.COMPLETED, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    sender = relationship("User", foreign_keys=[sender_id])
    recipient = relationship("User", foreign_keys=[recipient_id])


class DirectMessage(Base):
    """A single private message between two platform users. Messages are
    filtered/moderation-scanned server-side on send (see message_service)
    and keep everything inside the platform."""
    __tablename__ = "direct_messages"

    id = Column(String, primary_key=True, default=gen_id)
    sender_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    recipient_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    body = Column(Text, nullable=False)
    is_read = Column(Boolean, default=False, nullable=False)
    moderated = Column(Boolean, default=False, nullable=False)  # True if the content filter rewrote the body
    created_at = Column(DateTime, default=datetime.utcnow)

    sender = relationship("User", foreign_keys=[sender_id])
    recipient = relationship("User", foreign_keys=[recipient_id])


class UserBlock(Base):
    """A blocks B — A no longer receives messages from B."""
    __tablename__ = "user_blocks"

    id = Column(String, primary_key=True, default=gen_id)
    blocker_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    blocked_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (UniqueConstraint("blocker_id", "blocked_id", name="uq_blocker_blocked"),)


class ReportReason(str, enum.Enum):
    SCAM = "SCAM"
    SPAM = "SPAM"
    ABUSE = "ABUSE"
    HARASSMENT = "HARASSMENT"
    OTHER = "OTHER"


class Report(Base):
    """User-generated moderation report against another user, optionally
    tied to a specific message. Admins can inspect these (see /reports)."""
    __tablename__ = "reports"

    id = Column(String, primary_key=True, default=gen_id)
    reporter_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    reported_user_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    message_id = Column(String, ForeignKey("direct_messages.id"), nullable=True)
    reason = Column(Enum(ReportReason), nullable=False)
    details = Column(Text, nullable=True)
    resolved = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    reporter = relationship("User", foreign_keys=[reporter_id])
    reported = relationship("User", foreign_keys=[reported_user_id])
    message = relationship("DirectMessage")
