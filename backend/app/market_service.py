"""
Server-side authoritative demo market for GOLFUSDT. This is what actually
determines trade win/loss (see trading_service.py) — completely separate
from whatever the frontend's own chart-rendering code independently draws
for visual purposes. The frontend's existing candle-drawing/animation code
is untouched; this service only gives it a real, tamper-proof price series
to sync FROM (see GET /market/price and /market/candles).

The forming candle is advanced in memory on every tick and only
checkpointed to the SQLite file every CHECKPOINT_SECONDS. Previously the
DB was committed once per second, which touched backend/vanta.db every
tick — any file-watching dev server (Live Server, watchers…) reloaded the
page on every write, wiping the session and making the site "refresh every
second".

Simple bounded random walk, deliberately similar in shape to the existing
frontend's own local simulator (small noise + gentle drift + rare
impulses, clamped to a band) — there is no real underlying exchange for a
token that only exists in this test environment, so a fair, transparent,
non-gameable-by-the-client simulator standing in for the market is exactly
the "Simulation for the chart only" the spec calls for. The client can
never influence this value.
"""
import random
import json
import urllib.request
from datetime import datetime, timedelta
from sqlalchemy.orm import Session

from . import models
from .config import MARKET_STARTING_PRICE

SYMBOL = "GOLFUSDT"
CANDLE_INTERVAL_SECONDS = 60  # the 1m candle stays open a full 60s before a new one opens
PRICE_BAND_LOW = MARKET_STARTING_PRICE * 0.985
PRICE_BAND_HIGH = MARKET_STARTING_PRICE * 1.015

# Every coin the platform lists for convert / invest. USDT is the accounting
# base unit (price 1.0). GOLF keeps its own authoritative walk below; the
# other coins use CoinGecko when reachable and fall back to these defaults.
SUPPORTED_SYMBOLS = ("USDT", "USDC", "GOLF", "BTC", "ETH", "SOL", "TRX", "BNB", "XRP", "DOGE", "ADA", "LINK", "LTC")

DEFAULT_USD_PRICES = {
    "USDT": 1.0, "USDC": 1.0, "BTC": 77386.0, "ETH": 2434.95, "SOL": 94.82,
    "TRX": 0.3432, "BNB": 682.40, "XRP": 1.58, "DOGE": 0.18,
    "ADA": 0.82, "LINK": 22.41, "LTC": 82.15,
}

COINGECKO_IDS = {
    "BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana", "TRX": "tron",
    "BNB": "binancecoin", "XRP": "ripple", "DOGE": "dogecoin",
    "ADA": "cardano", "LINK": "chainlink",
    "USDC": "usd-coin", "LTC": "litecoin",
}

# Live-ish USD price table for the coins above (server-side, refreshed from
# CoinGecko by main.py; GOLF resolved separately from its own tick).
_usd_prices = {k: v for k, v in DEFAULT_USD_PRICES.items() if k != "USDT"}

# How often the in-memory market state is written back to the SQLite file.
CHECKPOINT_SECONDS = 300

# In-memory market state, updated every tick without touching the disk.
_state = {"forming": None, "closed": []}
_last_checkpoint = None


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _row_dict(row):
    return {
        "symbol": SYMBOL, "open": row.open, "high": row.high,
        "low": row.low, "close": row.close, "open_time": row.open_time,
    }


def _candle_orm(c):
    return models.Candle(
        symbol=c["symbol"], open=c["open"], high=c["high"],
        low=c["low"], close=c["close"], open_time=c["open_time"],
    )


def _last_db_candle(db: Session):
    return (
        db.query(models.Candle).filter_by(symbol=SYMBOL)
        .order_by(models.Candle.open_time.desc()).first()
    )


def ensure_seeded(db: Session):
    """Idempotent: seeds an initial candle history only if none exists yet."""
    exists = db.query(models.Candle).filter_by(symbol=SYMBOL).first()
    if exists:
        return
    now = datetime.utcnow()
    price = MARKET_STARTING_PRICE
    rows = []
    for i in range(120):
        open_ = price
        move = random.uniform(-0.3, 0.3)
        close = _clamp(open_ + move, PRICE_BAND_LOW, PRICE_BAND_HIGH)
        high = max(open_, close) + random.uniform(0.05, 0.3)
        low = min(open_, close) - random.uniform(0.05, 0.3)
        rows.append(models.Candle(
            symbol=SYMBOL, open=open_, high=high, low=low, close=close,
            open_time=now - timedelta(seconds=(120 - i) * CANDLE_INTERVAL_SECONDS),
        ))
        price = close
    db.add_all(rows)
    db.commit()


def _checkpoint(db: Session):
    """Write the accumulated in-memory candles back to the DB. Runs rarely
    (every CHECKPOINT_SECONDS), so the SQLite file is not touched every
    tick."""
    last = _last_db_candle(db)

    for c in _state["closed"]:
        if last is not None and last.open_time == c["open_time"]:
            last.open, last.high, last.low, last.close = (
                c["open"], c["high"], c["low"], c["close"],
            )
        else:
            db.add(_candle_orm(c))

    f = _state["forming"]
    if f is not None:
        if last is not None and last.open_time == f["open_time"]:
            last.open, last.high, last.low, last.close = f["open"], f["high"], f["low"], f["close"]
        else:
            last = _candle_orm(f)
            db.add(last)

    db.commit()
    _state["closed"] = []

    count = db.query(models.Candle).filter_by(symbol=SYMBOL).count()
    if count > 2000:
        old = (
            db.query(models.Candle).filter_by(symbol=SYMBOL)
            .order_by(models.Candle.open_time.asc()).limit(count - 2000).all()
        )
        for row in old:
            db.delete(row)
        db.commit()


def tick(db: Session):
    """Advances the market by one server tick — called periodically by the
    background loop in main.py. Either nudges the currently-forming candle
    or closes it and opens a new one, using plain OHLC-consistent math
    (high always >= max(open,close), low always <= min(open,close)).
    Updates live in memory; the DB is checkpointed rarely."""
    global _last_checkpoint
    now = datetime.utcnow()

    if _state["forming"] is None:
        last = _last_db_candle(db)
        if last is None:
            ensure_seeded(db)
            last = _last_db_candle(db)
        _state["forming"] = {
            "symbol": SYMBOL, "open": last.open, "high": last.high,
            "low": last.low, "close": last.close, "open_time": last.open_time,
        }
        _last_checkpoint = now

    forming = _state["forming"]
    age = (now - forming["open_time"]).total_seconds()

    noise = random.uniform(-0.06, 0.06)
    impulse = random.uniform(-0.15, 0.15) if random.random() < 0.05 else 0.0
    new_close = _clamp(forming["close"] + noise + impulse, PRICE_BAND_LOW, PRICE_BAND_HIGH)

    if age >= CANDLE_INTERVAL_SECONDS:
        closed = dict(forming)
        _state["closed"].append(closed)
        _state["forming"] = {
            "symbol": SYMBOL, "open": closed["close"],
            "high": max(closed["close"], new_close),
            "low": min(closed["close"], new_close),
            "close": new_close, "open_time": now,
        }
    else:
        forming["close"] = new_close
        forming["high"] = max(forming["high"], new_close)
        forming["low"] = min(forming["low"], new_close)

    if _last_checkpoint is None or (now - _last_checkpoint).total_seconds() >= CHECKPOINT_SECONDS:
        _checkpoint(db)
        _last_checkpoint = now


def get_current_price(db: Session) -> float:
    if _state["forming"] is not None:
        return _state["forming"]["close"]
    if _state["closed"]:
        return _state["closed"][-1]["close"]
    last = _last_db_candle(db)
    return last.close if last else MARKET_STARTING_PRICE


def get_candles(db: Session, limit: int = 150) -> list:
    rows = (
        db.query(models.Candle).filter_by(symbol=SYMBOL)
        .order_by(models.Candle.open_time.desc()).limit(limit).all()
    )

    mem = {}
    for c in _state["closed"]:
        mem[c["open_time"]] = c
    if _state["forming"] is not None:
        mem[_state["forming"]["open_time"]] = _state["forming"]

    out = []
    for row in rows:
        mine = mem.get(row.open_time)
        out.append(mine if mine is not None else _row_dict(row))

    out.reverse()

    newest = out[-1]["open_time"] if out else None
    extra = [c for c in _state["closed"] if newest is None or c["open_time"] > newest]
    extra.sort(key=lambda c: c["open_time"])
    if _state["forming"] is not None and (newest is None or _state["forming"]["open_time"] > newest):
        extra.append(_state["forming"])
    out = (out + extra)[-limit:]

    return out


# =============================================================================
# Multi-coin USD prices (for Convert / Invest on any listed currency)
# =============================================================================

def get_usd_price(db: Session, symbol: str) -> float:
    """Server-authoritative USD/coin price used for all conversions.
    USDT is the base unit (1.0); GOLF resolves from its own live tick; the
    remaining coins use the CoinGecko-refreshed table (or last-known value)."""
    if symbol == "USDT":
        return 1.0
    if symbol == "GOLF":
        return get_current_price(db)
    return _usd_prices.get(symbol) or DEFAULT_USD_PRICES.get(symbol, 0.0)


def get_all_usd_prices(db: Session) -> dict:
    """Price for every listed symbol in USDT (USDT itself = 1.0)."""
    out = {}
    for s in SUPPORTED_SYMBOLS:
        out[s] = get_usd_price(db, s)
    return out


def refresh_prices_from_coingecko() -> bool:
    """Best-effort refresh of the coin price table from CoinGecko. Never
    raises — on any network/parse failure it silently keeps the last good /
    default prices, so conversion simply continues on fallbacks."""
    try:
        ids = ",".join(COINGECKO_IDS.values())
        url = (
            "https://api.coingecko.com/api/v3/simple/price"
            f"?ids={ids}&vs_currencies=usd"
        )
        req = urllib.request.Request(
            url, headers={"User-Agent": "vanta-trade/1.0", "Accept": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        for sym, cid in COINGECKO_IDS.items():
            usd = data.get(cid, {}).get("usd")
            if isinstance(usd, (int, float)) and usd > 0:
                _usd_prices[sym] = float(usd)
        return True
    except Exception:
        return False