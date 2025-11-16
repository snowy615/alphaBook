# app/market_data.py

"""
Multi-provider price poller for the mini-exchange.

Supported providers (set via env var PROVIDER):
  - alpha_vantage  (requires ALPHA_VANTAGE_KEY)
  - finnhub        (requires FINNHUB_KEY)
  - yfinance       (no key; may fail on some networks)
  - synthetic      (no network; random-walk fallback)

Optional Alpha Vantage settings:
  AV_INTERVAL:   1min | 5min | 15min | 30min | 60min   (default: 1min)
  AV_OUTPUTSIZE: compact | full                         (default: compact)
  AV_EXTENDED:   true | false                           (default: true)
  AV_ADJUSTED:   true | false                           (default: true)
"""

from __future__ import annotations

import os
import asyncio
import random
import logging
from typing import Dict, Iterable, Optional

import httpx

# Config via env
PROVIDER = os.getenv("PROVIDER", "yfinance").lower()

ALPHA_VANTAGE_KEY = os.getenv("ALPHA_VANTAGE_KEY", "")
AV_INTERVAL = os.getenv("AV_INTERVAL", "1min")
AV_OUTPUTSIZE = os.getenv("AV_OUTPUTSIZE", "compact")
AV_EXTENDED = os.getenv("AV_EXTENDED", "true")
AV_ADJUSTED = os.getenv("AV_ADJUSTED", "true")

FINNHUB_KEY = os.getenv("FINNHUB_KEY", "")

# Quiet down noisy libs
logging.getLogger("yfinance").setLevel(logging.ERROR)
log = logging.getLogger("market_data")

# Last-seen prices
_last: Dict[str, float] = {}

# Shared HTTP client
_http = httpx.Client(timeout=10.0, headers={"User-Agent": "mini-exchange/0.1"})

# ------------------------ Providers ------------------------ #

def _synthetic(symbol: str) -> float:
    """Offline random-walk price (always succeeds)."""
    last = _last.get(symbol, 100.0)
    return round(last * (1 + random.uniform(-0.001, 0.001)), 4)

def _alpha_vantage(symbol: str) -> Optional[float]:
    if not ALPHA_VANTAGE_KEY:
        return None
    try:
        r = _http.get(
            "https://www.alphavantage.co/query",
            params={
                "function": "TIME_SERIES_INTRADAY",
                "symbol": symbol,
                "interval": AV_INTERVAL,
                "outputsize": AV_OUTPUTSIZE,
                "extended_hours": AV_EXTENDED,
                "adjusted": AV_ADJUSTED,
                "datatype": "json",
                "apikey": ALPHA_VANTAGE_KEY,
            },
        )
        j = r.json()
        # Rate-limit or errors come back as 'Note' or 'Error Message'
        if "Note" in j or "Error Message" in j:
            msg = j.get("Note") or j.get("Error Message")
            log.warning("AlphaVantage note/error for %s: %s", symbol, msg)
            return None
        ts_key = f"Time Series ({AV_INTERVAL})"
        ts = j.get(ts_key) or {}
        if not ts:
            return None
        last_ts = sorted(ts.keys())[-1]
        return float(ts[last_ts]["4. close"])
    except Exception as e:
        log.debug("AlphaVantage exception for %s: %r", symbol, e)
        return None

def _finnhub(symbol: str) -> Optional[float]:
    if not FINNHUB_KEY:
        return None
    try:
        r = _http.get("https://finnhub.io/api/v1/quote",
                      params={"symbol": symbol, "token": FINNHUB_KEY})
        j = r.json()
        px = j.get("c")
        return float(px) if px not in (None, 0) else None
    except Exception as e:
        log.debug("Finnhub exception for %s: %r", symbol, e)
        return None

def _yfinance(symbol: str) -> Optional[float]:
    # yfinance can fail behind some networks; keep it optional
    try:
        import yfinance as yf
        df = yf.download(symbol, period="1d", interval="1m", progress=False)
        if not df.empty:
            return float(df["Close"].iloc[-1])
        df = yf.download(symbol, period="5d", interval="1d", progress=False)
        if not df.empty:
            return float(df["Close"].iloc[-1])
        return None
    except Exception as e:
        log.debug("yfinance exception for %s: %r", symbol, e)
        return None

# -------------------- Poller / Public API ------------------ #

def fetch_price_sync(symbol: str) -> float:
    """Fetch one price synchronously using the selected provider, with synthetic fallback."""
    if PROVIDER == "synthetic":
        return _synthetic(symbol)
    if PROVIDER == "alpha_vantage":
        px = _alpha_vantage(symbol)
        return px if px is not None else _synthetic(symbol)
    if PROVIDER == "finnhub":
        px = _finnhub(symbol)
        return px if px is not None else _synthetic(symbol)
    # default: yfinance
    px = _yfinance(symbol)
    return px if px is not None else _synthetic(symbol)

async def poll_prices(symbols: Iterable[str], interval_sec: int = 60):
    """Background task: periodically refresh _last for each symbol."""
    syms = list(set(symbols))
    log.info("Market data provider=%s symbols=%s interval=%ss", PROVIDER, syms, interval_sec)
    while True:
        for s in syms:
            try:
                px = await asyncio.to_thread(fetch_price_sync, s)
            except Exception:
                px = _synthetic(s)
            _last[s] = px
        await asyncio.sleep(interval_sec)

def get_last(symbol: str) -> Optional[float]:
    return _last.get(symbol)
