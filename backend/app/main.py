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
    COIN_PRICE_REFRESH_SECONDS, TRADE_EXPIRY_SWEEP_SECONDS, AUTO_SETTLE_ON_EXPIRE,
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

    # --- Per-account Wallet PIN ------------------------------------------------
    # Adds the stored side of the Wallet PIN the page already collects: the
    # hash only, never the digits. Additive and nullable, so every pre-existing
    # row is simply "no PIN set yet" and no data is read, rewritten or removed.
    _add_column("users", "pin_hash", "VARCHAR(255)")

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
        # trades.close_reason records HOW a position left OPEN. "SOLD" is what
        # the shipping product writes, on the manual close. "EXPIRED" is legacy
        # and only reachable with AUTO_SETTLE_ON_EXPIRE on; a duration running
        # out no longer closes anything on its own. Nullable, so every
        # pre-existing trade reads NULL
        # and nothing is rewritten — and NULL is correct for them, because
        # they predate the distinction. No query filters on it: `status`
        # remains the authority for whether a position is settled, so adding
        # the column changes no existing behaviour whatsoever.
        ("trades", "close_reason", "VARCHAR(20)"),
        # The FROZEN result of a position whose duration ended: the price the
        # server recorded at 00:00 and the profit that price implies. All three
        # are nullable and start NULL, which correctly reads as "this position
        # has not reached its deadline yet". Deliberately separate from
        # exit_price / profit / status: those are the REALIZED record and are
        # what paint the top PROFIT and LOSS cards, so freezing must never
        # write them. NULL on every pre-existing trade is also correct — those
        # rows predate the promise, and a trade that was already closed has
        # nothing left to freeze.
        ("trades", "frozen_exit_price", "FLOAT"),
        ("trades", "frozen_profit", "FLOAT"),
        ("trades", "frozen_at", "DATETIME"),
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
# Background loops — the things that must keep running independent of any
# single HTTP request: the authoritative demo market tick, the trade freeze,
# deposit monitoring, and the CoinGecko price table. Each loop swallows its own
# exceptions so one bad iteration can never kill the whole background task.
#
# NO TIMER SETTLES A TRADE. What a timer may do is FREEZE, which is a different
# act entirely:
#
#     00:00  ->  the duration is finished, and the server records the price.
#                Nothing is paid, booked, credited or filed.
#     CLOSE  ->  the user collects it, and that is what books a result.
#
# So a position whose `closes_at` has passed stays OPEN, holds the balance
# reserved for it, is absent from the top PROFIT / LOSS cards, and is absent
# from closed history — but its result is LOCKED, and the locked figure is what
# the user sees from 00:00 and what they are ultimately paid.
#
# WHY THE FREEZE IS ALLOWED ON A TIMER WHEN SETTLEMENT IS NOT
# A freeze writes a price nobody has been charged for. It cannot lose a user
# money, it cannot move a card, and it cannot close a position — so running it
# promptly is safe. And it has to run promptly: the promise is that the figure
# at 00:00 is the figure paid, which only holds if the price is captured near
# the deadline rather than whenever the user next happened to reload. The read
# endpoints run the same sweep, so a position is frozen even if this loop is
# behind — a close can never find an unpriced finished position, because
# close_trade() freezes one itself if it has to.
#
# `_trade_expiry_loop` — the settlement loop, kept only for the
# AUTO_SETTLE_ON_EXPIRE opt-in — is not started at all unless that flag is on.
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


async def _trade_freeze_loop():
    """Always started. Records the price of every position whose duration has
    ended, and nothing else.

    The try/except is kept: a failing sweep must never kill the loop or take
    down the process with it. A position missed by one iteration is picked up
    by the next one, and by the read endpoints, and by close_trade() itself."""
    while True:
        db = SessionLocal()
        try:
            trading_service.freeze_due_trades(db)
        except Exception as e:
            logging.error(f"[trade_freeze_loop] {e}")
        finally:
            db.close()
        await asyncio.sleep(TRADE_EXPIRY_SWEEP_SECONDS)


async def _trade_expiry_loop():
    """Only ever started when config.AUTO_SETTLE_ON_EXPIRE is on.

    The try/except is kept: a failing sweep must never kill the loop or take
    down the process with it, because a half-settled batch is not retried.
    """
    while True:
        db = SessionLocal()
        try:
            trading_service.expire_due_trades(db)
        except Exception as e:
            logging.error(f"[trade_expiry_loop] {e}")
        finally:
            db.close()
        await asyncio.sleep(TRADE_EXPIRY_SWEEP_SECONDS)


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
    asyncio.create_task(_deposit_poll_loop())
    asyncio.create_task(_coin_price_refresh_loop())
    # Always on, and always safe: this records the price of a finished position
    # and moves no money. It is what makes the figure shown at 00:00 the figure
    # that is later paid.
    asyncio.create_task(_trade_freeze_loop())
    # Opt-in only. Off by default, and that is the point: a trade's duration
    # ending settles nothing. With this line absent, no background task in this
    # process can turn a past `closes_at` into a closed trade — the user
    # closing the position is the only event that books a result.
    if AUTO_SETTLE_ON_EXPIRE:
        asyncio.create_task(_trade_expiry_loop())
