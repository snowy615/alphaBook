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
                # on_tick already printed the account line; here we only surface
                # anything the exchange rejected (bad price, limit, rate limit…).
                result = send_orders(orders)
                rejects = [r for r in result.get("results", []) if not r.get("ok")]
                if rejects:
                    print(f"      ↳ {len(rejects)} rejected: {rejects[0]['error']}")
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
