import asyncio, os, random, time, logging, math, traceback
from datetime import datetime, timezone
from typing import Dict, List, Optional

log = logging.getLogger("uvicorn.error")

# ---------- State ----------
_official: Dict[str, float] = {}          # last real price from yfinance
_synth:    Dict[str, float] = {}          # smooth synthetic price (1.5s ticks)
_mid_hint: Dict[str, float] = {}          # last seen order book mid
_official_info: Dict[str, Dict[str, str]] = {}
_started = False

# ---------- Tunables ----------
ALPHA = 0.20           # mean-reversion speed per tick
NOISE_BP = 3           # random noise per tick in basis points
MAX_TICK_MOVE_BP = 20  # max single tick move in basis points
DEFAULT_SEED = 100.0
STATIC_SEEDS = {
    "AAPL": 190.0, "MSFT": 420.0, "NVDA": 900.0, "AMZN": 180.0,
    "GOOGL": 170.0, "META": 500.0, "TSLA": 250.0,
}


# ---------- yfinance fetch ----------
def _fetch_yf_price_sync(symbol: str) -> Optional[float]:
    """Fetch real market price via yfinance (runs in thread)."""
    try:
        import yfinance as yf
        ticker = yf.Ticker(symbol)

        # fast_info is the quickest path
        try:
            price = ticker.fast_info.last_price
            if price is not None and not math.isnan(float(price)) and float(price) > 0:
                return float(price)
        except Exception:
            pass

        # Fallback: latest 1-min bar
        hist = ticker.history(period="1d", interval="1m")
        if not hist.empty:
            return float(hist["Close"].iloc[-1])

        return None
    except Exception:
        log.error("[YF] Error fetching %s:\n%s", symbol, traceback.format_exc())
        return None


async def _fetch_official_price(symbol: str):
    """Async wrapper: returns (price, source, err_str)."""
    try:
        price = await asyncio.to_thread(_fetch_yf_price_sync, symbol)
        if price:
            return price, "yfinance", None
        return None, None, "no_data"
    except Exception:
        return None, None, "error"


# ---------- Engine public API ----------
def set_hint_mid(symbol: str, mid: Optional[float]) -> None:
    if mid is None or mid <= 0:
        return
    _mid_hint[symbol.upper()] = float(mid)


def get_ref_price(symbol: str) -> Optional[float]:
    sym = symbol.upper()
    if sym.startswith("GAME"):
        return _mid_hint.get(sym)
    return _synth.get(sym) or _official.get(sym) or _mid_hint.get(sym)


def get_official_info() -> Dict[str, Dict[str, str]]:
    return _official_info


get_last = get_ref_price  # back-compat alias


# ---------- Internal helpers ----------
def _seed_estimates(symbols: List[str]):
    for s in symbols:
        sym = s.upper()
        if sym not in _synth:
            base = STATIC_SEEDS.get(sym, DEFAULT_SEED)
            jitter = base * (random.random() - 0.5) * 0.02  # ±1%
            _synth[sym] = max(1e-6, base + jitter)


async def _bootstrap_symbol(sym: str):
    """Fetch real price at startup; retry with back-off if it fails."""
    backoff = 5.0
    for attempt in range(1, 6):
        price, src, err = await _fetch_official_price(sym)
        if price:
            _official[sym] = price
            _synth[sym] = price
            fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            _official_info[sym] = {
                "source": src or "yfinance",
                "provider_ts": fetched_at,
                "fetched_at": fetched_at,
            }
            log.info("[MARKET] %s bootstrapped at %.4f via %s", sym, price, src)
            return
        wait = backoff + random.uniform(0, 2)
        log.info("[MARKET] %s attempt=%d err=%s, retry in %.1fs", sym, attempt, err, wait)
        await asyncio.sleep(wait)
        backoff = min(backoff * 1.5, 60)

    log.warning("[MARKET] %s bootstrap failed after retries; using seed %.2f",
                sym, _synth.get(sym, DEFAULT_SEED))


async def _official_rotator(symbols: List[str], per_symbol_period_sec: int):
    """Periodically refresh official prices so synthetic engine stays anchored."""
    if not symbols:
        return
    i = 0
    while True:
        sym = symbols[i % len(symbols)].upper()
        price, src, err = await _fetch_official_price(sym)
        if price:
            _official[sym] = price
            _synth.setdefault(sym, price)
            fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            _official_info[sym] = {
                "source": src or "yfinance",
                "provider_ts": fetched_at,
                "fetched_at": fetched_at,
            }
            log.info("[MARKET] %s refreshed to %.4f", sym, price)
        else:
            log.warning("[MARKET] %s refresh failed: %s", sym, err)
        i += 1
        await asyncio.sleep(per_symbol_period_sec)


async def _fast_synth_loop(symbols: List[str], tick_sec: float):
    """Generate smooth intra-second price movements between official refreshes."""
    random.seed(time.time())
    while True:
        for s in symbols:
            sym = s.upper()
            s_px = _synth.get(sym)
            o = _official.get(sym)
            m = _mid_hint.get(sym)

            if o is not None and m is not None:
                target = 0.7 * o + 0.3 * m
            elif o is not None:
                target = o
            elif m is not None:
                target = m
            else:
                _synth.setdefault(sym, STATIC_SEEDS.get(sym, DEFAULT_SEED))
                continue

            if s_px is None:
                _synth[sym] = target
                continue

            step = ALPHA * (target - s_px)
            max_move = (MAX_TICK_MOVE_BP / 10000.0) * max(s_px, 1e-9)
            step = max(-max_move, min(max_move, step))
            noise = (NOISE_BP / 10000.0) * s_px * (random.random() - 0.5) * 2.0
            new_px = s_px + step + noise
            if new_px > 0:
                _synth[sym] = new_px

        await asyncio.sleep(tick_sec)


async def start_ref_engine(symbols: List[str], fast_tick: float = 1.5, official_period: int = 180):
    global _started
    if _started:
        return
    _started = True

    market_symbols = [s for s in symbols if not s.upper().startswith("GAME")]
    log.info("[MARKET] Starting engine for %s", market_symbols)

    if not market_symbols:
        return

    _seed_estimates(market_symbols)

    for s in market_symbols:
        asyncio.create_task(_bootstrap_symbol(s.upper()))

    asyncio.create_task(_fast_synth_loop(market_symbols, fast_tick))
    asyncio.create_task(_official_rotator(market_symbols, official_period))
