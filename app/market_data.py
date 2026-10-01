"""
Real market prices for the Market Simulation.
=============================================

Where a price comes from, best first:

1. **Finnhub** (``FINNHUB_API_KEY``): a free-tier key gives real-time US
   stock quotes at 60 calls a minute, and unlike Yahoo it doesn't refuse
   requests from cloud servers. Each symbol is refreshed every
   ``PERIOD_FINNHUB`` seconds while anyone is watching.
2. **Yahoo Finance's public chart API**: no key, but it rate-limits shared
   cloud addresses (HTTP 429) most of the time, so it's only a fallback.
3. **The last real price any instance saw**, kept in Firestore
   (``market_quotes/{SYMBOL}``). Cloud Run instances come and go and each
   has its own memory, so without this a fresh instance would forget every
   real price and fall back to stale seeds.
4. **Static seeds**, only until one of the above answers.

Between real quotes the displayed (synthetic) price moves a little around
the real one, so the market has life without wandering from reality: it's
pulled hard toward the real price, a jump of more than ``SNAP_PCT`` snaps
straight to it, order-book pressure can only nudge it within
``MID_NUDGE_MAX_BP``, and there's no random drift while the US market is
closed (a closed market doesn't move).
"""

import asyncio
import datetime as dt
import logging
import math
import os
import random
import time
import traceback
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger("uvicorn.error")

FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "").strip()

# ── State ──────────────────────────────────────────────────────────────────────
_official: Dict[str, float] = {}          # last real price
_official_info: Dict[str, Dict[str, str]] = {}   # source, quote time, fetched time
_synth:    Dict[str, float] = {}          # displayed price (moves between quotes)
_mid_hint: Dict[str, float] = {}          # order-book mid from the market maker
_started = False

# ── Tunables ───────────────────────────────────────────────────────────────────
PERIOD_FINNHUB   = 20     # seconds between real quotes per symbol, with a key
PERIOD_YAHOO     = 180    # without one (Yahoo throttles cloud servers hard)
ALPHA            = 0.35   # pull toward the real price per synthetic tick
NOISE_BP         = 1.0    # random noise per tick, in basis points (market open only)
MAX_TICK_MOVE_BP = 15     # cap on one tick's move, except a snap
SNAP_PCT         = 1.0    # a real price this far from the displayed one is jumped to
MID_NUDGE_MAX_BP = 15     # how far order-book pressure can lean the price off the real one
DEFAULT_SEED     = 100.0

QUOTE_CACHE           = "market_quotes"
CACHE_WRITE_EVERY_SEC = 120   # at most one Firestore write per symbol per this
CACHE_READ_EVERY_SEC  = 60    # how often a failing instance looks for another's quote

# Startup fallback only, until a real quote (or the shared cache) answers.
STATIC_SEEDS: Dict[str, float] = {
    "AAPL": 300.0, "MSFT": 422.0, "NVDA": 225.0, "AMZN": 264.0,
    "GOOGL": 397.0, "META": 614.0, "TSLA": 422.0,
}

_NEW_YORK = ZoneInfo("America/New_York")


def us_market_open(now: Optional[dt.datetime] = None) -> bool:
    """NYSE/Nasdaq regular hours: 9:30 to 16:00 New York time, Monday to
    Friday. Exchange holidays aren't modelled; on one, the real quote simply
    doesn't change."""
    t = (now or dt.datetime.now(dt.timezone.utc)).astimezone(_NEW_YORK)
    if t.weekday() >= 5:
        return False
    minutes = t.hour * 60 + t.minute
    return 9 * 60 + 30 <= minutes < 16 * 60


def _period() -> int:
    return PERIOD_FINNHUB if FINNHUB_API_KEY else PERIOD_YAHOO


# ── Quote providers ────────────────────────────────────────────────────────────
_YAHOO_URLS = [
    "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=1d",
    "https://query2.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=1d",
]
# Deliberately plain: Yahoo answers a bare "Mozilla/5.0" but refuses (429) a
# full desktop-Chrome User-Agent that arrives without the headers a real
# Chrome sends with it, which is what the old fetcher looked like.
_HTTP_HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "*/*"}


def _good(price) -> bool:
    try:
        p = float(price)
    except (TypeError, ValueError):
        return False
    return p > 0 and not math.isnan(p)


async def _fetch_finnhub(client: httpx.AsyncClient, symbol: str) -> Optional[Tuple[float, int]]:
    if not FINNHUB_API_KEY:
        return None
    resp = await client.get("https://finnhub.io/api/v1/quote", params={"symbol": symbol},
                            headers={"X-Finnhub-Token": FINNHUB_API_KEY})
    if resp.status_code != 200:
        log.warning("[QUOTE] %s Finnhub HTTP %d", symbol, resp.status_code)
        return None
    data = resp.json() or {}
    # "c" is the current price, "t" the quote's own time; an unknown symbol
    # comes back as all zeros.
    if not _good(data.get("c")):
        log.warning("[QUOTE] %s Finnhub returned no price", symbol)
        return None
    return float(data["c"]), int(data.get("t") or time.time())


async def _fetch_yahoo(client: httpx.AsyncClient, symbol: str) -> Optional[Tuple[float, int]]:
    for url_tmpl in _YAHOO_URLS:
        resp = await client.get(url_tmpl.format(symbol=symbol), headers=_HTTP_HEADERS)
        if resp.status_code == 429:
            log.warning("[QUOTE] %s Yahoo HTTP 429 on %s", symbol, httpx.URL(str(resp.url)).host)
            continue
        if resp.status_code != 200:
            log.warning("[QUOTE] %s Yahoo HTTP %d", symbol, resp.status_code)
            continue
        result = ((resp.json() or {}).get("chart") or {}).get("result") or []
        meta = (result[0].get("meta") or {}) if result else {}
        if _good(meta.get("regularMarketPrice")):
            return float(meta["regularMarketPrice"]), int(meta.get("regularMarketTime") or time.time())
    return None


async def _fetch_quote(symbol: str) -> Optional[Tuple[float, int, str]]:
    """(price, quote time, source) from the best provider that answers."""
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=10.0) as client:
            got = await _fetch_finnhub(client, symbol)
            if got:
                return got[0], got[1], "finnhub"
            got = await _fetch_yahoo(client, symbol)
            if got:
                return got[0], got[1], "yahoo"
    except Exception:
        log.error("[QUOTE] %s fetch error:\n%s", symbol, traceback.format_exc())
    return None


# ── The shared cache (Firestore) ───────────────────────────────────────────────
_last_cache_write: Dict[str, float] = {}
_last_cache_read: Dict[str, float] = {}


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def _quote_ts(sym: str) -> float:
    try:
        return datetime.fromisoformat(_official_info[sym]["quote_time"]).timestamp()
    except (KeyError, ValueError):
        return 0.0


def _accept(sym: str, price: float, quote_ts: float, source: str) -> None:
    """Take a real price: remember it, and snap the displayed price to it if
    it's far off (after a restart, or a gap in quotes)."""
    _official[sym] = price
    _official_info[sym] = {"source": source, "quote_time": _iso(quote_ts), "fetched_at": _iso(time.time())}
    shown = _synth.get(sym)
    if shown is None or abs(shown - price) / price * 100 > SNAP_PCT:
        _synth[sym] = price


async def _write_cache(sym: str) -> None:
    from app import db as db_module
    if db_module.db is None or time.time() - _last_cache_write.get(sym, 0) < CACHE_WRITE_EVERY_SEC:
        return
    _last_cache_write[sym] = time.time()
    try:
        await db_module.db.collection(QUOTE_CACHE).document(sym).set({
            "price": _official[sym], **_official_info[sym]})
    except Exception:
        log.warning("[QUOTE] %s couldn't save to the shared cache", sym)


async def _read_cache(sym: str) -> None:
    """Pick up a newer real price another instance saved."""
    from app import db as db_module
    if db_module.db is None or time.time() - _last_cache_read.get(sym, 0) < CACHE_READ_EVERY_SEC:
        return
    _last_cache_read[sym] = time.time()
    try:
        doc = await db_module.db.collection(QUOTE_CACHE).document(sym).get()
    except Exception:
        return
    data = (doc.to_dict() or {}) if doc.exists else {}
    if not _good(data.get("price")):
        return
    try:
        ts = datetime.fromisoformat(data.get("quote_time", "")).timestamp()
    except ValueError:
        return
    if ts > _quote_ts(sym):
        _accept(sym, float(data["price"]), ts, data.get("source") or "cache")
        _last_cache_write[sym] = time.time()   # it's already in the cache


# ── Public API ─────────────────────────────────────────────────────────────────

def set_hint_mid(symbol: str, mid: Optional[float]) -> None:
    """Called with the order book's mid, so trading can nudge the price."""
    if mid is None or mid <= 0:
        return
    _mid_hint[symbol.upper()] = float(mid)


def get_ref_price(symbol: str) -> Optional[float]:
    sym = symbol.upper()
    if sym.startswith("GAME"):
        # Custom games: only use order-book mid; no external data
        return _mid_hint.get(sym)
    return _synth.get(sym) or _official.get(sym) or _mid_hint.get(sym)


def get_official_price(symbol: str) -> Optional[float]:
    """The last real price (no synthetic movement), or None if there's none yet."""
    return _official.get(symbol.upper())


def get_official_info() -> Dict[str, Dict[str, str]]:
    return _official_info


def quote_status(symbol: str) -> Dict[str, object]:
    """What the page says about a price: where the real one came from, how
    old it is, and whether the US market is open."""
    sym = symbol.upper()
    info = _official_info.get(sym) or {}
    return {
        "symbol": sym,
        "price": get_ref_price(sym),
        "real_price": _official.get(sym),
        "source": info.get("source") or "simulated",
        "quote_time": info.get("quote_time"),
        "market_open": us_market_open(),
    }


get_last = get_ref_price  # backwards-compat alias


# ── Engine ─────────────────────────────────────────────────────────────────────
_last_synth_step: Dict[str, float] = {}
_last_fetch: Dict[str, float] = {}
_inflight: set = set()
_fast_tick_sec: float = 1.5


def _synth_step(sym: str) -> None:
    """Move the displayed price one tick: toward the real price (leaned on a
    little by the order book), plus a little noise while the market's open."""
    now = time.time()
    if now - _last_synth_step.get(sym, 0.0) < _fast_tick_sec:
        return
    _last_synth_step[sym] = now

    real = _official.get(sym)
    mid = _mid_hint.get(sym)
    shown = _synth.get(sym)
    if real is None:
        if shown is None:
            _synth[sym] = mid or STATIC_SEEDS.get(sym, DEFAULT_SEED)
        return

    lean = 0.0
    if mid:
        cap = real * MID_NUDGE_MAX_BP / 10_000
        lean = max(-cap, min(cap, 0.3 * (mid - real)))
    target = real + lean
    if shown is None:
        _synth[sym] = target
        return

    max_move = shown * MAX_TICK_MOVE_BP / 10_000
    step = max(-max_move, min(max_move, ALPHA * (target - shown)))
    noise = shown * (NOISE_BP / 10_000) * (random.random() - 0.5) * 2 if us_market_open() else 0.0
    if shown + step + noise > 0:
        _synth[sym] = shown + step + noise


async def _refresh(sym: str) -> None:
    try:
        got = await _fetch_quote(sym)
        if got:
            _accept(sym, *got)
            await _write_cache(sym)
        else:
            await _read_cache(sym)
    finally:
        _inflight.discard(sym)


def request_refresh(symbol: str) -> None:
    """
    Advance the engine from a request handler.

    On Cloud Run, CPU is throttled to ~zero between requests, so background
    loops stall in production; the page's own polling is what keeps prices
    moving whenever anyone is watching (and costs nothing when nobody is).
    """
    sym = symbol.upper()
    if sym.startswith("GAME"):
        return
    _synth_step(sym)
    now = time.time()
    if now - _last_fetch.get(sym, 0.0) >= _period() and sym not in _inflight:
        _last_fetch[sym] = now
        _inflight.add(sym)
        asyncio.create_task(_refresh(sym))


async def _bootstrap(symbols: List[str]) -> None:
    """On start: the shared cache first (instant, and real), then live quotes
    one symbol at a time (bursts trip rate limits)."""
    for sym in symbols:
        _last_cache_read.pop(sym, None)
        await _read_cache(sym)
    for sym in symbols:
        if sym in _inflight:
            continue
        _last_fetch[sym] = time.time()
        _inflight.add(sym)
        await _refresh(sym)
        await asyncio.sleep(1.0 if FINNHUB_API_KEY else 3.0)
    log.info("[MARKET] started: %s", {s: _official_info.get(s, {}).get("source", "seed") for s in symbols})


async def _loop(symbols: List[str]) -> None:
    """Background ticking while the instance has CPU (between requests it
    usually doesn't; request_refresh covers that)."""
    random.seed(time.time())
    while True:
        for sym in symbols:
            request_refresh(sym)
        await asyncio.sleep(_fast_tick_sec)


async def start_ref_engine(symbols: List[str], fast_tick: float = 1.5, official_period: int = 0) -> None:
    """Start the engine for the real-stock symbols. ``official_period`` is
    kept for compatibility; the refresh period follows the provider."""
    global _started, _fast_tick_sec
    if _started:
        return
    _started = True
    _fast_tick_sec = fast_tick
    market_syms = [s.upper() for s in symbols if not s.upper().startswith("GAME")]
    if not market_syms:
        return
    log.info("[MARKET] quote engine for %s via %s", market_syms,
             "Finnhub" if FINNHUB_API_KEY else "Yahoo (no FINNHUB_API_KEY set)")
    for sym in market_syms:
        _synth.setdefault(sym, STATIC_SEEDS.get(sym, DEFAULT_SEED))
    asyncio.create_task(_bootstrap(market_syms))
    asyncio.create_task(_loop(market_syms))
