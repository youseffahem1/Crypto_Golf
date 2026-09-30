"""
Opens live-value positions and settles them against the server's OWN
authoritative price (market_service.get_current_price) — the client never
supplies entry_price or exit_price, and settlement never trusts anything
the client sends at close time.

Trade model (momentum/exit style):
  * open_trade()      debits the stake and records the entry price.
  * close_trade()     closes a still-OPEN position and credits that value back
                       to the balance. A position still inside its duration is
                       priced off the live feed; a position past `closes_at` is
                       paid its FROZEN price, the one recorded at 00:00.
  * close_all_trades() collects every FINISHED position in ONE transaction.
  * freeze_due_trades() records the price of every position whose `closes_at`
                       has passed, and does nothing else.
  * expire_due_trades() is the old settle-on-expiry machinery. It is INERT by
                       default (config.AUTO_SETTLE_ON_EXPIRE is off) and takes no
                       action of any kind.

LIFECYCLE: OPEN -> WON / LOST.

A position leaves OPEN in exactly ONE way: the user collects it.

  * close_trade() collects a single still-OPEN position, or
  * close_all_trades() collects every FINISHED position in ONE transaction.

REACHING `closes_at` IS NOT ONE OF THOSE WAYS. `closes_at` is a deadline for how
long the position runs, and when it passes the server RECORDS A PRICE — and
records nothing else. The position stays OPEN, the balance is untouched, and the
account's Profit and Loss do not move. The frontend's countdown stops at 00:00
for the same reason and does not post a close of its own.

    00:00            -> the duration is finished; the price is recorded and
                        frozen. The trade leaves the chart, stays in the
                        positions list, and is still closable.
    CLOSE (the user) -> book the recorded P/L, credit the balance, file it.

So the clock fixes the PRICE and the user fixes the PAYMENT, and the two are
deliberately separate: nothing the server does on a timer may move the user's
money. The recorded figure is immutable, so however long the user waits, and
however far the market travels in the meantime, they are paid exactly what they
were shown. A position still inside its duration is priced live, as it always
was, and Close All deliberately does not touch those — it is a collect button,
not a liquidate button.

This is the exact opposite of the old `settle_due_trades()` that was removed
earlier, which force-booked any elapsed position as a total loss
(`profit = -amount`) on a timer, destroying a stake the moment the clock ran
out. Here the clock records a number; the user decides when money moves, and
when it does it is computed by the SAME pricing a manual sell uses — from the
server's own feed, the trade's own entry price, the trade's own direction.

One honest caveat on "the price at 00:00". The market is a random walk, so
there is no historical price to look up afterwards; the figure recorded is the
one the server was quoting at the moment it saw the deadline end. That is
bounded by the sweep interval (1s by default — the same cadence the market
itself ticks), not by the sweep being punctual. What is absolute, and what the
tests pin, is that the number cannot move afterwards. Bounded lateness is a
measurement; a promise that drifts is a lie.

Three rules make the whole lifecycle hold:

  * THE SERVER OWNS THE RESULT. The exit price is read from the server's own
    feed and the credited value is derived from it together with the trade's
    own direction. A `value` sent by the client is accepted by the request
    schema for backward compatibility and then ignored, so no client can name
    the figure it is paid.
  * THE SERVER OWNS THE DEADLINE. `closes_at` is written by the server and the
    client's countdown is drawn from it, so the number on screen and the
    instant it stops are the same instant. It does not, however, authorise
    anything: with auto-settlement off, no timer anywhere turns a past
    `closes_at` into a settled trade, so a client that never speaks, loses its
    connection, or lies about the time still cannot move a balance.
  * A CLOSE APPLIES EXACTLY ONCE. Settlement locks the trade row, so two
    concurrent closes of the same position cannot both credit the balance;
    a repeat close returns the already-recorded result and moves no money.

Every platform coin (GOLF, NOVA, ABC, …) has its own authoritative walk
and its own symbol on the trade; the rules are identical for all of them.
"""
import logging
import threading
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from . import models, market_service
from .config import (
    TRADE_PAYOUT_RATE,
    TRADE_PAYOUT_MULTIPLIER,
    MIN_TRADE_AMOUNT,
    MAX_TRADE_AMOUNT,
    PLATFORM_COINS,
    AUTO_SETTLE_ON_EXPIRE,
)

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

    This is THE payout rule, and the frontend's vantaPayoutFor() is the same
    arithmetic written once more for the screen. They must agree to the cent, or
    the row would promise an amount the wallet refuses to pay.

    A position mirrors the market. A BUY (UP) gains what the price gained; a
    SELL (DOWN) is the mirror image, because it wins when the price falls. The
    stake is a MULTIPLIER on the move and never a flat fee on top of it, so the
    P/L on a 100 stake is the same number of percentage points as on a 1,000
    stake -- only the money changes. That is what "everything follows the
    amount" means.

    TRADE_PAYOUT_MULTIPLIER scales how much of the market's move reaches the
    user. It multiplies the MOVE, not the stake, so wins and losses grow
    together and no position can ever be settled below what it is worth. 1.0 is
    the raw market move, which is what this has always done.
    """
    amount = float(amount or 0.0)
    entry = float(entry or 0.0)
    exit_price = float(exit_price or 0.0)
    if amount <= 0:
        return 0.0
    if entry <= 0 or exit_price <= 0:
        return amount
    multiplier = float(TRADE_PAYOUT_MULTIPLIER or 1.0)
    move = (exit_price / entry - 1.0) * multiplier
    value = amount * (1.0 - move if _is_sell(direction) else 1.0 + move)
    floor = amount * 0.01
    return value if value > floor else floor


def _live_exit_price(db: Session, trade: models.Trade) -> float:
    """The server's own current price for this position's instrument, with the
    usual fallbacks so a feed hiccup can never produce a zero-priced exit."""
    symbol = (trade.symbol or "GOLF").strip().upper()
    try:
        px = float(market_service.get_current_price(db, symbol) or 0.0)
    except Exception:
        px = 0.0
    if px <= 0:
        px = float(trade.entry_price or 0.0)
    return px or 1.0


def _payout_for_price(trade: models.Trade, exit_price: float) -> tuple[float, float]:
    """(payout, signed profit) for an EXPLICIT exit price. Pure arithmetic — no
    I/O, no clock, no feed — so the same code prices a live close, a frozen
    close and a legacy expiry sweep, and a position can never be paid two
    different amounts for the same price."""
    entry = float(trade.entry_price or 0.0) or exit_price or 1.0
    if exit_price <= 0:
        exit_price = entry
    amount = float(trade.amount or 0.0)
    payout = _exit_value(amount, entry, exit_price, trade.direction)
    return payout, round(payout - amount, 6)


def _book_result(trade: models.Trade, exit_price: float, profit: float) -> None:
    """Write a REALIZED result onto the row. Only ever called at settlement
    time — the frozen columns are the non-realized counterpart and are written
    by freeze_due_trades() instead."""
    trade.exit_price = round(exit_price, 8)
    trade.profit = profit
    trade.status = models.TradeStatus.WON if profit >= 0 else models.TradeStatus.LOST


def _mark_to_market(db: Session, trade: models.Trade) -> float:
    """Price an OPEN position at the LIVE price, write the realized result onto
    the row, and return the amount to credit. Does not touch any balance —
    the caller owns the commit, so a single transaction covers the position
    rows and the balance row together."""
    exit_price = _live_exit_price(db, trade)
    payout, profit = _payout_for_price(trade, exit_price)
    _book_result(trade, exit_price, profit)
    return payout


def _mark_to_frozen(trade: models.Trade) -> float:
    """Price an OPEN position at the price the server FROZEN at its deadline,
    write the realized result, and return the amount to credit.

    This is the payout path for any position that has passed 00:00. It reads
    `frozen_exit_price` and never the live feed, so the figure the user was
    shown at 00:00 is exactly the figure they are paid, however long they wait
    before pressing CLOSE or CLOSE ALL."""
    exit_price = float(trade.frozen_exit_price or 0.0)
    if exit_price <= 0:
        # Defensive only: a settled-from-frozen row always has a price, because
        # _freeze_row() writes the price and the profit together or not at all.
        exit_price = float(trade.entry_price or 0.0) or 1.0
    payout, profit = _payout_for_price(trade, exit_price)
    _book_result(trade, exit_price, profit)
    return payout


def _freeze_row(db: Session, trade: models.Trade) -> bool:
    """Record the current server price as this position's frozen result.

    Writes ONLY the frozen_* columns. It does not set `exit_price`, `profit`,
    `status`, `settled_at` or `close_reason`, and it does not touch the user's
    balance — because reaching 00:00 is a countdown ending, not a settlement.
    The position stays OPEN, keeps its reserved balance, and stays out of the
    top PROFIT / LOSS cards and closed history until the user collects it.

    Idempotent: a position that already has a frozen price is left untouched,
    so however often this runs the promise cannot change."""
    if trade.status != models.TradeStatus.OPEN:
        return False
    if trade.frozen_exit_price is not None:
        return False
    exit_price = _live_exit_price(db, trade)
    _payout, profit = _payout_for_price(trade, exit_price)
    trade.frozen_exit_price = round(exit_price, 8)
    trade.frozen_profit = profit
    trade.frozen_at = datetime.utcnow()
    return True


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
        # The position's deadline, and the only one that exists: the timestamp
        # the client counts down to. It is a COUNTDOWN and nothing more. When it
        # passes, the trade's duration is over and that is the whole effect: the
        # server records the price (see freeze_due_trades), the position stays
        # OPEN, and the balance reserved for it stays reserved. Only the user
        # pressing CLOSE books it, at the server's own price. A 1-minute trade
        # is therefore 60.0 seconds from `now` — no rounding, no client clock.
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
      * THE CLOCK IS NOT A FORFEITURE, AND IT IS NOT A PROFIT EITHER. Closing
        early and closing long after 00:00 both settle; neither is a loss. This
        function reads `closes_at` for exactly one purpose: to decide WHICH price
        to pay. A position still inside its duration is priced live. A position
        past its deadline is priced at the FROZEN price the server recorded at
        the deadline, so the figure the user watched stop moving at 00:00 is the
        figure they are paid. It is never priced as a loss for being late.

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

    # A position whose deadline has passed but whose freeze has not run yet
    # (the sweep is on a loop; this close got there first) is frozen HERE, in
    # this same transaction, so it can never be settled at a different price
    # than the one a concurrent read would have frozen.
    if trade.frozen_exit_price is None and trade.closes_at <= datetime.utcnow():
        _freeze_row(db, trade)

    if trade.frozen_exit_price is not None:
        payout = _mark_to_frozen(trade)
    else:
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
    """Collect every FINISHED position for this user in ONE atomic transaction.

    This is what the "CLOSE ALL" button calls, and it is a COLLECTION, not a
    liquidation:

        it closes positions whose duration has ended (00:00), and only those.

    A position still inside its duration is deliberately left alone. It has no
    frozen result yet, so there is no guaranteed figure to collect, and closing
    it here would sell the user a position they did not ask to sell at a price
    they never saw settle. Those are closed with their own CLOSE button, priced
    live, whenever the user chooses.

    Every collected position is paid its FROZEN price via `_mark_to_frozen`, so
    the batch total is exactly the sum of the figures already on screen.

    Unlike a loop of single closes, this is a single lock set and a single
    commit: the whole batch either lands in full or not at all. Positions are
    taken in a stable order (oldest first) purely so two concurrent CLOSE ALL
    presses can't deadlock waiting on each other.

    `symbol` restricts the batch to one instrument; omitted, it collects the
    user's whole finished book. Trades already closed are excluded, so calling
    this twice credits the balance once. A user with nothing finished is not an
    error — it returns an empty list."""
    # Anything whose duration ended but whose freeze has not run yet is frozen
    # first, in its own commit, so it is collectable by this same press. Running
    # the sweep here rather than relying on the loop is what makes CLOSE ALL
    # dependable at the exact instant a position hits 00:00.
    freeze_due_trades(db)

    q = db.query(models.Trade).filter(
        models.Trade.user_id == user_id,
        models.Trade.status == models.TradeStatus.OPEN,
        models.Trade.frozen_exit_price.isnot(None),
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
        if live and live.status == models.TradeStatus.OPEN and live.frozen_exit_price is not None:
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
        credited += _mark_to_frozen(live)
        live.settled_at = now
        live.close_reason = "SOLD"

    user.usdt_balance = float(user.usdt_balance or 0.0) + credited
    db.commit()
    for live in locked:
        db.refresh(live)
    return locked


# =============================================================================
# Reaching 00:00 — the freeze
# =============================================================================
# RUNNING OUT OF TIME IS NOT A SETTLEMENT. This is the single most important
# rule in the module, so it is stated before anything else here:
#
#     00:00  ->  the duration is over, and the result is LOCKED.
#
# A position whose `closes_at` has passed stays OPEN. Its trading balance is
# untouched, the account's Profit and Loss cards do not move, and it is not in
# closed history. What DOES happen is that the server records the price at that
# instant and stops:
#
#     frozen_exit_price / frozen_profit  —  written, never touched again
#     exit_price / profit / status       —  NOT written; those are realized
#     user balance                       —  NOT credited
#
# So the number on screen at 00:00 is a promise, not a projection: it cannot
# drift, and it is the exact figure paid when the user finally collects.
#
# `close_trade()` (the per-position CLOSE button) and `close_all_trades()` (the
# CLOSE ALL button) are the ONLY things in this module that price a position for
# real, credit a balance, book a result and file a trade into closed history —
# and for a finished position both of them pay the FROZEN price via
# `_mark_to_frozen`, never the live feed.
#
# `expire_due_trades()` further down is the machinery that used to turn a past
# `closes_at` into a settled trade. It is retained, unchanged, and gated behind
# `config.AUTO_SETTLE_ON_EXPIRE`, which defaults to OFF. With the flag off it
# returns an empty list and touches nothing at all, so all three of its call
# sites (the background loop and both read endpoints) are inert by construction
# rather than by each caller remembering to skip it. Turning the flag back on
# reinstates settle-on-expiry — and it would still price through `_mark_to_market`
# on top of the freeze, so the clock would decide WHEN, never WHAT.
#
# WHY THE MACHINERY IS KEPT RATHER THAN DELETED
# It is correct code and it is one env var away. Deleting it would make
# reinstate-on-expiry a rewrite instead of a switch, and would take the
# exactly-once locking and the SQLite sweep lock with it.


def unfrozen_due_query(db: Session, now: datetime):
    """The OPEN positions whose duration has elapsed and that have no frozen
    price yet, oldest deadline first. One place, so the background loop and the
    read paths can never disagree about which positions still need freezing."""
    return (
        db.query(models.Trade)
        .filter(
            models.Trade.status == models.TradeStatus.OPEN,
            models.Trade.closes_at <= now,
            models.Trade.frozen_exit_price.is_(None),
        )
        .order_by(models.Trade.closes_at.asc(), models.Trade.id.asc())
    )


def freeze_due_trades(db: Session, now: datetime | None = None, limit: int = 500) -> list[models.Trade]:
    """Freeze the result of every OPEN position whose `closes_at` has passed and
    which has not been frozen yet. Returns those positions.

    THIS MOVES NO MONEY. It writes `frozen_exit_price`, `frozen_profit` and
    `frozen_at`, and nothing else — no balance, no `status`, no `profit`, no
    `settled_at`, no `close_reason`. The position is still OPEN, still holds the
    balance reserved for it, and is still absent from the top PROFIT / LOSS cards
    and from closed history.

    Safe to call from anywhere, as often as you like:
      * a position is locked and re-checked under that lock, so one that has
        already been frozen — or already settled — is skipped, not re-priced;
      * `frozen_exit_price IS NOT NULL` is a permanent guard, so the promise
        cannot be revised by a later, different price;
      * all freezes land in ONE commit, so a failure part-way through leaves
        every position either frozen or untouched, never half-written.

    Unlike settlement this is deliberately allowed to run on a timer. Recording
    a price that nobody has been charged for cannot lose anybody money, and
    running it promptly is what makes the frozen figure the one the user sees
    the instant their countdown reaches 00:00."""
    now = now or datetime.utcnow()
    candidates = unfrozen_due_query(db, now).limit(limit).all()
    if not candidates:
        return []

    frozen: list[models.Trade] = []
    for candidate in candidates:
        # LOCK ORDER: the position row, before any balance row. The freeze never
        # takes a balance lock at all, but it uses the same _lock_trade() so a
        # position cannot be frozen and settled in the same instant by two
        # racing writers.
        live = _lock_trade(db, candidate.user_id, candidate.id)
        if not live:
            continue
        if _freeze_row(db, live):
            frozen.append(live)

    if not frozen:
        db.rollback()
        return []
    db.commit()
    for live in frozen:
        db.refresh(live)
    return frozen


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
    """Settle every OPEN position whose `closes_at` has passed — ONLY IF
    `AUTO_SETTLE_ON_EXPIRE` is on, which it is NOT by default.

    SHIPPED BEHAVIOUR: this is a no-op. It returns an empty list and does not
    read, write or lock anything. Reaching `closes_at` is not a settlement: the
    position stays OPEN, its P/L stays UNREALIZED, and the balance and the
    account Profit/Loss are untouched until the user closes it through
    `close_trade()`. The client's own countdown stops at 00:00 for the same
    reason, and does not post a close either.

    This was asked about directly and confirmed in the face of wording that
    reads the other way ("when the 1-minute timer finishes the trade must be
    finalized", "the stake must be lost when unsuccessful"). The rule stands: the
    clock finalizes the COUNTDOWN, not the trade. An unsuccessful close does lose
    its stake, but that is the payout rule doing it at the user's click, priced
    by the graded `_exit_value` with its 1% floor — never a forfeit booked by the
    timer running out.

    The gate is here, at the one function every caller shares, rather than in
    the three callers themselves (the background loop in main.py and both read
    endpoints). A gate in the callers is a rule three places can each forget; a
    gate here makes "nothing settles on a timer" a property of the system.

    `now` is injectable so a test can settle a position without sleeping 60
    seconds. Never raises: a failure here is logged and the positions stay OPEN
    for the next sweep, which is the safe direction to fail in — an unsettled
    position is retried, a half-credited one is not.
    """
    if not AUTO_SETTLE_ON_EXPIRE:
        # The duration ended. Nothing else did. Take no action of any kind.
        return []

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

