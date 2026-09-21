import os

# =============================================================================
# VANTA TRADE — backend configuration. Every secret comes from an environment
# variable (see .env.example) — nothing sensitive is hardcoded here.
# =============================================================================

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./vanta.db")

APP_SECRET_KEY = os.environ.get("APP_SECRET_KEY", "dev-secret-change-me")
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_MINUTES = int(os.environ.get("JWT_EXPIRE_MINUTES", str(60 * 24 * 7)))  # 7 days

ALLOWED_ORIGINS = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "*").split(",") if o.strip()]

# --- TRON Nile Testnet ONLY. Never Mainnet. ---------------------------------
# Hardcoded (not an env var) so a typo in an env var can never accidentally
# point this at Mainnet — see the warning in tron_service.py before ever
# changing this.
TRON_NETWORK = "nile"
TRON_NILE_API_BASE = os.environ.get("TRON_NILE_API_BASE", "https://nile.trongrid.io")
# Nile testnet has no strict rate limit requiring a key, but you can set one.
TRON_API_KEY = os.environ.get("TRON_API_KEY", "")

# Official USDT-TRC20 contract address ON NILE TESTNET (verified against
# nileex.io's official faucet page — NOT the real mainnet USDT contract,
# which is a different address entirely). Env-overridable so an operator
# can correct it without a code change if Nile's testnet USDT contract is
# ever redeployed.
USDT_TRC20_CONTRACT_NILE = os.environ.get(
    "USDT_TRC20_CONTRACT_NILE", "TXYZopYRdj2D9XRtbG411XZZ3kM5VkAeBf"
)
USDT_TRC20_DECIMALS = 6

TRON_REQUIRED_CONFIRMATIONS = int(os.environ.get("TRON_REQUIRED_CONFIRMATIONS", "19"))
DEPOSIT_POLL_INTERVAL_SECONDS = int(os.environ.get("DEPOSIT_POLL_INTERVAL_SECONDS", "20"))

# --- Trading (virtual balance only — never real funds) ---------------------
TRADE_PAYOUT_RATE = float(os.environ.get("TRADE_PAYOUT_RATE", "0.93"))  # matches the existing frontend constant
MIN_TRADE_AMOUNT = float(os.environ.get("MIN_TRADE_AMOUNT", "1"))
MAX_TRADE_AMOUNT = float(os.environ.get("MAX_TRADE_AMOUNT", "100000"))

# How often the server re-syncs the multi-coin USD price table from
# CoinGecko (soft failure: falls back to last known / default prices).
COIN_PRICE_REFRESH_SECONDS = int(os.environ.get("COIN_PRICE_REFRESH_SECONDS", "180"))

# --- Wallet structure -------------------------------------------------------
# Currency groups that drive the two-wallet layout served to the frontend
# (see GET /api/wallet/layout). A coin is either a platform-issued coin whose
# home is the Trading/Investment wallet, or an established cryptocurrency
# that lives in the Normal wallet. Every symbol listed in one of these two
# groups must also exist in market_service.SUPPORTED_SYMBOLS.
PLATFORM_COINS = os.environ.get("PLATFORM_COINS", "GOLF")
PLATFORM_COINS = tuple(c.strip().upper() for c in PLATFORM_COINS.split(",") if c.strip())
NORMAL_WALLET_COINS = os.environ.get(
    "NORMAL_WALLET_COINS",
    "USDT,USDC,BTC,ETH,BNB,SOL,LTC,TRX,XRP,DOGE,ADA,LINK",
)
NORMAL_WALLET_COINS = tuple(c.strip().upper() for c in NORMAL_WALLET_COINS.split(",") if c.strip())

# --- Demo market (authoritative server-side price simulator) ---------------
# The chart's *rendering* code in the frontend is untouched per spec — this
# is only the price series that actually determines trade win/loss, kept
# authoritative on the server so a client can never influence its own
# outcome. See market_service.py.
MARKET_TICK_INTERVAL_SECONDS = float(os.environ.get("MARKET_TICK_INTERVAL_SECONDS", "1"))
MARKET_STARTING_PRICE = float(os.environ.get("MARKET_STARTING_PRICE", "4052.0"))

ENVIRONMENT = os.environ.get("ENVIRONMENT", "test")
