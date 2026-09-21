import asyncio
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .database import Base, engine, SessionLocal
from . import models, market_service, deposit_monitor, trading_service
from .routes import (
    auth_routes, wallet_routes, trade_routes, swap_routes, golf_routes,
    market_routes, transfer_routes, message_routes, users_routes, admin_routes,
)
from .config import (
    ALLOWED_ORIGINS, MARKET_TICK_INTERVAL_SECONDS, DEPOSIT_POLL_INTERVAL_SECONDS,
    COIN_PRICE_REFRESH_SECONDS,
)

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="VANTA TRADE — Backend (TRON Nile Testnet, Test Environment)")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

Base.metadata.create_all(bind=engine)

_db = SessionLocal()
try:
    market_service.ensure_seeded(_db)
finally:
    _db.close()

app.include_router(auth_routes.router)
app.include_router(wallet_routes.router)
app.include_router(trade_routes.router)
app.include_router(swap_routes.router)
app.include_router(golf_routes.router)
app.include_router(market_routes.router)
app.include_router(transfer_routes.router)
app.include_router(message_routes.router)
app.include_router(users_routes.router)
app.include_router(admin_routes.router)

admin_routes.bootstrap_admin()


@app.get("/api/health")
def health():
    return {"status": "ok", "network": "TRON Nile Testnet"}


# =============================================================================
# Background loops — the three things that must keep running independent of
# any single HTTP request: the authoritative demo market tick, trade
# settlement, and deposit monitoring. Each loop swallows its own exceptions
# so one bad iteration can never kill the whole background task.
# =============================================================================

async def _market_tick_loop():
    while True:
        db = SessionLocal()
        try:
            market_service.tick(db)
        except Exception as e:
            logging.error(f"[market_tick_loop] {e}")
        finally:
            db.close()
        await asyncio.sleep(MARKET_TICK_INTERVAL_SECONDS)


async def _trade_settlement_loop():
    while True:
        db = SessionLocal()
        try:
            trading_service.settle_due_trades(db)
        except Exception as e:
            logging.error(f"[trade_settlement_loop] {e}")
        finally:
            db.close()
        await asyncio.sleep(2)


async def _deposit_poll_loop():
    while True:
        db = SessionLocal()
        try:
            deposit_monitor.poll_all_deposit_addresses(db)
        except Exception as e:
            logging.error(f"[deposit_poll_loop] {e}")
        finally:
            db.close()
        await asyncio.sleep(DEPOSIT_POLL_INTERVAL_SECONDS)


async def _coin_price_refresh_loop():
    """Keeps the multi-coin USD price table fresh from CoinGecko. Never
    raises; the refresh function itself falls back to last-known prices."""
    while True:
        try:
            market_service.refresh_prices_from_coingecko()
        except Exception as e:
            logging.error(f"[coin_price_loop] {e}")
        await asyncio.sleep(COIN_PRICE_REFRESH_SECONDS)


@app.on_event("startup")
async def start_background_loops():
    asyncio.create_task(_market_tick_loop())
    asyncio.create_task(_trade_settlement_loop())
    asyncio.create_task(_deposit_poll_loop())
    asyncio.create_task(_coin_price_refresh_loop())
