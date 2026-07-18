"""
Real market data via Yahoo Finance's public chart API.

(Stooq, the previous provider, now fronts its CSV endpoints with an
anti-bot challenge, so every server-side request 404s. Yahoo's
/v8/finance/chart endpoint needs no API key and returns JSON with a
regularMarketPrice field.)
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
# until the first successful quote fetch arrives (~5-10 seconds)
STATIC_SEEDS: Dict[str, float] = {
    "AAPL": 300.0,
    "MSFT": 422.0,
    "NVDA": 225.0,
    "AMZN": 264.0,
    "GOOGL": 397.0,
    "META": 614.0,
    "TSLA": 422.0,
}

# ── Quote fetch (Yahoo Finance) ────────────────────────────────────────────────
# Two interchangeable hosts; if one rate-limits (429), the other often accepts.
_QUOTE_URLS = [
    "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=1d",
    "https://query2.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=1d",
]

_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,*/*",
    "Accept-Language": "en-US,en;q=0.9",
}


async def _fetch_quote(symbol: str) -> Optional[float]:
    """
    Fetch the latest market price for *symbol* from Yahoo Finance.
    Tries each host in turn (rate limits differ per host).
    Returns None on any error.
    """
    try:
        async with httpx.AsyncClient(
            headers=_HTTP_HEADERS, follow_redirects=True, timeout=12.0
        ) as client:
            for url_tmpl in _QUOTE_URLS:
                url = url_tmpl.format(symbol=symbol.upper())
                resp = await client.get(url)

                if resp.status_code == 429:
                    log.warning("[QUOTE] %s HTTP 429 on %s", symbol, httpx.URL(url).host)
                    await asyncio.sleep(1.5)
                    continue
                if resp.status_code != 200:
                    log.warning("[QUOTE] %s HTTP %d", symbol, resp.status_code)
                    continue

                data = resp.json()
                result = (data.get("chart") or {}).get("result") or []
                if not result:
                    log.warning("[QUOTE] %s empty chart result", symbol)
                    continue

                meta = result[0].get("meta") or {}
                price = meta.get("regularMarketPrice")
                if price is None:
                    log.warning("[QUOTE] %s missing regularMarketPrice", symbol)
                    continue

                price = float(price)
                if price <= 0 or math.isnan(price):
                    log.warning("[QUOTE] %s bad price value: %s", symbol, price)
                    continue

                log.info("[QUOTE] %-6s → $%.4f  (market_time=%s)", symbol, price, meta.get("regularMarketTime"))
                return price

        return None

    except Exception:
        log.error("[QUOTE] %s fetch error:\n%s", symbol, traceback.format_exc())
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


async def _bootstrap_all(symbols: List[str]) -> None:
    """
    Fetch initial real prices one symbol at a time. Yahoo rate-limits
    bursts (HTTP 429), so requests are spaced out and failed symbols are
    retried in later passes with growing back-off.
    """
    remaining = [s.upper() for s in symbols]
    for attempt in range(1, 8):
        still_missing = []
        for sym in remaining:
            price = await _fetch_quote(sym)
            if price:
                _official[sym] = price
                _synth[sym] = price
                fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
                _official_info[sym] = {"source": "yahoo", "fetched_at": fetched_at}
            else:
                still_missing.append(sym)
            await asyncio.sleep(2.0 + random.uniform(0, 1))
        if not still_missing:
            log.info("[MARKET] bootstrap complete for all %d symbols", len(symbols))
            return
        remaining = still_missing
        wait = min(15 * attempt, 90)
        log.info("[MARKET] bootstrap pass %d: %s still missing, retry in %ds",
                 attempt, remaining, wait)
        await asyncio.sleep(wait)

    log.warning("[MARKET] bootstrap incomplete for %s; using static seeds", remaining)


_last_synth_step: Dict[str, float] = {}
_last_official_fetch: Dict[str, float] = {}
_fetch_inflight: set = set()
_fast_tick_sec: float = 1.5
_official_period_sec: int = 180


def _synth_step(sym: str) -> None:
    """Advance one symbol's synthetic price a single micro-tick (rate-limited)."""
    now = time.time()
    if now - _last_synth_step.get(sym, 0.0) < _fast_tick_sec:
        return
    _last_synth_step[sym] = now

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
        return

    if s_px is None:
        _synth[sym] = target
        return

    step = ALPHA * (target - s_px)
    max_move = (MAX_TICK_MOVE_BP / 10_000.0) * max(s_px, 1e-9)
    step = max(-max_move, min(max_move, step))
    noise = (NOISE_BP / 10_000.0) * s_px * (random.random() - 0.5) * 2.0
    new_px = s_px + step + noise
    if new_px > 0:
        _synth[sym] = new_px


async def _refresh_official(sym: str) -> None:
    try:
        price = await _fetch_quote(sym)
        if price:
            _official[sym] = price
            _synth.setdefault(sym, price)
            fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            _official_info[sym] = {"source": "yahoo", "fetched_at": fetched_at}
    finally:
        _fetch_inflight.discard(sym)


def request_refresh(symbol: str) -> None:
    """
    Opportunistic engine advance, called from request handlers.

    On Cloud Run, CPU is throttled to ~zero between requests, so pure
    background loops stall in production. Driving the engine from the
    polling requests themselves keeps prices moving whenever anyone is
    actually watching (and costs nothing when nobody is).
    """
    sym = symbol.upper()
    if sym.startswith("GAME"):
        return

    _synth_step(sym)

    now = time.time()
    if (now - _last_official_fetch.get(sym, 0.0) >= _official_period_sec
            and sym not in _fetch_inflight):
        _last_official_fetch[sym] = now
        _fetch_inflight.add(sym)
        asyncio.create_task(_refresh_official(sym))


async def _official_rotator(symbols: List[str], per_symbol_period_sec: int) -> None:
    """
    Cycle through symbols, refreshing each from the quote API every
    per_symbol_period_sec × len(symbols) seconds.
    """
    i = 0
    while True:
        sym = symbols[i % len(symbols)].upper()
        if sym not in _fetch_inflight:
            _last_official_fetch[sym] = time.time()
            _fetch_inflight.add(sym)
            await _refresh_official(sym)
        i += 1
        await asyncio.sleep(per_symbol_period_sec)


async def _fast_synth_loop(symbols: List[str], tick_sec: float) -> None:
    """
    Generate smooth micro-movements between quote refreshes.

    Target = 70% official + 30% order-book mid (so user trading activity
    creates realistic short-term pressure on the price).
    """
    random.seed(time.time())
    while True:
        for s in symbols:
            _synth_step(s.upper())
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
        official_period: seconds between quote refreshes per symbol
    """
    global _started, _fast_tick_sec, _official_period_sec
    if _started:
        return
    _started = True
    _fast_tick_sec = fast_tick
    _official_period_sec = official_period

    market_syms = [s for s in symbols if not s.upper().startswith("GAME")]
    log.info("[MARKET] Starting quote engine for %s", market_syms)

    if not market_syms:
        return

    _seed_estimates(market_syms)

    # Bootstrap sequentially — concurrent bursts trip Yahoo's rate limit
    asyncio.create_task(_bootstrap_all(market_syms))

    asyncio.create_task(_fast_synth_loop(market_syms, fast_tick))
    asyncio.create_task(_official_rotator(market_syms, official_period))
