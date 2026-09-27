import asyncio
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import inspect, text

from .database import Base, engine, SessionLocal
from . import models, market_service, deposit_monitor, trading_service
from .routes import (
    auth_routes, wallet_routes, trade_routes, swap_routes, golf_routes,
    market_routes, transfer_routes, message_routes, users_routes, admin_routes,
    platform_routes,
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


def _columns(table):
    """Reflected column definitions for `table`, or None if it does not exist.

    A fresh Inspector is built on every call on purpose. The DDL below runs its
    own transactions, so reusing one long-lived Inspector would reflect a
    snapshot taken before those changes and the "does this column already
    exist?" guard would be answering from stale information."""
    insp = inspect(engine)
    if not insp.has_table(table):
        return None
    return {c["name"]: c for c in insp.get_columns(table)}


def _add_column(table, column, decl):
    """Add one column if it is missing, in its own transaction.

    One statement per transaction is deliberate: a failed ALTER aborts the whole
    surrounding PostgreSQL transaction, so batching unrelated columns together
    means a single bad declaration silently rolls back every other column in
    the batch (they are then re-attempted on the next boot, but the deployment
    that ran them stays broken until then)."""
    existing = _columns(table)
    if existing is None or column in existing:
        return
    with engine.begin() as conn:
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {decl}"))
    logging.info(f"[migrate] added {table}.{column}")


# Dialects that can change an existing column's type constraints in place.
# SQLite (the local/dev default) has no boolean type and no ALTER COLUMN at
# all, so on a SQLite database the broken `DEFAULT 0` is already the correct
# representation of FALSE and there is nothing to repair there — the NULL
# backfill still runs, being plain DML that works on every backend.
_ALTERABLE_DIALECTS = ("postgresql", "mysql", "mariadb")


def _migrate_profit_moved():
    """Bring trades.profit_moved to a real BOOLEAN, however the database got
    there.

    An earlier build declared it `BOOLEAN DEFAULT 0`. SQLite stores that happily
    (dynamic typing) so it did land there, but PostgreSQL rejects it outright:

        ERROR: column "profit_moved" is of type boolean but default expression
               is of type integer

    A rejected DDL is rolled back, so on PostgreSQL the column is simply absent
    and gets created correctly below. Any database where the broken form did
    apply instead is repaired in place rather than skipped, so all databases
    converge on the one declaration in models.py. The NOT NULL part is not
    cosmetic: the profit-to-wallet guard is `profit_moved IS FALSE`
    (swap_service.move_realized_profit) and a NULL row does not match that
    predicate, so NULLs would let the same profit be moved into a wallet twice.
    """
    existing = _columns("trades")
    if existing is None:
        return
    if "profit_moved" not in existing:
        _add_column("trades", "profit_moved", "BOOLEAN DEFAULT FALSE NOT NULL")
        return

    # The column is already there (a pre-fix database, or a later rebuild):
    # re-assert the correct definition. Both statements are no-ops when the
    # column is already exactly as declared in models.py.
    nullable = existing["profit_moved"].get("nullable", True)
    with engine.begin() as conn:
        if nullable:
            conn.execute(text("UPDATE trades SET profit_moved = FALSE WHERE profit_moved IS NULL"))
            logging.info("[migrate] trades.profit_moved: backfilled NULLs")
        if engine.dialect.name in _ALTERABLE_DIALECTS:
            if nullable:
                conn.execute(text("ALTER TABLE trades ALTER COLUMN profit_moved SET NOT NULL"))
            conn.execute(text("ALTER TABLE trades ALTER COLUMN profit_moved SET DEFAULT FALSE"))
            logging.info("[migrate] trades.profit_moved: NOT NULL, default FALSE")


def _migrate():
    """Additive, non-destructive migrations for pre-existing databases
    (create_all does not alter existing tables). Every existing trade is a
    GOLF trade, so the new column is backfilled with 'GOLF' — historical
    settlement semantics are preserved exactly.

    Also adds the WALLET half of the two-ledger balance model. The existing
    columns stay the TRADING balances, and the new wallet columns default to
    0, so every pre-existing account starts with an empty wallet — exactly the
    required behaviour: a wallet only ever holds funds the user explicitly
    moved into it."""
    if _columns("trades") is None:
        return
    if "symbol" not in _columns("trades"):
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE trades ADD COLUMN symbol VARCHAR(20) DEFAULT 'GOLF'"))
            conn.execute(text("UPDATE trades SET symbol = 'GOLF' WHERE symbol IS NULL OR symbol = ''"))
        logging.info("[migrate] added trades.symbol = 'GOLF'")

    # --- Split every balance into a TRADING balance and a WALLET balance -----
    additive = [
        ("users", "usdt_wallet_balance"),
        ("users", "golf_wallet_balance"),
        ("coin_balances", "wallet_balance"),
    ]
    for table, column in additive:
        existing = _columns(table)
        if existing is None or column in existing:
            continue
        with engine.begin() as conn:
            conn.execute(text(
                f"ALTER TABLE {table} ADD COLUMN {column} FLOAT DEFAULT 0 NOT NULL"
            ))
            conn.execute(text(f"UPDATE {table} SET {column} = 0 WHERE {column} IS NULL"))
        logging.info(f"[migrate] added {table}.{column} = 0 (wallet starts empty)")

    # --- Realized profit classification --------------------------------------
    # trades.profit_moved marks a winning trade whose profit has already been
    # moved into the wallet. Every pre-existing trade predates the feature, so
    # nothing has been moved yet and FALSE is the correct backfill: their
    # profit is still fully available to transfer.
    #
    # wallet_transfers.kind / .usd_value let the audit history tell a realized
    # profit move (USDT out of trading, coin into the wallet) apart from an
    # ordinary same-coin balance move. Existing rows predate the distinction, so
    # they are ordinary balance moves.
    _migrate_profit_moved()
    for table, column, decl in [
        ("wallet_transfers", "kind", "VARCHAR(20) DEFAULT 'BALANCE' NOT NULL"),
        ("wallet_transfers", "usd_value", "FLOAT"),
    ]:
        _add_column(table, column, decl)


_migrate()

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
app.include_router(platform_routes.router)

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
