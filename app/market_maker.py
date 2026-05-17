"""
Market-maker bot for AlphaBook's market simulation.

Two behaviours run on a shared timer for each symbol:

  1. PASSIVE QUOTING — maintains a bid/ask ladder around the current
     reference price so there is always liquidity in the book.

  2. ACTIVE SWEEPING — when the reference price moves, resting user
     orders that have become significantly off-side are filled by the
     bot, keeping the book honest and aligned with the real price.

The bot inserts passive quotes *directly* into the book (no matching
step) so it never trades with itself.  Active sweeps go through the
matching engine, and the resulting fills are forwarded to a callback
(registered in main.py) that writes them to Firestore.
"""

import asyncio
import logging
import random
import uuid
from collections import deque
from decimal import Decimal
from typing import Callable, Coroutine, Dict, List

from app.market_data import get_ref_price
from app.order_book import Order as BookOrder
from app.state import books, locks

log = logging.getLogger("uvicorn.error")

# ---------- Identity ----------
BOT_USER_ID = "__MM_BOT__"

# ---------- Quoting parameters ----------
HALF_SPREAD_BPS: int = 8       # half of the bid-ask spread in basis points
LEVELS: int = 5                 # price levels to quote each side
LEVEL_STEP_BPS: int = 5         # bps between consecutive levels
BASE_QTY: int = 60              # base shares per level
QTY_TAPER: int = 15             # additional shares per outer level (realistic depth)
QTY_JITTER: int = 10            # random ±jitter on each level's qty

# ---------- Sweep parameters ----------
# A user resting order is swept when it is further than this from the
# current mid price in the "wrong" direction (i.e. a user bid above mid,
# or a user ask below mid).  Set tighter than HALF_SPREAD to ensure only
# clearly off-market orders are touched; set to ~HALF_SPREAD + small buffer.
SWEEP_THRESHOLD_BPS: int = HALF_SPREAD_BPS + 2   # 10 bps by default

# ---------- Timing ----------
REFRESH_SEC: float = 4.0        # full requote every N seconds
STARTUP_DELAY_SEC: float = 8.0  # wait for real prices to arrive first

# ---------- Per-symbol state ----------
# Track which order IDs the bot currently owns per symbol so we can
# cancel them on the next refresh.
_bot_bid_ids: Dict[str, List[str]] = {}
_bot_ask_ids: Dict[str, List[str]] = {}
_last_mid: Dict[str, float] = {}

_started = False


# ---------- Helpers ----------

def _round_px(raw: float, ref: float) -> Decimal:
    """Round to 2 decimal places for stocks priced above $1."""
    decimals = 2 if ref >= 1.0 else 6
    return Decimal(str(round(raw, decimals)))


def _cancel_bot_quotes(symbol: str, book) -> None:
    """Remove all resting bot orders from the book in O(n)."""
    book.cancel_all_for_user(BOT_USER_ID)
    _bot_bid_ids[symbol] = []
    _bot_ask_ids[symbol] = []


def _place_passive_quotes(symbol: str, book, mid: float) -> None:
    """Insert a bid/ask ladder around *mid* directly into the book."""
    bid_ids: List[str] = []
    ask_ids: List[str] = []

    for i in range(LEVELS):
        bps = HALF_SPREAD_BPS + i * LEVEL_STEP_BPS
        bid_px = _round_px(mid * (1.0 - bps / 10_000.0), mid)
        ask_px = _round_px(mid * (1.0 + bps / 10_000.0), mid)

        qty_raw = BASE_QTY + i * QTY_TAPER + random.randint(-QTY_JITTER, QTY_JITTER)
        qty = Decimal(str(max(10, qty_raw)))

        bid_id = str(uuid.uuid4())
        book.bids.setdefault(bid_px, deque()).append(
            BookOrder(id=bid_id, user_id=BOT_USER_ID, side="BUY",
                      price=bid_px, qty=qty, orig_qty=qty)
        )
        bid_ids.append(bid_id)

        ask_id = str(uuid.uuid4())
        book.asks.setdefault(ask_px, deque()).append(
            BookOrder(id=ask_id, user_id=BOT_USER_ID, side="SELL",
                      price=ask_px, qty=qty, orig_qty=qty)
        )
        ask_ids.append(ask_id)

    _bot_bid_ids[symbol] = bid_ids
    _bot_ask_ids[symbol] = ask_ids


def _sweep_stale_orders(symbol: str, book, mid: float) -> List[dict]:
    """
    Fill user orders that are clearly off-market.

    Returns a list of fill dicts:
      {price, qty, buyer_id, seller_id, maker_order_id}
    """
    fills: List[dict] = []
    threshold = SWEEP_THRESHOLD_BPS / 10_000.0

    # Sweep user BIDS that are above mid + threshold
    # (price has fallen; their bid is now above market — bot sells to them)
    upper = mid * (1.0 + threshold)
    while True:
        best_bid = book._best_bid()
        if best_bid is None or float(best_bid) <= upper:
            break
        dq = book.bids[best_bid]
        head = dq[0]
        if head.user_id == BOT_USER_ID:
            # Shouldn't happen (we cancel bot orders first), but guard anyway
            dq.popleft()
            if not dq:
                del book.bids[best_bid]
            continue
        fill_qty = head.qty
        fills.append({
            "price": best_bid,
            "qty": fill_qty,
            "buyer_id": head.user_id,
            "seller_id": BOT_USER_ID,
            "maker_order_id": head.id,
        })
        dq.popleft()
        if not dq:
            del book.bids[best_bid]

    # Sweep user ASKS that are below mid - threshold
    # (price has risen; their ask is now below market — bot buys from them)
    lower = mid * (1.0 - threshold)
    while True:
        best_ask = book._best_ask()
        if best_ask is None or float(best_ask) >= lower:
            break
        dq = book.asks[best_ask]
        head = dq[0]
        if head.user_id == BOT_USER_ID:
            dq.popleft()
            if not dq:
                del book.asks[best_ask]
            continue
        fill_qty = head.qty
        fills.append({
            "price": best_ask,
            "qty": fill_qty,
            "buyer_id": BOT_USER_ID,
            "seller_id": head.user_id,
            "maker_order_id": head.id,
        })
        dq.popleft()
        if not dq:
            del book.asks[best_ask]

    return fills


# ---------- Per-symbol loop ----------

async def _run_mm_for_symbol(
    symbol: str,
    broadcast_fn: Callable,
    fill_handler: Callable[..., Coroutine],
) -> None:
    await asyncio.sleep(STARTUP_DELAY_SEC + random.uniform(0, 2))

    while True:
        try:
            mid = get_ref_price(symbol)
            if mid is None or mid <= 0:
                await asyncio.sleep(REFRESH_SEC)
                continue

            book = books[symbol]
            lock = locks[symbol]

            async with lock:
                _cancel_bot_quotes(symbol, book)
                sweep_fills = _sweep_stale_orders(symbol, book, mid)
                _place_passive_quotes(symbol, book, mid)
                snap = book.snapshot(depth=10)

            _last_mid[symbol] = mid

            if sweep_fills:
                asyncio.create_task(fill_handler(symbol, sweep_fills))

            await broadcast_fn(symbol, {
                "type": "snapshot",
                "symbol": symbol,
                "book": snap,
                "ref_price": mid,
            })

        except Exception:
            import traceback
            log.error("[MM] Error for %s:\n%s", symbol, traceback.format_exc())

        await asyncio.sleep(REFRESH_SEC)


# ---------- Public API ----------

async def start_market_maker(
    symbols: List[str],
    broadcast_fn: Callable,
    fill_handler: Callable[..., Coroutine],
) -> None:
    """
    Start market-making tasks for all non-custom-game symbols.

    Args:
        symbols:       full list of active symbols
        broadcast_fn:  async fn(symbol, payload) — sends WS updates
        fill_handler:  async fn(symbol, fills)   — records bot fills in DB
    """
    global _started
    if _started:
        return
    _started = True

    market_syms = [s.upper() for s in symbols if not s.upper().startswith("GAME")]
    log.info("[MM] Starting market maker for %s", market_syms)

    for i, sym in enumerate(market_syms):
        _bot_bid_ids[sym] = []
        _bot_ask_ids[sym] = []
        asyncio.create_task(_run_mm_for_symbol(sym, broadcast_fn, fill_handler))
        await asyncio.sleep(0.3)  # small stagger so all symbols don't hit at once
