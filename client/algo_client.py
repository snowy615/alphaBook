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
1. Open a run at https://alphabook.uk/market-sim-py and click "Connect a bot"
   to get your RUN_ID and TOKEN.
2. Fill them in below (or pass them as environment variables / CLI args).
3. Run it:  ``python algo_client.py``
4. Edit ``on_tick`` and re-run. That's the whole game.

No third-party packages required — this uses only the Python standard library,
so it runs anywhere Python 3.8+ is installed.

The rules
---------
* Position limit: 1000 per item, long or short. Resting orders count.
* You may send at most ~10 orders per second (the server rate-limits you; this
  client paces itself to stay under it).
* P&L is marked at each item's hidden fair value, revealed only at the end.
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

LOOKBACK = 20


def on_tick(ctx):
    """A simple mean-reversion example. Replace with your own idea."""
    orders = [{"action": "cancel_all"}]
    history = ctx["memory"].setdefault("mids", {})

    for item in ctx["items"]:
        q = ctx["market"][item]
        mid = q["mid"]
        if mid is None:
            continue

        window = history.setdefault(item, [])
        window.append(mid)
        if len(window) > LOOKBACK:
            window.pop(0)
        if len(window) < 5:
            continue

        average = sum(window) / len(window)
        edge = (average - mid) / mid              # positive => price looks cheap
        position = ctx["position"][item]
        room = ctx["limit"] - abs(position)
        if room < 10:
            continue

        size = max(5, min(50, int(abs(edge) * 40000)))
        size = min(size, room)

        if edge > 0.0008:
            orders.append({"item": item, "side": "BUY",  "price": round(q["bid"], 2), "qty": size})
        elif edge < -0.0008:
            orders.append({"item": item, "side": "SELL", "price": round(q["ask"], 2), "qty": size})

    return orders


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
            return

        ctx = build_ctx(snapshot, memory)
        try:
            orders = on_tick(ctx) or []
        except Exception as e:  # your bug, not the server's — keep going
            print(f"[on_tick error] {type(e).__name__}: {e}")
            orders = []

        if orders:
            try:
                result = send_orders(orders)
                rejects = [r for r in result.get("results", []) if not r.get("ok")]
                note = f"  ({len(rejects)} rejected: {rejects[0]['error']})" if rejects else ""
                print(f"t={ctx['tick']:>3}  pnl={result.get('pnl'):>10}  "
                      f"sent {len(orders)}{note}")
            except ApiError as e:
                print(f"[orders] {e}")

        # Pace the loop so we never exceed the server's rate limit.
        elapsed = time.monotonic() - loop_start
        time.sleep(max(0.0, LOOP_SECONDS - elapsed))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
