from datetime import datetime
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


class TradeOut(BaseModel):
    id: str
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
