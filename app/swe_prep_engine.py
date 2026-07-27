"""
SWE Prep — the simulation engine.
=============================================

A self-contained algo-trading contest.  Every player submits a Python strategy
(see :mod:`app.swe_prep_sandbox`); the engine calls each one once per tick for ten
minutes while house bots quote and take on the other side.  Whoever ends with
the best mark-to-market P&L wins.

Deliberate design choices
-------------------------
* **Isolated from the live game.**  Its own books, its own fictional items, its
  own Firestore collection.  Nothing here touches the AAPL/MSFT order book that
  human traders use.
* **Fair-value marking.**  P&L marks unrealised positions at each item's hidden
  fair value, not at the book mid.  Marking at mid would let a player print a
  silly quote against themselves and manufacture profit.
* **Request-driven, like the rest of AlphaBook.**  Cloud Run throttles the CPU
  between requests, so background loops stall in production.  :meth:`Run.advance`
  catches up however many ticks have elapsed and is called from the state
  endpoint that the front end polls.
* **`advance()` never awaits.**  It is a plain synchronous function, so the
  event loop runs it to completion and concurrent pollers cannot interleave
  half-finished ticks.
"""

from __future__ import annotations

import datetime as dt
import logging
import random
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Deque, Dict, List, Optional, Tuple

from app.swe_prep_sandbox import (
    SandboxError,
    Strategy,
    StrategyTimeout,
    compile_strategy,
)
from app.order_book import Order as BookOrder, OrderBook

log = logging.getLogger("uvicorn.error")

# ── Contest rules ────────────────────────────────────────────────────────────
POSITION_LIMIT: int = 1000        # absolute position cap, per item, per player
RUN_SECONDS: int = 600            # ten minutes
TICK_SECONDS: float = 1.0
TOTAL_TICKS: int = int(RUN_SECONDS / TICK_SECONDS)

MAX_PARTICIPANTS: int = 24        # keeps the worst-case tick cost bounded
MAX_ORDERS_PER_TICK: int = 20
# Hard ceiling on the returned list length. A strategy can build a huge list
# cheaply with ``[x] * N`` (one bytecode, so the line budget never sees it);
# without this guard, ``_apply_orders`` would then copy the whole thing. We
# refuse to even measure past this by checking len() — O(1) on a list — first.
MAX_ORDER_LIST: int = 1000
MAX_CATCHUP_TICKS: int = 60       # ticks replayed in a single advance() call

# ── Strategy budgets ─────────────────────────────────────────────────────────
# A tick has to stay cheap: advance() runs on the event loop, so the worst case
# is MAX_PARTICIPANTS × STRATEGY_TIMEOUT_SEC of blocking. A well-behaved
# strategy costs well under a millisecond.
STRATEGY_TIMEOUT_SEC: float = 0.08
STRATEGY_LOAD_TIMEOUT_SEC: float = 1.0
MAX_TIMEOUTS: int = 3             # timeouts tolerated before disqualification
MAX_ERRORS: int = 25              # exceptions tolerated before disqualification
LOG_LINES: int = 200              # per-player log ring buffer

# ── Market microstructure ────────────────────────────────────────────────────
PRICE_DP: int = 2
MAX_PRICE: float = 1_000_000.0

MM_LEVELS: int = 3
MM_HALF_SPREAD_BPS: float = 15.0
MM_STEP_BPS: float = 12.0
MM_BASE_QTY: int = 30
MM_QTY_STEP: int = 20
MM_NOISE_BPS: float = 4.0
# The maker quotes around its own lagging opinion of fair value, not the real
# one. Without this it would be unbeatable, and there would be no edge for a
# strategy to find; with it, price discovery is the whole game.
MM_FAIR_PULL: float = 0.15
# Quotes lean away from the maker's inventory, so it works its position back
# toward flat instead of parking at the limit and going one-sided.
MM_SKEW_BPS: float = 70.0

FLOW_PROB: float = 0.35           # chance per item per tick of a bot print
FLOW_QTY_MIN: int = 5
FLOW_QTY_MAX: int = 40
FLOW_BIAS_MAX: float = 0.40       # how far mispricing tilts the bot's side

MM_BOT_ID = "__SWE_MM__"
FLOW_BOT_ID = "__SWE_FLOW__"

# ── The tradable items ───────────────────────────────────────────────────────
# Fictional on purpose: no real ticker means no outside data to look up, so the
# contest is decided by what a strategy does with the book in front of it.
ITEM_SPECS: List[Dict[str, Any]] = [
    {"symbol": "WIDGET", "name": "Widget Co",       "start": 100.00, "vol_bps": 12.0, "drift_bps": 0.0},
    {"symbol": "GADGET", "name": "Gadget Industries", "start": 50.00, "vol_bps": 22.0, "drift_bps": 0.4},
    {"symbol": "GIZMO",  "name": "Gizmo Holdings",  "start": 250.00, "vol_bps": 8.0,  "drift_bps": -0.2},
    {"symbol": "DOODAD", "name": "Doodad PLC",      "start": 20.00,  "vol_bps": 35.0, "drift_bps": 0.0},
]
ITEM_SYMBOLS: List[str] = [spec["symbol"] for spec in ITEM_SPECS]


STARTER_CODE = '''\
# SWE Prep — starter strategy
#
# on_tick(ctx) is called once per second for ten minutes.
# Return a list of orders; each one is a dict:
#
#   {"item": "WIDGET", "side": "BUY", "price": 99.95, "qty": 10}   resting limit
#   {"item": "WIDGET", "side": "SELL", "qty": 10}                  market order
#   {"action": "cancel_all"}                                       pull everything
#   {"action": "cancel_all", "item": "WIDGET"}                     pull one item
#
# Position limit: 1000 per item, long or short. Resting orders count toward it.
# P&L marks your position at each item's hidden fair value.
#
# Available without importing: math, stats (mean/median/stdev/clamp), random.

LOOKBACK = 20


def on_tick(ctx):
    orders = [{"action": "cancel_all"}]
    history = ctx["memory"].setdefault("mids", {})

    for item in ctx["items"]:
        quote = ctx["market"][item]
        mid = quote["mid"]
        if mid is None:
            continue

        # Keep a rolling window of mids for this item.
        window = history.setdefault(item, [])
        window.append(mid)
        if len(window) > LOOKBACK:
            window.pop(0)
        if len(window) < 5:
            continue

        # Simple mean reversion: fade moves away from the recent average.
        average = stats.mean(window)
        edge = (average - mid) / mid
        position = ctx["position"][item]
        room = ctx["limit"] - abs(position)
        if room < 10:
            continue

        size = int(stats.clamp(abs(edge) * 40000, 5, 50))
        size = min(size, room)

        if edge > 0.0008:
            orders.append({"item": item, "side": "BUY", "price": round(quote["bid"], 2), "qty": size})
        elif edge < -0.0008:
            orders.append({"item": item, "side": "SELL", "price": round(quote["ask"], 2), "qty": size})

    return orders
'''


# ─────────────────────────────────────────────────────────────────────────────
# Participants
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Participant:
    uid: str
    name: str
    is_bot: bool = False

    code: str = ""
    strategy: Optional[Strategy] = None
    status: str = "no_code"           # no_code | ready | error | disqualified
    error: str = ""

    cash: float = 0.0
    pos: Dict[str, float] = field(default_factory=lambda: {s: 0.0 for s in ITEM_SYMBOLS})
    memory: Dict[str, Any] = field(default_factory=dict)

    fills: int = 0
    volume: float = 0.0
    orders_accepted: int = 0
    orders_rejected: int = 0
    last_reject: str = ""
    timeouts: int = 0
    errors: int = 0
    logs: Deque[Dict[str, Any]] = field(default_factory=lambda: deque(maxlen=LOG_LINES))

    def note(self, tick: int, text: str, kind: str = "print") -> None:
        self.logs.append({"tick": tick, "text": text, "kind": kind})

    def pnl(self, fair: Dict[str, float]) -> float:
        mark = sum(self.pos[s] * fair[s] for s in ITEM_SYMBOLS)
        return self.cash + mark


# ─────────────────────────────────────────────────────────────────────────────
# Items
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Item:
    symbol: str
    name: str
    fair: float
    vol_bps: float
    drift_bps: float
    last: Optional[float] = None
    open_price: float = 0.0
    mm_centre: float = 0.0      # the market maker's lagging estimate of fair

    def step(self, rng: random.Random) -> None:
        """One geometric random-walk step of the hidden fair value."""
        shock = rng.gauss(self.drift_bps, self.vol_bps) / 10_000.0
        self.fair = max(0.01, round(self.fair * (1.0 + shock), 4))


# ─────────────────────────────────────────────────────────────────────────────
# The run
# ─────────────────────────────────────────────────────────────────────────────

class Run:
    """One ten-minute contest: its items, books, players and clock."""

    def __init__(self, run_id: str, join_code: str, name: str, creator_id: str, seed: Optional[int] = None):
        self.id = run_id
        self.join_code = join_code
        self.name = name
        self.creator_id = creator_id
        self.created_at = dt.datetime.utcnow()

        self.status = "lobby"                       # lobby | running | finished
        self.tick = 0
        self.started_at: Optional[dt.datetime] = None
        self.finished_at: Optional[dt.datetime] = None
        self._t0: Optional[float] = None            # monotonic clock at start

        self.rng = random.Random(seed)
        self.items: Dict[str, Item] = {
            spec["symbol"]: Item(
                symbol=spec["symbol"], name=spec["name"], fair=spec["start"],
                vol_bps=spec["vol_bps"], drift_bps=spec["drift_bps"],
                open_price=spec["start"], mm_centre=spec["start"],
            )
            for spec in ITEM_SPECS
        }
        self.books: Dict[str, OrderBook] = {s: OrderBook() for s in ITEM_SYMBOLS}
        self.participants: Dict[str, Participant] = {}
        self.tape: Deque[Dict[str, Any]] = deque(maxlen=60)
        self.results: List[Dict[str, Any]] = []

        self._add_bots()

    # -- membership ------------------------------------------------------
    def _add_bots(self) -> None:
        self.participants[MM_BOT_ID] = Participant(uid=MM_BOT_ID, name="Market Maker Bot", is_bot=True)
        self.participants[FLOW_BOT_ID] = Participant(uid=FLOW_BOT_ID, name="Flow Bot", is_bot=True)

    @property
    def players(self) -> List[Participant]:
        """Human participants, in join order."""
        return [p for p in self.participants.values() if not p.is_bot]

    def join(self, uid: str, username: str) -> Participant:
        existing = self.participants.get(uid)
        if existing:
            return existing
        if len(self.players) >= MAX_PARTICIPANTS:
            raise ValueError(f"this run is full ({MAX_PARTICIPANTS} players)")
        if self.status != "lobby":
            raise ValueError("this run has already started")
        p = Participant(uid=uid, name=username)
        self.participants[uid] = p
        return p

    def set_code(self, uid: str, code: str) -> None:
        p = self.participants.get(uid)
        if p is None or p.is_bot:
            raise ValueError("you have not joined this run")
        if self.status != "lobby":
            raise ValueError("strategies are locked once the run starts")
        # Compile now so the author sees sandbox errors immediately, then throw
        # the handle away — the real one is built fresh at start().
        compile_strategy(code).load(timeout=STRATEGY_LOAD_TIMEOUT_SEC)
        p.code = code
        p.status = "ready"
        p.error = ""

    # -- lifecycle -------------------------------------------------------
    def start(self) -> None:
        if self.status != "lobby":
            raise ValueError("run has already started")
        ready = [p for p in self.players if p.code]
        if not ready:
            raise ValueError("no strategies submitted yet")

        for p in self.players:
            if not p.code:
                p.status = "no_code"
                continue
            self._build_strategy(p)

        self.status = "running"
        self.started_at = dt.datetime.utcnow()
        self._t0 = time.monotonic()
        self.tick = 0

    def _build_strategy(self, p: Participant) -> None:
        """Compile the player's code into the handle used for the whole run."""
        def on_print(text: str, _p: Participant = p) -> None:
            _p.note(self.tick, text, "print")

        try:
            strategy = compile_strategy(p.code, on_print=on_print, seed=self.rng.randrange(1 << 30))
            strategy.load(timeout=STRATEGY_LOAD_TIMEOUT_SEC)
            if not strategy.has("on_tick"):
                raise SandboxError("strategy must define on_tick(ctx)")
        except (SandboxError, StrategyTimeout) as e:
            p.status = "error"
            p.error = str(e)
            p.note(0, str(e), "error")
            return
        except Exception as e:  # noqa: BLE001 - author's own error
            p.status = "error"
            p.error = f"{type(e).__name__}: {e}"
            p.note(0, p.error, "error")
            return

        p.strategy = strategy
        p.status = "ready"
        if strategy.has("on_start"):
            orders = self._invoke(p, "on_start", self._context(p))
            if orders is not None:
                self._apply_orders(p, orders)

    @property
    def seconds_left(self) -> float:
        if self.status == "lobby":
            return float(RUN_SECONDS)
        if self.status == "finished":
            return 0.0
        return max(0.0, RUN_SECONDS - self.tick * TICK_SECONDS)

    def advance(self, now: Optional[float] = None) -> int:
        """Replay whichever ticks the wall clock says are due.

        Synchronous on purpose: the caller is an async endpoint, and keeping
        this free of awaits means two concurrent pollers can never interleave
        partial ticks.  Returns the number of ticks executed.
        """
        if self.status != "running" or self._t0 is None:
            return 0

        now = time.monotonic() if now is None else now
        due = int((now - self._t0) / TICK_SECONDS)
        target = min(due, TOTAL_TICKS)
        executed = 0
        while self.tick < target and executed < MAX_CATCHUP_TICKS:
            self._run_tick()
            self.tick += 1
            executed += 1

        if self.tick >= TOTAL_TICKS:
            self.finish()
        return executed

    def finish(self) -> None:
        """Close the run and freeze the leaderboard. Idempotent."""
        if self.status == "finished":
            return
        self.status = "finished"
        self.finished_at = dt.datetime.utcnow()
        for book in self.books.values():
            book.clear_all_orders()
        self.results = self.leaderboard()

    # -- one tick --------------------------------------------------------
    def _run_tick(self) -> None:
        for item in self.items.values():
            item.step(self.rng)

        # Bots go first so a strategy always sees a two-sided market.
        self._bot_quote()
        self._bot_flow()

        # Randomise the order players act in each tick. Otherwise the first to
        # join would always quote into a fresh book ahead of everyone else —
        # a small but real edge that has nothing to do with strategy quality.
        active = [p for p in self.players if p.status == "ready" and p.strategy is not None]
        self.rng.shuffle(active)
        for p in active:
            orders = self._invoke(p, "on_tick", self._context(p))
            if orders is not None:
                self._apply_orders(p, orders)

    def _invoke(self, p: Participant, fn_name: str, ctx: Dict[str, Any]) -> Optional[Any]:
        """Call one of the author's functions, absorbing whatever it does wrong."""
        try:
            return p.strategy.call(fn_name, ctx, timeout=STRATEGY_TIMEOUT_SEC)
        except StrategyTimeout as e:
            p.timeouts += 1
            p.note(self.tick, str(e), "error")
            if p.timeouts >= MAX_TIMEOUTS:
                self._disqualify(p, f"timed out {p.timeouts} times")
        except SandboxError as e:
            self._disqualify(p, str(e))
        except Exception as e:  # noqa: BLE001 - author's own error
            p.errors += 1
            p.note(self.tick, f"{type(e).__name__}: {e}", "error")
            if p.errors >= MAX_ERRORS:
                self._disqualify(p, f"raised {p.errors} errors")
        return None

    def _disqualify(self, p: Participant, reason: str) -> None:
        p.status = "disqualified"
        p.error = reason
        p.note(self.tick, f"disqualified: {reason}", "error")
        for symbol in ITEM_SYMBOLS:
            self.books[symbol].cancel_all_for_user(p.uid)

    # -- strategy-facing view -------------------------------------------
    def _context(self, p: Participant) -> Dict[str, Any]:
        market = {}
        open_orders = {}
        for symbol in ITEM_SYMBOLS:
            book = self.books[symbol]
            bid, ask = self._touch(book)
            resting_buy, resting_sell = self._resting(book, p.uid)
            market[symbol] = {
                "bid": bid,
                "ask": ask,
                "bid_qty": self._qty_at(book.bids, bid),
                "ask_qty": self._qty_at(book.asks, ask),
                "mid": None if bid is None or ask is None else round((bid + ask) / 2, 4),
                "spread": None if bid is None or ask is None else round(ask - bid, 4),
                "last": self.items[symbol].last,
            }
            open_orders[symbol] = {"buy": resting_buy, "sell": resting_sell}

        fair = {s: item.fair for s, item in self.items.items()}
        return {
            "tick": self.tick,
            "seconds_left": round(self.seconds_left, 1),
            "limit": POSITION_LIMIT,
            "items": list(ITEM_SYMBOLS),
            "market": market,
            "position": dict(p.pos),
            "open_orders": open_orders,
            "cash": round(p.cash, 4),
            "pnl": round(p.pnl(fair), 4),
            "memory": p.memory,
        }

    @staticmethod
    def _touch(book: OrderBook) -> Tuple[Optional[float], Optional[float]]:
        best_bid = max(book.bids.keys(), default=None)
        best_ask = min(book.asks.keys(), default=None)
        return (
            float(best_bid) if best_bid is not None else None,
            float(best_ask) if best_ask is not None else None,
        )

    @staticmethod
    def _qty_at(side: Dict[Decimal, Any], price: Optional[float]) -> float:
        if price is None:
            return 0.0
        dq = side.get(Decimal(str(price)))
        return float(sum(o.qty for o in dq)) if dq else 0.0

    @staticmethod
    def _resting(book: OrderBook, uid: str) -> Tuple[float, float]:
        """Live quantity this participant has resting on each side."""
        buy = sum(float(o.qty) for dq in book.bids.values() for o in dq if o.user_id == uid)
        sell = sum(float(o.qty) for dq in book.asks.values() for o in dq if o.user_id == uid)
        return buy, sell

    # -- order handling --------------------------------------------------
    def _apply_orders(self, p: Participant, orders: Any) -> None:
        if isinstance(orders, dict):
            orders = [orders]
        if not isinstance(orders, (list, tuple)):
            self._reject(p, "on_tick must return a list of order dicts")
            return
        # Bail before touching the contents if the list itself is absurd, so a
        # cheaply-built giant list can never be copied or iterated.
        if len(orders) > MAX_ORDER_LIST:
            self._reject(p, f"returned {len(orders)} orders; the limit is {MAX_ORDER_LIST} per tick")
            return
        for raw in orders[:MAX_ORDERS_PER_TICK]:
            try:
                self._apply_one(p, raw)
            except ValueError as e:
                self._reject(p, str(e))
        if len(orders) > MAX_ORDERS_PER_TICK:
            self._reject(p, f"only the first {MAX_ORDERS_PER_TICK} orders of a tick are accepted")

    def _reject(self, p: Participant, reason: str) -> None:
        p.orders_rejected += 1
        p.last_reject = reason
        p.note(self.tick, reason, "reject")

    def _apply_one(self, p: Participant, raw: Any) -> None:
        if not isinstance(raw, dict):
            raise ValueError("each order must be a dict")

        action = raw.get("action")
        if action == "cancel_all":
            symbols = [raw["item"]] if raw.get("item") else list(ITEM_SYMBOLS)
            for symbol in symbols:
                if symbol not in self.books:
                    raise ValueError(f"unknown item {symbol!r}")
                self.books[symbol].cancel_all_for_user(p.uid)
            return
        if action:
            raise ValueError(f"unknown action {action!r}")

        symbol = raw.get("item")
        if symbol not in self.books:
            raise ValueError(f"unknown item {symbol!r}")

        side = str(raw.get("side", "")).upper()
        if side not in ("BUY", "SELL"):
            raise ValueError("side must be 'BUY' or 'SELL'")

        qty = self._coerce_qty(raw.get("qty"))
        price = raw.get("price")
        is_market = price is None
        limit_price = self._market_price(symbol, side) if is_market else self._coerce_price(price)

        if not self._within_limit(p, symbol, side, qty):
            raise ValueError(
                f"{side} {qty} {symbol} would breach the {POSITION_LIMIT} position limit "
                f"(position {p.pos[symbol]:.0f}, resting orders count)"
            )

        self._submit(p, symbol, side, limit_price, qty, cancel_remainder=is_market)
        p.orders_accepted += 1

    @staticmethod
    def _coerce_qty(value: Any) -> int:
        try:
            qty = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"qty must be a whole number, got {value!r}") from None
        if qty <= 0:
            raise ValueError("qty must be positive")
        if qty > POSITION_LIMIT:
            raise ValueError(f"qty may not exceed the {POSITION_LIMIT} position limit")
        return qty

    @staticmethod
    def _coerce_price(value: Any) -> Decimal:
        try:
            px = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"price must be a number, got {value!r}") from None
        if px != px or px in (float("inf"), float("-inf")):
            raise ValueError("price must be finite")
        if px <= 0 or px > MAX_PRICE:
            raise ValueError(f"price must be between 0 and {MAX_PRICE:,.0f}")
        return Decimal(str(round(px, PRICE_DP)))

    def _market_price(self, symbol: str, side: str) -> Decimal:
        """A price aggressive enough to sweep; the remainder is pulled after."""
        book = self.books[symbol]
        bid, ask = self._touch(book)
        reference = ask if side == "BUY" else bid
        if reference is None:
            raise ValueError(f"no {'offer' if side == 'BUY' else 'bid'} to hit in {symbol}")
        through = reference * (1.05 if side == "BUY" else 0.95)
        return Decimal(str(round(min(through, MAX_PRICE), PRICE_DP)))

    def _within_limit(self, p: Participant, symbol: str, side: str, qty: int) -> bool:
        """Worst case is every resting order and this one filling in full."""
        resting_buy, resting_sell = self._resting(self.books[symbol], p.uid)
        position = p.pos[symbol]
        if side == "BUY":
            return position + resting_buy + qty <= POSITION_LIMIT
        return position - resting_sell - qty >= -POSITION_LIMIT

    def _submit(
        self,
        p: Participant,
        symbol: str,
        side: str,
        price: Decimal,
        qty: int,
        cancel_remainder: bool = False,
    ) -> None:
        book = self.books[symbol]
        order = BookOrder(
            id=str(uuid.uuid4()),
            user_id=p.uid,
            side=side,
            price=price,
            qty=Decimal(qty),
            orig_qty=Decimal(qty),
        )
        fills = book.add(order)
        if cancel_remainder and order.qty > 0:
            book.cancel(order.id, p.uid)
        for fill in fills:
            self._settle(symbol, fill, taker_side=side)

    def _settle(self, symbol: str, fill: Dict[str, Any], taker_side: str) -> None:
        price = float(fill["price"])
        qty = float(fill["qty"])
        buyer = self.participants.get(str(fill["buyer_id"]))
        seller = self.participants.get(str(fill["seller_id"]))
        notional = price * qty

        if buyer:
            buyer.cash -= notional
            buyer.pos[symbol] += qty
            buyer.fills += 1
            buyer.volume += qty
        if seller:
            seller.cash += notional
            seller.pos[symbol] -= qty
            seller.fills += 1
            seller.volume += qty

        self.items[symbol].last = price
        self.tape.appendleft({
            "tick": self.tick,
            "item": symbol,
            "price": round(price, PRICE_DP),
            "qty": qty,
            "buyer": buyer.name if buyer else "?",
            "seller": seller.name if seller else "?",
            "taker_side": taker_side,
        })

    # -- house bots ------------------------------------------------------
    def _bot_quote(self) -> None:
        """Wipe and re-post the market maker's ladder.

        The ladder is centred on the maker's own lagging, noisy estimate of
        fair value, tilted against whatever inventory it is carrying.
        """
        bot = self.participants[MM_BOT_ID]
        for symbol, item in self.items.items():
            book = self.books[symbol]
            book.cancel_all_for_user(MM_BOT_ID)

            item.mm_centre += (item.fair - item.mm_centre) * MM_FAIR_PULL
            item.mm_centre *= 1 + self.rng.gauss(0, MM_NOISE_BPS) / 10_000.0
            skew = -(bot.pos[symbol] / POSITION_LIMIT) * MM_SKEW_BPS / 10_000.0
            centre = max(0.01, item.mm_centre * (1 + skew))

            for level in range(MM_LEVELS):
                offset = (MM_HALF_SPREAD_BPS + level * MM_STEP_BPS) / 10_000.0
                qty = MM_BASE_QTY + level * MM_QTY_STEP + self.rng.randint(-5, 5)
                if qty <= 0:
                    continue
                for side, sign in (("BUY", -1), ("SELL", 1)):
                    if not self._within_limit(bot, symbol, side, qty):
                        continue
                    price = self._coerce_price(centre * (1 + sign * offset))
                    self._submit(bot, symbol, side, price, qty)

    def _bot_flow(self) -> None:
        """Occasional liquidity-taking prints, tilted toward the mispricing."""
        bot = self.participants[FLOW_BOT_ID]
        for symbol, item in self.items.items():
            if self.rng.random() > FLOW_PROB:
                continue
            book = self.books[symbol]
            bid, ask = self._touch(book)
            if bid is None or ask is None:
                continue

            mid = (bid + ask) / 2
            gap = (item.fair - mid) / item.fair if item.fair else 0.0
            bias = max(-FLOW_BIAS_MAX, min(FLOW_BIAS_MAX, gap * 40))
            side = "BUY" if self.rng.random() < 0.5 + bias else "SELL"
            qty = self.rng.randint(FLOW_QTY_MIN, FLOW_QTY_MAX)
            if not self._within_limit(bot, symbol, side, qty):
                continue
            try:
                price = self._market_price(symbol, side)
            except ValueError:
                continue
            self._submit(bot, symbol, side, price, qty, cancel_remainder=True)

    # -- reporting -------------------------------------------------------
    def leaderboard(self) -> List[Dict[str, Any]]:
        fair = {s: item.fair for s, item in self.items.items()}
        rows = []
        for p in self.participants.values():
            rows.append({
                "user_id": p.uid,
                "username": p.name,
                "is_bot": p.is_bot,
                "status": "bot" if p.is_bot else p.status,
                "pnl": round(p.pnl(fair), 2),
                "cash": round(p.cash, 2),
                "fills": p.fills,
                "volume": round(p.volume, 0),
                "orders_accepted": p.orders_accepted,
                "orders_rejected": p.orders_rejected,
                "positions": {s: round(p.pos[s], 0) for s in ITEM_SYMBOLS},
                "error": p.error,
            })
        rows.sort(key=lambda r: r["pnl"], reverse=True)
        for rank, row in enumerate(rows, start=1):
            row["rank"] = rank
        return rows

    def market_view(self, reveal_fair: bool = False) -> List[Dict[str, Any]]:
        out = []
        for symbol in ITEM_SYMBOLS:
            item = self.items[symbol]
            book = self.books[symbol]
            bid, ask = self._touch(book)
            row = {
                "item": symbol,
                "name": item.name,
                "bid": bid,
                "ask": ask,
                "bid_qty": self._qty_at(book.bids, bid),
                "ask_qty": self._qty_at(book.asks, ask),
                "mid": None if bid is None or ask is None else round((bid + ask) / 2, PRICE_DP),
                "last": item.last,
                "open": item.open_price,
            }
            if reveal_fair:
                row["fair"] = round(item.fair, PRICE_DP)
            out.append(row)
        return out

    def player_view(self, uid: str) -> Optional[Dict[str, Any]]:
        p = self.participants.get(uid)
        if p is None or p.is_bot:
            return None
        fair = {s: item.fair for s, item in self.items.items()}
        return {
            "status": p.status,
            "error": p.error,
            "has_code": bool(p.code),
            "cash": round(p.cash, 2),
            "pnl": round(p.pnl(fair), 2),
            "fills": p.fills,
            "orders_accepted": p.orders_accepted,
            "orders_rejected": p.orders_rejected,
            "last_reject": p.last_reject,
            "positions": {s: round(p.pos[s], 0) for s in ITEM_SYMBOLS},
            "logs": list(p.logs)[-60:],
        }


# ─────────────────────────────────────────────────────────────────────────────
# Run registry (in memory — the service is pinned to a single instance)
# ─────────────────────────────────────────────────────────────────────────────

_runs: Dict[str, Run] = {}


def _new_join_code() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # no look-alike characters
    while True:
        code = "".join(random.choice(alphabet) for _ in range(6))
        if not any(r.join_code == code for r in _runs.values()):
            return code


def create_run(name: str, creator_id: str, seed: Optional[int] = None) -> Run:
    run_id = str(uuid.uuid4())
    run = Run(run_id, _new_join_code(), name or "SWE Prep", creator_id, seed=seed)
    _runs[run_id] = run
    _prune()
    return run


def get_run(run_id: str) -> Optional[Run]:
    return _runs.get(run_id)


def find_by_code(code: str) -> Optional[Run]:
    code = (code or "").strip().upper()
    for run in _runs.values():
        if run.join_code == code and run.status != "finished":
            return run
    return None


def open_runs() -> List[Run]:
    return [r for r in _runs.values() if r.status in ("lobby", "running")]


def _prune(keep: int = 40) -> None:
    """Drop the oldest finished runs so a long-lived instance stays bounded."""
    finished = sorted(
        (r for r in _runs.values() if r.status == "finished"),
        key=lambda r: r.finished_at or r.created_at,
    )
    for run in finished[:max(0, len(finished) - keep)]:
        _runs.pop(run.id, None)


def reset() -> None:
    """Clear the registry (tests)."""
    _runs.clear()
