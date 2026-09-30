import uuid
import enum
from datetime import datetime

from sqlalchemy import Column, String, Float, Boolean, DateTime, ForeignKey, Enum, Integer, Text, Numeric, UniqueConstraint, false
from sqlalchemy.orm import relationship

from .database import Base


def gen_id():
    return str(uuid.uuid4())


class User(Base):
    __tablename__ = "users"

    id = Column(String, primary_key=True, default=gen_id)
    email = Column(String, nullable=False, unique=True, index=True)
    password_hash = Column(String, nullable=False)
    # The account's 4–5 digit Wallet PIN, stored ONLY as a bcrypt hash — the
    # exact mechanism password_hash already uses (see auth.hash_password). It
    # is never stored, logged or returned in plaintext. NULL means the account
    # has not set a PIN yet, which is the state of every pre-existing row, so
    # the column is nullable and needs no backfill.
    pin_hash = Column(String, nullable=True)
    label = Column(String, nullable=True)
    is_admin = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    # -------------------------------------------------------------------------
    # TRADING ACCOUNT (usdt_balance / golf_balance below) and WALLET ACCOUNT
    # (usdt_wallet_balance / golf_wallet_balance) are TWO INDEPENDENT LEDGERS.
    # They are never derived from one another and never synchronised.
    #
    #   * TRADING balance — where deposits land, where trades are staked and
    #     settled, and where the platform-coin investment book lives. This is
    #     what Home/Trading shows.
    #   * WALLET balance — starts at 0 for every account and only ever changes
    #     through an explicit user action (see wallet_routes POST /transfer and
    #     the WalletTransfer ledger below). This is what the Wallet page shows.
    #
    # Virtual balances only — they never represent real, spendable funds. USDT
    # here is credited ONLY after a real Nile-testnet deposit is verified
    # (see DepositMonitor) or from trade settlement / virtual swap.
    # -------------------------------------------------------------------------
    usdt_balance = Column(Float, default=0.0, nullable=False)
    golf_balance = Column(Float, default=0.0, nullable=False)
    usdt_wallet_balance = Column(Float, default=0.0, nullable=False)
    golf_wallet_balance = Column(Float, default=0.0, nullable=False)

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
    the client sends. amount is virtual USDT only. symbol identifies the
    platform coin the position is opened on (GOLF, NOVA, ABC, …); default
    "GOLF" keeps every historical trade's semantics unchanged."""
    __tablename__ = "trades"

    id = Column(String, primary_key=True, default=gen_id)
    user_id = Column(String, ForeignKey("users.id"), nullable=False)
    symbol = Column(String, default="GOLF", nullable=False)
    direction = Column(Enum(TradeDirection), nullable=False)
    amount = Column(Float, nullable=False)
    entry_price = Column(Float, nullable=False)
    exit_price = Column(Float, nullable=True)
    payout_rate = Column(Float, nullable=False)
    duration_seconds = Column(Integer, nullable=False)
    status = Column(Enum(TradeStatus), default=TradeStatus.OPEN, nullable=False)
    profit = Column(Float, nullable=True)  # signed: positive on WON, negative on LOST
    # A winning trade's profit may be moved into the wallet exactly once. This
    # flag is what makes that a guarantee rather than a UI convention: the
    # moment the profit is transferred the trade is stamped, and every later
    # "available profit" query simply stops counting it. A losing trade is
    # never stamped and is never transferable.
    profit_moved = Column(Boolean, default=False, server_default=false(), nullable=False)
    opened_at = Column(DateTime, default=datetime.utcnow)
    # The moment this position's duration runs out. It is written once, at
    # open, as `opened_at + duration_seconds`, and it is the ONE deadline the
    # server acts on. The frontend draws its countdown straight from this
    # timestamp, so the client clock can never disagree with the server about
    # when a trade is over. Passing it does NOT settle the position — see the
    # frozen_* columns below.
    closes_at = Column(DateTime, nullable=False)
    settled_at = Column(DateTime, nullable=True)
    # =====================================================================
    # The FROZEN result — the guarantee behind 00:00
    # =====================================================================
    # When a position's duration ends, the server records the market price at
    # that instant and STOPS. The number it writes here is the number the user
    # is guaranteed to be paid, whenever they choose to collect it — which is
    # the whole point: the figure on screen at 00:00 cannot drift afterwards,
    # and a user who walks away and comes back tomorrow gets the same money.
    #
    # These are deliberately SEPARATE from `exit_price` / `profit` / `status`:
    #   * `profit` and `status` are the REALIZED record. They are read by
    #     realized_split() to paint the top PROFIT and LOSS cards, so writing a
    #     frozen result into them would move the cards and credit the P/L at
    #     00:00 — the exact bug this design exists to prevent.
    #   * A frozen position is still OPEN, still holds the balance reserved for
    #     it, and is still absent from closed history.
    #
    # `frozen_exit_price` NULL means "this position has not reached its deadline
    # yet". Once set it is never recomputed, which is what makes it a promise.
    frozen_exit_price = Column(Float, nullable=True)
    # The signed profit implied by the frozen price. Also never realized.
    frozen_profit = Column(Float, nullable=True)
    # When the server recorded the freeze (a diagnostic, not a deadline).
    frozen_at = Column(DateTime, nullable=True)
    # Why a position left the OPEN state: "SOLD" (the user closed it early) or
    # "EXPIRED" (legacy, only reachable with AUTO_SETTLE_ON_EXPIRE on). NULL on
    # a position that is still open. Purely descriptive — status is the
    # authority, and this exists so a client and an operator can tell the two
    # apart instead of seeing one undifferentiated CLOSED.
    close_reason = Column(String, nullable=True)

    user = relationship("User", back_populates="trades")

    @property
    def payout_multiplier(self) -> float:
        """The payout rule this position is priced with, so a client can mark it
        to market using the SAME arithmetic the server settles it with.

        A property rather than a column on purpose: it is a rule of the platform,
        not a fact about this particular position, and reading it from config
        means changing it never requires a migration or a backfill of rows that
        were already opened. A trade always reflects the rule in force when it is
        priced, which is the rule that priced it.
        """
        from .config import TRADE_PAYOUT_MULTIPLIER

        return float(TRADE_PAYOUT_MULTIPLIER or 1.0)


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
    user holds. USDT and GOLF keep dedicated columns on the User row (the
    historical columns all existing code reads), every other tradeable coin
    lives here so users can hold/convert as many currencies as the platform
    lists.

    `balance` is the TRADING balance and `wallet_balance` is the WALLET
    balance. They are separate, independently persisted numbers: a coin's
    wallet balance is 0 until the user explicitly moves funds into it, and
    moving funds is the ONLY thing that ever changes it (see
    wallet_routes.POST /api/wallet/transfer and the WalletTransfer ledger).
    """
    __tablename__ = "coin_balances"

    id = Column(String, primary_key=True, default=gen_id)
    user_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    symbol = Column(String, nullable=False, index=True)
    balance = Column(Float, default=0.0, nullable=False)
    wallet_balance = Column(Float, default=0.0, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (UniqueConstraint("user_id", "symbol", name="uq_user_symbol"),)


class WalletTransferDirection(str, enum.Enum):
    """Which way an explicit wallet move went. The only two things that can
    ever change a wallet balance in the whole platform."""

    TO_WALLET = "TO_WALLET"      # trading account -> wallet
    TO_TRADING = "TO_TRADING"    # wallet -> trading account


class WalletTransferKind(str, enum.Enum):
    """What kind of explicit move a WalletTransfer row records.

    A profit move is not a balance move: it debits the TRADING account in USDT
    (where a settled trade's profit actually sits) and credits the WALLET in the
    traded coin, so it needs to be distinguishable from an ordinary same-coin
    transfer in the history.
    """

    PROFIT = "PROFIT"          # realized trading profit -> same-coin wallet
    BALANCE = "BALANCE"        # an ordinary trading <-> wallet balance move


class WalletTransfer(Base):
    """Audit row for every explicit move between the trading account and the
    wallet. Because the wallet starts at 0 and nothing auto-synchronises it,
    this table is the complete, authoritative history of how the wallet got
    funded. Written in the SAME transaction as the two balance updates, so a
    wallet can never move without a matching row.

    For an ordinary BALANCE move `amount` is the quantity of `symbol` that
    crossed over and `*_balance_after` are that same coin's two ledgers. For a
    PROFIT move the profit is denominated in USDT, so `amount` is the quantity
    of `symbol` credited to the wallet, `usd_value` is the USDT debited from
    trading, `trading_balance_after` is the trading USDT balance and
    `wallet_balance_after` is the coin's wallet balance.
    """

    __tablename__ = "wallet_transfers"

    id = Column(String, primary_key=True, default=gen_id)
    user_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    symbol = Column(String, nullable=False, index=True)
    amount = Column(Numeric(24, 8), nullable=False)          # always positive
    direction = Column(Enum(WalletTransferDirection), nullable=False)
    kind = Column(String, default=WalletTransferKind.BALANCE.value, nullable=False)
    usd_value = Column(Float, nullable=True)                 # PROFIT moves only
    trading_balance_after = Column(Numeric(24, 8), nullable=False)
    wallet_balance_after = Column(Numeric(24, 8), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


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
