"""
Market Simulation Py — the market engine (client-side execution model).
=======================================================================

An algo-trading contest where **no player code ever runs on the server**.
Players run their strategy on their own machine and reach the market only
through the authenticated, rate-limited HTTP order gateway
(:mod:`app.market_sim_py`). The server owns the trusted half: the items, the
order books, the house bots, the fair-value marks, the position limit, and the
clock. It matches whatever orders arrive — it never executes a strategy.

Why this shape
--------------
* **No sandbox.** Untrusted code lives on the players' machines, so the whole
  class of code-execution and denial-of-service risks simply does not exist on
  our side. The server only ever sees orders (validated JSON), never code.
* **Continuous matching.** Orders match on arrival against the live book, the
  way a real exchange works. Fairness comes from a per-user rate limit in the
  gateway, not from batching.
* **Fair-value marking.** P&L marks positions at each item's hidden fair value,
  not the book mid, so quoting a silly price to yourself can't manufacture
  profit.
* **Request-driven clock.** Cloud Run throttles the CPU between requests, so the
  market's heartbeat (fair-value walk + bot quoting) is advanced from the same
  polls the clients make, via :meth:`Run.advance`. ``advance`` never awaits, so
  concurrent pollers can't interleave a half-finished tick.
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

from app import world as world_mod
from app.order_book import Order as BookOrder, OrderBook
from app.world import World

log = logging.getLogger("uvicorn.error")

# ── Contest rules ────────────────────────────────────────────────────────────
POSITION_LIMIT: int = 1000        # absolute position cap, per item, per player
RUN_SECONDS: int = 600            # ten minutes
TICK_SECONDS: float = 1.0         # market heartbeat (fair-value walk + bots)
TOTAL_TICKS: int = int(RUN_SECONDS / TICK_SECONDS)

MAX_PARTICIPANTS: int = 50        # humans per run
MAX_CATCHUP_TICKS: int = 60       # heartbeat ticks replayed in one advance() call

# The world map is sized at run creation, before anyone has joined. Four gives
# a board where neighbours actually make contact within the ten minutes.
WORLD_MAP_PLAYERS: int = 4

# ── Order gateway limits ─────────────────────────────────────────────────────
# The contest's timing fairness lives here: a bot may send at most ORDER_RATE
# orders per second (with a small burst), so a faster connection can't turn the
# game into a latency race.
ORDER_RATE_PER_SEC: int = 10
ORDER_BURST: int = 10
MAX_ORDERS_PER_REQUEST: int = 20  # a single POST /orders may carry this many

# ── Market microstructure ────────────────────────────────────────────────────
PRICE_DP: int = 2
MAX_PRICE: float = 1_000_000.0

MM_LEVELS: int = 3          # depth of a market-making ladder
MM_STEP_BPS: float = 12.0   # extra spread per deeper ladder level
MM_SKEW_BPS: float = 70.0   # how hard quotes lean against inventory

MAX_BOTS: int = 12          # house bots per run

# Default two-bot roster every run starts with (admins add more, or remove).
MM_BOT_ID = "__ALGO_MM__"
FLOW_BOT_ID = "__ALGO_FLOW__"

# ── Bot skill levels ─────────────────────────────────────────────────────────
# Skill scales how *good* a bot is. A better bot tracks the hidden fair value
# faster and more cleanly (fair_lag up, noise_bps down), quotes tighter
# (spread_bps down), needs less of an edge before it acts (edge_bps down),
# trades bigger (size), and acts more often (act_prob). noob → cracked.
SKILL_PARAMS: Dict[str, Dict[str, float]] = {
    "noob":    {"fair_lag": 0.04, "noise_bps": 14.0, "spread_bps": 45.0, "edge_bps": 40.0, "size": 8,  "act_prob": 0.45},
    "normal":  {"fair_lag": 0.15, "noise_bps": 6.0,  "spread_bps": 25.0, "edge_bps": 22.0, "size": 15, "act_prob": 0.75},
    "good":    {"fair_lag": 0.35, "noise_bps": 2.5,  "spread_bps": 15.0, "edge_bps": 12.0, "size": 22, "act_prob": 0.92},
    "cracked": {"fair_lag": 0.70, "noise_bps": 0.8,  "spread_bps": 9.0,  "edge_bps": 6.0,  "size": 30, "act_prob": 1.0},
}
SKILL_LEVELS: List[str] = ["noob", "normal", "good", "cracked"]

# ── Bot archetypes ───────────────────────────────────────────────────────────
# "phase" orders execution within a tick: makers (0) post their quotes before
# takers (1) come through, so a taker always sees a two-sided book.
BOT_ARCHETYPES: Dict[str, Dict[str, Any]] = {
    "market_maker":   {"label": "Market Maker",    "phase": 0, "desc": "Quotes both sides around fair; earns the spread."},
    "conservative":   {"label": "Conservative",    "phase": 0, "desc": "Small size, wide quotes, flattens inventory fast."},
    "mean_reversion": {"label": "Mean Reversion",  "phase": 0, "desc": "Fades moves back toward fair with limit orders."},
    "bull":           {"label": "Bull (long)",     "phase": 0, "desc": "Long-biased; accumulates and holds longs."},
    "bear":           {"label": "Bear (short)",    "phase": 0, "desc": "Short-biased; accumulates and holds shorts."},
    "taker":          {"label": "Liquidity Taker", "phase": 1, "desc": "Lifts offers and hits bids that look mispriced."},
    "momentum":       {"label": "Momentum",        "phase": 1, "desc": "Chases trends by taking liquidity."},
}

# ── The tradable items ───────────────────────────────────────────────────────
# Fictional on purpose: no real ticker means no outside data to look up, so the
# contest is decided by what a strategy does with the book in front of it.
ITEM_SPECS: List[Dict[str, Any]] = [
    {"symbol": "WIDGET", "name": "Widget Co",         "start": 100.00, "vol_bps": 12.0, "drift_bps": 0.0},
    {"symbol": "GADGET", "name": "Gadget Industries", "start": 50.00,  "vol_bps": 22.0, "drift_bps": 0.4},
    {"symbol": "GIZMO",  "name": "Gizmo Holdings",    "start": 250.00, "vol_bps": 8.0,  "drift_bps": -0.2},
    {"symbol": "DOODAD", "name": "Doodad PLC",        "start": 20.00,  "vol_bps": 35.0, "drift_bps": 0.0},
]
ITEM_SYMBOLS: List[str] = [spec["symbol"] for spec in ITEM_SPECS]


class OrderRejected(ValueError):
    """An order failed validation or the position-limit check."""


# ─────────────────────────────────────────────────────────────────────────────
# Participants
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Participant:
    uid: str
    name: str
    is_bot: bool = False

    cash: float = 0.0
    pos: Dict[str, float] = field(default_factory=lambda: {s: 0.0 for s in ITEM_SYMBOLS})

    fills: int = 0
    volume: float = 0.0
    orders_accepted: int = 0
    orders_rejected: int = 0
    last_reject: str = ""
    last_seen: float = 0.0        # monotonic clock of the last API call

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

    def step(self, rng: random.Random) -> None:
        """One geometric random-walk step of the hidden fair value."""
        shock = rng.gauss(self.drift_bps, self.vol_bps) / 10_000.0
        self.fair = max(0.01, round(self.fair * (1.0 + shock), 4))


@dataclass
class Bot:
    """A house bot's behaviour plus its private state.

    Its cash/positions live in the matching Participant (``participants[uid]``);
    this holds the strategy knobs, its own noisy fair-value estimate per item,
    and any scratch memory the archetype needs between ticks.
    """
    uid: str
    archetype: str
    skill: str
    activate_tick: int = 0
    est: Dict[str, float] = field(default_factory=dict)
    memory: Dict[str, Any] = field(default_factory=dict)


# ─────────────────────────────────────────────────────────────────────────────
# The run
# ─────────────────────────────────────────────────────────────────────────────

class Run:
    """One ten-minute contest: its items, books, players, bots and clock."""

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
                open_price=spec["start"],
            )
            for spec in ITEM_SPECS
        }
        self.books: Dict[str, OrderBook] = {s: OrderBook() for s in ITEM_SYMBOLS}
        self.participants: Dict[str, Participant] = {}
        self.bots: List[Bot] = []
        self._bot_seq = 0
        self.tape: Deque[Dict[str, Any]] = deque(maxlen=60)
        self.results: List[Dict[str, Any]] = []

        # The empire layer, seeded from the same value as the market so a run
        # replays identically end to end.
        #
        # The map has to be sized now, before anyone has joined, so it is built
        # for a typical run rather than the eight-player maximum: at full size
        # the bases end up so far apart that nobody meets inside ten minutes and
        # the whole conflict half of the game never happens.
        self.world = World(seed=seed if seed is not None else self.rng.randrange(1 << 30),
                           players=WORLD_MAP_PLAYERS)
        self.world_results: List[Dict[str, Any]] = []

        self._add_default_bots()

    # -- bots ------------------------------------------------------------
    def _add_default_bots(self) -> None:
        """Every run opens with a normal maker and a normal taker; admins tune."""
        self.add_bot("market_maker", "normal", 0, name="Market Maker Bot", uid=MM_BOT_ID)
        self.add_bot("taker", "normal", 0, name="Flow Bot", uid=FLOW_BOT_ID)

    def add_bot(
        self,
        archetype: str,
        skill: str,
        activate_tick: int = 0,
        name: Optional[str] = None,
        uid: Optional[str] = None,
    ) -> Bot:
        """Register a house bot that starts acting once ``activate_tick`` is reached."""
        if archetype not in BOT_ARCHETYPES:
            raise ValueError(f"unknown bot type {archetype!r}")
        if skill not in SKILL_PARAMS:
            raise ValueError(f"unknown skill level {skill!r}")
        if len(self.bots) >= MAX_BOTS:
            raise ValueError(f"a run may have at most {MAX_BOTS} bots")
        if uid is None:
            self._bot_seq += 1
            uid = f"__BOT_{self._bot_seq}__"
        name = name or f"{BOT_ARCHETYPES[archetype]['label']} · {skill}"
        self.participants[uid] = Participant(uid=uid, name=name, is_bot=True)
        bot = Bot(uid=uid, archetype=archetype, skill=skill, activate_tick=max(0, int(activate_tick)))
        self.bots.append(bot)
        return bot

    def remove_bot(self, uid: str) -> None:
        """Remove a bot that has not entered yet (or any bot while in the lobby)."""
        bot = next((b for b in self.bots if b.uid == uid), None)
        if bot is None:
            raise ValueError("no such bot in this run")
        if self.status == "running" and self.tick >= bot.activate_tick:
            raise ValueError("this bot has already entered and can't be removed")
        for symbol in ITEM_SYMBOLS:
            self.books[symbol].cancel_all_for_user(uid)
        self.bots.remove(bot)
        self.participants.pop(uid, None)

    def bots_view(self) -> List[Dict[str, Any]]:
        fair = {s: it.fair for s, it in self.items.items()}
        out = []
        for b in self.bots:
            part = self.participants[b.uid]
            meta = BOT_ARCHETYPES[b.archetype]
            out.append({
                "uid": b.uid,
                "name": part.name,
                "archetype": b.archetype,
                "archetype_label": meta["label"],
                "skill": b.skill,
                "activate_tick": b.activate_tick,
                "enters_at": int(round(b.activate_tick * TICK_SECONDS)),
                "active": self.status != "lobby" and self.tick >= b.activate_tick,
                "removable": self.status == "lobby" or self.tick < b.activate_tick,
                "pnl": round(part.pnl(fair), 2),
            })
        out.sort(key=lambda r: (r["activate_tick"], r["name"]))
        return out

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
        if self.status == "finished":
            raise ValueError("this run has already finished")
        p = Participant(uid=uid, name=username)
        self.participants[uid] = p
        # A seat at the market comes with a base on the map. If the map is full
        # the player still trades — they just have nothing to spend it on.
        try:
            self.world.add_player(uid, username)
        except world_mod.WorldRejected as exc:
            log.info("world: %s could not be placed (%s)", username, exc)
        return p

    def member(self, uid: str) -> Optional[Participant]:
        p = self.participants.get(uid)
        return p if (p is not None and not p.is_bot) else None

    # -- lifecycle -------------------------------------------------------
    def start(self) -> None:
        if self.status != "lobby":
            raise ValueError("run has already started")
        self.status = "running"
        self.started_at = dt.datetime.utcnow()
        self._t0 = time.monotonic()
        self.tick = 0

    @property
    def seconds_left(self) -> float:
        if self.status == "lobby":
            return float(RUN_SECONDS)
        if self.status == "finished":
            return 0.0
        return max(0.0, RUN_SECONDS - self.tick * TICK_SECONDS)

    def advance(self, now: Optional[float] = None) -> int:
        """Replay the market heartbeat ticks the wall clock says are due.

        Only the *market* advances here — fair-value walk and the two house
        bots. Player orders are matched on arrival in :meth:`submit_orders`,
        not on this clock. Synchronous on purpose: with no awaits, two
        concurrent pollers can never interleave partial ticks.
        """
        if self.status != "running" or self._t0 is None:
            return 0

        now = time.monotonic() if now is None else now
        due = int((now - self._t0) / TICK_SECONDS)
        target = min(due, TOTAL_TICKS)
        executed = 0
        while self.tick < target and executed < MAX_CATCHUP_TICKS:
            self._heartbeat()
            self.tick += 1
            executed += 1

        self._advance_world()

        if self.tick >= TOTAL_TICKS:
            self.finish()
        return executed

    def _advance_world(self) -> None:
        """Push P&L into the empires, then run any world ticks now due.

        The world runs on the market's tick counter rather than its own clock,
        so it inherits the same catch-up behaviour and can never drift from the
        contest it is funded by.
        """
        fair = {s: item.fair for s, item in self.items.items()}
        for p in self.participants.values():
            if not p.is_bot:
                self.world.set_pnl(p.uid, p.pnl(fair))

        ticks_due = int(self.tick * TICK_SECONDS / world_mod.WORLD_TICK_SECONDS)
        while self.world.tick_no < ticks_due:
            self.world.tick()

    def finish(self) -> None:
        """Close the run and freeze the leaderboard. Idempotent."""
        if self.status == "finished":
            return
        self.status = "finished"
        self.finished_at = dt.datetime.utcnow()
        for book in self.books.values():
            book.clear_all_orders()
        self.results = self.leaderboard()
        self.world_results = self.world.standings()

    # -- the market heartbeat -------------------------------------------
    def _heartbeat(self) -> None:
        for item in self.items.values():
            item.step(self.rng)
        # Makers quote first (phase 0) so takers (phase 1) see a two-sided book.
        active = [b for b in self.bots if self.tick >= b.activate_tick]
        active.sort(key=lambda b: BOT_ARCHETYPES[b.archetype]["phase"])
        for bot in active:
            self._run_bot(bot)

    # ── Order gateway (called per API request, matched on arrival) ──────

    def submit_orders(self, uid: str, orders: List[dict], allowance: int) -> Dict[str, Any]:
        """Apply up to ``allowance`` of a player's orders, matching each on arrival.

        ``allowance`` is how many orders the rate limiter granted this request;
        any beyond it are reported as rate-limited rather than applied. Returns
        a per-order result list plus the player's resulting book state.
        """
        p = self.member(uid)
        if p is None:
            raise OrderRejected("you have not joined this run")
        if self.status != "running":
            raise OrderRejected("this run is not currently live")
        p.last_seen = time.monotonic()

        results: List[Dict[str, Any]] = []
        for i, raw in enumerate(orders[:MAX_ORDERS_PER_REQUEST]):
            if i >= allowance:
                results.append({"ok": False, "error": "rate limited — slow down (max "
                                f"{ORDER_RATE_PER_SEC}/s)", "rate_limited": True})
                p.orders_rejected += 1
                continue
            try:
                results.append({"ok": True, **self._apply_one(p, raw)})
                p.orders_accepted += 1
            except OrderRejected as e:
                results.append({"ok": False, "error": str(e)})
                p.orders_rejected += 1
                p.last_reject = str(e)

        if len(orders) > MAX_ORDERS_PER_REQUEST:
            results.append({"ok": False,
                            "error": f"only {MAX_ORDERS_PER_REQUEST} orders per request are accepted"})

        fair = {s: item.fair for s, item in self.items.items()}
        return {
            "results": results,
            "position": dict(p.pos),
            "cash": round(p.cash, 4),
            "pnl": round(p.pnl(fair), 4),
        }

    def cancel(self, uid: str, item: Optional[str] = None) -> int:
        """Cancel a player's resting orders (one item, or all). Returns count."""
        p = self.member(uid)
        if p is None:
            raise OrderRejected("you have not joined this run")
        p.last_seen = time.monotonic()
        symbols = [item] if item else list(ITEM_SYMBOLS)
        removed = 0
        for symbol in symbols:
            if symbol not in self.books:
                raise OrderRejected(f"unknown item {symbol!r}")
            removed += self.books[symbol].cancel_all_for_user(uid)
        return removed

    def _apply_one(self, p: Participant, raw: Any) -> Dict[str, Any]:
        if not isinstance(raw, dict):
            raise OrderRejected("each order must be an object")

        action = raw.get("action")
        if action == "cancel_all":
            symbols = [raw["item"]] if raw.get("item") else list(ITEM_SYMBOLS)
            removed = 0
            for symbol in symbols:
                if symbol not in self.books:
                    raise OrderRejected(f"unknown item {symbol!r}")
                removed += self.books[symbol].cancel_all_for_user(p.uid)
            return {"action": "cancel_all", "cancelled": removed}
        if action:
            raise OrderRejected(f"unknown action {action!r}")

        symbol = raw.get("item")
        if symbol not in self.books:
            raise OrderRejected(f"unknown item {symbol!r}")

        side = str(raw.get("side", "")).upper()
        if side not in ("BUY", "SELL"):
            raise OrderRejected("side must be 'BUY' or 'SELL'")

        qty = self._coerce_qty(raw.get("qty"))
        price = raw.get("price")
        is_market = price is None
        limit_price = self._market_price(symbol, side) if is_market else self._coerce_price(price)

        if not self._within_limit(p, symbol, side, qty):
            raise OrderRejected(
                f"{side} {qty} {symbol} would breach the {POSITION_LIMIT} position limit "
                f"(position {p.pos[symbol]:.0f}, resting orders count)"
            )

        filled, resting, order_id = self._place(p, symbol, side, limit_price, qty, cancel_remainder=is_market)
        return {
            "order_id": order_id,
            "item": symbol,
            "side": side,
            "type": "market" if is_market else "limit",
            "filled": filled,
            "resting": resting,
            "position": p.pos[symbol],
        }

    # -- validation helpers ---------------------------------------------
    @staticmethod
    def _coerce_qty(value: Any) -> int:
        try:
            qty = int(value)
        except (TypeError, ValueError):
            raise OrderRejected(f"qty must be a whole number, got {value!r}") from None
        if qty <= 0:
            raise OrderRejected("qty must be positive")
        if qty > POSITION_LIMIT:
            raise OrderRejected(f"qty may not exceed the {POSITION_LIMIT} position limit")
        return qty

    @staticmethod
    def _coerce_price(value: Any) -> Decimal:
        try:
            px = float(value)
        except (TypeError, ValueError):
            raise OrderRejected(f"price must be a number, got {value!r}") from None
        if px != px or px in (float("inf"), float("-inf")):
            raise OrderRejected("price must be finite")
        if px <= 0 or px > MAX_PRICE:
            raise OrderRejected(f"price must be between 0 and {MAX_PRICE:,.0f}")
        return Decimal(str(round(px, PRICE_DP)))

    def _market_price(self, symbol: str, side: str) -> Decimal:
        """A price aggressive enough to sweep; the remainder is pulled after."""
        book = self.books[symbol]
        bid, ask = self._touch(book)
        reference = ask if side == "BUY" else bid
        if reference is None:
            raise OrderRejected(f"no {'offer' if side == 'BUY' else 'bid'} to hit in {symbol}")
        through = reference * (1.05 if side == "BUY" else 0.95)
        return Decimal(str(round(min(through, MAX_PRICE), PRICE_DP)))

    def _within_limit(self, p: Participant, symbol: str, side: str, qty: int) -> bool:
        """Worst case is every resting order and this one filling in full."""
        resting_buy, resting_sell = self._resting(self.books[symbol], p.uid)
        position = p.pos[symbol]
        if side == "BUY":
            return position + resting_buy + qty <= POSITION_LIMIT
        return position - resting_sell - qty >= -POSITION_LIMIT

    # -- matching --------------------------------------------------------
    def _place(
        self,
        p: Participant,
        symbol: str,
        side: str,
        price: Decimal,
        qty: int,
        cancel_remainder: bool = False,
    ) -> Tuple[float, float, str]:
        """Add an order to the book, settle fills. Returns (filled, resting, id)."""
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
        remainder = float(order.qty)
        if cancel_remainder and order.qty > 0:
            book.cancel(order.id, p.uid)
            remainder = 0.0
        for fill in fills:
            self._settle(symbol, fill, taker_side=side)
        return qty - remainder, remainder, order.id

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

    # -- book helpers ----------------------------------------------------
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
        """Live quantity a participant has resting on each side of one book."""
        buy = sum(float(o.qty) for dq in book.bids.values() for o in dq if o.user_id == uid)
        sell = sum(float(o.qty) for dq in book.asks.values() for o in dq if o.user_id == uid)
        return buy, sell

    # -- house bots ------------------------------------------------------
    def _run_bot(self, bot: Bot) -> None:
        handler = {
            "market_maker": self._bot_market_maker,
            "conservative": self._bot_conservative,
            "mean_reversion": self._bot_mean_reversion,
            "bull": self._bot_bull,
            "bear": self._bot_bear,
            "taker": self._bot_taker,
            "momentum": self._bot_momentum,
        }.get(bot.archetype)
        if handler is not None:
            handler(bot)

    def _bot_est(self, bot: Bot, symbol: str) -> float:
        """Advance and return the bot's own noisy, skill-scaled estimate of fair.

        No bot ever sees the true fair value; a smarter bot's estimate simply
        tracks it faster and with less noise. This is what makes them beatable.
        """
        p = SKILL_PARAMS[bot.skill]
        item = self.items[symbol]
        cur = bot.est.get(symbol, item.open_price)
        cur += (item.fair - cur) * p["fair_lag"]
        cur *= 1 + self.rng.gauss(0, p["noise_bps"]) / 10_000.0
        cur = max(0.01, cur)
        bot.est[symbol] = cur
        return cur

    def _bot_take(self, bot: Bot, symbol: str, side: str, qty: int) -> None:
        """A bot crosses the spread with a market order (remainder pulled)."""
        part = self.participants[bot.uid]
        qty = int(qty)
        if qty <= 0 or not self._within_limit(part, symbol, side, qty):
            return
        try:
            price = self._market_price(symbol, side)
        except OrderRejected:
            return
        self._place(part, symbol, side, price, qty, cancel_remainder=True)

    def _bot_market_maker(
        self, bot: Bot, size_mult: float = 1.0, spread_mult: float = 1.0, inv_aversion: float = 1.0
    ) -> None:
        p = SKILL_PARAMS[bot.skill]
        part = self.participants[bot.uid]
        for symbol in ITEM_SYMBOLS:
            self.books[symbol].cancel_all_for_user(bot.uid)
            # A market maker quotes continuously — it's the book's liquidity, so
            # it never skips a tick (skill shows up in its spread and fair read,
            # not in whether it shows up at all).
            est = self._bot_est(bot, symbol)
            skew = -(part.pos[symbol] / POSITION_LIMIT) * MM_SKEW_BPS * inv_aversion / 10_000.0
            centre = max(0.01, est * (1 + skew))
            base = max(1, int(p["size"] * size_mult))
            half = p["spread_bps"] * spread_mult
            for level in range(MM_LEVELS):
                offset = (half + level * MM_STEP_BPS) / 10_000.0
                qty = base + level * max(1, base // 2) + self.rng.randint(-2, 2)
                if qty <= 0:
                    continue
                for side, sign in (("BUY", -1), ("SELL", 1)):
                    if not self._within_limit(part, symbol, side, qty):
                        continue
                    self._place(part, symbol, side, self._coerce_price(centre * (1 + sign * offset)), qty)

    def _bot_conservative(self, bot: Bot) -> None:
        # A timid maker: small size, wide quotes, strong inventory aversion…
        self._bot_market_maker(bot, size_mult=0.4, spread_mult=1.6, inv_aversion=2.5)
        # …that also crosses the spread to cut risk when inventory builds up.
        part = self.participants[bot.uid]
        for symbol in ITEM_SYMBOLS:
            pos = part.pos[symbol]
            if abs(pos) > 150:
                self._bot_take(bot, symbol, "SELL" if pos > 0 else "BUY", min(20, int(abs(pos) / 3)))

    def _bot_mean_reversion(self, bot: Bot) -> None:
        p = SKILL_PARAMS[bot.skill]
        part = self.participants[bot.uid]
        for symbol in ITEM_SYMBOLS:
            self.books[symbol].cancel_all_for_user(bot.uid)
            est = self._bot_est(bot, symbol)
            if self.rng.random() > p["act_prob"]:
                continue
            bid, ask = self._touch(self.books[symbol])
            if bid is None or ask is None:
                continue
            gap_bps = (est - (bid + ask) / 2) / est * 10_000.0
            qty = int(p["size"])
            if gap_bps > p["edge_bps"] * 0.5 and self._within_limit(part, symbol, "BUY", qty):
                self._place(part, symbol, "BUY", self._coerce_price(bid), qty)
            elif gap_bps < -p["edge_bps"] * 0.5 and self._within_limit(part, symbol, "SELL", qty):
                self._place(part, symbol, "SELL", self._coerce_price(ask), qty)

    def _bot_directional(self, bot: Bot, want_long: bool) -> None:
        """Shared body for bull/bear: accumulate one way, trim near the cap.

        A skilled directional bot only builds at prices its own estimate says
        are *favourable* — a bull buys the bid only when the bid is below its
        fair estimate — so a good bot enters cheap and a noob overpays. That is
        what ties skill to P&L instead of just how hard it leans.
        """
        p = SKILL_PARAMS[bot.skill]
        part = self.participants[bot.uid]
        build_side = "BUY" if want_long else "SELL"
        trim_side = "SELL" if want_long else "BUY"
        for symbol in ITEM_SYMBOLS:
            self.books[symbol].cancel_all_for_user(bot.uid)
            est = self._bot_est(bot, symbol)
            if self.rng.random() > p["act_prob"]:
                continue
            signed = part.pos[symbol] if want_long else -part.pos[symbol]
            if signed > POSITION_LIMIT * 0.9:
                self._bot_take(bot, symbol, trim_side, min(20, int(p["size"])))
                continue
            bid, ask = self._touch(self.books[symbol])
            touch = bid if want_long else ask
            qty = int(p["size"])
            # Only accumulate when the entry price is on the right side of fair.
            favourable_rest = touch is not None and ((touch < est) if want_long else (touch > est))
            if favourable_rest and self._within_limit(part, symbol, build_side, qty):
                self._place(part, symbol, build_side, self._coerce_price(touch), qty)
            # Occasionally lift to build faster, but only if the offer/bid we'd
            # cross is still favourable versus our estimate (never overpay).
            cross = ask if want_long else bid
            can_cross = cross is not None and ((cross < est) if want_long else (cross > est))
            if can_cross and self.rng.random() < 0.3:
                self._bot_take(bot, symbol, build_side, min(10, qty))

    def _bot_bull(self, bot: Bot) -> None:
        self._bot_directional(bot, want_long=True)

    def _bot_bear(self, bot: Bot) -> None:
        self._bot_directional(bot, want_long=False)

    def _bot_taker(self, bot: Bot) -> None:
        """Lift stale quotes. Only crosses when the *touch price* — the price it
        would actually pay — is favourable versus its estimate, so paying the
        spread is still +EV. A sharper estimate finds more of these safely."""
        p = SKILL_PARAMS[bot.skill]
        # A small cushion beyond the touch covers noise; better bots need less.
        margin = 1 + (p["edge_bps"] * 0.1) / 10_000.0
        for symbol in ITEM_SYMBOLS:
            est = self._bot_est(bot, symbol)
            if self.rng.random() > p["act_prob"]:
                continue
            bid, ask = self._touch(self.books[symbol])
            qty = int(p["size"]) + self.rng.randint(0, max(1, int(p["size"]) // 2))
            if ask is not None and est > ask * margin:
                self._bot_take(bot, symbol, "BUY", qty)      # the offer is below fair → lift it
            elif bid is not None and est < bid / margin:
                self._bot_take(bot, symbol, "SELL", qty)     # the bid is above fair → hit it

    def _bot_momentum(self, bot: Bot) -> None:
        """Chase the trend in its estimate, but still only cross when the touch
        is on the right side of fair, so it doesn't bleed the spread."""
        p = SKILL_PARAMS[bot.skill]
        for symbol in ITEM_SYMBOLS:
            est = self._bot_est(bot, symbol)
            prev = bot.memory.get(symbol, est)
            bot.memory[symbol] = est
            if self.rng.random() > p["act_prob"]:
                continue
            chg_bps = (est - prev) / prev * 10_000.0 if prev else 0.0
            if abs(chg_bps) < p["edge_bps"] * 0.4:
                continue
            bid, ask = self._touch(self.books[symbol])
            if chg_bps > 0 and ask is not None and est > ask:
                self._bot_take(bot, symbol, "BUY", int(p["size"]))
            elif chg_bps < 0 and bid is not None and est < bid:
                self._bot_take(bot, symbol, "SELL", int(p["size"]))

    # -- views -----------------------------------------------------------
    def market_snapshot(self, reveal_fair: bool = False) -> List[Dict[str, Any]]:
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
                "spread": None if bid is None or ask is None else round(ask - bid, PRICE_DP),
                "last": item.last,
                "open": item.open_price,
            }
            if reveal_fair:
                row["fair"] = round(item.fair, PRICE_DP)
            out.append(row)
        return out

    def player_view(self, uid: str) -> Optional[Dict[str, Any]]:
        p = self.member(uid)
        if p is None:
            return None
        fair = {s: item.fair for s, item in self.items.items()}
        open_orders = {}
        for symbol in ITEM_SYMBOLS:
            rb, rs = self._resting(self.books[symbol], uid)
            open_orders[symbol] = {"buy": rb, "sell": rs}
        return {
            "cash": round(p.cash, 2),
            "pnl": round(p.pnl(fair), 2),
            "fills": p.fills,
            "orders_accepted": p.orders_accepted,
            "orders_rejected": p.orders_rejected,
            "last_reject": p.last_reject,
            "positions": {s: round(p.pos[s], 0) for s in ITEM_SYMBOLS},
            "open_orders": open_orders,
        }

    def leaderboard(self) -> List[Dict[str, Any]]:
        fair = {s: item.fair for s, item in self.items.items()}
        rows = []
        now = time.monotonic()
        # A bot scheduled to enter later shouldn't sit on the board at 0 yet.
        pending_bots = {b.uid for b in self.bots if self.tick < b.activate_tick}
        for p in self.participants.values():
            if p.uid in pending_bots:
                continue
            rows.append({
                "user_id": p.uid,
                "username": p.name,
                "is_bot": p.is_bot,
                "pnl": round(p.pnl(fair), 2),
                "cash": round(p.cash, 2),
                "fills": p.fills,
                "volume": round(p.volume, 0),
                "orders_accepted": p.orders_accepted,
                "orders_rejected": p.orders_rejected,
                "positions": {s: round(p.pos[s], 0) for s in ITEM_SYMBOLS},
                "connected": (not p.is_bot) and (now - p.last_seen < 10.0),
            })
        rows.sort(key=lambda r: r["pnl"], reverse=True)
        for rank, row in enumerate(rows, start=1):
            row["rank"] = rank
        return rows


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
    run = Run(run_id, _new_join_code(), name or "Market Simulation Coding", creator_id, seed=seed)
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
