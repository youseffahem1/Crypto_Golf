"""
Opens live-value positions and settles them against the server's OWN
authoritative price (market_service.get_current_price) — the client never
supplies entry_price or exit_price, and settlement never trusts anything
the client sends at close time.

Trade model (momentum/exit style):
  * open_trade()      debits the stake and records the entry price.
  * close_trade()     closes a still-OPEN position at the server's live price
                      and credits that value back to the balance. Selling
                      while the price is up banks a gain; selling while it's
                      down salvages what's left.
  * close_all_trades() closes every OPEN position in ONE transaction.
  * expire_due_trades() closes every OPEN position whose duration has elapsed,
                      on the server's own schedule, at the server's own price.

LIFECYCLE: OPEN -> WON / LOST.

A position leaves OPEN in exactly one of two ways, and both end in the same
place — the same pricing function, the same fields, the same one-time credit:

  * the user closes it early (close_trade / close_all_trades), or
  * its chosen duration elapses and `expire_due_trades()` settles it.

`closes_at` is what the second rule keys on. It is written once at open as
`opened_at + duration_seconds` and is the server's own deadline: a 1-minute
trade has `closes_at == opened_at + 60s` and is settled the moment that
passes. The frontend draws its countdown from the same timestamp, so the
number on screen and the moment the server acts on are the same instant.

Expiring a position is NOT a forfeiture. This is the important part, and it
is the exact opposite of the old `settle_due_trades()` that was removed
earlier: that function force-booked any position whose `closes_at` had passed
as a total loss (`profit = -amount`), so waiting for the timer destroyed the
stake and turned a lucky position into a loss. Expiry here runs the position
through the SAME `_mark_to_market` a manual sell runs through — the server's
own live price, the trade's own entry price, the trade's own direction. A
1-minute BUY that is up when its minute is up books a win; a SELL that is
down books a win. The clock decides WHEN a position ends, never WHAT it is
worth.

Three rules make the whole lifecycle hold:

  * THE SERVER OWNS THE RESULT. The exit price is read from the server's own
    feed and the credited value is derived from it together with the trade's
    own direction. A `value` sent by the client is accepted by the request
    schema for backward compatibility and then ignored, so no client can name
    the figure it is paid.
  * THE SERVER OWNS THE DEADLINE. `closes_at` is written by the server, and
    `expire_due_trades()` is the only thing that acts on it. The client
    reports "this one's up" by calling the ordinary close endpoint — which
    settles at the server's price under the same rules — but the server would
    have settled the position on its own schedule regardless, so a client
    that never speaks, loses its connection, or lies about the time changes
    nothing.
  * A CLOSE APPLIES EXACTLY ONCE. Settlement locks the trade row, so two
    concurrent closes of the same position cannot both credit the balance;
    a repeat close returns the already-recorded result and moves no money.
    The expiry sweep takes the same locks and the same status guard, so a
    position that a user SELLs and the sweep reaches in the same instant is
    settled by one of them and replayed by the other — never paid twice.

Every platform coin (GOLF, NOVA, ABC, …) has its own authoritative walk
and its own symbol on the trade; the rules are identical for all of them.
"""
import logging
import threading
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from . import models, market_service
from .config import TRADE_PAYOUT_RATE, MIN_TRADE_AMOUNT, MAX_TRADE_AMOUNT, PLATFORM_COINS

TRADEABLE_SYMBOLS = tuple(s for s in PLATFORM_COINS if s)

# Serialises the expiry sweep inside this process. The row locks are what make
# settlement exactly-once against the DATABASE (PostgreSQL in production), but
# SQLite has no row locks, and the sweep is reachable from both the background
# loop and the read endpoints — so two request threads could otherwise read the
# same OPEN row and both credit. This lock makes it one at a time, which is
# enough: the sweep is cheap when there is nothing due.
_expiry_sweep_lock = threading.Lock()


class TradingError(Exception):
    pass


# =============================================================================
# Row locking — the two writes that must never happen twice
# =============================================================================
# Crediting a balance and marking a position closed are two halves of ONE
# fact: "this stake was returned at this value". If both halves were left to
# race, a double-click (or the per-position Close button firing alongside the
# poller) could credit the balance twice for a single position.
#
# `with_for_update()` is what makes the close atomic, and it is a real
# `SELECT ... FOR UPDATE` on PostgreSQL, which is the deployed database. SQLite
# has no row locks, so the statement is a no-op there and the code falls back
# to an ordinary read; local dev is single-writer and cannot interleave two
# requests on the same session anyway.
#
# LOCK ORDER IS ALWAYS trade-then-user, and every path that settles a position
# obeys it: close_trade(), close_all_trades() and expire_due_trades() all take
# the position rows before the balance row. That is what stops a user's SELL
# and the expiry sweep — the two writes most likely to meet on the same
# position in the same second — from grabbing the same two rows in opposite
# orders and deadlocking. open_trade() is exempt because it creates a position
# rather than settling one, so it has no position row to take first.

def _lock_trade(db: Session, user_id: str, trade_id: str) -> models.Trade | None:
    """This user's own position, locked for update. Returns None if the trade
    does not exist OR belongs to somebody else — the two are deliberately
    indistinguishable, so the endpoint cannot be used to probe for another
    account's trade ids."""
    try:
        return (
            db.query(models.Trade)
            .filter_by(id=trade_id, user_id=user_id)
            .with_for_update()
            .first()
        )
    except Exception:
        db.rollback()
        return db.query(models.Trade).filter_by(id=trade_id, user_id=user_id).first()


def _lock_user(db: Session, user_id: str) -> models.User | None:
    try:
        return db.query(models.User).filter_by(id=user_id).with_for_update().first()
    except Exception:
        db.rollback()
        return db.query(models.User).filter_by(id=user_id).first()


# =============================================================================
# Mark-to-market — the one place a position's result is decided
# =============================================================================
# Both close paths go through here, so a single position and a Close All
# cannot disagree about what a position is worth.

def _is_sell(direction) -> bool:
    """Is this position a SELL/DOWN bet?

    `Trade.direction` is an Enum column, and a str-mixin enum's `str()` is the
    member path ("TradeDirection.DOWN"), not its value — so comparing against
    "DOWN" with str() silently returns False and every SELL would settle as if
    it were a BUY. Read `.value` when there is one, and fall back to the raw
    string for the plain-string case (older rows, direct construction)."""
    value = getattr(direction, "value", direction)
    return str(value or "").strip().upper() in ("DOWN", "SELL")


def _exit_value(amount: float, entry: float, exit_price: float, direction) -> float:
    """What the stake is worth right now, from the server's own price.

    A BUY (UP) rises with the price: value = amount * exit / entry. A SELL
    (DOWN) is the mirror image, because it wins when the price falls:
    value = amount * (2 - exit / entry), floored at 1% of the stake so a
    position that has gone completely the wrong way still returns something
    rather than a negative balance.
    """
    amount = float(amount or 0.0)
    entry = float(entry or 0.0)
    exit_price = float(exit_price or 0.0)
    if amount <= 0:
        return 0.0
    if entry <= 0 or exit_price <= 0:
        return amount
    ratio = exit_price / entry
    if _is_sell(direction):
        value = amount * (2.0 - ratio)
        floor = amount * 0.01
        return value if value > floor else floor
    return amount * ratio


def _mark_to_market(db: Session, trade: models.Trade) -> float:
    """Price an OPEN position with the server's feed, write the realized result
    onto the row, and return the amount to credit. Does not touch any balance —
    the caller owns the commit, so a single transaction covers the position
    rows and the balance row together."""
    symbol = (trade.symbol or "GOLF").strip().upper()
    try:
        exit_price = float(market_service.get_current_price(db, symbol) or 0.0)
    except Exception:
        exit_price = 0.0

    entry = float(trade.entry_price or 0.0) or exit_price or 1.0
    if exit_price <= 0:
        exit_price = entry

    amount = float(trade.amount or 0.0)
    payout = _exit_value(amount, entry, exit_price, trade.direction)
    profit = round(payout - amount, 6)

    trade.exit_price = round(exit_price, 8)
    trade.profit = profit
    trade.status = models.TradeStatus.WON if profit >= 0 else models.TradeStatus.LOST
    return payout


# =============================================================================
# Realized result classification — PROFIT and LOSS are two separate totals
# =============================================================================
# A closed trade has ONE signed `profit`. Reporting that signed number as
# "PROFIT" is what produced a card reading "PROFIT -$681.01": a loss dressed up
# as a profit, with a "Move to Wallet" button underneath it inviting the user
# to transfer it. The classification below makes that impossible by never
# producing a negative profit in the first place:
#
#     win   -> profit +$20.00   loss  $0.00
#     loss  -> profit  $0.00    loss -$60.00
#     mixed -> profit +$20.00   loss -$60.00     (never netted to -$40.00)
#
# `available` is `profit` minus whatever has already been moved into the wallet,
# so it is the only figure "Move to Wallet" is ever allowed to act on.

def _closed_trades(db: Session, user_id: str, symbol: str | None = None):
    q = db.query(models.Trade).filter(
        models.Trade.user_id == user_id,
        models.Trade.status.in_([models.TradeStatus.WON, models.TradeStatus.LOST]),
    )
    if symbol:
        q = q.filter(models.Trade.symbol == symbol.strip().upper())
    return q.all()


def realized_split(db: Session, user_id: str, symbol: str | None = None) -> dict:
    """Realized trading result for `user_id` as separate profit / loss totals.

    Returns profit >= 0, loss <= 0 and their net, plus `available` — the profit
    that has not been moved to the wallet yet. Losses never reduce profit and
    are never transferable, so a losing account reports $0.00 available rather
    than a negative, un-transferable amount.

    `profit` is the all-time realized total and deliberately still counts a
    winner whose profit has already been transferred: moving money to the wallet
    does not rewrite the trading history. `available` is the part of it that is
    still movable, and it is what the PROFIT card reads — see the card in
    frontend/index.html, which is the one place that shows $0.00 after a move.

    With no `symbol` the result also carries `by_symbol`, so per-coin figures
    come from the very same classification pass as the total rather than a
    second, independently-written one."""
    profit = 0.0
    loss = 0.0
    available = 0.0
    count = 0
    by_symbol: dict[str, dict] = {}

    for t in _closed_trades(db, user_id, symbol):
        p = float(t.profit or 0.0)
        sym = (t.symbol or "GOLF").strip().upper()
        count += 1

        if p > 0:
            profit += p
            if not t.profit_moved:
                available += p
        elif p < 0:
            loss += p

        if symbol is None:
            row = by_symbol.setdefault(sym, {"profit": 0.0, "loss": 0.0, "available": 0.0})
            if p > 0:
                row["profit"] += p
                if not t.profit_moved:
                    row["available"] += p
            elif p < 0:
                row["loss"] += p

    out = {
        "profit": round(profit, 8),
        "loss": round(loss, 8),
        "net": round(profit + loss, 8),
        "available": round(available, 8),
        "count": count,
    }
    if symbol is None:
        out["by_symbol"] = {
            sym: {
                "profit": round(row["profit"], 8),
                "loss": round(row["loss"], 8),
                "available": round(row["available"], 8),
            }
            for sym, row in by_symbol.items()
        }
    return out


def open_trade(
    db: Session, user_id: str, direction: str, amount: float, duration_seconds: int, symbol: str = "GOLF"
) -> models.Trade:
    symbol = (symbol or "GOLF").strip().upper()
    if direction not in ("UP", "DOWN"):
        raise TradingError("Invalid direction")
    if symbol not in TRADEABLE_SYMBOLS:
        raise TradingError(f"{symbol} is not a tradeable platform coin")
    if amount < MIN_TRADE_AMOUNT or amount > MAX_TRADE_AMOUNT:
        raise TradingError(f"Amount must be between {MIN_TRADE_AMOUNT} and {MAX_TRADE_AMOUNT}")
    if duration_seconds not in (60, 300, 900, 1800):
        raise TradingError("Invalid duration")

    user = _lock_user(db, user_id)
    if not user:
        raise TradingError("User not found")
    if float(user.usdt_balance or 0.0) < amount:
        raise TradingError("Insufficient virtual USDT balance")

    entry_price = market_service.get_current_price(db, symbol)
    now = datetime.utcnow()

    user.usdt_balance = float(user.usdt_balance or 0.0) - float(amount)
    trade = models.Trade(
        user_id=user_id, symbol=symbol, direction=direction, amount=amount, entry_price=entry_price,
        payout_rate=TRADE_PAYOUT_RATE, duration_seconds=duration_seconds,
        status=models.TradeStatus.OPEN, opened_at=now,
        # The position's deadline, and the only one that exists: the server's
        # own expiry sweep settles this position the moment it passes, at the
        # server's own price (see expire_due_trades). It is also the exact
        # timestamp the client counts down to, so the on-screen number and the
        # moment the server acts are the same instant. A 1-minute trade is
        # therefore 60.0 seconds from `now` — no rounding, no client clock.
        closes_at=now + timedelta(seconds=duration_seconds),
    )
    db.add(trade)
    db.commit()
    db.refresh(trade)
    return trade


def close_trade(
    db: Session, user_id: str, trade_id: str, value: float | None = None
) -> models.Trade:
    """Close one still-OPEN position at the server's own live price and credit
    the result to the trading balance, in a single atomic transaction.

    Four properties, all of them deliberate:

      * SERVER-AUTHORITATIVE. The credited figure comes from
        `market_service.get_current_price` plus the trade's own entry price and
        direction, via `_mark_to_market`. The `value` argument is accepted for
        backward compatibility with older clients and is IGNORED — a client
        cannot name the amount it is paid, which is what made the old
        1%–10,000% clamp exploitable.
      * DIRECTION-AWARE. A SELL wins when the price falls and is priced as the
        mirror of a BUY, instead of being credited as if it were a BUY.
      * EXACTLY ONCE. The position row is locked first, so two simultaneous
        closes cannot both credit the balance. A repeat close of an
        already-settled position is NOT an error and moves no money: it
        returns the recorded result, which is what makes a double-click on
        Close, a retry after a dropped response, and a user SELL racing the
        expiry sweep all safe.
      * THE CLOCK IS NOT A FORFEITURE. Closing early and being closed by
        `closes_at` run the identical pricing; this function neither reads
        `closes_at` nor treats lateness as a loss, so a position closed long
        after it was opened settles at the live market like any other.

    Never called with client-supplied prices."""
    trade = _lock_trade(db, user_id, trade_id)
    if not trade:
        # Missing and not-yours are the same answer on purpose.
        raise TradingError("Trade not found")

    if trade.status != models.TradeStatus.OPEN:
        # Already settled — idempotent replay. The result is already on the row
        # and the balance was already credited by whoever settled it, so
        # returning here is what guarantees "close applies exactly once".
        db.rollback()
        db.refresh(trade)
        return trade

    user = _lock_user(db, user_id)
    if not user:
        raise TradingError("User not found")

    payout = _mark_to_market(db, trade)
    user.usdt_balance = float(user.usdt_balance or 0.0) + payout
    trade.settled_at = datetime.utcnow()
    trade.close_reason = "SOLD"

    # One commit for the position row AND the balance row: there is no window
    # in which one has been written without the other.
    db.commit()
    db.refresh(trade)
    return trade


def close_all_trades(db: Session, user_id: str, symbol: str | None = None) -> list[models.Trade]:
    """Close EVERY open position for this user in ONE atomic transaction.

    This is what the "CLOSE ALL" button calls. It is the bulk sibling of
    close_trade() and shares its pricing, so closing one position and closing
    all of them can never produce different numbers for the same position.

    Unlike a loop of single closes, this is a single lock set and a single
    commit: the whole batch either lands in full or not at all. Positions are
    taken in a stable order (oldest first) purely so two concurrent CLOSE ALL
    presses can't deadlock waiting on each other.

    `symbol` restricts the batch to one instrument; omitted, it closes the
    user's whole book. Trades already closed are excluded, so calling this
    twice credits the balance once. A user with nothing open is not an error —
    it returns an empty list."""
    q = db.query(models.Trade).filter(
        models.Trade.user_id == user_id,
        models.Trade.status == models.TradeStatus.OPEN,
    )
    if symbol:
        sym = symbol.strip().upper()
        q = q.filter(models.Trade.symbol == sym)
    rows = q.order_by(models.Trade.opened_at.asc(), models.Trade.id.asc()).all()
    if not rows:
        return []

    # LOCK ORDER: positions first, THEN the balance row — the invariant this
    # module documents, shared with close_trade() and expire_due_trades().
    # An earlier draft locked the user first and then re-read the positions
    # with a plain count, which both inverted the order against a manual SELL
    # and left the "still open?" check racing the very rows it was guarding.
    # A real FOR UPDATE per row makes the check a fact rather than a guess.
    locked: list[models.Trade] = []
    for trade in rows:
        live = _lock_trade(db, user_id, trade.id)
        if live and live.status == models.TradeStatus.OPEN:
            locked.append(live)
    if not locked:
        return []

    user = _lock_user(db, user_id)
    if not user:
        db.rollback()
        raise TradingError("User not found")

    now = datetime.utcnow()
    credited = 0.0
    for live in locked:
        credited += _mark_to_market(db, live)
        live.settled_at = now
        live.close_reason = "SOLD"

    user.usdt_balance = float(user.usdt_balance or 0.0) + credited
    db.commit()
    for live in locked:
        db.refresh(live)
    return locked


# =============================================================================
# Expiry — the position's duration running out
# =============================================================================
# This is the one function that turns a past `closes_at` into a settled trade.
# It is deliberately the SAME settlement as a manual close, differing only in
# who asked:
#
#     close_trade()          "the user pressed SELL"
#     expire_due_trades()    "the position's duration elapsed"
#
# and not in how the result is decided. Both price through `_mark_to_market`,
# so an expiry reads the server's own live feed and applies the trade's own
# direction exactly as a sale would. That is the whole point, and it is what the
# old `settle_due_trades()` got wrong: that function booked any elapsed
# position as `profit = -amount`, a flat total loss, so a 1-minute trade that
# was $5 in the money at the 60-second mark was destroyed by the clock instead
# of banked. Here the clock decides WHEN, never WHAT. Nothing about reaching
# `closes_at` forfeits a stake.
#
# WHY IT IS SAFE TO CALL FREQUENTLY, FROM ANYWHERE
# Every write goes through the same two guards as the manual paths:
#
#   * the position row is locked, and its status re-read under that lock, so a
#     trade that is no longer OPEN is skipped rather than paid again;
#   * all of a user's positions are settled in ONE commit, and the balance is
#     credited once for that whole batch, so a failure part-way through cannot
#     leave some settled and others not;
#   * `_expiry_sweep_lock` keeps two sweeps in this process from interleaving on
#     a database without row locks (SQLite).
#
# So the caller may run it on a timer, on a read, or both, and run it as often
# as it likes: the worst a redundant call can do is find nothing due.

def due_trades_query(db: Session, now: datetime):
    """The OPEN positions whose duration has already elapsed, oldest deadline
    first. One place, so the timer and the read paths can never disagree about
    which positions are due."""
    return (
        db.query(models.Trade)
        .filter(
            models.Trade.status == models.TradeStatus.OPEN,
            models.Trade.closes_at <= now,
        )
        .order_by(models.Trade.closes_at.asc(), models.Trade.id.asc())
    )


def expire_due_trades(db: Session, now: datetime | None = None, limit: int = 500) -> list[models.Trade]:
    """Settle every OPEN position whose `closes_at` has passed. Returns the
    positions settled by THIS call (empty when there was nothing to do).

    Called from two places on purpose, so expiry never depends on a timer being
    alive:
      * `_trade_expiry_loop` in main.py, once a second, so a position ends
        promptly even if nobody is looking at the page;
      * the read endpoints, so a client that refreshes, reconnects or switches
        tabs finds the expired position already closed and never sees it as
        OPEN. The second caller is what makes "the server is the source of
        truth" true even for a server that was restarted mid-trade.

    `now` is injectable so a test can settle a position without sleeping 60
    seconds. Never raises: a failure here is logged and the positions stay OPEN
    for the next sweep, which is the safe direction to fail in — an unsettled
    position is retried, a half-credited one is not.
    """
    now = now or datetime.utcnow()

    # Cheap check before taking the lock or touching a row: the overwhelmingly
    # common case is that nothing is due, and this keeps the per-second loop
    # from doing real work every second.
    if due_trades_query(db, now).first() is None:
        return []

    if not _expiry_sweep_lock.acquire(blocking=False):
        # Another sweep in this process is already working through the same
        # set. Skipping is correct, not a lost update: that sweep either
        # settles this position or leaves it for the next one, and the caller
        # re-reads the open list either way.
        return []
    try:
        rows = due_trades_query(db, now).limit(limit).all()
        if not rows:
            return []

        # Oldest deadline first is a stable order, so two overlapping sweeps
        # cannot deadlock waiting on the same two rows in opposite directions
        # (same rule as close_all_trades).
        by_user: dict[str, list[models.Trade]] = {}
        for trade in rows:
            by_user.setdefault(trade.user_id, []).append(trade)

        settled: list[models.Trade] = []
        for user_id in sorted(by_user):
            candidates = by_user[user_id]

            # LOCK ORDER: positions first, THEN the balance row — the same
            # order close_trade() uses, and the invariant this module
            # documents. An earlier draft of this function locked the user
            # first to group the credit, which put it in the exact opposite
            # order to a manual SELL: the sweep holding the user and waiting
            # for a position, the SELL holding that position and waiting for
            # the user. That is a textbook AB-BA deadlock on PostgreSQL, and
            # a user SELL racing expiry is the single most likely race there
            # is, so the credit grouping is not worth it.
            live_trades: list[models.Trade] = []
            for trade in candidates:
                # Re-read under the lock. A user's SELL, a CLOSE ALL or an
                # earlier sweep may have settled this position between the
                # query above and this line; status is the guard that makes
                # "exactly once" true, and skipping is a no-op, not an error.
                live = _lock_trade(db, user_id, trade.id)
                if live and live.status == models.TradeStatus.OPEN:
                    live_trades.append(live)

            if not live_trades:
                continue

            user = _lock_user(db, user_id)
            if not user:
                # No balance row to credit. Release the position locks we just
                # took and leave these positions OPEN rather than settle them
                # into a hole; nothing is lost, because the stake was never
                # debited from a row that does not exist.
                db.rollback()
                continue

            credited = 0.0
            for live in live_trades:
                credited += _mark_to_market(db, live)
                live.settled_at = now
                live.close_reason = "EXPIRED"

            # One balance write for the whole batch, in the same commit as the
            # position rows — there is no instant at which a position is closed
            # and its money has not been credited, or the reverse.
            user.usdt_balance = float(user.usdt_balance or 0.0) + credited
            try:
                db.commit()
            except Exception:
                # Never leave a balance credited against positions that are
                # still OPEN: roll the whole batch back and let the next sweep
                # retry it as one unit.
                db.rollback()
                continue
            for live in live_trades:
                db.refresh(live)
                settled.append(live)

        return settled
    except Exception:
        db.rollback()
        logging.error("[expire_due_trades] sweep failed; positions stay open for retry")
        return []
    finally:
        _expiry_sweep_lock.release()

