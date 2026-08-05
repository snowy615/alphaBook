"""
Cross-game scoring: a skill rating per mode, and one overall number.
====================================================================

Every mode used to compute a leaderboard live and then throw it away inside
its own game document, so nothing followed a player around. This module is
the shared spine: each finished game reports one number per player, and that
turns into

* a **per-mode rating** — the player's percentile among everyone who has
  played that mode, on a 0–100 scale, and
* an **overall rating** — the mean of the modes they've played, plus a small
  breadth bonus.

Two deliberate choices come from how this is used (ranking students, not
running a casino):

1. **Volume is capped.** A mode's representative number is the player's
   *average* result, never a running total, so grinding one mode cannot
   inflate a rating. Playing more only helps through the breadth bonus,
   which is small enough that it can never carry a weak player past a
   strong one.
2. **Thin samples are shrunk toward the middle.** With fewer than
   MIN_GAMES_FULL results a rating is pulled back toward 50, so a single
   lucky game doesn't top the board. Ratings below that bar are flagged
   provisional in the UI.

Storage: one `player_scores` document per user holding per-mode aggregates
(cheap to scan for a leaderboard — it is one document per player, not per
game), plus an append-only `score_events` document per result for history.
"""
from __future__ import annotations

import datetime as dt
import logging
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from app import db as db_module

log = logging.getLogger("uvicorn.error")

EVENTS_COLLECTION = "score_events"
PLAYERS_COLLECTION = "player_scores"

# A rating only counts at full weight once the player has this many results.
MIN_GAMES_FULL = 3
# Breadth is worth a nudge, not a shortcut.
BREADTH_BONUS_MAX = 6.0
# How many recent results to keep inline for trend detection.
RECENT_KEEP = 10
# Leaderboard cache, seconds.
CACHE_TTL = 20.0


# ─────────────────────────────────────────────────────────────────────────────
# Mode registry
# ─────────────────────────────────────────────────────────────────────────────
# kind:
#   "per_game" → each finished game reports a result; representative value is
#                the mean of those results.
#   "gauge"    → a continuously updated figure (live trading P&L); the latest
#                value is the representative one, and `games` carries the
#                activity count used for the confidence shrink.

MODES: Dict[str, Dict[str, Any]] = {
    "market_sim": {
        "label": "Market Simulation",
        "metric": "P&L",
        "unit": "$",
        "href": "/market",
        "kind": "gauge",
        "blurb": "Live order-book trading against the market maker.",
    },
    "market_sim_py": {
        "label": "Market Sim Py",
        "metric": "P&L",
        "unit": "$",
        "href": "/market-sim-py",
        "kind": "per_game",
        "blurb": "Your Python bot trading a live book.",
    },
    "swe_prep": {
        "label": "SWE Prep",
        "metric": "P&L",
        "unit": "$",
        "href": "/swe-prep",
        "kind": "per_game",
        "blurb": "Strategy coding sandbox.",
    },
    "risks": {
        "label": "Risks",
        "metric": "Score",
        "unit": "",
        "href": "/risks",
        "kind": "per_game",
        "blurb": "Surviving a synthetic crash with a market-neutral book.",
    },
    "headline": {
        "label": "Headline Trading",
        "metric": "P&L",
        "unit": "$",
        "href": "/headline",
        "kind": "per_game",
        "blurb": "Trading a futures market as news breaks.",
    },
    "fiveos": {
        "label": "5Os",
        "metric": "P&L",
        "unit": "",
        "href": "/5os",
        "kind": "per_game",
        "blurb": "Estimating hidden card statistics under pressure.",
    },
    "poker_auction": {
        "label": "Poker Auction",
        "metric": "Final bankroll",
        "unit": "$",
        "href": "/poker-auction",
        "kind": "per_game",
        "blurb": "Sealed-bid second-price auctions for cards.",
    },
    "mental_math": {
        "label": "Mental Math",
        "metric": "Accuracy",
        "unit": "%",
        "href": "/mental-math",
        "kind": "per_game",
        "blurb": "Timed arithmetic drills.",
    },
    "crash_ledger": {
        "label": "Crash Ledger",
        "metric": "Score",
        "unit": "",
        "href": "/crash-ledger",
        "kind": "per_game",
        "blurb": "Making markets on how real names behaved in past crashes.",
    },
}

MODE_KEYS: List[str] = list(MODES)


def mode_meta(mode: str) -> Dict[str, Any]:
    return MODES.get(mode, {"label": mode, "metric": "Score", "unit": "", "kind": "per_game"})


# ─────────────────────────────────────────────────────────────────────────────
# Recording
# ─────────────────────────────────────────────────────────────────────────────

def _blank_mode_agg() -> Dict[str, Any]:
    return {"games": 0, "sum": 0.0, "best": None, "last": None, "recent": []}


async def record_result(
    mode: str,
    user_id: str,
    username: str,
    value: float,
    *,
    game_id: Optional[str] = None,
    detail: Optional[Dict[str, Any]] = None,
    feedback: Optional[Dict[str, Any]] = None,
) -> None:
    """
    Record one finished game for one player.

    Never raises: a scoring failure must not break the game flow that called
    it, so problems are logged and swallowed.
    """
    if mode not in MODES or not user_id:
        return
    try:
        value = float(value)
    except (TypeError, ValueError):
        return

    try:
        now = dt.datetime.utcnow()
        await db_module.db.collection(EVENTS_COLLECTION).document(str(uuid.uuid4())).set({
            "mode": mode,
            "user_id": str(user_id),
            "username": username or "",
            "value": value,
            "game_id": game_id or "",
            "detail": detail or {},
            "created_at": now,
        })

        ref = db_module.db.collection(PLAYERS_COLLECTION).document(str(user_id))
        doc = await ref.get()
        data = (doc.to_dict() or {}) if doc.exists else {}
        modes = data.get("modes") or {}
        agg = modes.get(mode) or _blank_mode_agg()

        agg["games"] = int(agg.get("games", 0)) + 1
        agg["sum"] = float(agg.get("sum", 0.0)) + value
        best = agg.get("best")
        agg["best"] = value if best is None else max(float(best), value)
        agg["last"] = value
        recent = list(agg.get("recent") or [])
        recent.append(value)
        agg["recent"] = recent[-RECENT_KEEP:]
        agg["updated_at"] = now
        if feedback:
            # The dashboard shows the most recent game's coaching per mode, so
            # keep it on the aggregate rather than re-querying the event log.
            agg["last_feedback"] = feedback

        modes[mode] = agg
        await ref.set({
            "username": username or data.get("username") or "",
            "modes": modes,
            "updated_at": now,
        }, merge=True)
    except Exception:
        log.warning("record_result failed (mode=%s user=%s)", mode, user_id, exc_info=True)


async def set_gauge(mode: str, user_id: str, username: str, value: float, activity: int) -> None:
    """
    Set a continuously-measured mode's current figure (live trading P&L).

    `activity` stands in for game count when deciding how much confidence the
    rating deserves — someone with two fills should not rank as firmly as
    someone with fifty.
    """
    if mode not in MODES or not user_id:
        return
    try:
        now = dt.datetime.utcnow()
        ref = db_module.db.collection(PLAYERS_COLLECTION).document(str(user_id))
        await ref.set({
            "username": username or "",
            "modes": {mode: {
                "games": int(activity),
                "sum": float(value),
                "best": float(value),
                "last": float(value),
                "recent": [],
                "updated_at": now,
            }},
            "updated_at": now,
        }, merge=True)
    except Exception:
        log.warning("set_gauge failed (mode=%s user=%s)", mode, user_id, exc_info=True)


_last_market_sync = 0.0
_synced_market: Dict[str, Tuple[float, int]] = {}


async def sync_market_sim(min_interval: float = 60.0) -> None:
    """
    Refresh live-trading ratings from order-book P&L.

    Market Simulation has no "finished game" to hook, so its rating is a gauge
    recomputed from every user's trades. Throttled, and only written for users
    whose figures actually moved, so a busy leaderboard doesn't hammer
    Firestore.
    """
    global _last_market_sync
    now = time.monotonic()
    if now - _last_market_sync < min_interval:
        return
    _last_market_sync = now

    try:
        from app.admin import calculate_all_user_stats

        pnls, trade_counts = await calculate_all_user_stats()
        if not pnls:
            return

        user_docs = await db_module.db.collection("users").get()
        names = {d.id: (d.to_dict() or {}).get("username", "") for d in user_docs}
        admins = {d.id for d in user_docs if (d.to_dict() or {}).get("is_admin")}

        for uid, pnl in pnls.items():
            if uid in admins or uid not in names:
                continue                      # bots and admins stay off the board
            trades = int(trade_counts.get(uid, 0))
            if trades <= 0:
                continue
            prev = _synced_market.get(uid)
            if prev and abs(prev[0] - pnl) < 0.01 and prev[1] == trades:
                continue
            _synced_market[uid] = (pnl, trades)
            await set_gauge("market_sim", uid, names[uid], pnl, trades)
        invalidate_cache()
    except Exception:
        log.warning("market_sim score sync failed", exc_info=True)


def representative_value(mode: str, agg: Dict[str, Any]) -> Optional[float]:
    """The one number that represents a player in a mode."""
    games = int(agg.get("games", 0) or 0)
    if games <= 0:
        return None
    if mode_meta(mode).get("kind") == "gauge":
        return float(agg.get("last", 0.0) or 0.0)
    return float(agg.get("sum", 0.0) or 0.0) / games        # mean, never the total


# ─────────────────────────────────────────────────────────────────────────────
# Rating maths
# ─────────────────────────────────────────────────────────────────────────────

def _percentile(sorted_values: List[float], v: float) -> float:
    """
    Percentile of v within sorted_values, ties shared.

    A lone player in a mode has nobody to be measured against, so they sit at
    the midpoint rather than being crowned.
    """
    n = len(sorted_values)
    if n <= 1:
        return 50.0
    below = sum(1 for x in sorted_values if x < v)
    equal = sum(1 for x in sorted_values if x == v)
    return 100.0 * (below + 0.5 * equal) / n


def _confidence(games: int) -> float:
    return min(1.0, games / float(MIN_GAMES_FULL))


def compute_ratings(players: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Turn raw per-mode aggregates into ratings for every player.

    Returns {"players": [...], "mode_players": {mode: [rows sorted]}} where
    each player row carries per-mode ratings and one overall figure.
    """
    # Representative value per mode per player
    per_mode_values: Dict[str, List[float]] = {m: [] for m in MODE_KEYS}
    prepared: List[Dict[str, Any]] = []

    for p in players:
        modes = p.get("modes") or {}
        row_modes: Dict[str, Dict[str, Any]] = {}
        for mode in MODE_KEYS:
            agg = modes.get(mode)
            if not agg:
                continue
            value = representative_value(mode, agg)
            if value is None:
                continue
            games = int(agg.get("games", 0) or 0)
            row_modes[mode] = {
                "value": value,
                "games": games,
                "best": agg.get("best"),
                "last": agg.get("last"),
                "recent": list(agg.get("recent") or []),
                "last_feedback": agg.get("last_feedback"),
            }
            per_mode_values[mode].append(value)
        prepared.append({
            "user_id": p.get("user_id", ""),
            "username": p.get("username") or "player",
            "modes": row_modes,
        })

    sorted_by_mode = {m: sorted(v) for m, v in per_mode_values.items()}

    for row in prepared:
        ratings: Dict[str, Any] = {}
        for mode, info in row["modes"].items():
            raw = _percentile(sorted_by_mode[mode], info["value"])
            conf = _confidence(info["games"])
            rating = 50.0 + (raw - 50.0) * conf      # shrink thin samples to the middle
            ratings[mode] = {
                "rating": round(rating, 1),
                "raw_percentile": round(raw, 1),
                "value": info["value"],
                "games": info["games"],
                "best": info["best"],
                "last": info["last"],
                "provisional": info["games"] < MIN_GAMES_FULL,
                "trend": _trend(info["recent"]),
                "last_feedback": info.get("last_feedback"),
                "recent": info["recent"],
            }
        row["ratings"] = ratings

        played = list(ratings.values())
        if played:
            base = sum(r["rating"] for r in played) / len(played)
            breadth = BREADTH_BONUS_MAX * (len(played) / len(MODE_KEYS))
            row["overall"] = round(min(100.0, base + breadth), 1)
        else:
            row["overall"] = 0.0
        row["modes_played"] = len(played)
        row["total_games"] = sum(r["games"] for r in played)
        row.pop("modes", None)

    ranked = sorted(prepared, key=lambda r: r["overall"], reverse=True)
    for i, row in enumerate(ranked, start=1):
        row["rank"] = i

    mode_boards: Dict[str, List[Dict[str, Any]]] = {}
    for mode in MODE_KEYS:
        rows = [
            {
                "user_id": r["user_id"],
                "username": r["username"],
                **r["ratings"][mode],
            }
            for r in ranked if mode in r["ratings"]
        ]
        rows.sort(key=lambda r: r["rating"], reverse=True)
        for i, row in enumerate(rows, start=1):
            row["rank"] = i
        mode_boards[mode] = rows

    return {"players": ranked, "mode_boards": mode_boards}


def _trend(recent: List[float]) -> str:
    """Improving / steady / slipping, from the first vs second half of recent results."""
    vals = [float(v) for v in recent if v is not None]
    if len(vals) < 4:
        return "new"
    half = len(vals) // 2
    first = sum(vals[:half]) / half
    second = sum(vals[half:]) / (len(vals) - half)
    if first == 0:
        return "steady" if second == 0 else ("improving" if second > 0 else "slipping")
    change = (second - first) / abs(first)
    if change > 0.15:
        return "improving"
    if change < -0.15:
        return "slipping"
    return "steady"


# ─────────────────────────────────────────────────────────────────────────────
# Reads
# ─────────────────────────────────────────────────────────────────────────────

_cache: Dict[str, Any] = {"at": 0.0, "data": None}


async def _load_players() -> List[Dict[str, Any]]:
    """
    Every ranked player.

    Admins are dropped: staff run and test the games, so leaving them in would
    put instructors on a board meant for students.
    """
    try:
        user_docs = await db_module.db.collection("users").get()
        admins = {d.id for d in user_docs if (d.to_dict() or {}).get("is_admin")}
    except Exception:
        log.warning("Could not load users while ranking; keeping everyone", exc_info=True)
        admins = set()

    docs = await db_module.db.collection(PLAYERS_COLLECTION).get()
    out = []
    for d in docs:
        if d.id in admins:
            continue
        data = d.to_dict() or {}
        data["user_id"] = d.id
        out.append(data)
    return out


async def leaderboard(force: bool = False) -> Dict[str, Any]:
    """Full computed board, cached briefly (every viewer would otherwise rescan)."""
    now = time.monotonic()
    if not force and _cache["data"] is not None and now - _cache["at"] < CACHE_TTL:
        return _cache["data"]

    players = await _load_players()
    data = compute_ratings(players)
    data["modes"] = [
        {"key": k, **{kk: vv for kk, vv in MODES[k].items()}} for k in MODE_KEYS
    ]
    data["min_games_full"] = MIN_GAMES_FULL
    _cache["at"] = now
    _cache["data"] = data
    return data


def invalidate_cache() -> None:
    _cache["at"] = 0.0
    _cache["data"] = None


async def scorecard(user_id: str) -> Dict[str, Any]:
    """One player's ratings, standing, and written feedback."""
    board = await leaderboard()
    me = next((r for r in board["players"] if r["user_id"] == str(user_id)), None)
    total_players = len(board["players"])

    if me is None:
        return {
            "found": False,
            "overall": 0.0,
            "rank": None,
            "total_players": total_players,
            "modes": [],
            "untried": [{"key": k, "label": MODES[k]["label"], "href": MODES[k]["href"]}
                        for k in MODE_KEYS],
            "feedback": [{
                "kind": "start",
                "text": "You haven't finished a scored game yet. Play any mode and "
                        "your rating will appear here.",
            }],
        }

    rows = []
    for mode, r in me["ratings"].items():
        meta = mode_meta(mode)
        mode_rank = next(
            (x["rank"] for x in board["mode_boards"][mode] if x["user_id"] == me["user_id"]),
            None,
        )
        rows.append({
            "key": mode,
            "label": meta["label"],
            "metric": meta["metric"],
            "unit": meta.get("unit", ""),
            "href": meta.get("href", "/"),
            "rank": mode_rank,
            "field": len(board["mode_boards"][mode]),
            **r,
        })
    rows.sort(key=lambda r: r["rating"], reverse=True)

    untried = [
        {"key": k, "label": MODES[k]["label"], "href": MODES[k]["href"], "blurb": MODES[k]["blurb"]}
        for k in MODE_KEYS if k not in me["ratings"]
    ]

    return {
        "found": True,
        "username": me["username"],
        "overall": me["overall"],
        "rank": me["rank"],
        "total_players": total_players,
        "modes_played": me["modes_played"],
        "total_modes": len(MODE_KEYS),
        "total_games": me["total_games"],
        "modes": rows,
        "untried": untried,
        "feedback": _feedback(me, rows, untried, total_players),
    }


def _feedback(me: Dict[str, Any], rows: List[Dict[str, Any]], untried: List[Dict[str, Any]],
              total_players: int) -> List[Dict[str, Any]]:
    """
    Plain-language read on a player's performance.

    Kept concrete and non-patronising: what they're good at, where the gap is,
    what to try next — each tied to an actual number.
    """
    out: List[Dict[str, Any]] = []
    scored = [r for r in rows if not r["provisional"]]

    # Standing
    if me["rank"] and total_players > 1:
        top_pct = round(100.0 * me["rank"] / total_players)
        out.append({
            "kind": "standing",
            "text": f"Overall rating {me['overall']} — rank {me['rank']} of {total_players}"
                    f" ({'top ' + str(top_pct) + '%' if top_pct <= 50 else 'bottom ' + str(100 - top_pct) + '%'})"
                    f" across {me['modes_played']} of {len(MODE_KEYS)} modes.",
        })

    # Strength
    if scored:
        best = scored[0]
        out.append({
            "kind": "strength",
            "text": f"Strongest in {best['label']}: rating {best['rating']}"
                    f" (rank {best['rank']} of {best['field']}) over"
                    f" {best['games']} game{'' if best['games'] == 1 else 's'}.",
        })

    # Weakness — only worth saying when there's a real spread
    if len(scored) >= 2:
        worst = scored[-1]
        if scored[0]["rating"] - worst["rating"] >= 12:
            out.append({
                "kind": "gap",
                "text": f"Weakest in {worst['label']}: rating {worst['rating']}."
                        f" That's {round(scored[0]['rating'] - worst['rating'])} points below your"
                        f" {scored[0]['label']} form — the fastest place to gain overall.",
            })

    # Trend
    improving = [r for r in rows if r["trend"] == "improving"]
    slipping = [r for r in rows if r["trend"] == "slipping"]
    if improving:
        out.append({
            "kind": "trend_up",
            "text": "Improving: " + ", ".join(r["label"] for r in improving[:3])
                    + " — recent results are ahead of your earlier ones.",
        })
    if slipping:
        out.append({
            "kind": "trend_down",
            "text": "Slipping: " + ", ".join(r["label"] for r in slipping[:3])
                    + " — recent results are behind your earlier ones.",
        })

    # Provisional ratings
    prov = [r for r in rows if r["provisional"]]
    if prov:
        out.append({
            "kind": "provisional",
            "text": "Provisional (held near 50 until "
                    f"{MIN_GAMES_FULL} games): " + ", ".join(f"{r['label']} ({r['games']})" for r in prov[:4])
                    + ". Finish a few more and these ratings will move to your real level.",
        })

    # Breadth
    if untried:
        out.append({
            "kind": "breadth",
            "text": f"{len(untried)} modes untried ({', '.join(u['label'] for u in untried[:3])}"
                    f"{'…' if len(untried) > 3 else ''}). Each one you play adds to your overall.",
        })

    return out
