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
from .config import MARKET_STARTING_PRICE, PLATFORM_COINS, PLATFORM_COIN_NAMES, PLATFORM_COIN_LAUNCH_USD, PLATFORM_COIN_WALK_BAND_PCT

SYMBOL = "GOLFUSDT"
CANDLE_INTERVAL_SECONDS = 60  # the 1m candle stays open a full 60s before a new one opens
PRICE_BAND_LOW = MARKET_STARTING_PRICE * 0.985
PRICE_BAND_HIGH = MARKET_STARTING_PRICE * 1.015

# Platform coins that get their own independent authoritative price walk.
# GOLF uses the original single-walk machinery below (unchanged behaviour);
# every other configured platform coin (NOVA, ABC, …) runs its own bounded
# walk seeded from its launch price so change-% since launch is real data.
PLATFORM_WALK_SYMBOLS = tuple(
    s for s in PLATFORM_COINS if s and s != "GOLF"
)

# Extra-coins default metadata (used by /api/platform/coins fallback).
PLATFORM_EXTRA_META = {
    sym: {
        "name": PLATFORM_COIN_NAMES.get(sym, f"{sym} Coin"),
        "launch": float(PLATFORM_COIN_LAUNCH_USD.get(sym, 0.01)),
    }
    for sym in PLATFORM_WALK_SYMBOLS
}

# Every coin the platform lists for convert / invest. USDT is the accounting
# base unit (price 1.0). GOLF keeps its own authoritative walk below; the
# other coins use CoinGecko when reachable and fall back to these defaults,
# and platform walk coins (NOVA/ABC) resolve from their own walks.
SUPPORTED_SYMBOLS = (
    "USDT", "USDC", "BTC", "ETH", "SOL", "TRX", "BNB", "XRP", "DOGE", "ADA", "LINK", "LTC"
) + PLATFORM_COINS

DEFAULT_USD_PRICES = {
    "USDT": 1.0, "USDC": 1.0, "BTC": 77386.0, "ETH": 2434.95, "SOL": 94.82,
    "TRX": 0.3432, "BNB": 682.40, "XRP": 1.58, "DOGE": 0.18,
    "ADA": 0.82, "LINK": 22.41, "LTC": 82.15,
}
for _sym, _meta in PLATFORM_EXTRA_META.items():
    DEFAULT_USD_PRICES.setdefault(_sym, _meta["launch"])

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

# ============================================================================
# Per-coin authoritative walks for the other platform coins (NOVA, ABC, …).
# Each coin gets its OWN independent bounded random walk seeded from its
# launch price, checkpointed to the candles table under "<SYM>USDT". The
# client can never influence these values — same guarantee as GOLF.
# ============================================================================
_extra_states = {
    sym: {
        "forming": None, "closed": [],
        "start": float(meta["launch"]),
        "lo": float(meta["launch"]) * (1.0 - PLATFORM_COIN_WALK_BAND_PCT / 100.0),
        "hi": float(meta["launch"]) * (1.0 + PLATFORM_COIN_WALK_BAND_PCT / 100.0),
    }
    for sym, meta in PLATFORM_EXTRA_META.items()
}
_extra_last_checkpoint = None


def extra_candle_symbol(sym: str) -> str:
    return sym + "USDT"


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _row_dict(row, symbol=SYMBOL):
    return {
        "symbol": symbol, "open": row.open, "high": row.high,
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
    """Idempotent: seeds an initial candle history for GOLF and every other
    configured platform coin (each from its own launch price) if none exists
    yet."""
    _seed_golf(db)
    for sym in list(_extra_states.keys()):
        _seed_extra(db, sym)


def _seed_golf(db: Session):
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


def _seed_extra(db: Session, sym: str):
    st = _extra_states[sym]
    c_sym = extra_candle_symbol(sym)
    exists = db.query(models.Candle).filter_by(symbol=c_sym).first()
    if exists:
        return
    now = datetime.utcnow()
    launch, lo, hi = st["start"], st["lo"], st["hi"]
    target = launch * (1.0 + random.uniform(-PLATFORM_COIN_WALK_BAND_PCT / 200.0, PLATFORM_COIN_WALK_BAND_PCT / 100.0))
    price = launch
    rows = []
    for i in range(120):
        t = (i + 1) / 120.0
        base = launch + (target - launch) * t
        open_ = price
        close = _clamp(base + random.uniform(-abs(hi) * 0.01, abs(hi) * 0.01), lo, hi)
        high = max(open_, close) + abs(random.uniform(lo, hi)) * 0.001
        low = min(open_, close) - abs(random.uniform(lo, hi)) * 0.001
        rows.append(models.Candle(
            symbol=c_sym, open=open_, high=high, low=low, close=close,
            open_time=now - timedelta(seconds=(120 - i) * CANDLE_INTERVAL_SECONDS),
        ))
        price = close
    db.add_all(rows)
    db.commit()


def _merge_in_memory(db: Session, state, candle_symbol: str):
    """Write one walk's closed/forming candles into the DB, merging by
    open_time so a checkpoint never duplicates a candle."""
    last = db.query(models.Candle).filter_by(symbol=candle_symbol) \
        .order_by(models.Candle.open_time.desc()).first()

    for c in state["closed"]:
        if last is not None and last.open_time == c["open_time"]:
            last.open, last.high, last.low, last.close = (
                c["open"], c["high"], c["low"], c["close"],
            )
        else:
            db.add(_candle_orm(c))

    f = state["forming"]
    if f is not None:
        if last is not None and last.open_time == f["open_time"]:
            last.open, last.high, last.low, last.close = f["open"], f["high"], f["low"], f["close"]
        else:
            db.add(_candle_orm(f))

    state["closed"] = []


def _checkpoint(db: Session):
    """Write the accumulated in-memory GOLF candles back to the DB. Runs
    rarely (every CHECKPOINT_SECONDS), so the SQLite file is not touched
    every tick."""
    _merge_in_memory(db, _state, SYMBOL)
    db.commit()

    count = db.query(models.Candle).filter_by(symbol=SYMBOL).count()
    if count > 2000:
        old = (
            db.query(models.Candle).filter_by(symbol=SYMBOL)
            .order_by(models.Candle.open_time.asc()).limit(count - 2000).all()
        )
        for row in old:
            db.delete(row)
        db.commit()


def _extra_checkpoint(db: Session):
    for sym, st in _extra_states.items():
        _merge_in_memory(db, st, extra_candle_symbol(sym))
    db.commit()


def tick(db: Session):
    """Advances the market by one server tick — called periodically by the
    background loop in main.py. Advances GOLF AND every other configured
    platform coin. Either nudges the currently-forming candle or closes it
    and opens a new one, using plain OHLC-consistent math (high always >=
    max(open,close), low always <= min(open,close)). Updates live in memory;
    the DB is checkpointed rarely."""
    now = datetime.utcnow()
    global _last_checkpoint

    if _state["forming"] is None:
        last = _last_db_candle(db)
        if last is None:
            _seed_golf(db)
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

    _tick_extras(db, now)


def _tick_extras(db: Session, now: datetime):
    global _extra_last_checkpoint
    for sym, st in _extra_states.items():
        c_sym = extra_candle_symbol(sym)
        if st["forming"] is None:
            last = db.query(models.Candle).filter_by(symbol=c_sym) \
                .order_by(models.Candle.open_time.desc()).first()
            if last is None:
                _seed_extra(db, sym)
                last = db.query(models.Candle).filter_by(symbol=c_sym) \
                    .order_by(models.Candle.open_time.desc()).first()
            st["forming"] = {
                "symbol": c_sym, "open": last.open, "high": last.high,
                "low": last.low, "close": last.close, "open_time": last.open_time,
            }

        forming = st["forming"]
        age = (now - forming["open_time"]).total_seconds()
        span = st["hi"] - st["lo"]
        noise = random.uniform(-span * 0.001, span * 0.001)
        impulse = random.uniform(-span * 0.004, span * 0.004) if random.random() < 0.05 else 0.0
        new_close = _clamp(forming["close"] + noise + impulse, st["lo"], st["hi"])

        if age >= CANDLE_INTERVAL_SECONDS:
            closed = dict(forming)
            st["closed"].append(closed)
            st["forming"] = {
                "symbol": c_sym, "open": closed["close"],
                "high": max(closed["close"], new_close),
                "low": min(closed["close"], new_close),
                "close": new_close, "open_time": now,
            }
        else:
            forming["close"] = new_close
            forming["high"] = max(forming["high"], new_close)
            forming["low"] = min(forming["low"], new_close)

    if _extra_last_checkpoint is None or (now - _extra_last_checkpoint).total_seconds() >= CHECKPOINT_SECONDS:
        _extra_checkpoint(db)
        _extra_last_checkpoint = now


def _extra_current(db: Session, sym: str) -> float:
    st = _extra_states.get(sym)
    if not st:
        return 0.0
    if st["forming"] is not None:
        return st["forming"]["close"]
    if st["closed"]:
        return st["closed"][-1]["close"]
    last = db.query(models.Candle).filter_by(symbol=extra_candle_symbol(sym)) \
        .order_by(models.Candle.open_time.desc()).first()
    return last.close if last else st["start"]


def get_current_price(db: Session, symbol: str = "GOLF") -> float:
    """Server-authoritative current price of a platform coin. GOLF keeps the
    original walk; NOVA/ABC/… use their own independent walks."""
    if symbol != "GOLF":
        return _extra_current(db, symbol)
    if _state["forming"] is not None:
        return _state["forming"]["close"]
    if _state["closed"]:
        return _state["closed"][-1]["close"]
    last = _last_db_candle(db)
    return last.close if last else MARKET_STARTING_PRICE


def get_candles(db: Session, symbol: str = SYMBOL, limit: int = 150) -> list:
    """Candle history for a platform coin. `symbol` is the paired candle
    id (default "GOLFUSDT"); extra coins accept "<SYM>USDT"."""
    if symbol != SYMBOL:
        return _extra_candles(db, symbol, limit)

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


def _extra_candles(db: Session, candle_symbol: str, limit: int) -> list:
    coin = candle_symbol[:-4] if candle_symbol.endswith("USDT") else candle_symbol
    st = _extra_states.get(coin)
    rows = (
        db.query(models.Candle).filter_by(symbol=candle_symbol)
        .order_by(models.Candle.open_time.desc()).limit(limit).all()
    )

    mem = {}
    if st:
        for c in st["closed"]:
            mem[c["open_time"]] = c
        if st["forming"] is not None:
            mem[st["forming"]["open_time"]] = st["forming"]

    out = []
    for row in rows:
        mine = mem.get(row.open_time)
        out.append(mine if mine is not None else _row_dict(row, candle_symbol))
    out.reverse()

    newest = out[-1]["open_time"] if out else None
    extra = [c for c in (st["closed"] if st else []) if newest is None or c["open_time"] > newest]
    extra.sort(key=lambda c: c["open_time"])
    if st and st["forming"] is not None and (newest is None or st["forming"]["open_time"] > newest):
        extra.append(st["forming"])
    return (out + extra)[-limit:]


def platform_launch_price(symbol: str) -> float:
    """USD price the platform coin launched at — the denominator for every
    change-% / performance figure, so it is always derived, never invented."""
    if symbol == "GOLF":
        return MARKET_STARTING_PRICE
    meta = PLATFORM_EXTRA_META.get(symbol, {})
    return float(meta.get("launch", DEFAULT_USD_PRICES.get(symbol, 0.0)))


def get_change_pct(db: Session, symbol: str) -> float:
    """Percent change of a platform coin since launch (real, derived)."""
    price = get_current_price(db, symbol)
    launch = platform_launch_price(symbol)
    if not launch:
        return 0.0
    return round((price - launch) / launch * 100.0, 2)


# =============================================================================
# Multi-coin USD prices (for Convert / Invest on any listed currency)
# =============================================================================

def get_usd_price(db: Session, symbol: str) -> float:
    """Server-authoritative USD/coin price used for all conversions.
    USDT is the base unit (1.0); GOLF and every other platform coin resolve
    from their own live walks; the remaining coins use the CoinGecko-
    refreshed table (or last-known default value)."""
    if symbol == "USDT":
        return 1.0
    if symbol == "GOLF":
        return get_current_price(db)
    if symbol in _extra_states:
        return _extra_current(db, symbol)
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