"""
World — the empire layer that trading profit pays for.
======================================================

The coding game has two halves. The market half (:mod:`app.algo_engine`) is a
real order book where a player's algorithm makes or loses money. This half
turns that money into something to spend it on: a shared map where every player
holds a base, claims ground, raises farms and factories, trains units and
fights over territory.

The link between the halves is deliberately one line of arithmetic::

    credits = START_GRANT + trading_pnl + earned - spent

That is the whole economy. Trade well and the map opens up; trade badly and the
budget closes. Buildings already standing are never clawed back when P&L falls
— you simply cannot fund anything new until it recovers, which keeps a bad
market run from erasing an hour of building.

Why the module looks like this
------------------------------
* **Pure and synchronous.** No I/O, no clock, no framework. Every rule here is
  a function of state plus arguments, so the whole ruleset is unit-testable
  without standing up a market, and :mod:`app.market_sim_py` can stay a thin
  transport over it. Same reason :mod:`app.risk_episodes` is shaped this way.
* **Seeded and deterministic.** Map generation and combat jitter both come from
  the run's seed, so a run replays identically and a test can assert on exact
  outcomes rather than ranges.
* **No fog of war.** The whole map is visible to everyone, all the time. The
  dashboard is meant to be read at a glance, and hiding the board would make
  the interesting decisions (where is the weak neighbour?) unavailable to an
  algorithm that can only see through a JSON view.
* **Chebyshev distance throughout.** Adjacency, attack range and claim radius
  are all ``max(|dx|, |dy|)``. One distance rule is one thing for a player to
  learn, and it makes a claim radius a readable square on screen.

The turn structure is coarse on purpose. The market heartbeat runs once a
second; the world advances once every :data:`WORLD_TICK_SECONDS`, which gives an
algorithm time to look at the board and decide, and keeps the map from becoming
a reflex contest.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# ── Economy ──────────────────────────────────────────────────────────────────
START_GRANT: float = 2_000.0      # credits every player starts with, before P&L
CREDITS_PER_DOLLAR: float = 1.0   # exchange rate from market P&L into credits
START_FOOD: float = 40.0
START_MATERIALS: float = 60.0
START_WORKERS: int = 3

WORLD_TICK_SECONDS: float = 5.0   # one world tick per five market heartbeats
FOOD_PER_WORKER: float = 1.0      # eaten each world tick
GROWTH_FOOD_SURPLUS: float = 3.0  # spare food needed for the population to grow
STARVATION_LOSS: int = 1          # workers lost per tick when food runs out

BASE_POP_CAP: int = 6             # the base houses this many on its own

# ── Map ──────────────────────────────────────────────────────────────────────
MIN_SIDE: int = 18
MAX_SIDE: int = 40
WATER_SHARE: float = 0.10
SPAWN_CLEARANCE: int = 5          # minimum Chebyshev gap between two bases


class WorldRejected(ValueError):
    """An action the rules do not allow. The message is shown to the player."""


# ── Terrain ──────────────────────────────────────────────────────────────────
# ``defense`` is a multiplier added to a defender's strength on the tile;
# ``move_cost`` is what it costs a unit to enter.
TERRAIN: Dict[str, Dict[str, Any]] = {
    "plain":  {"label": "Plain",  "passable": True,  "defense": 0.00, "move_cost": 1},
    "forest": {"label": "Forest", "passable": True,  "defense": 0.25, "move_cost": 2},
    "hills":  {"label": "Hills",  "passable": True,  "defense": 0.50, "move_cost": 2},
    "water":  {"label": "Water",  "passable": False, "defense": 0.00, "move_cost": 0},
}
LAND: Tuple[str, ...] = ("plain", "forest", "hills")

# ── Buildings ────────────────────────────────────────────────────────────────
# ``value`` feeds the development score — it is what a building is worth to the
# ranking, and is roughly a third of its credit cost so that spending on the
# map always beats hoarding credits.
BUILDINGS: Dict[str, Dict[str, Any]] = {
    "base": {
        "label": "Base", "cost": {}, "terrain": LAND, "hp": 300, "value": 40,
        "claim": 2, "workers": 0, "desc": "Your capital. Claims the ground around it. Lose it and you are out.",
    },
    "farm": {
        "label": "Farm", "cost": {"credits": 120, "materials": 20}, "terrain": ("plain",),
        "hp": 60, "value": 10, "workers": 1, "yield": {"food": 5},
        "desc": "Feeds workers. Plains only.",
    },
    "lumber": {
        "label": "Lumber camp", "cost": {"credits": 100}, "terrain": ("forest",),
        "hp": 60, "value": 10, "workers": 1, "yield": {"materials": 3},
        "desc": "Steady materials from forest.",
    },
    "mine": {
        "label": "Mine", "cost": {"credits": 180, "materials": 20}, "terrain": ("hills",),
        "hp": 80, "value": 16, "workers": 2, "yield": {"materials": 5},
        "desc": "The best materials income. Hills only.",
    },
    "house": {
        "label": "Housing", "cost": {"credits": 90, "materials": 10}, "terrain": ("plain", "forest"),
        "hp": 50, "value": 8, "workers": 0, "pop_cap": 4,
        "desc": "Raises your population cap by four.",
    },
    "market": {
        "label": "Market", "cost": {"credits": 250, "materials": 30}, "terrain": ("plain",),
        "hp": 80, "value": 20, "workers": 2, "yield": {"credits": 5}, "claim": 1,
        "desc": "Credits every tick, and claims the ring around it.",
    },
    "factory": {
        "label": "Factory", "cost": {"credits": 400, "materials": 60}, "terrain": ("plain", "hills"),
        "hp": 100, "value": 34, "workers": 3, "yield": {"credits": 10},
        "upkeep": {"materials": 2},
        "desc": "The big earner. Eats materials, pays credits, needs three workers.",
    },
    "barracks": {
        "label": "Barracks", "cost": {"credits": 300, "materials": 40}, "terrain": ("plain", "hills"),
        "hp": 120, "value": 22, "workers": 1,
        "trains": ("soldier", "cavalry", "cannon"),
        "desc": "Required before you can train soldiers, cavalry or cannon.",
    },
    "fort": {
        "label": "Fort", "cost": {"credits": 350, "materials": 80}, "terrain": LAND,
        "hp": 240, "value": 24, "workers": 1, "defense": 0.60, "aura": 0.25,
        "desc": "Hard to crack, and stiffens the tiles around it.",
    },
}
BUILDABLE: Tuple[str, ...] = tuple(k for k in BUILDINGS if k != "base")

# ── Units ────────────────────────────────────────────────────────────────────
# ``claims`` marks a unit that takes ground simply by standing on it; ``siege``
# multiplies damage against buildings.
UNITS: Dict[str, Dict[str, Any]] = {
    "explorer": {
        "label": "Explorer", "cost": {"credits": 60}, "hp": 20, "attack": 2, "defense": 3,
        "speed": 3, "workers": 0, "claims": True, "value": 3,
        "desc": "Cheap and quick. Claims any unowned tile it stands on.",
    },
    "settler": {
        "label": "Settler", "cost": {"credits": 200, "materials": 20}, "hp": 30, "attack": 0,
        "defense": 4, "speed": 1, "workers": 1, "founds": 1, "value": 8,
        "desc": "Spend it to claim a two-tile radius anywhere you can reach.",
    },
    "soldier": {
        "label": "Soldier", "cost": {"credits": 100, "materials": 15}, "hp": 50, "attack": 12,
        "defense": 10, "speed": 1, "workers": 1, "value": 8, "needs": "barracks",
        "desc": "The line unit. Cheap, tough, slow.",
    },
    "cavalry": {
        "label": "Cavalry", "cost": {"credits": 240, "materials": 40}, "hp": 70, "attack": 22,
        "defense": 12, "speed": 2, "workers": 1, "value": 16, "needs": "barracks",
        "desc": "Hits hard and covers ground. Good for raiding.",
    },
    "cannon": {
        "label": "Cannon", "cost": {"credits": 320, "materials": 70}, "hp": 45, "attack": 34,
        "defense": 6, "speed": 1, "workers": 2, "value": 20, "needs": "barracks", "siege": 2.0,
        "desc": "Double damage to buildings, and folds fast if caught in the open.",
    },
}
TRAINABLE: Tuple[str, ...] = tuple(UNITS)

# ── Combat ───────────────────────────────────────────────────────────────────
COMBAT_JITTER: float = 0.15       # +/- swing on each blow, drawn from the seed
MIN_DAMAGE: float = 1.0
COUNTER_SHARE: float = 0.60       # the defender hits back at this fraction

# ── Resource exchange ────────────────────────────────────────────────────────
# A player can convert between credits and materials or food at a fixed spread,
# so a good trading run can be turned straight into stone and bread. The spread
# means shuttling back and forth is a slow bleed rather than a free option.
EXCHANGE: Dict[str, Dict[str, float]] = {
    "materials": {"buy": 6.0, "sell": 4.0},
    "food":      {"buy": 3.0, "sell": 2.0},
}

# ── Development score ────────────────────────────────────────────────────────
# What the ranking rewards, in the user's words: spanning large areas, having
# factories, having workers.
TILE_POINTS: float = 3.0
WORKER_POINTS: float = 4.0
ARMY_WEIGHT: float = 1.0


# ─────────────────────────────────────────────────────────────────────────────
# State
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Tile:
    x: int
    y: int
    terrain: str
    owner: Optional[str] = None
    building: Optional[str] = None
    hp: float = 0.0

    @property
    def passable(self) -> bool:
        return bool(TERRAIN[self.terrain]["passable"])


@dataclass
class Unit:
    uid: str            # unit id, unique across the world
    owner: str
    kind: str
    x: int
    y: int
    hp: float
    moves_left: float = 0.0

    @property
    def spec(self) -> Dict[str, Any]:
        return UNITS[self.kind]


@dataclass
class Empire:
    uid: str
    name: str
    home: Tuple[int, int]
    colour: str

    pnl_credits: float = 0.0    # mirrored from the market half each tick
    earned: float = 0.0         # credits produced by factories and markets
    spent: float = 0.0          # credits committed to buildings, units, trades

    food: float = START_FOOD
    materials: float = START_MATERIALS
    workers: int = START_WORKERS

    alive: bool = True
    log: List[str] = field(default_factory=list)

    @property
    def credits(self) -> float:
        """Spendable credits. Floored at zero so a P&L drawdown stalls new
        spending rather than driving the empire into a debt it cannot escape."""
        return max(0.0, START_GRANT + self.pnl_credits + self.earned - self.spent)

    def note(self, text: str) -> None:
        self.log.append(text)
        del self.log[:-40]


# Eight colours, distinguishable and already in the site's palette range.
COLOURS: Tuple[str, ...] = (
    "#4d9fd6", "#35c98b", "#f2555a", "#e8b84b",
    "#a97bd6", "#4fc3c9", "#e8843c", "#8f9bb3",
)


# ─────────────────────────────────────────────────────────────────────────────
# Distance helpers
# ─────────────────────────────────────────────────────────────────────────────

def distance(a: Tuple[int, int], b: Tuple[int, int]) -> int:
    """Chebyshev distance: one step covers a diagonal as cheaply as a straight."""
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]))


def neighbours(x: int, y: int) -> Iterable[Tuple[int, int]]:
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            if dx or dy:
                yield x + dx, y + dy


# ─────────────────────────────────────────────────────────────────────────────
# The world
# ─────────────────────────────────────────────────────────────────────────────

class World:
    """The map, the empires on it, and the rules that move between them."""

    def __init__(self, seed: Optional[int] = None, players: int = 4):
        self.seed = int(seed if seed is not None else random.randrange(1 << 30))
        self.rng = random.Random(self.seed)
        self.side = self._side_for(players)
        self.tick_no = 0
        self.grid: List[List[Tile]] = self._generate()
        self.empires: Dict[str, Empire] = {}
        self.units: Dict[str, Unit] = {}
        self._unit_seq = 0
        self._spawns = self._spawn_points()
        self.events: List[Dict[str, Any]] = []

    # ── Map generation ───────────────────────────────────────────────────────
    @staticmethod
    def _side_for(players: int) -> int:
        """Room to expand without a long walk to the nearest neighbour."""
        return max(MIN_SIDE, min(MAX_SIDE, 14 + 4 * max(1, players)))

    def _generate(self) -> List[List[Tile]]:
        """Blob-grown terrain: scatter seeds of each type and let them spread.

        Cheaper than real noise and produces the same thing that matters here —
        contiguous forests and ridges rather than confetti, so a hills cluster
        is worth fighting over.
        """
        n = self.side
        grid = [[Tile(x, y, "plain") for x in range(n)] for y in range(n)]

        def grow(terrain: str, blobs: int, size: int) -> None:
            for _ in range(blobs):
                cx = self.rng.randrange(n)
                cy = self.rng.randrange(n)
                frontier = [(cx, cy)]
                painted = 0
                while frontier and painted < size:
                    x, y = frontier.pop(self.rng.randrange(len(frontier)))
                    if not (0 <= x < n and 0 <= y < n):
                        continue
                    if grid[y][x].terrain != "plain":
                        continue
                    grid[y][x].terrain = terrain
                    painted += 1
                    for nx, ny in neighbours(x, y):
                        if self.rng.random() < 0.55:
                            frontier.append((nx, ny))

        area = n * n
        grow("forest", blobs=max(3, n // 4), size=max(8, area // 22))
        grow("hills", blobs=max(3, n // 5), size=max(6, area // 30))
        grow("water", blobs=max(2, n // 7), size=max(5, int(area * WATER_SHARE / 3)))
        return grid

    def _spawn_points(self) -> List[Tuple[int, int]]:
        """Bases spread evenly around a ring, nudged to the nearest land.

        A ring rather than random placement so nobody opens the game boxed into
        a corner while a rival owns the middle.
        """
        n = self.side
        centre = (n - 1) / 2.0
        radius = n * 0.34
        points: List[Tuple[int, int]] = []
        for i in range(len(COLOURS)):
            angle = 2 * math.pi * i / len(COLOURS) - math.pi / 4
            x = int(round(centre + radius * math.cos(angle)))
            y = int(round(centre + radius * math.sin(angle)))
            spot = self._nearest_land(x, y, avoid=points)
            if spot is not None:
                points.append(spot)
        return points

    def _nearest_land(self, x: int, y: int,
                      avoid: Sequence[Tuple[int, int]] = ()) -> Optional[Tuple[int, int]]:
        n = self.side
        best: Optional[Tuple[int, int]] = None
        best_d = 1 << 30
        for ty in range(n):
            for tx in range(n):
                tile = self.grid[ty][tx]
                if not tile.passable or tile.owner is not None:
                    continue
                if any(distance((tx, ty), p) < SPAWN_CLEARANCE for p in avoid):
                    continue
                d = (tx - x) ** 2 + (ty - y) ** 2
                if d < best_d:
                    best, best_d = (tx, ty), d
        return best

    # ── Lookups ──────────────────────────────────────────────────────────────
    def in_bounds(self, x: int, y: int) -> bool:
        return 0 <= x < self.side and 0 <= y < self.side

    def tile(self, x: int, y: int) -> Tile:
        if not self.in_bounds(x, y):
            raise WorldRejected(f"({x}, {y}) is off the map")
        return self.grid[y][x]

    def empire(self, uid: str) -> Empire:
        e = self.empires.get(uid)
        if e is None:
            raise WorldRejected("You have no base on this map yet")
        if not e.alive:
            raise WorldRejected("Your base has fallen — you are out of the world game")
        return e

    def units_at(self, x: int, y: int) -> List[Unit]:
        return [u for u in self.units.values() if u.x == x and u.y == y]

    def owned_tiles(self, uid: str) -> List[Tile]:
        return [t for row in self.grid for t in row if t.owner == uid]

    def buildings_of(self, uid: str) -> List[Tile]:
        return [t for t in self.owned_tiles(uid) if t.building]

    def has_building(self, uid: str, kind: str) -> bool:
        return any(t.building == kind for t in self.owned_tiles(uid))

    # ── Joining ──────────────────────────────────────────────────────────────
    def add_player(self, uid: str, name: str) -> Empire:
        """Place a base and hand over the starting stake. Idempotent."""
        if uid in self.empires:
            return self.empires[uid]
        if not self._spawns:
            raise WorldRejected("The map is full")

        home = self._spawns.pop(0)
        colour = COLOURS[len(self.empires) % len(COLOURS)]
        emp = Empire(uid=uid, name=name, home=home, colour=colour)
        self.empires[uid] = emp

        hx, hy = home
        base = self.tile(hx, hy)
        base.owner = uid
        base.building = "base"
        base.hp = float(BUILDINGS["base"]["hp"])
        self._claim_radius(uid, hx, hy, int(BUILDINGS["base"]["claim"]))

        # One explorer to open with, so the first thing a player can do is move.
        self._spawn_unit(uid, "explorer", hx, hy)
        emp.note(f"Base founded at ({hx}, {hy})")
        return emp

    def _claim_radius(self, uid: str, x: int, y: int, radius: int) -> int:
        """Take every unowned, passable tile within ``radius``. Never steals."""
        taken = 0
        for ty in range(y - radius, y + radius + 1):
            for tx in range(x - radius, x + radius + 1):
                if not self.in_bounds(tx, ty):
                    continue
                t = self.grid[ty][tx]
                if t.owner is None and t.passable:
                    t.owner = uid
                    taken += 1
        return taken

    def _spawn_unit(self, uid: str, kind: str, x: int, y: int) -> Unit:
        self._unit_seq += 1
        unit = Unit(uid=f"u{self._unit_seq}", owner=uid, kind=kind, x=x, y=y,
                    hp=float(UNITS[kind]["hp"]), moves_left=float(UNITS[kind]["speed"]))
        self.units[unit.uid] = unit
        return unit

    # ── The economy tick ─────────────────────────────────────────────────────
    def set_pnl(self, uid: str, pnl: float) -> None:
        """Mirror the market half's P&L into the empire's budget."""
        emp = self.empires.get(uid)
        if emp is not None and emp.alive:
            emp.pnl_credits = round(pnl * CREDITS_PER_DOLLAR, 2)

    def tick(self) -> List[Dict[str, Any]]:
        """Advance one world tick: production, upkeep, population, refresh."""
        self.tick_no += 1
        self.events = []

        for emp in self.empires.values():
            if not emp.alive:
                continue
            self._produce(emp)

        for unit in self.units.values():
            unit.moves_left = float(UNITS[unit.kind]["speed"])

        return self.events

    def _produce(self, emp: Empire) -> None:
        """Yields, then upkeep, then food, then growth — in that order.

        The order matters: a factory that cannot pay its materials upkeep this
        tick still produced first, so a single lean tick idles it rather than
        cascading into a shutdown.
        """
        tiles = self.buildings_of(emp.uid)

        # Workers are the constraint. Food producers are staffed first and the
        # rest by value: filling the factory before the farm starves the very
        # workforce the factory needs, and a growing empire would then collapse
        # for no reason a player could see.
        def priority(t: Tile) -> Tuple[int, float]:
            spec = BUILDINGS[t.building]
            feeds = "food" in spec.get("yield", {})
            return (0 if feeds else 1, -float(spec["value"]))

        staffed: List[Tile] = []
        free = emp.workers
        for t in sorted(tiles, key=priority):
            need = int(BUILDINGS[t.building].get("workers", 0))
            if need <= free:
                free -= need
                staffed.append(t)

        gained = {"credits": 0.0, "food": 0.0, "materials": 0.0}
        upkeep_materials = 0.0
        for t in staffed:
            spec = BUILDINGS[t.building]
            for res, amount in spec.get("upkeep", {}).items():
                if res == "materials":
                    upkeep_materials += float(amount)
            for res, amount in spec.get("yield", {}).items():
                gained[res] += float(amount)

        # A factory that cannot be fed does not pay out.
        if upkeep_materials > emp.materials + gained["materials"]:
            for t in staffed:
                spec = BUILDINGS[t.building]
                if spec.get("upkeep"):
                    for res, amount in spec.get("yield", {}).items():
                        gained[res] -= float(amount)
            upkeep_materials = 0.0
            emp.note("Materials ran out — factories idled this tick")

        emp.earned += max(0.0, gained["credits"])
        emp.materials = max(0.0, emp.materials + gained["materials"] - upkeep_materials)
        emp.food += gained["food"]

        # Feeding.
        eaten = emp.workers * FOOD_PER_WORKER
        if emp.food >= eaten:
            emp.food -= eaten
            cap = self.pop_cap(emp.uid)
            if emp.food >= GROWTH_FOOD_SURPLUS and emp.workers < cap:
                emp.workers += 1
                emp.food -= GROWTH_FOOD_SURPLUS
        else:
            emp.food = 0.0
            if emp.workers > 1:
                emp.workers -= STARVATION_LOSS
                emp.note("Not enough food — a worker left")

    def pop_cap(self, uid: str) -> int:
        cap = BASE_POP_CAP
        for t in self.buildings_of(uid):
            cap += int(BUILDINGS[t.building].get("pop_cap", 0))
        return cap

    def workers_used(self, uid: str) -> int:
        used = sum(int(BUILDINGS[t.building].get("workers", 0))
                   for t in self.buildings_of(uid))
        used += sum(int(UNITS[u.kind].get("workers", 0))
                    for u in self.units.values() if u.owner == uid)
        return used

    # ── Spending ─────────────────────────────────────────────────────────────
    def _afford(self, emp: Empire, cost: Dict[str, float], what: str) -> None:
        if cost.get("credits", 0) > emp.credits:
            raise WorldRejected(
                f"{what} costs {int(cost['credits'])} credits, you have {int(emp.credits)}. "
                "Credits come from your trading P&L, factories and markets.")
        if cost.get("materials", 0) > emp.materials:
            raise WorldRejected(
                f"{what} needs {int(cost['materials'])} materials, you have {int(emp.materials)}")
        if cost.get("food", 0) > emp.food:
            raise WorldRejected(f"{what} needs {int(cost['food'])} food, you have {int(emp.food)}")

    def _charge(self, emp: Empire, cost: Dict[str, float]) -> None:
        emp.spent += float(cost.get("credits", 0))
        emp.materials -= float(cost.get("materials", 0))
        emp.food -= float(cost.get("food", 0))

    # ── Actions ──────────────────────────────────────────────────────────────
    def act(self, uid: str, action: Dict[str, Any]) -> Dict[str, Any]:
        """Apply one action. Raises :class:`WorldRejected` with a readable reason."""
        kind = str(action.get("type", "")).lower().strip()
        emp = self.empire(uid)
        handler = {
            "build": self._do_build,
            "demolish": self._do_demolish,
            "train": self._do_train,
            "move": self._do_move,
            "attack": self._do_attack,
            "found": self._do_found,
            "trade": self._do_trade,
        }.get(kind)
        if handler is None:
            raise WorldRejected(
                f"Unknown action '{kind}'. Try build, demolish, train, move, attack, found or trade.")
        return handler(emp, action)

    # build ------------------------------------------------------------------
    def _do_build(self, emp: Empire, a: Dict[str, Any]) -> Dict[str, Any]:
        kind = str(a.get("building", "")).lower()
        if kind not in BUILDABLE:
            raise WorldRejected(f"'{kind}' is not something you can build. "
                                f"Options: {', '.join(BUILDABLE)}")
        x, y = _xy(a)
        tile = self.tile(x, y)
        spec = BUILDINGS[kind]

        if tile.owner != emp.uid:
            raise WorldRejected(f"({x}, {y}) is not yours — claim it with an explorer first")
        if tile.building:
            raise WorldRejected(f"({x}, {y}) already has a {BUILDINGS[tile.building]['label'].lower()}")
        if tile.terrain not in spec["terrain"]:
            allowed = ", ".join(TERRAIN[t]["label"].lower() for t in spec["terrain"])
            raise WorldRejected(f"A {spec['label'].lower()} needs {allowed}, "
                                f"and ({x}, {y}) is {TERRAIN[tile.terrain]['label'].lower()}")

        self._afford(emp, spec["cost"], spec["label"])
        self._charge(emp, spec["cost"])
        tile.building = kind
        tile.hp = float(spec["hp"])
        claimed = self._claim_radius(emp.uid, x, y, int(spec.get("claim", 0)))
        emp.note(f"Built a {spec['label'].lower()} at ({x}, {y})")
        return {"ok": True, "built": kind, "at": [x, y], "claimed": claimed}

    def _do_demolish(self, emp: Empire, a: Dict[str, Any]) -> Dict[str, Any]:
        x, y = _xy(a)
        tile = self.tile(x, y)
        if tile.owner != emp.uid or not tile.building:
            raise WorldRejected(f"You have nothing to demolish at ({x}, {y})")
        if tile.building == "base":
            raise WorldRejected("You cannot demolish your own base")
        kind = tile.building
        # Half the materials back, no credits — demolition is a correction, not
        # a savings account.
        emp.materials += float(BUILDINGS[kind]["cost"].get("materials", 0)) * 0.5
        tile.building = None
        tile.hp = 0.0
        emp.note(f"Demolished the {BUILDINGS[kind]['label'].lower()} at ({x}, {y})")
        return {"ok": True, "demolished": kind, "at": [x, y]}

    # train ------------------------------------------------------------------
    def _do_train(self, emp: Empire, a: Dict[str, Any]) -> Dict[str, Any]:
        kind = str(a.get("unit", "")).lower()
        if kind not in UNITS:
            raise WorldRejected(f"'{kind}' is not a unit. Options: {', '.join(TRAINABLE)}")
        spec = UNITS[kind]

        needs = spec.get("needs")
        if needs and not self.has_building(emp.uid, needs):
            raise WorldRejected(f"A {spec['label'].lower()} needs a "
                                f"{BUILDINGS[needs]['label'].lower()} first")

        count = max(1, min(10, int(a.get("count", 1) or 1)))
        made: List[str] = []
        for _ in range(count):
            need_workers = int(spec.get("workers", 0))
            if self.workers_used(emp.uid) + need_workers > emp.workers:
                if made:
                    break
                raise WorldRejected(
                    f"No spare workers. You have {emp.workers} and they are all assigned — "
                    "build housing and farms to grow.")
            try:
                self._afford(emp, spec["cost"], spec["label"])
            except WorldRejected:
                if made:
                    break
                raise
            self._charge(emp, spec["cost"])
            hx, hy = self._muster_point(emp, a)
            made.append(self._spawn_unit(emp.uid, kind, hx, hy).uid)

        emp.note(f"Trained {len(made)} x {spec['label'].lower()}")
        return {"ok": True, "trained": kind, "units": made, "count": len(made)}

    def _muster_point(self, emp: Empire, a: Dict[str, Any]) -> Tuple[int, int]:
        """Where a new unit appears: a named barracks if given, else the base."""
        if a.get("x") is not None and a.get("y") is not None:
            x, y = _xy(a)
            tile = self.tile(x, y)
            if tile.owner == emp.uid and tile.building in ("base", "barracks"):
                return x, y
            raise WorldRejected(f"({x}, {y}) is not your base or a barracks")
        return emp.home

    # move -------------------------------------------------------------------
    def _do_move(self, emp: Empire, a: Dict[str, Any]) -> Dict[str, Any]:
        unit = self._own_unit(emp, a)
        x, y = _xy(a)
        target = self.tile(x, y)
        if not target.passable:
            raise WorldRejected(f"({x}, {y}) is water")
        if unit.moves_left <= 0:
            raise WorldRejected(f"{UNITS[unit.kind]['label']} {unit.uid} has already moved this tick")

        path = self._path(unit, (x, y))
        if path is None:
            raise WorldRejected(f"{unit.uid} cannot reach ({x}, {y}) this tick — "
                                f"it has {unit.moves_left:g} movement left")

        claimed = 0
        for (nx, ny), cost in path:
            unit.x, unit.y, unit.moves_left = nx, ny, unit.moves_left - cost
            if UNITS[unit.kind].get("claims"):
                t = self.grid[ny][nx]
                if t.owner is None:
                    t.owner = emp.uid
                    claimed += 1
        if claimed:
            emp.note(f"Explorer claimed {claimed} tiles")
        return {"ok": True, "unit": unit.uid, "at": [unit.x, unit.y],
                "moves_left": round(unit.moves_left, 2), "claimed": claimed}

    def _path(self, unit: Unit, goal: Tuple[int, int]
              ) -> Optional[List[Tuple[Tuple[int, int], float]]]:
        """Cheapest route within the unit's remaining movement, or None.

        Small Dijkstra rather than a straight line so an algorithm can name a
        destination and let the unit walk around a lake, which is what anyone
        writing a bot would expect ``move`` to mean.
        """
        budget = unit.moves_left
        start = (unit.x, unit.y)
        if start == goal:
            return []

        best: Dict[Tuple[int, int], float] = {start: 0.0}
        prev: Dict[Tuple[int, int], Tuple[int, int]] = {}
        frontier = [(0.0, start)]
        while frontier:
            frontier.sort()
            spent, here = frontier.pop(0)
            if here == goal:
                break
            if spent > best.get(here, 1e9):
                continue
            for nx, ny in neighbours(*here):
                if not self.in_bounds(nx, ny):
                    continue
                t = self.grid[ny][nx]
                if not t.passable:
                    continue
                cost = spent + float(TERRAIN[t.terrain]["move_cost"])
                if cost > budget:
                    continue
                if cost < best.get((nx, ny), 1e9):
                    best[(nx, ny)] = cost
                    prev[(nx, ny)] = here
                    frontier.append((cost, (nx, ny)))

        if goal not in best:
            return None

        steps: List[Tuple[Tuple[int, int], float]] = []
        node = goal
        while node != start:
            parent = prev[node]
            steps.append((node, best[node] - best[parent]))
            node = parent
        steps.reverse()
        return steps

    # attack -----------------------------------------------------------------
    def _do_attack(self, emp: Empire, a: Dict[str, Any]) -> Dict[str, Any]:
        unit = self._own_unit(emp, a)
        x, y = _xy(a)
        target = self.tile(x, y)

        if distance((unit.x, unit.y), (x, y)) > 1:
            raise WorldRejected(f"{unit.uid} is at ({unit.x}, {unit.y}) — "
                                f"move next to ({x}, {y}) before attacking")
        if unit.moves_left <= 0:
            raise WorldRejected(f"{unit.uid} has already acted this tick")
        if float(UNITS[unit.kind]["attack"]) <= 0:
            raise WorldRejected(f"A {UNITS[unit.kind]['label'].lower()} cannot attack")

        unit.moves_left = 0.0
        defenders = [u for u in self.units_at(x, y) if u.owner != emp.uid]
        if defenders:
            return self._fight(emp, unit, defenders, target)
        if target.building and target.owner != emp.uid:
            return self._siege(emp, unit, target)
        raise WorldRejected(f"There is nothing of anyone else's at ({x}, {y})")

    def _defence_bonus(self, x: int, y: int) -> float:
        """Terrain, plus the tile's own fort, plus any fort standing next door."""
        tile = self.grid[y][x]
        bonus = float(TERRAIN[tile.terrain]["defense"])
        if tile.building:
            bonus += float(BUILDINGS[tile.building].get("defense", 0.0))
        for nx, ny in neighbours(x, y):
            if self.in_bounds(nx, ny):
                nb = self.grid[ny][nx]
                if nb.building and nb.owner == tile.owner:
                    bonus += float(BUILDINGS[nb.building].get("aura", 0.0))
        return bonus

    def _jitter(self) -> float:
        return 1.0 + self.rng.uniform(-COMBAT_JITTER, COMBAT_JITTER)

    def _fight(self, emp: Empire, attacker: Unit, defenders: List[Unit],
               tile: Tile) -> Dict[str, Any]:
        """One exchange against the strongest defender on the tile.

        Both sides land a blow, so trading an explorer into a fort is a real
        loss rather than a free probe. Whoever is left holds the ground.
        """
        target = max(defenders, key=lambda u: float(UNITS[u.kind]["defense"]) * u.hp)
        bonus = self._defence_bonus(tile.x, tile.y)

        atk = float(UNITS[attacker.kind]["attack"]) * self._jitter()
        dfn = float(UNITS[target.kind]["defense"]) * (1.0 + bonus)
        dealt = max(MIN_DAMAGE, atk - dfn * 0.5)

        counter_atk = float(UNITS[target.kind]["attack"]) * (1.0 + bonus) * self._jitter()
        counter_dfn = float(UNITS[attacker.kind]["defense"])
        taken = max(0.0, counter_atk - counter_dfn * 0.5) * COUNTER_SHARE

        target.hp -= dealt
        attacker.hp -= taken

        killed: List[str] = []
        for u in (target, attacker):
            if u.hp <= 0:
                killed.append(u.uid)
                self.units.pop(u.uid, None)

        took_ground = False
        if target.hp <= 0 and attacker.hp > 0 and not [
                u for u in self.units_at(tile.x, tile.y) if u.owner != emp.uid]:
            if not tile.building:
                attacker.x, attacker.y = tile.x, tile.y
                if tile.owner != emp.uid:
                    tile.owner = emp.uid
                    took_ground = True

        self.events.append({"kind": "battle", "at": [tile.x, tile.y],
                            "attacker": emp.name, "damage": round(dealt, 1)})
        emp.note(f"Fought at ({tile.x}, {tile.y}) for {dealt:.0f} damage")
        return {"ok": True, "result": "battle", "damage_dealt": round(dealt, 1),
                "damage_taken": round(taken, 1), "killed": killed,
                "captured": took_ground, "at": [tile.x, tile.y]}

    def _siege(self, emp: Empire, attacker: Unit, tile: Tile) -> Dict[str, Any]:
        spec = BUILDINGS[tile.building]
        siege = float(UNITS[attacker.kind].get("siege", 1.0))
        bonus = self._defence_bonus(tile.x, tile.y)
        dealt = max(MIN_DAMAGE,
                    float(UNITS[attacker.kind]["attack"]) * siege * self._jitter() / (1.0 + bonus))
        tile.hp -= dealt

        razed = tile.hp <= 0
        toppled_base = False
        if razed:
            loser = tile.owner
            was = tile.building
            tile.building = None
            tile.hp = 0.0
            tile.owner = emp.uid
            attacker.x, attacker.y = tile.x, tile.y
            emp.note(f"Razed a {spec['label'].lower()} at ({tile.x}, {tile.y})")
            self.events.append({"kind": "razed", "at": [tile.x, tile.y],
                                "building": was, "by": emp.name})
            if was == "base" and loser:
                toppled_base = True
                self._eliminate(loser, emp)

        return {"ok": True, "result": "siege", "damage_dealt": round(dealt, 1),
                "building_hp": round(max(0.0, tile.hp), 1), "razed": razed,
                "eliminated": toppled_base, "at": [tile.x, tile.y]}

    def _eliminate(self, loser_uid: str, winner: Empire) -> None:
        """A fallen base hands its whole territory to whoever took it."""
        loser = self.empires.get(loser_uid)
        if loser is None or not loser.alive:
            return
        loser.alive = False
        for t in self.owned_tiles(loser_uid):
            t.owner = winner.uid
        for u in [u for u in self.units.values() if u.owner == loser_uid]:
            self.units.pop(u.uid, None)
        loser.note(f"Your base fell to {winner.name}")
        winner.note(f"You took {loser.name}'s base and their territory")
        self.events.append({"kind": "eliminated", "player": loser.name, "by": winner.name})

    # found ------------------------------------------------------------------
    def _do_found(self, emp: Empire, a: Dict[str, Any]) -> Dict[str, Any]:
        unit = self._own_unit(emp, a)
        if not UNITS[unit.kind].get("founds"):
            raise WorldRejected(f"Only a settler can found an outpost, and {unit.uid} "
                                f"is a {UNITS[unit.kind]['label'].lower()}")
        tile = self.tile(unit.x, unit.y)
        if tile.owner not in (None, emp.uid):
            raise WorldRejected("You cannot found an outpost on someone else's ground")

        tile.owner = emp.uid
        claimed = 1 + self._claim_radius(emp.uid, unit.x, unit.y, 2)
        self.units.pop(unit.uid, None)   # the settler becomes the outpost
        emp.note(f"Founded an outpost at ({unit.x}, {unit.y}), claiming {claimed} tiles")
        return {"ok": True, "founded": [unit.x, unit.y], "claimed": claimed}

    # trade ------------------------------------------------------------------
    def _do_trade(self, emp: Empire, a: Dict[str, Any]) -> Dict[str, Any]:
        side = str(a.get("side", "")).lower()
        resource = str(a.get("resource", "")).lower()
        if side not in ("buy", "sell"):
            raise WorldRejected("A trade needs side 'buy' or 'sell'")
        if resource not in EXCHANGE:
            raise WorldRejected(f"You can only trade {', '.join(EXCHANGE)}")
        qty = float(a.get("qty", 0) or 0)
        if qty <= 0:
            raise WorldRejected("Trade quantity must be positive")

        price = EXCHANGE[resource][side]
        value = price * qty
        held = emp.materials if resource == "materials" else emp.food

        if side == "buy":
            self._afford(emp, {"credits": value}, f"{qty:g} {resource}")
            emp.spent += value
            delta = qty
        else:
            if qty > held:
                raise WorldRejected(f"You only have {held:g} {resource}")
            emp.spent -= value      # selling gives credits back to the budget
            delta = -qty

        if resource == "materials":
            emp.materials += delta
        else:
            emp.food += delta

        emp.note(f"{side.title()} {qty:g} {resource} at {price:g}")
        return {"ok": True, "side": side, "resource": resource, "qty": qty,
                "price": price, "credits": round(emp.credits, 2)}

    def _own_unit(self, emp: Empire, a: Dict[str, Any]) -> Unit:
        uid = str(a.get("unit_id") or a.get("unit") or "")
        unit = self.units.get(uid)
        if unit is None:
            raise WorldRejected(f"No unit '{uid}'. Check your world view for live unit ids.")
        if unit.owner != emp.uid:
            raise WorldRejected(f"{uid} is not yours")
        return unit

    # ── Scoring ──────────────────────────────────────────────────────────────
    def development(self, uid: str) -> Dict[str, Any]:
        """The ranking number, plus the parts it is made of.

        Broken out rather than returned bare so the dashboard can show a player
        exactly which lever to pull next.
        """
        tiles = self.owned_tiles(uid)
        built = [t for t in tiles if t.building]
        emp = self.empires.get(uid)

        land = len(tiles) * TILE_POINTS
        structures = sum(float(BUILDINGS[t.building]["value"]) for t in built)
        people = (emp.workers if emp else 0) * WORKER_POINTS
        army = sum(float(UNITS[u.kind]["value"])
                   for u in self.units.values() if u.owner == uid) * ARMY_WEIGHT

        return {
            "score": round(land + structures + people + army, 1),
            "land": round(land, 1), "structures": round(structures, 1),
            "people": round(people, 1), "army": round(army, 1),
            "tiles": len(tiles), "buildings": len(built),
            "factories": sum(1 for t in built if t.building == "factory"),
        }

    def standings(self) -> List[Dict[str, Any]]:
        rows = []
        for uid, emp in self.empires.items():
            dev = self.development(uid)
            rows.append({
                "user_id": uid, "name": emp.name, "colour": emp.colour,
                "alive": emp.alive, "credits": round(emp.credits, 2),
                "food": round(emp.food, 1), "materials": round(emp.materials, 1),
                "workers": emp.workers, "pop_cap": self.pop_cap(uid),
                "units": sum(1 for u in self.units.values() if u.owner == uid),
                **dev,
            })
        rows.sort(key=lambda r: (r["alive"], r["score"]), reverse=True)
        for rank, row in enumerate(rows, start=1):
            row["rank"] = rank
        return rows

    # ── Views ────────────────────────────────────────────────────────────────
    def map_view(self) -> Dict[str, Any]:
        """The whole board, packed column-major-free and short-keyed.

        Terrain and ownership are sent as parallel flat arrays rather than a
        list of tile objects: at 40x40 that is the difference between a payload
        the dashboard can poll every couple of seconds and one it cannot.
        """
        terrain_index = {name: i for i, name in enumerate(TERRAIN)}
        owner_index = {uid: i + 1 for i, uid in enumerate(self.empires)}

        terrain: List[int] = []
        owners: List[int] = []
        builds: List[Dict[str, Any]] = []
        for row in self.grid:
            for t in row:
                terrain.append(terrain_index[t.terrain])
                owners.append(owner_index.get(t.owner or "", 0))
                if t.building:
                    builds.append({
                        "x": t.x, "y": t.y, "b": t.building,
                        "o": owner_index.get(t.owner or "", 0),
                        "hp": round(t.hp, 1),
                        "max": float(BUILDINGS[t.building]["hp"]),
                    })

        return {
            "side": self.side,
            "tick": self.tick_no,
            "terrain_key": list(TERRAIN),
            "terrain": terrain,
            "owners": owners,
            "buildings": builds,
            "units": [{"id": u.uid, "k": u.kind, "x": u.x, "y": u.y,
                       "o": owner_index.get(u.owner, 0), "hp": round(u.hp, 1)}
                      for u in self.units.values()],
            "players": [{"i": owner_index[uid], "user_id": uid, "name": e.name,
                         "colour": e.colour, "home": list(e.home), "alive": e.alive}
                        for uid, e in self.empires.items()],
            "events": self.events,
        }

    def player_view(self, uid: str) -> Dict[str, Any]:
        """What one player's algorithm sees about itself each poll."""
        emp = self.empires.get(uid)
        if emp is None:
            return {"joined": False}
        dev = self.development(uid)
        return {
            "joined": True,
            "alive": emp.alive,
            "name": emp.name,
            # The same index the map view paints this player's tiles with, so a
            # client can match "who owns this tile" without guessing by name.
            "index": list(self.empires).index(uid) + 1,
            "colour": emp.colour,
            "home": list(emp.home),
            "tick": self.tick_no,
            "credits": round(emp.credits, 2),
            "pnl_credits": round(emp.pnl_credits, 2),
            "food": round(emp.food, 1),
            "materials": round(emp.materials, 1),
            "workers": emp.workers,
            "workers_free": max(0, emp.workers - self.workers_used(uid)),
            "pop_cap": self.pop_cap(uid),
            "development": dev,
            "tiles": [[t.x, t.y] for t in self.owned_tiles(uid)],
            "buildings": [{"x": t.x, "y": t.y, "kind": t.building,
                           "hp": round(t.hp, 1)}
                          for t in self.buildings_of(uid)],
            "units": [{"id": u.uid, "kind": u.kind, "x": u.x, "y": u.y,
                       "hp": round(u.hp, 1), "moves_left": round(u.moves_left, 2)}
                      for u in self.units.values() if u.owner == uid],
            "log": list(emp.log[-8:]),
        }


def _xy(a: Dict[str, Any]) -> Tuple[int, int]:
    try:
        return int(a["x"]), int(a["y"])
    except (KeyError, TypeError, ValueError):
        raise WorldRejected("This action needs integer x and y") from None


def catalogue() -> Dict[str, Any]:
    """Everything a player needs to know to write a strategy, in one payload."""
    return {
        "terrain": {k: {"label": v["label"], "passable": v["passable"],
                        "defense": v["defense"], "move_cost": v["move_cost"]}
                    for k, v in TERRAIN.items()},
        "buildings": {k: {"label": v["label"], "cost": v["cost"],
                          "terrain": list(v["terrain"]), "hp": v["hp"],
                          "value": v["value"], "workers": v.get("workers", 0),
                          "yield": v.get("yield", {}), "upkeep": v.get("upkeep", {}),
                          "desc": v["desc"]}
                      for k, v in BUILDINGS.items()},
        "units": {k: {"label": v["label"], "cost": v["cost"], "hp": v["hp"],
                      "attack": v["attack"], "defense": v["defense"],
                      "speed": v["speed"], "workers": v.get("workers", 0),
                      "needs": v.get("needs"), "value": v["value"], "desc": v["desc"]}
                  for k, v in UNITS.items()},
        "exchange": EXCHANGE,
        "economy": {
            "start_grant": START_GRANT,
            "credits_per_dollar": CREDITS_PER_DOLLAR,
            "world_tick_seconds": WORLD_TICK_SECONDS,
            "base_pop_cap": BASE_POP_CAP,
            "food_per_worker": FOOD_PER_WORKER,
        },
        "scoring": {
            "tile_points": TILE_POINTS,
            "worker_points": WORKER_POINTS,
            "army_weight": ARMY_WEIGHT,
        },
    }
