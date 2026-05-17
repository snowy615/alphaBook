"""
Real market data via Stooq (https://stooq.com).

Stooq provides real stock quotes (15-min delayed during market hours,
last close otherwise) with no API key and no rate limit for normal use.
We call it asynchronously via httpx, which is already in requirements.txt.

Response format (CSV):
  Symbol, Date, Time, Open, High, Low, Close, Volume, Name
  AAPL.US,2026-05-15,22:00:19,297.9,303.2,296.52,300.23,54862836,APPLE INC
"""

import asyncio
import logging
import math
import random
import time
import traceback
from datetime import datetime, timezone
from typing import Dict, List, Optional

import httpx

log = logging.getLogger("uvicorn.error")

# ── State ──────────────────────────────────────────────────────────────────────
_official: Dict[str, float] = {}          # last confirmed real price
_synth:    Dict[str, float] = {}          # smooth synthetic price (fast ticks)
_mid_hint: Dict[str, float] = {}          # order-book mid hint from MM bot
_official_info: Dict[str, Dict[str, str]] = {}
_started = False

# ── Synthetic engine tunables ──────────────────────────────────────────────────
ALPHA            = 0.20   # mean-reversion speed per tick toward target
NOISE_BP         = 3      # random noise per tick in basis points
MAX_TICK_MOVE_BP = 20     # hard cap on a single tick's move
DEFAULT_SEED     = 100.0

# Updated to real prices as of May 2026 — used only as startup fallback
# until the first successful Stooq fetch arrives (~5-10 seconds)
STATIC_SEEDS: Dict[str, float] = {
    "AAPL": 300.0,
    "MSFT": 422.0,
    "NVDA": 225.0,
    "AMZN": 264.0,
    "GOOGL": 397.0,
    "META": 614.0,
    "TSLA": 422.0,
}

# ── Stooq fetch ────────────────────────────────────────────────────────────────
_STOOQ_URL = "https://stooq.com/q/l/?s={symbol}.US&f=sd2t2ohlcvn"

_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*",
    "Accept-Language": "en-US,en;q=0.9",
}


async def _fetch_stooq(symbol: str) -> Optional[float]:
    """
    Fetch the latest close price for *symbol* from Stooq.

    CSV layout (0-based columns):
      0=symbol  1=date  2=time  3=open  4=high  5=low  6=close  7=volume  8=name
    Returns None on any error.
    """
    url = _STOOQ_URL.format(symbol=symbol.upper())
    try:
        async with httpx.AsyncClient(
            headers=_HTTP_HEADERS, follow_redirects=True, timeout=12.0
        ) as client:
            resp = await client.get(url)

        if resp.status_code != 200:
            log.warning("[STOOQ] %s HTTP %d", symbol, resp.status_code)
            return None

        text = resp.text.strip()
        # Last non-empty line is the data row (first line is header if any)
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if not lines:
            log.warning("[STOOQ] %s empty response", symbol)
            return None

        # Stooq returns one data line per symbol (no header row for this URL)
        parts = lines[-1].split(",")
        if len(parts) < 7:
            log.warning("[STOOQ] %s unexpected CSV: %r", symbol, lines[-1][:80])
            return None

        close_str = parts[6].strip()
        price = float(close_str)
        if price <= 0 or math.isnan(price):
            log.warning("[STOOQ] %s bad price value: %s", symbol, close_str)
            return None

        provider_ts = f"{parts[1].strip()} {parts[2].strip()}"
        log.info("[STOOQ] %-6s → $%.4f  (provider_ts=%s)", symbol, price, provider_ts)
        return price

    except Exception:
        log.error("[STOOQ] %s fetch error:\n%s", symbol, traceback.format_exc())
        return None


# ── Public engine API ──────────────────────────────────────────────────────────

def set_hint_mid(symbol: str, mid: Optional[float]) -> None:
    """Called by the order-book / MM bot to share the current book mid-price."""
    if mid is None or mid <= 0:
        return
    _mid_hint[symbol.upper()] = float(mid)


def get_ref_price(symbol: str) -> Optional[float]:
    sym = symbol.upper()
    if sym.startswith("GAME"):
        # Custom games: only use order-book mid; no external data
        return _mid_hint.get(sym)
    return _synth.get(sym) or _official.get(sym) or _mid_hint.get(sym)


def get_official_info() -> Dict[str, Dict[str, str]]:
    return _official_info


get_last = get_ref_price  # backwards-compat alias


# ── Internal engine ────────────────────────────────────────────────────────────

def _seed_estimates(symbols: List[str]) -> None:
    """Seed synthetic prices from STATIC_SEEDS ± 1% jitter before first fetch."""
    for s in symbols:
        sym = s.upper()
        if sym not in _synth:
            base = STATIC_SEEDS.get(sym, DEFAULT_SEED)
            jitter = base * (random.random() - 0.5) * 0.02
            _synth[sym] = max(1e-6, base + jitter)


async def _bootstrap_symbol(sym: str) -> None:
    """Fetch real price at startup; retry with back-off on failure."""
    backoff = 5.0
    for attempt in range(1, 8):
        price = await _fetch_stooq(sym)
        if price:
            _official[sym] = price
            _synth[sym] = price
            fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            _official_info[sym] = {
                "source": "stooq",
                "fetched_at": fetched_at,
            }
            return
        wait = backoff + random.uniform(0, 2)
        log.info("[MARKET] %s bootstrap attempt %d failed, retry in %.1fs", sym, attempt, wait)
        await asyncio.sleep(wait)
        backoff = min(backoff * 1.5, 60)

    log.warning("[MARKET] %s: all bootstrap attempts failed; using seed $%.2f",
                sym, _synth.get(sym, DEFAULT_SEED))


async def _official_rotator(symbols: List[str], per_symbol_period_sec: int) -> None:
    """
    Cycle through symbols, refreshing each from Stooq every
    per_symbol_period_sec × len(symbols) seconds.
    """
    i = 0
    while True:
        sym = symbols[i % len(symbols)].upper()
        price = await _fetch_stooq(sym)
        if price:
            _official[sym] = price
            _synth.setdefault(sym, price)
            fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            _official_info[sym] = {"source": "stooq", "fetched_at": fetched_at}
        i += 1
        await asyncio.sleep(per_symbol_period_sec)


async def _fast_synth_loop(symbols: List[str], tick_sec: float) -> None:
    """
    Generate smooth intra-second micro-movements between Stooq refreshes.

    Target = 70% official + 30% order-book mid (so user trading activity
    creates realistic short-term pressure on the price).
    """
    random.seed(time.time())
    while True:
        for s in symbols:
            sym = s.upper()
            s_px = _synth.get(sym)
            o    = _official.get(sym)
            m    = _mid_hint.get(sym)

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
            max_move = (MAX_TICK_MOVE_BP / 10_000.0) * max(s_px, 1e-9)
            step = max(-max_move, min(max_move, step))
            noise = (NOISE_BP / 10_000.0) * s_px * (random.random() - 0.5) * 2.0
            new_px = s_px + step + noise
            if new_px > 0:
                _synth[sym] = new_px

        await asyncio.sleep(tick_sec)


async def start_ref_engine(
    symbols: List[str],
    fast_tick: float = 1.5,
    official_period: int = 120,
) -> None:
    """
    Start the market data engine.

    Args:
        symbols:         all active game symbols
        fast_tick:       seconds between synthetic micro-ticks
        official_period: seconds between Stooq refreshes per symbol
    """
    global _started
    if _started:
        return
    _started = True

    market_syms = [s for s in symbols if not s.upper().startswith("GAME")]
    log.info("[MARKET] Starting Stooq engine for %s", market_syms)

    if not market_syms:
        return

    _seed_estimates(market_syms)

    # Bootstrap all symbols concurrently (staggered slightly)
    for i, s in enumerate(market_syms):
        asyncio.create_task(_bootstrap_symbol(s.upper()))
        await asyncio.sleep(0.5)  # stagger to avoid hitting Stooq all at once

    asyncio.create_task(_fast_synth_loop(market_syms, fast_tick))
    asyncio.create_task(_official_rotator(market_syms, official_period))
