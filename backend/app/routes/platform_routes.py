"""
Platform-coin trading / investment overview. Every number here is derived
from a real server-side source: prices from market_service's authoritative
walks, coin quantities from the user's recorded balances, and the
investment cost basis from the user's own swap ledger (average-cost method)
— never invented, never client-supplied.
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import schemas, models, market_service, swap_service, trading_service, auth
from ..config import (
    PLATFORM_COINS, PLATFORM_COIN_NAMES, PLATFORM_COINS_LIVE,
)
from ..database import get_db
from ..auth import get_current_user_id

router = APIRouter(prefix="/api/platform", tags=["platform"])


def _tradeable(symbol: str) -> bool:
    if PLATFORM_COINS_LIVE:
        return symbol in PLATFORM_COINS_LIVE
    return True


def _investment_book(db: Session, user_id: str) -> dict:
    """Average-cost ledger per platform coin from real swap transactions:
    coin -> [total USDT invested, total coins acquired]. Building this from
    the SwapTx table keeps "Original Investment" auditable, never a guess."""
    book = {}
    for tx in db.query(models.SwapTx).filter_by(user_id=user_id).all():
        if tx.from_symbol != "USDT" or tx.to_symbol not in PLATFORM_COINS:
            continue
        row = book.setdefault(tx.to_symbol, [0.0, 0.0])
        row[0] += float(tx.from_amount)
        row[1] += float(tx.to_amount)
    return book


@router.get("/coins", response_model=schemas.PlatformOverviewOut)
def platform_coins(
    db: Session = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    balances = swap_service.user_balances(db, user_id)
    book = _investment_book(db, user_id)
    user = db.query(models.User).filter_by(id=user_id).first()

    coins = []
    total_value = 0.0
    total_invested = 0.0
    total_unrealized = 0.0

    for sym in PLATFORM_COINS:
        price = market_service.get_usd_price(db, sym)
        launch = market_service.platform_launch_price(sym)
        change = market_service.get_change_pct(db, sym)
        balance = float(balances.get(sym, 0.0) or 0.0)

        spent, acquired = book.get(sym, (0.0, 0.0))
        avg_cost = spent / acquired if acquired > 0 else 0.0
        usdt_invested = spent if balance >= acquired else avg_cost * balance
        current_value = balance * price
        unrealized = current_value - usdt_invested

        total_value += current_value
        total_invested += usdt_invested
        total_unrealized += unrealized
        coins.append(schemas.PlatformCoinOut(
            symbol=sym,
            name=PLATFORM_COIN_NAMES.get(sym, sym),
            price=round(price, 8),
            launch_price=round(launch, 8),
            change_pct=change,
            tradeable=_tradeable(sym),
            balance=round(balance, 8),
            usdt_invested=round(usdt_invested, 2),
            current_value=round(current_value, 2),
            unrealized_pnl=round(unrealized, 2),
        ))

    closed = db.query(models.Trade).filter(
        models.Trade.user_id == user_id,
        models.Trade.status.in_([models.TradeStatus.WON, models.TradeStatus.LOST]),
    ).all()
    total_trade_profit = sum(float(t.profit or 0.0) for t in closed)

    # PROFIT and LOSS are reported as two separate, never-netted totals. The
    # signed net above is the only place a single signed figure exists, and it
    # is not what the PROFIT card reads — see trading_service.realized_split().
    split = trading_service.realized_split(db, user_id)
    by_symbol = split["by_symbol"]
    for coin in coins:
        per_coin = by_symbol.get(coin.symbol, {})
        coin.realized_profit = per_coin.get("profit", 0.0)
        coin.realized_loss = per_coin.get("loss", 0.0)
        coin.available_profit = per_coin.get("available", 0.0)

    return schemas.PlatformOverviewOut(
        coins=coins,
        usdt_balance=round(float(user.usdt_balance) if user else 0.0, 2),
        total_platform_value=round(total_value, 2),
        total_usdt_invested=round(total_invested, 2),
        total_unrealized_pnl=round(total_unrealized, 2),
        total_trade_profit=round(total_trade_profit, 2),
        realized_profit=split["profit"],
        realized_loss=split["loss"],
        available_profit=split["available"],
    )


@router.post("/move-profit", response_model=schemas.MoveProfitOut)
def move_profit(
    payload: schemas.MoveProfitRequest,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Move the user's positive REALIZED profit into a chosen wallet.

    Powers the trading screen's "Move to Wallet" button, and is the only bridge
    between a trade result and the wallet. Five things are deliberate:

      * the Wallet PIN is checked FIRST, against the row belonging to the
        authenticated user, before a single balance is read or written — so a
        wrong PIN cannot leave a partial transfer behind, and there is no
        ordering in which the money moves and the check is skipped;
      * the amount is resolved here from the closed-trade records, so a request
        cannot name a number at all. A loss, a stake, an unrealized value, the
        trading balance and an already-transferred profit are all outside what
        this can ever move;
      * `realized_split` splits the result first, so a losing account has
        $0.00 available and is refused here rather than being handed a negative
        transfer;
      * `symbol` is the coin traded and so selects which trades are spent;
      * the destination is `dest_symbol` when given, else `symbol`, and is
        validated against the real coin list either way.

    Trades settled as a loss are never eligible: only profit > 0 is summed, and
    only a positive amount is accepted by swap_service.move_profit_to_wallet.
    """
    # 1. The PIN, before anything else happens.
    user = db.query(models.User).filter_by(id=user_id).first()
    if not user:
        raise HTTPException(status_code=401, detail="Account not found.")
    pin_error = auth.check_wallet_pin(user_id, payload.pin, user.pin_hash)
    if pin_error:
        # Nothing has been written at this point, so an invalid PIN moves no
        # money and changes no balance.
        raise HTTPException(status_code=400, detail=pin_error)

    # 2. The source: the coin traded, and the profit still un-moved.
    symbol = (payload.symbol or "GOLF").strip().upper()
    split = trading_service.realized_split(db, user_id, symbol)
    available = float(split["available"])
    if available <= 0:
        raise HTTPException(
            status_code=400,
            detail=f"No realized {symbol} profit available to move to your wallet.",
        )

    # 3. The destination: the user's pick, or the traded coin by default.
    dest = (payload.dest_symbol or symbol).strip().upper()
    if dest not in market_service.SUPPORTED_SYMBOLS:
        raise HTTPException(
            status_code=400, detail=f"Unsupported destination coin: {dest}."
        )

    # 4. The price is the DESTINATION coin's, because the profit is USDT and the
    #    wallet is credited in coin. This is the same conversion the wallet
    #    already uses everywhere else — no second rate is invented here.
    try:
        price = market_service.get_usd_price(db, dest)
    except Exception:
        price = 0.0
    if not price or price <= 0:
        raise HTTPException(
            status_code=400, detail=f"Price unavailable for {dest} right now — try again shortly."
        )

    try:
        result = swap_service.move_profit_to_wallet(
            db, user_id, symbol, available, price, dest
        )
    except swap_service.SwapError as e:
        raise HTTPException(status_code=400, detail=str(e))

    after = trading_service.realized_split(db, user_id, symbol)
    return schemas.MoveProfitOut(
        symbol=result["symbol"],
        dest_symbol=result["dest_symbol"],
        usd_moved=result["usd_moved"],
        coin_amount=result["amount"],
        price=result["price"],
        trades_settled=result["trades_settled"],
        trading_balance=result["trading_balance"],
        wallet_balance=result["wallet_balance"],
        available_profit=after["available"],
    )


@router.post("/liquidate")
def liquidate(db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    """Sell every platform-coin holding (GOLF/NOVA/ABC…) back into USDT at the
    current authoritative price and move the full value into the wallet.
    Powers the dashboard's "Move to Wallet" button — after it runs there are
    no platform holdings left, so total_usdt_invested wipes to 0. Every sale
    is recorded in the swap ledger (coin → USDT) so it stays auditable."""
    balances = swap_service.user_balances(db, user_id)
    try:
        user = db.query(models.User).filter_by(id=user_id).with_for_update().first()
    except Exception:
        db.rollback()
        user = db.query(models.User).filter_by(id=user_id).first()  # SQLite fallback
    if not user:
        return {"moved": [], "usd_moved": 0.0}

    moved = []
    transfers = []
    usd_moved = 0.0
    for sym in PLATFORM_COINS:
        bal = float(balances.get(sym, 0.0) or 0.0)
        if bal <= 0:
            continue
        price = float(market_service.get_usd_price(db, sym) or 0.0)
        value = bal * price
        if value <= 0:
            value = 0.0
        swap_service.set_balance(db, user, sym, 0.0)
        transfers.append(models.SwapTx(
            user_id=user_id, from_symbol=sym, to_symbol="USDT",
            from_amount=bal, to_amount=value, rate=price,
        ))
        moved.append({"symbol": sym, "balance": round(bal, 8), "usd_value": round(value, 2)})
        usd_moved += value

    if transfers:
        user.usdt_balance = float(user.usdt_balance or 0.0) + usd_moved
        db.add_all(transfers)
        db.commit()

    return {"moved": moved, "usd_moved": round(usd_moved, 2)}