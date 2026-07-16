"""
Market-maker bot for AlphaBook's market simulation.

Design goals
------------
1. ALWAYS have orders in the book — never wipe the whole ladder at once.
2. GRADUAL updates — at most MAX_UPDATES_PER_TICK level changes per tick so
   the order book evolves smoothly instead of flashing in and out.
3. REALISTIC depth — qty varies per level (more size further out) with small
   random jitter, giving a natural-looking ladder.
4. DELAYED sweeps — a user order is only swept if it has been clearly
   off-market (> SWEEP_THRESHOLD_BPS from mid) for at least SWEEP_DELAY_SEC
   seconds.  Mild mismatches are left alone; only truly ridiculous prices
   disappear.

Architecture
------------
Each symbol gets its own async loop running every TICK_SEC seconds.
The bot tracks exactly which order is sitting at each level (bid_0 … bid_4,
ask_0 … ask_4).  Each tick it:
  a) computes where each level *should* be given the current mid price,
  b) identifies levels that are missing, consumed by a fill, or more than
     LEVEL_TOLERANCE_BPS from their target,
  c) cancels-and-replaces the most-deviated levels, up to MAX_UPDATES_PER_TICK,
  d) checks for user orders that have been off-market long enough to sweep.
"""

import asyncio
import logging
import random
import time
import traceback
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, Coroutine, Dict, List, Optional, Tuple

from app.market_data import get_ref_price
from app.order_book import Order as BookOrder
from app.state import books, locks

log = logging.getLogger("uvicorn.error")

# ── Identity ──────────────────────────────────────────────────────────────────
BOT_USER_ID = "__MM_BOT__"

# ── Quoting parameters ────────────────────────────────────────────────────────
LEVELS: int             = 5      # price levels per side
HALF_SPREAD_BPS: int    = 8      # innermost level offset from mid (basis points)
LEVEL_STEP_BPS: int     = 5      # gap between consecutive levels (bps)
LEVEL_TOLERANCE_BPS: float = 1.5 # skip update if within this many bps of target
BASE_QTY: int           = 30     # shares at the tightest (innermost) level
QTY_STEP: int           = 8      # extra shares per outer level
QTY_JITTER: int         = 7      # ±random variation on each level

# ── Update pacing ─────────────────────────────────────────────────────────────
TICK_SEC: float             = 0.9   # how often the loop runs per symbol
MAX_UPDATES_PER_TICK: int   = 2     # max level changes (cancel+replace) per tick
BROADCAST_EVERY_N_TICKS: int = 6    # heartbeat broadcast even with no changes (~5s)

# ── Sweep parameters ──────────────────────────────────────────────────────────
SWEEP_THRESHOLD_BPS: int = 55    # bps off-market before a user order is flagged
SWEEP_DELAY_SEC: float   = 5.0   # seconds flagged before actually swept

# ── Startup ───────────────────────────────────────────────────────────────────
STARTUP_DELAY_SEC: float = 10.0  # wait for yfinance prices before quoting


# ── Per-level state ───────────────────────────────────────────────────────────
@dataclass
class _Level:
    order_id: str
    price: Decimal


# ── Module-level state (one entry per symbol) ─────────────────────────────────
_bot_bids: Dict[str, List[Optional[_Level]]] = {}   # symbol → [level0 … level4]
_bot_asks: Dict[str, List[Optional[_Level]]] = {}
_pending_sweeps: Dict[str, Dict[str, float]] = defaultdict(dict)  # {order_id: sweep_at}
_tick_counter: Dict[str, int] = defaultdict(int)

_started = False


# ── Helpers ───────────────────────────────────────────────────────────────────

def _round_px(raw: float, ref: float) -> Decimal:
    """Round to 2 decimal places for stocks; more for very cheap instruments."""
    decimals = 2 if ref >= 1.0 else 6
    return Decimal(str(round(raw, decimals)))


def _target_prices(mid: float) -> Tuple[List[Decimal], List[Decimal]]:
    """Return (bid_targets, ask_targets) for all LEVELS."""
    bids, asks = [], []
    for i in range(LEVELS):
        bps = HALF_SPREAD_BPS + i * LEVEL_STEP_BPS
        bids.append(_round_px(mid * (1.0 - bps / 10_000.0), mid))
        asks.append(_round_px(mid * (1.0 + bps / 10_000.0), mid))
    return bids, asks


def _make_qty(level_index: int) -> Decimal:
    """Varied qty — more size at outer levels with random jitter."""
    base = BASE_QTY + level_index * QTY_STEP
    jitter = random.randint(-QTY_JITTER, QTY_JITTER)
    return Decimal(str(max(10, base + jitter)))


def _place_bot_order(book, side: str, price: Decimal, level_index: int) -> _Level:
    """Insert one bot resting order directly into the book."""
    oid = str(uuid.uuid4())
    qty = _make_qty(level_index)
    order = BookOrder(
        id=oid, user_id=BOT_USER_ID, side=side,
        price=price, qty=qty, orig_qty=qty,
    )
    if side == "BUY":
        book.bids.setdefault(price, deque()).append(order)
    else:
        book.asks.setdefault(price, deque()).append(order)
    return _Level(order_id=oid, price=price)


def _update_quotes(symbol: str, book, mid: float) -> int:
    """
    Incrementally update at most MAX_UPDATES_PER_TICK bot levels.

    Priority order:
      1. Levels with no order at all (missing)
      2. Levels whose order was consumed by a fill (no longer in book)
      3. Levels furthest from their target price (most deviated first)

    Returns the number of levels actually updated.
    """
    bids_state = _bot_bids.setdefault(symbol, [None] * LEVELS)
    asks_state = _bot_asks.setdefault(symbol, [None] * LEVELS)

    bid_targets, ask_targets = _target_prices(mid)

    # Build candidate list: (priority_score, side, level_index, target_price)
    # Higher score → higher update priority
    candidates: List[Tuple[float, str, int, Decimal]] = []

    for i, target in enumerate(bid_targets):
        lvl = bids_state[i]
        if lvl is None:
            candidates.append((1e9, "BUY", i, target))           # missing
        elif not book.has_active_order(lvl.order_id):
            candidates.append((1e9 - 1, "BUY", i, target))       # consumed
        else:
            dev = abs(float(lvl.price) - float(target)) / float(target) * 10_000
            if dev > LEVEL_TOLERANCE_BPS:
                candidates.append((dev, "BUY", i, target))

    for i, target in enumerate(ask_targets):
        lvl = asks_state[i]
        if lvl is None:
            candidates.append((1e9, "SELL", i, target))
        elif not book.has_active_order(lvl.order_id):
            candidates.append((1e9 - 1, "SELL", i, target))
        else:
            dev = abs(float(lvl.price) - float(target)) / float(target) * 10_000
            if dev > LEVEL_TOLERANCE_BPS:
                candidates.append((dev, "SELL", i, target))

    # Most urgent first
    candidates.sort(key=lambda x: x[0], reverse=True)

    updates = 0
    for _, side, i, target_px in candidates:
        if updates >= MAX_UPDATES_PER_TICK:
            break

        state_list = bids_state if side == "BUY" else asks_state

        # Cancel old order if still resting (may already be consumed)
        existing = state_list[i]
        if existing is not None:
            book.cancel(existing.order_id, BOT_USER_ID)
        state_list[i] = None

        # Place fresh order
        state_list[i] = _place_bot_order(book, side, target_px, i)
        updates += 1

    return updates


def _check_pending_sweeps(symbol: str, book, mid: float) -> List[dict]:
    """
    Two-phase sweep:
      Phase 1 — flag user orders that are > SWEEP_THRESHOLD_BPS off-market.
      Phase 2 — execute the sweep on any that have been flagged ≥ SWEEP_DELAY_SEC.

    Also cleans up flags for orders that came back in-range or were cancelled
    by the user themselves.
    """
    now = time.time()
    threshold = SWEEP_THRESHOLD_BPS / 10_000.0
    pending = _pending_sweeps[symbol]
    fills: List[dict] = []

    # ── Clean up stale flags (user cancelled, or order got filled elsewhere) ──
    for oid in list(pending.keys()):
        if not book.has_active_order(oid):
            del pending[oid]

    # ── Scan bids: flag/sweep user bids above mid + threshold ─────────────────
    upper_limit = mid * (1.0 + threshold)
    for px in list(book.bids.keys()):
        dq = book.bids.get(px)
        if dq is None:
            continue
        if float(px) > upper_limit:
            # Flag non-bot orders in this price bucket
            for order in list(dq):
                if order.user_id == BOT_USER_ID:
                    continue
                oid = order.id
                if oid not in pending:
                    pending[oid] = now + SWEEP_DELAY_SEC   # start the clock
                elif now >= pending[oid]:
                    # Sweep time — bot sells to this buyer
                    fill_qty = order.qty
                    fills.append({
                        "price": px, "qty": fill_qty,
                        "buyer_id": order.user_id,
                        "seller_id": BOT_USER_ID,
                        "maker_order_id": oid,
                        "maker_orig_qty": order.orig_qty,
                    })
                    dq.remove(order)
                    if not dq:
                        del book.bids[px]
                    pending.pop(oid, None)
        else:
            # Back in range — remove flags for any orders here
            for order in dq:
                pending.pop(order.id, None)

    # ── Scan asks: flag/sweep user asks below mid - threshold ─────────────────
    lower_limit = mid * (1.0 - threshold)
    for px in list(book.asks.keys()):
        dq = book.asks.get(px)
        if dq is None:
            continue
        if float(px) < lower_limit:
            for order in list(dq):
                if order.user_id == BOT_USER_ID:
                    continue
                oid = order.id
                if oid not in pending:
                    pending[oid] = now + SWEEP_DELAY_SEC
                elif now >= pending[oid]:
                    fill_qty = order.qty
                    fills.append({
                        "price": px, "qty": fill_qty,
                        "buyer_id": BOT_USER_ID,
                        "seller_id": order.user_id,
                        "maker_order_id": oid,
                        "maker_orig_qty": order.orig_qty,
                    })
                    dq.remove(order)
                    if not dq:
                        del book.asks[px]
                    pending.pop(oid, None)
        else:
            for order in dq:
                pending.pop(order.id, None)

    return fills


# ── Per-symbol loop ───────────────────────────────────────────────────────────

async def _run_mm_for_symbol(
    symbol: str,
    broadcast_fn: Callable,
    fill_handler: Callable[..., Coroutine],
) -> None:
    # Stagger startup: real prices need time to arrive from yfinance
    await asyncio.sleep(STARTUP_DELAY_SEC + random.uniform(0, 3))
    log.info("[MM] %s: market-maker loop started", symbol)

    while True:
        try:
            mid = get_ref_price(symbol)
            if mid is None or mid <= 0:
                await asyncio.sleep(TICK_SEC)
                continue

            book  = books[symbol]
            lock  = locks[symbol]

            async with lock:
                n_updates    = _update_quotes(symbol, book, mid)
                sweep_fills  = _check_pending_sweeps(symbol, book, mid)
                tick_n       = _tick_counter[symbol]
                _tick_counter[symbol] = (tick_n + 1) % BROADCAST_EVERY_N_TICKS
                need_broadcast = (n_updates > 0 or bool(sweep_fills)
                                  or tick_n == 0)
                snap = book.snapshot(depth=10) if need_broadcast else None

            if sweep_fills:
                asyncio.create_task(fill_handler(symbol, sweep_fills))

            if snap is not None:
                await broadcast_fn(symbol, {
                    "type": "snapshot",
                    "symbol": symbol,
                    "book": snap,
                    "ref_price": mid,
                })

        except Exception:
            log.error("[MM] %s error:\n%s", symbol, traceback.format_exc())

        await asyncio.sleep(TICK_SEC)


# ── Public entry point ────────────────────────────────────────────────────────

async def start_market_maker(
    symbols: List[str],
    broadcast_fn: Callable,
    fill_handler: Callable[..., Coroutine],
) -> None:
    """
    Launch a market-making loop for every non-custom-game symbol.

    Args:
        symbols:        full list of active game symbols
        broadcast_fn:   async (symbol, payload) → broadcasts WS updates
        fill_handler:   async (symbol, fills)   → records bot fills in DB
    """
    global _started
    if _started:
        return
    _started = True

    market_syms = [s.upper() for s in symbols if not s.upper().startswith("GAME")]
    log.info("[MM] Launching market maker for %s", market_syms)

    for sym in market_syms:
        _bot_bids[sym] = [None] * LEVELS
        _bot_asks[sym] = [None] * LEVELS
        asyncio.create_task(_run_mm_for_symbol(sym, broadcast_fn, fill_handler))
        await asyncio.sleep(0.4)   # stagger symbol starts
