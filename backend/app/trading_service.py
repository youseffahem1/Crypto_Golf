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

A POSITION STAYS OPEN UNTIL IT IS EXPLICITLY CLOSED.

`closes_at` is still recorded, because it is part of the position the user
chose (its duration is shown on the chart and in the position table) — but it
is display metadata ONLY. Nothing expires, nothing is force-settled, and the
background loop that used to turn a past `closes_at` into a total loss has
been removed. A stake is never forfeited by waiting: the timer running out
changes nothing about the position or the balance, so the trade survives a
page refresh, a browser restart, days of inactivity and a logout, and it is
only ever settled by the user pressing Close (one) or Close All, or by an
explicit call to close_trade()/close_all_trades().

Two rules make that guarantee hold:

  * THE SERVER OWNS THE RESULT. The exit price is read from the server's own
    feed and the credited value is derived from it together with the trade's
    own direction. A `value` sent by the client is accepted by the request
    schema for backward compatibility and then ignored, so no client can name
    the figure it is paid.
  * A CLOSE APPLIES EXACTLY ONCE. Settlement locks the trade row, so two
    concurrent closes of the same position cannot both credit the balance;
    a repeat close returns the already-recorded result and moves no money.

Every platform coin (GOLF, NOVA, ABC, …) has its own authoritative walk
and its own symbol on the trade; the rules are identical for all of them.
"""
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from . import models, market_service
from .config import TRADE_PAYOUT_RATE, MIN_TRADE_AMOUNT, MAX_TRADE_AMOUNT, PLATFORM_COINS

TRADEABLE_SYMBOLS = tuple(s for s in PLATFORM_COINS if s)


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
# LOCK ORDER IS ALWAYS trade-then-user. close_trade() and close_all_trades()
# both take the position rows before the balance row, so the two paths can
# never deadlock against each other by grabbing the same two rows in opposite
# orders.

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
        # Display metadata only: this is the duration the user picked, shown on
        # the chart and in the position table. It does NOT schedule a
        # settlement — the position stays open until it is explicitly closed.
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
        Close, or a retry after a dropped response, safe.
      * NON-DESTRUCTIVE TO WAITING. Nothing about this function depends on
        `closes_at`; a position closed days after it was opened settles
        normally, and one that is never closed stays open.

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

    user = _lock_user(db, user_id)
    if not user:
        raise TradingError("User not found")

    now = datetime.utcnow()
    credited = 0.0
    for trade in rows:
        credited += _mark_to_market(db, trade)
        trade.settled_at = now

    # Re-assert the open filter before crediting: if anything changed the rows
    # underneath us we must not pay for a position twice.
    still_open = (
        db.query(models.Trade)
        .filter(
            models.Trade.id.in_([t.id for t in rows]),
            models.Trade.status == models.TradeStatus.OPEN,
        )
        .count()
    )
    if still_open != len(rows):
        db.rollback()
        raise TradingError("Positions changed while closing — please try again.")

    user.usdt_balance = float(user.usdt_balance or 0.0) + credited
    db.commit()
    for trade in rows:
        db.refresh(trade)
    return rows
