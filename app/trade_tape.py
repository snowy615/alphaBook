# app/trade_tape.py
"""
In-memory recent-trades tape, one ring buffer per symbol.

Every execution — human/human, human/bot, and simulated bot prints —
gets appended here with display names already resolved, so the
frontend "Recent Trades" panel can show who traded without extra
lookups. Real trades are also persisted to Firestore elsewhere; this
tape is only the fast display feed and resets on restart (it is
re-seeded from Firestore on first request).
"""
import time
from collections import deque
from typing import Deque, Dict, List, Optional

MAX_TAPE = 100

_tapes: Dict[str, Deque[dict]] = {}
_seeded: set = set()


def record(
    symbol: str,
    price,
    qty,
    buyer_name: str,
    seller_name: str,
    taker_side: Optional[str] = None,   # "BUY" / "SELL" side of the aggressor, if known
    kind: str = "user",                 # "user" | "bot" | "sweep" | "history"
    ts_ms: Optional[int] = None,
) -> None:
    tape = _tapes.setdefault(symbol.upper(), deque(maxlen=MAX_TAPE))
    tape.append({
        "ts": ts_ms if ts_ms is not None else int(time.time() * 1000),
        "price": float(price),
        "qty": float(qty),
        "buyer": buyer_name,
        "seller": seller_name,
        "side": taker_side,
        "kind": kind,
    })


def get_tape(symbol: str, limit: int = 40) -> List[dict]:
    """Newest first."""
    tape = _tapes.get(symbol.upper())
    if not tape:
        return []
    return list(tape)[-limit:][::-1]


def needs_seed(symbol: str) -> bool:
    sym = symbol.upper()
    return sym not in _seeded and not _tapes.get(sym)


def mark_seeded(symbol: str) -> None:
    _seeded.add(symbol.upper())


def clear_all() -> None:
    _tapes.clear()
    _seeded.clear()
