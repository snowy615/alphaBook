import asyncio, json, os, random, time, logging, traceback, ssl
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlencode
from urllib.request import urlopen

# Log via uvicorn so messages always show
log = logging.getLogger("uvicorn.error")

# ---------- State ----------
_official: Dict[str, float] = {}          # last official price
_synth:    Dict[str, float] = {}          # smooth price (1.5s updates)
_mid_hint: Dict[str, float] = {}          # last seen book mid
_official_info: Dict[str, Dict[str, str]] = {}  # meta: source/provider_ts/fetched_at
_started = False

# ---------- Tunables ----------
INTERVAL_STR = "1min"
ALPHA = 0.20                  # mean-reversion speed per tick
NOISE_BP = 3                  # random noise per tick in bps
MAX_TICK_MOVE_BP = 20
DEFAULT_SEED = 100.0
STATIC_SEEDS = {
    "AAPL": 190.0, "MSFT": 420.0, "NVDA": 1150.0, "AMZN": 170.0,
    "GOOGL": 150.0, "META": 330.0, "TSLA": 200.0,
}

# ---------- SSL toggle (TEMPORARY) ----------
# Set SKIP_SSL_VERIFY=0 to re-enable verification.
SKIP_SSL_VERIFY = str(os.getenv("SKIP_SSL_VERIFY", "0")).strip().lower() in ("1", "true", "yes", "y")
if SKIP_SSL_VERIFY:
    log.warning("[AV] SSL verification is DISABLED (temporary). Do not use in production.")

def _ssl_context():
    if SKIP_SSL_VERIFY:
        try:
            return ssl._create_unverified_context()
        except Exception:
            return None
    # Default verified context
    try:
        return ssl.create_default_context()
    except Exception:
        return None

# ---------- Env key ----------
def _av_key() -> Optional[str]:
    return (
        os.getenv("ALPHAVANTAGE_API_KEY")
        or os.getenv("ALPHA_VANTAGE_API_KEY")
    )

# ---------- HTTP helpers ----------
async def _http_json(url: str) -> Tuple[Optional[dict], Optional[str], Optional[str]]:
    """Return (json, err_code, raw_text). err_code in {'throttle','error',None}."""
    try:
        def _get():
            ctx = _ssl_context()
            # Pass context when available; urllib handles http/https appropriately.
            if ctx is not None:
                with urlopen(url, timeout=15, context=ctx) as r:
                    return r.read()
            else:
                with urlopen(url, timeout=15) as r:
                    return r.read()

        raw: bytes = await asyncio.to_thread(_get)
        text = raw.decode("utf-8", errors="ignore")
        try:
            data = json.loads(text)
        except Exception:
            log.error("[AV] JSON parse error. first200=%r", text[:200])
            return None, "error", text

        if any(k in data for k in ("Note", "Information")):
            msg = data.get("Note") or data.get("Information")
            log.info("[AV] throttle: %s", msg)
            return None, "throttle", text
        if "Error Message" in data:
            log.warning("[AV] error msg: %s", data["Error Message"])
            return None, "error", text
        return data, None, text
    except Exception:
        log.error("[AV] fetch exception: %s\n%s", url, traceback.format_exc())
        return None, "error", None

async def _fetch_global_quote(symbol: str) -> Tuple[Optional[float], Optional[str], Optional[str]]:
    """GLOBAL_QUOTE: (price, provider_ts (YYYY-MM-DD), err_code)."""
    key = _av_key()
    if not key:
        return None, None, "error"
    url = "https://www.alphavantage.co/query?" + urlencode(
        {"function": "GLOBAL_QUOTE", "symbol": symbol, "apikey": key}
    )
    data, err, raw = await _http_json(url)
    if err or not data:
        return None, None, err
    gq = data.get("Global Quote") or {}
    px = gq.get("05. price") or gq.get("05. Price")
    day = gq.get("07. latest trading day") or gq.get("07. Latest Trading Day")
    if not px:
        log.info("[AV] GLOBAL_QUOTE empty for %s. raw=%r", symbol, (raw[:200] if raw else raw))
        return None, None, "error"
    try:
        return float(px), day, None
    except Exception:
        return None, None, "error"

async def _fetch_intraday_once(symbol: str) -> Tuple[Optional[float], Optional[str], Optional[str]]:
    """INTRADAY: (price, provider_ts (YYYY-MM-DD HH:MM:SS), err_code)."""
    key = _av_key()
    if not key:
        return None, None, "error"
    url = "https://www.alphavantage.co/query?" + urlencode(
        {
            "function": "TIME_SERIES_INTRADAY",
            "symbol": symbol,
            "interval": INTERVAL_STR,
            "outputsize": "compact",
            "datatype": "json",
            "apikey": key,
        }
    )
    data, err, raw = await _http_json(url)
    if err or not data:
        return None, None, err
    key_ts = f"Time Series ({INTERVAL_STR})"
    ts = data.get(key_ts) or data.get("Time Series (1min)") or {}
    if not ts:
        log.info("[AV] INTRADAY empty for %s. raw=%r", symbol, (raw[:200] if raw else raw))
        return None, None, "error"
    latest_ts = max(ts.keys())
    last_close = ts[latest_ts].get("4. close") or ts[latest_ts].get("5. adjusted close")
    try:
        return float(last_close), latest_ts, None
    except Exception:
        return None, None, "error"

async def _fetch_official_meta(symbol: str) -> Tuple[Optional[float], Optional[str], Optional[str], Optional[str]]:
    """Try GLOBAL_QUOTE then INTRADAY. Returns (price, source, provider_ts, err_code)."""
    px, day, err = await _fetch_global_quote(symbol)
    if px:
        return px, "GLOBAL_QUOTE", day or "N/A", None
    if err == "throttle":
        return None, None, None, "throttle"
    px, ts, err2 = await _fetch_intraday_once(symbol)
    if px:
        return px, f"INTRADAY_{INTERVAL_STR}", ts or "N/A", None
    return None, None, None, err2 or err

# ---------- Engine API ----------
def set_hint_mid(symbol: str, mid: Optional[float]) -> None:
    if mid is None or mid <= 0:
        return
    _mid_hint[symbol.upper()] = float(mid)

def get_ref_price(symbol: str) -> Optional[float]:
    sym = symbol.upper()
    return _synth.get(sym) or _official.get(sym) or _mid_hint.get(sym)

def get_official_info() -> Dict[str, Dict[str, str]]:
    return _official_info

get_last = get_ref_price  # back-compat

def _seed_estimates(symbols: List[str]):
    for s in symbols:
        sym = s.upper()
        if sym not in _synth:
            base = STATIC_SEEDS.get(sym, DEFAULT_SEED)
            jitter = base * (random.random() - 0.5) * 0.02  # ±1%
            _synth[sym] = max(1e-6, base + jitter)

async def _bootstrap_symbol(sym: str):
    backoff = 6.0
    attempt = 1
    while True:
        px, src, provider_ts, err = await _fetch_official_meta(sym)
        if px:
            _official[sym] = px
            _synth[sym] = px
            fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            _official_info[sym] = {
                "source": src or "unknown",
                "provider_ts": provider_ts or "N/A",
                "fetched_at": fetched_at,
            }
            log.info("[OFFICIAL] %s %.4f via %s | provider_ts=%s | fetched_at=%s",
                     sym, px, _official_info[sym]["source"], _official_info[sym]["provider_ts"], fetched_at)
            return
        key_present = bool(_av_key())
        if err == "throttle":
            wait = 15 + random.uniform(0, 3)
            log.info("[RETRY] %s attempt=%d err=throttle wait=%.1fs key_present=%s",
                     sym, attempt, wait, key_present)
        else:
            wait = backoff + random.uniform(0, 2)
            backoff = min(backoff * 1.5, 30)
            log.info("[RETRY] %s attempt=%d err=%s wait=%.1fs key_present=%s",
                     sym, attempt, err or "error", wait, key_present)
        attempt += 1
        await asyncio.sleep(wait)

async def _official_rotator(symbols: List[str], per_symbol_period_sec: int):
    if not symbols:
        return
    i = 0
    while True:
        sym = symbols[i % len(symbols)].upper()
        px, src, provider_ts, err = await _fetch_official_meta(sym)
        if px:
            _official[sym] = px
            _synth.setdefault(sym, px)
            fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            _official_info[sym] = {
                "source": src or "unknown",
                "provider_ts": provider_ts or "N/A",
                "fetched_at": fetched_at,
            }
            log.info("[REFRESH]  %s %.4f via %s | provider_ts=%s | fetched_at=%s",
                     sym, px, _official_info[sym]["source"], _official_info[sym]["provider_ts"], fetched_at)
        i += 1
        await asyncio.sleep(per_symbol_period_sec)

async def _fast_synth_loop(symbols: List[str], tick_sec: float):
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
            if step > max_move: step = max_move
            if step < -max_move: step = -max_move
            noise = (NOISE_BP / 10000.0) * s_px * (random.random() - 0.5) * 2.0
            new_s = s_px + step + noise
            if new_s > 0:
                _synth[sym] = new_s

        await asyncio.sleep(tick_sec)

async def start_ref_engine(symbols: List[str], fast_tick: float = 1.5, official_period: int = 180):
    global _started
    if _started:
        return
    _started = True
    log.info("[REF-ENGINE] start symbols=%s api_key_present=%s", symbols, bool(_av_key()))
    if not symbols:
        return
    _seed_estimates(symbols)
    for s in symbols:
        asyncio.create_task(_bootstrap_symbol(s.upper()))
    asyncio.create_task(_fast_synth_loop(symbols, fast_tick))
    asyncio.create_task(_official_rotator(symbols, official_period))
