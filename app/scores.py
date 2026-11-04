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

Calibration. A raw result means little on its own: $500 against noob bots
in a calm market isn't $500 against cracked bots, and 90% at easy mental
maths isn't 90% at hard. So each finished game is rated against who and what
it was played against (:func:`rate_game`), Elo-style: every result is a set of
head-to-heads against everyone else in that game, and a win over a strong
opponent is worth more than one over a weak one. House bots take part at a
fixed strength set by their skill level (BOT_SKILL_RATING). A game played
alone is measured against "par", the typical result for the same mode and
settings. A mode's rating ranks players on this skill, not on their raw
average.

Two more deliberate choices come from how this is used (ranking students,
not running a casino):

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

# ── Calibrated skill (see the module docstring) ─────────────────────────────
SKILL_START = 1000.0
# House bots' fixed strength, by skill level: beating cracked bots is worth more.
BOT_SKILL_RATING: Dict[str, float] = {"noob": 850.0, "normal": 1000.0, "good": 1150.0, "cracked": 1300.0}
PAR_RATING = 1000.0           # "par", the typical result, plays at an average strength
PAR_MIN_RESULTS = 5           # fewer results than this for the settings and there's no par yet
PAR_KEEP = 200                # results kept per mode and settings to find par
K_NEW, K_SETTLED = 48.0, 24.0  # rating movement: bigger while a player is new to a mode
K_SETTLE_GAMES = 10
CONTEXTS_COLLECTION = "score_contexts"
RATED_GAMES_COLLECTION = "score_games"


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
    "toxic_flow": {
        "label": "Toxic Flow",
        "metric": "Final capital",
        "unit": "$",
        "href": "/toxic-flow",
        "kind": "per_game",
        "blurb": "Bluffing where every claim is collateralised.",
    },
    "dark_pool": {
        "label": "Dark Pool",
        "metric": "Final capital",
        "unit": "$",
        "href": "/dark-pool",
        "kind": "per_game",
        "blurb": "Hidden-info betting where every real decision is graded against the true odds.",
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

    # A result counts toward a competition when the player is enrolled in one
    # that is running, and that competition scores this mode. Resolved here so
    # every mode stays unaware of competitions.
    competition_id = ""
    try:
        from app import competitions

        comp = await competitions.active_for_user(str(user_id))
        if comp and (not comp.get("modes") or mode in comp["modes"]):
            competition_id = comp["id"]
    except Exception:
        log.warning("competition tagging failed (mode=%s user=%s)", mode, user_id, exc_info=True)

    try:
        now = dt.datetime.utcnow()
        await db_module.db.collection(EVENTS_COLLECTION).document(str(uuid.uuid4())).set({
            "mode": mode,
            "user_id": str(user_id),
            "username": username or "",
            "value": value,
            "game_id": game_id or "",
            "competition_id": competition_id,
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
        strategy = (detail or {}).get("strategy") or {}
        if strategy:
            # How they trade, across games: the style each game read as, and
            # what their edge and their direction made on average.
            key = (strategy.get("style") or {}).get("key")
            if key:
                styles = dict(agg.get("styles") or {})
                styles[key] = int(styles.get(key, 0)) + 1
                agg["styles"] = styles
            agg["strategy_games"] = int(agg.get("strategy_games", 0)) + 1
            for field, src in (("edge_sum", "edge"), ("direction_sum", "direction")):
                if strategy.get(src) is not None:
                    agg[field] = float(agg.get(field, 0.0)) + float(strategy[src])
            if strategy.get("direction_hit") is not None:
                agg["hit_sum"] = float(agg.get("hit_sum", 0.0)) + float(strategy["direction_hit"])
                agg["hit_games"] = int(agg.get("hit_games", 0)) + 1

        modes[mode] = agg
        await ref.set({
            "username": username or data.get("username") or "",
            "modes": modes,
            "updated_at": now,
        }, merge=True)

        # Every finished game pays XP, whatever the result — the platform
        # level rewards showing up and playing, not only winning.
        from app import rewards

        await rewards.award_xp(str(user_id))
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


# ─────────────────────────────────────────────────────────────────────────────
# Calibrated skill: rating a game against who and what it was played against
# ─────────────────────────────────────────────────────────────────────────────

def _expected(r_a: float, r_b: float) -> float:
    return 1.0 / (1.0 + 10.0 ** ((r_b - r_a) / 400.0))


def _k(games: int) -> float:
    return K_NEW if games < K_SETTLE_GAMES else K_SETTLED


def _median(values: List[float]) -> float:
    v = sorted(values)
    n = len(v)
    return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2.0


def elo_deltas(field: List[Dict[str, Any]]) -> Dict[str, float]:
    """One game's rating changes, every result taken as a head-to-head with
    each other entrant (higher value wins, equal is a draw). ``field`` rows:
    {"id", "value", "rating", "games", "fixed"}; fixed entrants (house bots,
    par) don't move. Each player's change is scaled by how many opponents they
    had, so a big game isn't worth more than a small one."""
    out: Dict[str, float] = {}
    for me in field:
        if me.get("fixed"):
            continue
        others = [o for o in field if o is not me]
        if not others:
            continue
        total = 0.0
        for o in others:
            score = 1.0 if me["value"] > o["value"] else 0.5 if me["value"] == o["value"] else 0.0
            total += score - _expected(me["rating"], o["rating"])
        out[me["id"]] = _k(me["games"]) * total / len(others)
    return out


def context_of(mode: str, detail: Optional[Dict[str, Any]]) -> str:
    """The settings that change what a result means, for finding par: the
    difficulty in Mental Math, the scenario in Headline."""
    detail = detail or {}
    if mode == "mental_math":
        return str(detail.get("difficulty") or "")
    if mode == "headline":
        return str(detail.get("template") or "")
    return ""


async def rate_game(mode: str, game_id: str, rows: List[Dict[str, Any]], context: str = "") -> Dict[str, float]:
    """Rate one finished game. ``rows``: every entrant, {"user_id",
    "username", "value"}, with "bot_skill" for a house bot. Call once, after
    the game's record_result calls. A game with a single entrant is played
    against par for its settings (``context``). Once only per game; never
    raises. Returns the rating changes."""
    if mode not in MODES or mode_meta(mode).get("kind") != "per_game" or not game_id:
        return {}
    try:
        flag = db_module.db.collection(RATED_GAMES_COLLECTION).document(f"{mode}__{game_id}")
        if (await flag.get()).exists:
            return {}
        humans = [r for r in rows if r.get("user_id") and "bot_skill" not in r]
        if not humans:
            return {}
        refs = {r["user_id"]: db_module.db.collection(PLAYERS_COLLECTION).document(str(r["user_id"])) for r in humans}
        current: Dict[str, Dict[str, Any]] = {}
        for uid, ref in refs.items():
            doc = await ref.get()
            agg = (((doc.to_dict() or {}).get("modes") or {}).get(mode) or {}) if doc.exists else {}
            current[uid] = {"rating": float(agg.get("skill", SKILL_START)), "games": int(agg.get("skill_games", 0))}
        field = [{"id": r["user_id"], "value": float(r["value"]), **current[r["user_id"]]} for r in humans]
        field += [{"id": f"bot:{i}", "value": float(r["value"]), "fixed": True, "games": 0,
                   "rating": BOT_SKILL_RATING.get(str(r.get("bot_skill")), SKILL_START)}
                  for i, r in enumerate(rows) if "bot_skill" in r]

        ctx_ref = db_module.db.collection(CONTEXTS_COLLECTION).document(f"{mode}__{context or 'any'}")
        ctx_doc = await ctx_ref.get()
        past = [float(v) for v in ((ctx_doc.to_dict() or {}).get("values") or [])] if ctx_doc.exists else []
        if len(field) == 1 and len(past) >= PAR_MIN_RESULTS:
            field.append({"id": "par", "value": _median(past), "rating": PAR_RATING, "fixed": True, "games": 0})

        deltas = elo_deltas(field)
        now = dt.datetime.utcnow()
        for uid, delta in deltas.items():
            await refs[uid].set({"modes": {mode: {
                "skill": round(current[uid]["rating"] + delta, 2),
                "skill_games": current[uid]["games"] + 1, "skill_updated_at": now,
            }}}, merge=True)
        await ctx_ref.set({"values": (past + [float(r["value"]) for r in humans])[-PAR_KEEP:], "updated_at": now})
        await flag.set({"mode": mode, "game_id": game_id, "rated_at": now, "deltas": deltas})
        invalidate_cache()
        return deltas
    except Exception:
        log.warning("rate_game failed (mode=%s game=%s)", mode, game_id, exc_info=True)
        return {}


def _bot_skill_from_name(name: str) -> str:
    """Older runs saved bots by name only: "Momentum · good", or the two
    defaults ("Market Maker Bot", "Flow Bot"), which are normal."""
    tail = (name or "").rsplit("·", 1)[-1].strip().lower()
    return tail if tail in BOT_SKILL_RATING else "normal"


def replay_skills(events: List[Dict[str, Any]], bots_by_game: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Any]:
    """Rate every past game in order, as :func:`rate_game` would have.
    ``events``: score_events rows; ``bots_by_game``: coding-game run id ->
    its house bots' rows. Returns {"skills": {(uid, mode): (skill, games)},
    "contexts": {doc id: values}, "games": [doc ids rated]}. Pure: no reads
    or writes, so it can be checked before anything is saved."""
    games: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for e in events:
        mode = e.get("mode")
        if mode not in MODES or mode_meta(mode).get("kind") != "per_game" or not e.get("user_id"):
            continue
        key = (mode, e.get("game_id") or f"solo:{e.get('user_id')}:{e.get('created_at')}")
        games.setdefault(key, []).append(e)
    order = sorted(games, key=lambda k: min(str(x.get("created_at") or "") for x in games[k]))
    skills: Dict[Tuple[str, str], Tuple[float, int]] = {}
    contexts: Dict[str, List[float]] = {}
    rated: List[str] = []
    for mode, game_id in order:
        rows = games[(mode, game_id)]
        seen = {}
        for e in rows:                                  # one result per player per game
            seen[e["user_id"]] = e
        rows = list(seen.values())
        field = [{"id": e["user_id"], "value": float(e.get("value") or 0.0),
                  "rating": skills.get((e["user_id"], mode), (SKILL_START, 0))[0],
                  "games": skills.get((e["user_id"], mode), (SKILL_START, 0))[1]} for e in rows]
        if mode == "market_sim_py":
            field += [{"id": f"bot:{i}", "value": float(b.get("pnl") or 0.0), "fixed": True, "games": 0,
                       "rating": BOT_SKILL_RATING.get(b.get("bot_skill") or _bot_skill_from_name(b.get("username", "")),
                                                      SKILL_START)}
                      for i, b in enumerate(bots_by_game.get(game_id, []))]
        ctx = f"{mode}__{context_of(mode, rows[0].get('detail')) or 'any'}"
        past = contexts.get(ctx, [])
        if len(field) == 1 and len(past) >= PAR_MIN_RESULTS:
            field.append({"id": "par", "value": _median(past), "rating": PAR_RATING, "fixed": True, "games": 0})
        for uid, delta in elo_deltas(field).items():
            r, g = skills.get((uid, mode), (SKILL_START, 0))
            skills[(uid, mode)] = (r + delta, g + 1)
        contexts[ctx] = (past + [float(e.get("value") or 0.0) for e in rows])[-PAR_KEEP:]
        if not str(game_id).startswith("solo:"):
            rated.append(f"{mode}__{game_id}")
    return {"skills": skills, "contexts": contexts, "games": rated}


async def rebuild_skills(write: bool = False) -> Dict[str, Any]:
    """Rate every game played so far, oldest first, so ratings start from
    everyone's history rather than from scratch. Reads the score history
    once; writes only with ``write``. Run once when calibration goes live."""
    events = [d.to_dict() or {} for d in await db_module.db.collection(EVENTS_COLLECTION).get()]
    bots_by_game: Dict[str, List[Dict[str, Any]]] = {}
    for d in await db_module.db.collection("algo_sim_runs").get():
        bots_by_game[d.id] = list((d.to_dict() or {}).get("bots") or [])
    out = replay_skills(events, bots_by_game)
    if write:
        now = dt.datetime.utcnow()
        for (uid, mode), (skill, games) in out["skills"].items():
            await db_module.db.collection(PLAYERS_COLLECTION).document(str(uid)).set(
                {"modes": {mode: {"skill": round(skill, 2), "skill_games": games, "skill_updated_at": now}}}, merge=True)
        for ctx, values in out["contexts"].items():
            await db_module.db.collection(CONTEXTS_COLLECTION).document(ctx).set({"values": values, "updated_at": now})
        for doc_id in out["games"]:
            await db_module.db.collection(RATED_GAMES_COLLECTION).document(doc_id).set(
                {"rated_at": now, "rebuilt": True})
        invalidate_cache()
    return {"events": len(events), "games": len(out["games"]), "players": len({u for u, _ in out["skills"]}),
            "skills": out["skills"]}


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
            skill = float(agg.get("skill", SKILL_START)) if mode_meta(mode).get("kind") == "per_game" else None
            row_modes[mode] = {
                "value": value,
                "skill": skill,
                "skill_games": int(agg.get("skill_games", 0) or 0),
                "strategy": _strategy_summary(agg),
                "games": games,
                "best": agg.get("best"),
                "last": agg.get("last"),
                "recent": list(agg.get("recent") or []),
                "last_feedback": agg.get("last_feedback"),
            }
            per_mode_values[mode].append(skill if skill is not None else value)
        prepared.append({
            "user_id": p.get("user_id", ""),
            "username": p.get("username") or "player",
            "modes": row_modes,
        })

    sorted_by_mode = {m: sorted(v) for m, v in per_mode_values.items()}

    for row in prepared:
        ratings: Dict[str, Any] = {}
        for mode, info in row["modes"].items():
            ranked_on = info["skill"] if info["skill"] is not None else info["value"]
            raw = _percentile(sorted_by_mode[mode], ranked_on)
            conf = _confidence(info["games"])
            rating = 50.0 + (raw - 50.0) * conf      # shrink thin samples to the middle
            ratings[mode] = {
                "rating": round(rating, 1),
                "raw_percentile": round(raw, 1),
                "value": info["value"],
                "skill": round(info["skill"]) if info["skill"] is not None else None,
                "skill_games": info["skill_games"],
                "strategy": info["strategy"],
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


def _strategy_summary(agg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """How someone trades in a mode, over their games: their usual style and
    what their edge and their direction made per game on average."""
    n = int(agg.get("strategy_games", 0) or 0)
    if not n:
        return None
    styles = agg.get("styles") or {}
    usual, count = max(styles.items(), key=lambda kv: kv[1]) if styles else (None, 0)
    hit_games = int(agg.get("hit_games", 0) or 0)
    return {"style": usual, "style_games": int(count), "games": n,
            "edge_per_game": round(float(agg.get("edge_sum", 0.0)) / n, 2),
            "direction_per_game": round(float(agg.get("direction_sum", 0.0)) / n, 2),
            "direction_hit": round(float(agg.get("hit_sum", 0.0)) / hit_games, 3) if hit_games else None}


def public_strategy_summary(st: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Someone's strategy as others see it on the board: the usual style and
    coarse words, not the per-game figures (those are theirs alone)."""
    if not st:
        return None
    out: Dict[str, Any] = {k: st[k] for k in ("style", "style_games", "games") if k in st}
    e, d = st.get("edge_per_game"), st.get("direction_per_game")
    if e or d:
        out["made_on"] = "pricing" if abs(e or 0) >= abs(d or 0) else "direction"
    hit = st.get("direction_hit")
    if hit is not None:
        out["call"] = "mostly right" if hit >= 0.6 else "mostly wrong" if hit < 0.4 else "mixed"
    return out


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
        # Just the admins (a handful), not every account.
        from google.cloud.firestore_v1.base_query import FieldFilter
        query = db_module.db.collection("users").where(filter=FieldFilter("is_admin", "==", True))
        admins = {d.id for d in await query.select(["is_admin"]).get()}
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
