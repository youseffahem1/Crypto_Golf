from datetime import datetime
from decimal import Decimal
from typing import Optional
from pydantic import BaseModel, Field


class SignupRequest(BaseModel):
    email: str = Field(min_length=3, max_length=200)
    password: str = Field(min_length=8, max_length=200)
    label: Optional[str] = Field(default=None, max_length=80)


class LoginRequest(BaseModel):
    email: str
    password: str


class UserOut(BaseModel):
    id: str
    email: str
    label: Optional[str] = None
    is_admin: bool
    usdt_balance: float
    golf_balance: float
    created_at: datetime

    class Config:
        from_attributes = True


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


class DepositAddressOut(BaseModel):
    address: str
    network: str
    token: str = "USDT_TRC20"


class DepositOut(BaseModel):
    id: str
    tx_hash: str
    token: str
    network: str
    from_address: str
    to_address: str
    amount: float
    confirmations: int
    status: str
    failure_reason: Optional[str] = None
    created_at: datetime
    confirmed_at: Optional[datetime] = None
    explorer_url: str

    class Config:
        from_attributes = True


class TradeOpenRequest(BaseModel):
    direction: str  # "UP" | "DOWN"
    amount: float = Field(gt=0)
    duration_seconds: int
    symbol: str = Field(default="GOLF", max_length=20)


class TradeCloseRequest(BaseModel):
    trade_id: str


class TradeOut(BaseModel):
    id: str
    symbol: str = "GOLF"
    direction: str
    amount: float
    entry_price: float
    exit_price: Optional[float] = None
    payout_rate: float
    duration_seconds: int
    status: str
    profit: Optional[float] = None
    opened_at: datetime
    closes_at: datetime
    settled_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class SwapRequest(BaseModel):
    from_symbol: str
    to_symbol: str
    amount: float = Field(gt=0)


class SwapOut(BaseModel):
    id: str
    from_symbol: str
    to_symbol: str
    from_amount: float
    to_amount: float
    rate: float
    created_at: datetime

    class Config:
        from_attributes = True


class CandleOut(BaseModel):
    open: float
    high: float
    low: float
    close: float
    open_time: datetime

    class Config:
        from_attributes = True


class GolfStatsOut(BaseModel):
    price_usdt: float
    market_cap_usdt: float
    liquidity_usdt: float
    volume_24h_usdt: float
    holders: int
    buyers_24h: int
    likes: int
    change_24h_pct: float
    is_demo: bool
    updated_at: datetime

    class Config:
        from_attributes = True


class WalletLayoutOut(BaseModel):
    platform_coins: list[str]
    normal_wallet_coins: list[str]
    balances: dict
    deposit_addresses: dict = Field(default_factory=dict)
    withdraw_addresses: dict = Field(default_factory=dict)


# --- Platform coin trading / investment overview ---------------------------

class PlatformCoinOut(BaseModel):
    symbol: str
    name: str
    price: float
    launch_price: float
    change_pct: float
    tradeable: bool
    balance: float = 0.0
    usdt_invested: float = 0.0
    current_value: float = 0.0
    unrealized_pnl: float = 0.0


class PlatformOverviewOut(BaseModel):
    coins: list[PlatformCoinOut]
    usdt_balance: float = 0.0
    total_platform_value: float = 0.0
    total_usdt_invested: float = 0.0
    total_unrealized_pnl: float = 0.0
    total_trade_profit: float = 0.0


class UserSearchOut(BaseModel):
    id: str
    email: str
    label: Optional[str] = None


class TransferRequest(BaseModel):
    recipient_email: str
    symbol: str
    amount: float = Field(gt=0)
    note: Optional[str] = Field(default=None, max_length=200)


class TransferOut(BaseModel):
    id: str
    symbol: str
    amount: float
    note: Optional[str] = None
    status: str
    created_at: datetime


class TransferHistoryOut(BaseModel):
    sent: list[TransferOut]
    received: list[TransferOut]


class MessageSendRequest(BaseModel):
    recipient_id: str
    body: str = Field(min_length=1, max_length=500)


class MessageOut(BaseModel):
    id: str
    sender_id: str
    sender_email: str
    sender_label: Optional[str] = None
    body: str
    is_read: bool
    moderated: bool
    created_at: datetime

    class Config:
        from_attributes = True


class ConversationOut(BaseModel):
    user_id: str
    email: str
    label: Optional[str] = None
    last_message: Optional[str] = None
    last_message_at: Optional[datetime] = None
    unread_count: int


class ReportRequest(BaseModel):
    reported_user_id: str
    reason: str  # SCAM | SPAM | ABUSE | HARASSMENT | OTHER
    message_id: Optional[str] = None
    details: Optional[str] = Field(default=None, max_length=500)


class BlockRequest(BaseModel):
    user_id: str


# =============================================================================
# Admin panel schemas — used only by admin endpoints (require_admin).
# =============================================================================

class AdminUserOut(BaseModel):
    id: str
    email: str
    label: Optional[str] = None
    is_admin: bool
    usdt_balance: float
    golf_balance: float
    created_at: datetime
    balances: dict = Field(default_factory=dict)


class AdminUserListItem(BaseModel):
    """One row of the paginated admin users list — includes lightweight
    activity counters so the table is useful without a click."""
    id: str
    email: str
    label: Optional[str] = None
    is_admin: bool
    usdt_balance: float
    golf_balance: float
    created_at: datetime
    balances: dict = Field(default_factory=dict)
    trades: int = 0
    deposits: int = 0
    last_activity: Optional[datetime] = None


class AdminUserPageOut(BaseModel):
    items: list[AdminUserListItem]
    total: int
    page: int
    page_size: int


class AdminUserDetailOut(AdminUserListItem):
    """Full detail view for a single user: identity + live balances for every
    listed coin + activity counters + the watch-only deposit address."""
    swaps: int = 0
    transfers_sent: int = 0
    transfers_received: int = 0
    balance_transactions: int = 0
    messages_sent: int = 0
    messages_received: int = 0
    deposit_address: Optional[str] = None
    usd_total: float = 0.0


class AdminTransactionItem(BaseModel):
    """One row of the merged admin transactions feed. Every entity that moves
    money in this platform is flattened into this shape: trades, verified
    deposits, swaps, internal transfers and admin balance adjustments."""
    id: str
    user_id: str
    user_email: str = ""
    type: str  # TRADE / DEPOSIT / SWAP / TRANSFER / CREDIT
    symbol: str
    amount: float
    direction: Optional[str] = None  # TRADE: UP/DOWN, TRANSFER: SENT/RECEIVED, CREDIT: IN
    status: str
    created_at: datetime
    note: Optional[str] = None
    meta: dict = Field(default_factory=dict)


class AdminTransactionPageOut(BaseModel):
    items: list[AdminTransactionItem]
    total: int
    page: int
    page_size: int


class AdminStatsOut(BaseModel):
    users: int
    admins: int
    usdt_total: float
    golf_total: float
    trades: int
    deposits: int
    deposit_total: float
    balance_adjustments: int
    transactions: int
    new_users_7d: int
    total_balance_usd: float
    recent_transactions: list[AdminTransactionItem] = Field(default_factory=list)


class AdminCreditRequest(BaseModel):
    email: Optional[str] = Field(default=None, max_length=200)
    user_id: Optional[str] = None
    symbol: str = "USDT"
    amount: float = Field(gt=0)


class BalanceAddRequest(BaseModel):
    """Add funds to a user's LIVE balance. amount is Decimal so money never
    round-trips through binary float before validation. Zero/negative values
    are accepted here and rejected with a clean 400 in the route — so the
    frontend can surface a readable message instead of an opaque 422."""
    symbol: str = Field(default="USDT", examples=["USDT", "GOLF"])
    amount: Decimal = Field(examples=["50.00"])


class BalanceAddOut(BaseModel):
    ok: bool = True
    tx_id: str
    symbol: str
    amount: str               # decimal string, e.g. "50.00"
    balance_after: dict
    user: AdminUserDetailOut


class AdminSetAdminRequest(BaseModel):
    is_admin: bool = True


class AdminActivityItem(BaseModel):
    type: str  # TRADE / DEPOSIT / SWAP / TRANSFER
    symbol: str
    amount: float
    direction: Optional[str] = None  # trade UP/DOWN, transfer SENT/RECEIVED
    status: str
    created_at: datetime
    meta: dict = Field(default_factory=dict)
