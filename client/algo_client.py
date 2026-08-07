#!/usr/bin/env python3
"""
AlphaBook — Market Simulation Py client.
========================================

Run this on YOUR OWN machine. It logs into a run with the API token you copy
from the run page, polls the market once per loop, calls your ``on_tick``, and
posts the orders it returns. Your strategy code never leaves your computer —
the server only ever receives orders.

Quick start
-----------
If you downloaded this from a run's "Connect your bot" panel, your run id and
token are ALREADY filled in below — just run it:

    python3 algo_client.py

Then edit ``on_tick`` and re-run. That's the whole game.

(Downloaded the blank version instead? Fill in RUN_ID and TOKEN below — from the
Connect panel — or pass them as environment variables.)

No third-party packages required — this uses only the Python standard library,
so it runs anywhere Python 3.8+ is installed.

Two games, one strategy
-----------------------
There is a market, and there is a map. Your trading P&L IS your budget on the
map::

    credits = 2000 + your P&L + what your factories earn - what you have spent

So ``on_tick`` trades and ``on_world_tick`` spends. You win the world half by
having the most developed empire at the bell: territory, buildings, workers and
army. Trade badly and you simply cannot afford to expand.

The rules
---------
* Position limit: 1000 per item, long or short. Resting orders count.
* You may send at most ~10 orders per second (the server rate-limits you; this
  client paces itself to stay under it). World actions come out of the same
  budget, which is why they are sent once every five seconds, not every loop.
* P&L is marked at each item's hidden fair value, revealed only at the end.
* The world advances once every 5 seconds. Units get their movement back then.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request

# ─────────────────────────────────────────────────────────────────────────────
# Configuration — fill these in (or set env vars ALPHABOOK_BASE / RUN_ID / TOKEN)
# ─────────────────────────────────────────────────────────────────────────────
BASE_URL = os.environ.get("ALPHABOOK_BASE", "https://alphabook.uk")
RUN_ID   = os.environ.get("RUN_ID", "PASTE_YOUR_RUN_ID_HERE")
TOKEN    = os.environ.get("TOKEN", "PASTE_YOUR_TOKEN_HERE")

# How often to poll + act. The server allows ~10 orders/sec; one loop per second
# that sends a handful of orders stays comfortably under that.
LOOP_SECONDS = 1.0

# The world only advances every five seconds, so acting on it faster than that
# just burns rate limit your orders could be using.
WORLD_LOOP_SECONDS = 5.0


# ═════════════════════════════════════════════════════════════════════════════
# YOUR STRATEGY
# ═════════════════════════════════════════════════════════════════════════════
# on_tick(ctx) is called once per loop. Return a list of orders.
#
#   {"item": "WIDGET", "side": "BUY",  "price": 99.95, "qty": 10}  # resting limit
#   {"item": "WIDGET", "side": "SELL", "qty": 10}                  # market order
#   {"action": "cancel_all"}                                       # pull everything
#   {"action": "cancel_all", "item": "WIDGET"}                     # pull one item
#
# ctx contains:
#   ctx["items"]            -> ["WIDGET", "GADGET", "GIZMO", "DOODAD"]
#   ctx["limit"]            -> 1000
#   ctx["seconds_left"]     -> time remaining
#   ctx["market"][item]     -> {"bid","ask","bid_qty","ask_qty","mid","spread","last"}
#   ctx["position"][item]   -> your signed position (negative = short)
#   ctx["open_orders"][item]-> {"buy": qty, "sell": qty} still resting
#   ctx["cash"], ctx["pnl"]
#   ctx["memory"]           -> a dict that PERSISTS between ticks; keep state here

# This starter isn't clever — it's a tour of everything you can do, so you have
# a working base to rip apart. Each tick it: CHECKS your account, MAKES a market
# (quotes both sides), PLACES resting limit orders, and TAKES liquidity with a
# market order when it's carrying too much inventory. Keep the total under ~10
# orders per tick to stay inside the rate limit (1 cancel + 2 per item = 9).

INVENTORY_TRIM = 100   # once |position| passes this, cross the spread to reduce
QUOTE_SIZE = 15        # lots per side when making a market


def check_account(ctx):
    """CHECK — read and print your P&L, positions and resting orders."""
    held = {k: int(v) for k, v in ctx["position"].items() if v}
    resting = {
        item: o for item, o in ctx["open_orders"].items()
        if o.get("buy") or o.get("sell")
    }
    print(
        f"t={ctx['tick']:>3} {ctx['seconds_left']:>4.0f}s | "
        f"pnl={ctx['pnl']:>10.2f}  cash={ctx['cash']:>10.2f} | "
        f"pos={held or 'flat'} | resting={resting or 'none'}"
    )


def on_tick(ctx):
    check_account(ctx)

    # CANCEL — clear last tick's resting orders so we can re-quote fresh.
    orders = [{"action": "cancel_all"}]

    for item in ctx["items"]:
        q = ctx["market"][item]
        mid, bid, ask = q["mid"], q["bid"], q["ask"]
        if mid is None:
            continue

        position = ctx["position"][item]
        room = ctx["limit"] - abs(position)      # headroom before the limit
        if room < QUOTE_SIZE:
            continue

        # A spread ~10 bps wide (at least 2 cents), leaning against inventory so
        # we naturally drift back toward flat.
        half = max(mid * 0.001, 0.02)
        skew = -position * (half * 0.02)
        size = min(QUOTE_SIZE, room)

        if position > INVENTORY_TRIM and ask is not None:
            # TAKE — too long: cross the spread with a market SELL to cut risk,
            # and keep a passive bid working to buy back cheaper.
            orders.append({"item": item, "side": "SELL", "qty": 10})           # market order
            orders.append({"item": item, "side": "BUY",  "price": round(mid - half + skew, 2), "qty": size})
        elif position < -INVENTORY_TRIM and bid is not None:
            # TAKE — too short: market BUY to cover, keep a passive ask working.
            orders.append({"item": item, "side": "BUY",  "qty": 10})           # market order
            orders.append({"item": item, "side": "SELL", "price": round(mid + half + skew, 2), "qty": size})
        else:
            # MAKE A MARKET — quote both sides with resting limit orders (PLACE).
            orders.append({"item": item, "side": "BUY",  "price": round(mid - half + skew, 2), "qty": size})
            orders.append({"item": item, "side": "SELL", "price": round(mid + half + skew, 2), "qty": size})

    return orders


# ═════════════════════════════════════════════════════════════════════════════
# YOUR EMPIRE
# ═════════════════════════════════════════════════════════════════════════════
# on_world_tick(w) is called once every 5 seconds. Return a list of actions.
#
#   {"type": "build",    "building": "farm", "x": 5, "y": 7}
#   {"type": "train",    "unit": "explorer", "count": 2}
#   {"type": "move",     "unit_id": "u3", "x": 6, "y": 7}
#   {"type": "attack",   "unit_id": "u3", "x": 7, "y": 7}   # must be adjacent
#   {"type": "found",    "unit_id": "u9"}                   # settler -> outpost
#   {"type": "trade",    "side": "buy", "resource": "materials", "qty": 20}
#   {"type": "demolish", "x": 5, "y": 7}
#
# w contains:
#   w["credits"], w["food"], w["materials"], w["workers"], w["workers_free"]
#   w["tiles"]        -> [[x, y], ...] every tile you own
#   w["buildings"]    -> [{"x","y","kind","hp"}, ...]
#   w["units"]        -> [{"id","kind","x","y","hp","moves_left"}, ...]
#   w["development"]  -> {"score","land","structures","people","army","factories"}
#   w["terrain"](x,y) -> "plain" | "forest" | "hills" | "water"
#   w["owner"](x,y)   -> a player index, or 0 for unclaimed ground
#   w["mine"](x,y)    -> True if that tile is yours
#   w["memory"]       -> persists between world ticks, like ctx["memory"]
#
# Buildings: farm (plains, food), lumber (forest, materials), mine (hills,
# materials), house (+4 population cap), market (credits + claims ground),
# factory (the big credit earner, needs 3 workers and eats materials),
# barracks (unlocks soldiers), fort (defence).

# A deliberately simple opening: feed people, then industrialise. Farms are
# spread through it because every factory you add needs three more mouths fed,
# and a workforce that starves takes the factories offline with it.
BUILD_ORDER = ["farm", "lumber", "house", "farm", "mine", "house",
               "market", "farm", "factory", "farm", "factory"]


def on_world_tick(w):
    """Spend the trading profit. This starter is a plan, not a good plan."""
    print(f"  world t={w['tick']:>3} | credits={w['credits']:>9.0f} "
          f"food={w['food']:>5.0f} mat={w['materials']:>5.0f} "
          f"workers={w['workers']} | score={w['development']['score']}")

    actions = []

    # EXPLORE — send every explorer outward from home to claim ground. An
    # explorer takes any unowned tile it walks over, so movement is territory.
    hx, hy = w["home"]
    explorers = [u for u in w["units"] if u["kind"] == "explorer"]
    for i, u in enumerate(explorers):
        if u["moves_left"] <= 0:
            continue
        # Fan out: each explorer keeps pushing along its own heading.
        heading = w["memory"].setdefault("headings", {}).setdefault(
            u["id"], [(1, 0), (0, 1), (-1, 0), (0, -1), (1, 1), (-1, -1)][i % 6])
        tx = min(max(u["x"] + heading[0] * 2, 0), w["side"] - 1)
        ty = min(max(u["y"] + heading[1] * 2, 0), w["side"] - 1)
        if w["terrain"](tx, ty) != "water":
            actions.append({"type": "move", "unit_id": u["id"], "x": tx, "y": ty})

    # EXPAND — a couple more explorers early on; they pay for themselves in land.
    if len(explorers) < 3 and w["credits"] > 400:
        actions.append({"type": "train", "unit": "explorer"})

    # BUILD — work through the build order, on the first tile that suits.
    # The step is derived from what actually stands (ignoring the base) rather
    # than counted in memory, so a build you could not afford is simply retried
    # next tick instead of being skipped.
    step = max(0, len(w["buildings"]) - 1)
    if step < len(BUILD_ORDER):
        kind = BUILD_ORDER[step]
        wants = {"farm": ("plain",), "lumber": ("forest",), "mine": ("hills",),
                 "house": ("plain", "forest"), "market": ("plain",),
                 "factory": ("plain", "hills"), "barracks": ("plain", "hills"),
                 "fort": ("plain", "forest", "hills")}[kind]
        built = {(b["x"], b["y"]) for b in w["buildings"]}
        # Nearest suitable free tile to home, so the empire stays compact and
        # defensible rather than sprawling into someone else's reach.
        spots = sorted(
            (t for t in w["tiles"]
             if tuple(t) not in built and w["terrain"](t[0], t[1]) in wants),
            key=lambda t: max(abs(t[0] - hx), abs(t[1] - hy)))
        if spots:
            actions.append({"type": "build", "building": kind,
                            "x": spots[0][0], "y": spots[0][1]})

    return actions


# ═════════════════════════════════════════════════════════════════════════════
# The runner — you shouldn't need to touch anything below here.
# ═════════════════════════════════════════════════════════════════════════════

class ApiError(Exception):
    pass


def _request(method, path, body=None):
    url = f"{BASE_URL.rstrip('/')}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {TOKEN}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        try:
            detail = json.loads(detail).get("detail", detail)
        except Exception:
            pass
        raise ApiError(f"{e.code} {detail}") from None
    except urllib.error.URLError as e:
        raise ApiError(f"connection failed: {e.reason}") from None


def fetch_market():
    return _request("GET", f"/market-sim-py/run/{RUN_ID}/market")


def send_orders(orders):
    return _request("POST", f"/market-sim-py/run/{RUN_ID}/orders", {"orders": orders})


def fetch_world():
    return _request("GET", f"/market-sim-py/run/{RUN_ID}/world")


def send_world_actions(actions):
    return _request("POST", f"/market-sim-py/run/{RUN_ID}/world/actions",
                    {"actions": actions})


def build_world_ctx(snapshot, memory):
    """Turn a /world response into the dict on_world_tick expects.

    The map arrives as flat arrays (cheap to send, awkward to read), so this
    wraps them in ``terrain(x, y)`` / ``owner(x, y)`` lookups. Strategy code
    should never have to do index arithmetic.
    """
    me = snapshot.get("me") or {}
    board = snapshot.get("map") or {}
    side = board.get("side", 0)
    key = board.get("terrain_key", [])
    terrain = board.get("terrain", [])
    owners = board.get("owners", [])

    def at(grid, default):
        def look(x, y):
            if not (0 <= x < side and 0 <= y < side):
                return default
            return grid[y * side + x]
        return look

    raw_terrain = at(terrain, 0)
    owner_at = at(owners, 0)
    my_index = me.get("index", 0)

    ctx = dict(me)
    ctx.update({
        "side": side,
        "tick": snapshot.get("world_tick", 0),
        "terrain": lambda x, y: key[raw_terrain(x, y)] if key else "plain",
        "owner": owner_at,
        "mine": lambda x, y: owner_at(x, y) == my_index,
        "standings": snapshot.get("standings", []),
        "memory": memory,
    })
    ctx.setdefault("home", [0, 0])
    ctx.setdefault("units", [])
    ctx.setdefault("buildings", [])
    ctx.setdefault("tiles", [])
    ctx.setdefault("development", {"score": 0})
    return ctx


def build_ctx(snapshot, memory):
    """Turn a /market response into the ctx dict on_tick expects."""
    market = {row["item"]: row for row in snapshot["market"]}
    me = snapshot.get("me") or {}
    positions = me.get("positions") or {s: 0 for s in snapshot["items"]}
    open_orders = me.get("open_orders") or {}
    return {
        "items": snapshot["items"],
        "limit": snapshot["position_limit"],
        "seconds_left": snapshot["seconds_left"],
        "tick": snapshot["tick"],
        "market": market,
        "position": positions,
        "open_orders": open_orders,
        "cash": me.get("cash", 0.0),
        "pnl": me.get("pnl", 0.0),
        "memory": memory,
    }


def main():
    if "PASTE_YOUR" in RUN_ID or "PASTE_YOUR" in TOKEN:
        sys.exit("Set RUN_ID and TOKEN (edit the file, or export RUN_ID / TOKEN / ALPHABOOK_BASE).")

    print(f"Connecting to {BASE_URL} run {RUN_ID} …")
    memory = {}
    world_memory = {}
    world_next = 0.0
    waited_for_start = False

    while True:
        loop_start = time.monotonic()
        try:
            snapshot = fetch_market()
        except ApiError as e:
            print(f"[market] {e}")
            time.sleep(LOOP_SECONDS)
            continue

        status = snapshot["status"]
        if status == "lobby":
            if not waited_for_start:
                print("Waiting for the host to start the run …")
                waited_for_start = True
            time.sleep(LOOP_SECONDS)
            continue
        if status == "finished":
            me = snapshot.get("me") or {}
            print(f"Run finished. Final P&L: {me.get('pnl')}  positions: {me.get('positions')}")
            try:
                w = (fetch_world().get("me") or {})
                if w.get("joined"):
                    d = w.get("development", {})
                    print(f"Empire: score {d.get('score')} from {d.get('tiles')} tiles, "
                          f"{d.get('buildings')} buildings ({d.get('factories')} factories), "
                          f"{w.get('workers')} workers.")
            except ApiError:
                pass
            return

        ctx = build_ctx(snapshot, memory)
        try:
            orders = on_tick(ctx) or []
        except Exception as e:  # your bug, not the server's — keep going
            print(f"[on_tick error] {type(e).__name__}: {e}")
            orders = []

        if orders:
            try:
                # on_tick already printed the account line; here we only surface
                # anything the exchange rejected (bad price, limit, rate limit…).
                result = send_orders(orders)
                rejects = [r for r in result.get("results", []) if not r.get("ok")]
                if rejects:
                    print(f"      ↳ {len(rejects)} rejected: {rejects[0]['error']}")
            except ApiError as e:
                print(f"[orders] {e}")

        # ---- the empire, once every WORLD_LOOP_SECONDS ----
        # Deliberately slower than the trading loop: the world only advances
        # every five seconds, so polling it faster spends rate-limit budget that
        # your orders need and gets you the same board back.
        if loop_start >= world_next:
            world_next = loop_start + WORLD_LOOP_SECONDS
            try:
                w = build_world_ctx(fetch_world(), world_memory)
                if w.get("joined") and w.get("alive"):
                    try:
                        actions = on_world_tick(w) or []
                    except Exception as e:      # your bug — keep trading
                        print(f"[on_world_tick error] {type(e).__name__}: {e}")
                        actions = []
                    if actions:
                        result = send_world_actions(actions)
                        for r in result.get("results", []):
                            if not r.get("ok"):
                                print(f"      ↳ world: {r.get('error')}")
            except ApiError as e:
                print(f"[world] {e}")

        # Pace the loop so we never exceed the server's rate limit.
        elapsed = time.monotonic() - loop_start
        time.sleep(max(0.0, LOOP_SECONDS - elapsed))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
