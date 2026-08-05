"""
Crash Ledger — "Crash Call", a crash-data duel with a gamification layer.
========================================================================

The page keeps its reference job (browse crash-period behaviour, with a live
chart per ticker) and wraps a game around it. Beyond the base duel, the design
leans on a handful of engagement mechanics:

* **Dynamic difficulty (flow).** Each game tracks a difficulty that rises when
  you're right and falls when you're wrong, and round generation serves closer
  (harder) or more obvious (easier) pairs to match — keeping you near your edge.
  Points scale with difficulty, so competence is rewarded.
* **Layered feedback.** Immediate points → streak milestones → session score →
  persistent **XP and levels**, so there's always a reward on some horizon.
* **Endowed progress.** New players are seeded with a little XP so their level
  bar starts partly full — the task feels already underway.
* **Habit.** A **daily streak** pays an escalating bonus the first game each day.
* **Cooperation.** Every correct call feeds a **shared daily desk goal**.
* **Tiered leaderboards.** Players are bucketed into Bronze→Diamond leagues by
  level and ranked within their tier.

Games are short and solo, so they live in memory on the single pinned instance;
only the persistent profile (XP, streak, best) and the daily goal go to
Firestore.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import math
import random
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.templating import Jinja2Templates
from google.cloud import firestore as gfs
from pydantic import BaseModel

from app import db as db_module
from app import scores
from app.auth import current_user
from app.models import User

log = logging.getLogger("uvicorn.error")

router = APIRouter(prefix="/crash-ledger", tags=["crash-ledger"])
BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

PROFILES_COLLECTION = "crash_ledger_profiles"
DAILY_COLLECTION = "crash_ledger_daily"

ROUNDS_PER_GAME = 10
NEW_PLAYER_XP = 30          # endowed-progress head start on the level bar
DAILY_GOAL = 200            # shared "desk" goal: correct calls across everyone

# Dynamic difficulty: 0 = obvious pairs, 1 = razor-close calls.
DIFF_START = 0.35
DIFF_UP = 0.09
DIFF_DOWN = 0.13
DIFF_MIN = 0.05
DIFF_MAX = 0.97

MAX_GAMES = 500

# Leagues, easiest → hardest, by the minimum level to sit in each.
TIERS = [
    {"key": "bronze", "label": "Bronze", "min_level": 1, "color": "#b08d57"},
    {"key": "silver", "label": "Silver", "min_level": 4, "color": "#9aa3b2"},
    {"key": "gold", "label": "Gold", "min_level": 8, "color": "#e0b341"},
    {"key": "platinum", "label": "Platinum", "min_level": 15, "color": "#7ec8ff"},
    {"key": "diamond", "label": "Diamond", "min_level": 25, "color": "#6c5ce7"},
]

_STOCKS: List[Dict[str, Any]] = json.loads((BASE_DIR / "crash_data.json").read_text())

# ─────────────────────────────────────────────────────────────────────────────
# Market-making rounds
# ─────────────────────────────────────────────────────────────────────────────
# The player quotes a two-sided market on a real crash statistic and the house
# trades against them at the true value. A binary "which one fell harder?" is
# 50/50 guessable; pricing a number forces an actual opinion and is scored on
# calibration — the thing trading desks actually test.
#
# Only statistics with a sane, roughly linear spread are quotable. total_return
# and best_period run from -95% to +66,000%, so no fair fixed-width market can
# be made on them and they stay out of the game.

MARKET_PROMPTS: List[Dict[str, Any]] = [
    {"key": "worst_drawdown",
     "q": "How far did {name} fall from its peak at the worst of it?",
     "label": "worst drawdown", "unit": "%", "lo": -100.0, "hi": 0.0, "step": 0.5},
    {"key": "worst_period",
     "q": "How bad was {name}'s single worst crash period?",
     "label": "worst single period", "unit": "%", "lo": -100.0, "hi": 0.0, "step": 0.5},
    {"key": "volatility",
     "q": "How much did {name} move on an average day?",
     "label": "avg daily volatility", "unit": "%", "lo": 0.0, "hi": 12.0, "step": 0.1},
    {"key": "avg_return",
     "q": "What did {name} return on average across its crash periods?",
     "label": "avg crash return", "unit": "%", "lo": -100.0, "hi": 200.0, "step": 1.0},
]

# Scoring. Widths are measured in units of each statistic's spread across the
# whole set, so a 10-point market on drawdown (sd ≈ 26) and a 1-point market on
# volatility (sd ≈ 2.3) are treated as equally brave.
BASE_POINTS = 200          # a perfectly tight market that holds
MISS_PENALTY = 120         # per spread-unit of being picked off
MAX_LOSS = 250             # worst single round
MAX_TRADEABLE_WIDTH = 3.0  # wider than this in spread-units and nobody trades it
STREAK_BONUS = 20          # per consecutive held quote, capped
STREAK_BONUS_CAP = 100


def _stat_spread(key: str) -> Tuple[float, float]:
    """(mean, standard deviation) of a statistic across the whole set."""
    vals = [s[key] for s in _STOCKS if s.get(key) is not None]
    mean = sum(vals) / len(vals)
    var = sum((v - mean) ** 2 for v in vals) / len(vals)
    return mean, max(var ** 0.5, 1e-6)


_STAT_SPREAD: Dict[str, Tuple[float, float]] = {
    p["key"]: _stat_spread(p["key"]) for p in MARKET_PROMPTS
}


# ─────────────────────────────────────────────────────────────────────────────
# Leveling / tiers
# ─────────────────────────────────────────────────────────────────────────────

def _xp_for_level(level: int) -> int:
    """XP required to *reach* a level. Quadratic, so each level costs more."""
    return 50 * (level - 1) ** 2


def _level_for_xp(xp: int) -> int:
    return int((max(0, xp) / 50) ** 0.5) + 1


def _level_progress(xp: int) -> Tuple[int, int, int, float]:
    lvl = _level_for_xp(xp)
    lo, hi = _xp_for_level(lvl), _xp_for_level(lvl + 1)
    into, span = xp - lo, hi - lo
    return lvl, into, span, (into / span if span else 0.0)


def _tier_for_level(level: int) -> Dict[str, Any]:
    tier = TIERS[0]
    for t in TIERS:
        if level >= t["min_level"]:
            tier = t
    return tier


def _today() -> str:
    return dt.date.today().isoformat()


def _yesterday() -> str:
    return (dt.date.today() - dt.timedelta(days=1)).isoformat()


# ─────────────────────────────────────────────────────────────────────────────
# Rounds & game
# ─────────────────────────────────────────────────────────────────────────────

def _public_stock(s: Dict[str, Any]) -> Dict[str, Any]:
    return {"ticker": s["ticker"], "name": s["name"], "exchange": s["exchange"]}


def _make_round(rng: random.Random, difficulty: float) -> Dict[str, Any]:
    """
    Build a round to quote, at the given difficulty (0 easy → 1 hard).

    Difficulty is how much of an outlier the true value is: a name sitting near
    the cohort average is easy to price off the anchor, while an extreme one
    punishes anyone who just quotes around the average.
    """
    prompt = rng.choice(MARKET_PROMPTS)
    key = prompt["key"]
    mean, sd = _STAT_SPREAD[key]

    cands = [(abs((s[key] - mean) / sd), s) for s in _STOCKS if s.get(key) is not None]
    cands.sort(key=lambda c: c[0])          # closest to the average (easiest) first
    if not cands:                           # pathological fallback
        cands = [(0.0, rng.choice(_STOCKS))]

    # Sample a small band around the difficulty percentile so repeated rounds at
    # the same difficulty don't serve the same name every time.
    target = min(len(cands) - 1, int(round(difficulty * (len(cands) - 1))))
    window = [c for c in cands[max(0, target - 2): target + 3]]
    _, stock = rng.choice(window)

    return {
        "prompt": key,
        "q": prompt["q"].format(name=stock["name"]),
        "label": prompt["label"],
        "unit": prompt["unit"],
        "lo": prompt["lo"],
        "hi": prompt["hi"],
        "step": prompt["step"],
        "stock": stock,
        "truth": float(stock[key]),
        "spread": sd,
        "cohort_avg": round(mean, 1),
    }


def _round_view(rnd: Dict[str, Any], index: int, total: int) -> Dict[str, Any]:
    """The part of a round a player is allowed to see."""
    s = rnd["stock"]
    return {
        "index": index,
        "total": total,
        "question": rnd["q"],
        "label": rnd["label"],
        "unit": rnd["unit"],
        "lo": rnd["lo"],
        "hi": rnd["hi"],
        "step": rnd["step"],
        "stock": {**_public_stock(s), "category": s.get("category", "")},
        # A public anchor: the average across all 35 names for this statistic.
        # It makes the round a judgement about *this* company rather than a
        # blind stab at an unfamiliar scale.
        "cohort_avg": rnd["cohort_avg"],
        "spread": round(rnd["spread"], 2),
        # Sent so the page can show the payout for a given width live, without
        # a second copy of the scoring rules drifting out of step.
        "scoring": {
            "base": BASE_POINTS,
            "max_width_units": MAX_TRADEABLE_WIDTH,
            "miss_penalty": MISS_PENALTY,
            "max_loss": MAX_LOSS,
        },
    }


def score_quote(rnd: Dict[str, Any], bid: float, ask: float, streak: int) -> Dict[str, Any]:
    """
    Trade the house against a quoted market and score it.

    * truth inside the market  → the quote held; points scale with tightness
    * market wider than MAX_TRADEABLE_WIDTH → nobody trades it, no points
    * truth outside            → picked off, losing the edge you gave away
    """
    truth = rnd["truth"]
    sd = rnd["spread"]
    width_units = (ask - bid) / sd

    if truth > ask:
        # The house lifts the offer: you sold at ask, it was worth more.
        miss = truth - ask
        side = "lifted"
    elif truth < bid:
        # The house hits the bid: you bought at bid, it was worth less.
        miss = bid - truth
        side = "hit"
    else:
        miss = 0.0
        side = "held"

    if side == "held":
        if width_units > MAX_TRADEABLE_WIDTH:
            points = 0
            note = "too wide to trade — nobody lifts a market that loose"
        else:
            points = int(round(BASE_POINTS / (1.0 + max(0.0, width_units))))
            if streak > 0:
                points += min(STREAK_BONUS * streak, STREAK_BONUS_CAP)
            note = "your market held"
    else:
        points = -min(MAX_LOSS, int(round(MISS_PENALTY * miss / sd)))
        note = ("the house lifted your offer" if side == "lifted"
                else "the house hit your bid")

    return {
        "points": points,
        "side": side,
        "held": side == "held",
        "tradeable": width_units <= MAX_TRADEABLE_WIDTH,
        "miss": round(miss, 2),
        "width": round(ask - bid, 2),
        "width_units": round(width_units, 2),
        "truth": round(truth, 2),
        "note": note,
    }


def _difficulty_tag(d: float) -> str:
    if d < 0.30:
        return "Warm-up"
    if d < 0.55:
        return "Steady"
    if d < 0.80:
        return "Sharp"
    return "Brutal"


class Game:
    def __init__(self, gid: str, uid: str, username: str, rng: random.Random):
        self.id = gid
        self.uid = uid
        self.username = username
        self.rng = rng
        self.total = ROUNDS_PER_GAME
        self.idx = 0
        self.score = 0
        self.streak = 0
        self.best_streak = 0
        self.correct = 0
        self.difficulty = DIFF_START
        self.done = False
        self.created = time.monotonic()
        self.current = _make_round(rng, self.difficulty)

    def round_view(self) -> Dict[str, Any]:
        return _round_view(self.current, self.idx, self.total)

    def quote(self, bid: float, ask: float) -> Dict[str, Any]:
        r = self.current
        res = score_quote(r, bid, ask, self.streak)

        if res["held"] and res["tradeable"]:
            self.streak += 1
            self.best_streak = max(self.best_streak, self.streak)
            self.correct += 1
            self.difficulty = min(DIFF_MAX, self.difficulty + DIFF_UP)
        else:
            self.streak = 0
            if not res["held"]:
                self.difficulty = max(DIFF_MIN, self.difficulty - DIFF_DOWN)

        self.score += res["points"]
        milestone = self.streak if self.streak in (3, 5, 7, 10) else 0

        self.idx += 1
        self.done = self.idx >= self.total
        out: Dict[str, Any] = {
            **res,
            "bid": bid,
            "ask": ask,
            "label": r["label"],
            "unit": r["unit"],
            "score": self.score,
            "streak": self.streak,
            "milestone": milestone,
            "difficulty": round(self.difficulty, 2),
            "difficulty_tag": _difficulty_tag(self.difficulty),
            "done": self.done,
        }
        if not self.done:
            self.current = _make_round(self.rng, self.difficulty)
            out["round"] = self.round_view()
        return out


_games: Dict[str, Game] = {}


def _prune() -> None:
    if len(_games) <= MAX_GAMES:
        return
    for gid in sorted(_games, key=lambda g: _games[g].created)[: len(_games) - MAX_GAMES]:
        _games.pop(gid, None)


# ─────────────────────────────────────────────────────────────────────────────
# Persistence: profile, streak, daily goal
# ─────────────────────────────────────────────────────────────────────────────

async def _load_profile(uid: str, username: str) -> Tuple[Any, Dict[str, Any]]:
    ref = db_module.db.collection(PROFILES_COLLECTION).document(uid)
    doc = await ref.get()
    if doc.exists:
        return ref, (doc.to_dict() or {})
    prof = {
        "username": username, "xp": NEW_PLAYER_XP, "best_score": 0,
        "games_played": 0, "total_correct": 0, "day_streak": 0,
        "last_played": "", "updated_at": dt.datetime.utcnow(),
    }
    await ref.set(prof)
    return ref, prof


def _profile_view(prof: Dict[str, Any]) -> Dict[str, Any]:
    xp = int(prof.get("xp", 0))
    lvl, into, span, pct = _level_progress(xp)
    tier = _tier_for_level(lvl)
    return {
        "xp": xp, "level": lvl, "level_into": into, "level_span": span,
        "level_pct": round(pct, 4), "tier": tier,
        "best_score": int(prof.get("best_score", 0)),
        "games_played": int(prof.get("games_played", 0)),
        "day_streak": int(prof.get("day_streak", 0)),
    }


async def _daily_goal_state() -> Dict[str, Any]:
    try:
        doc = await db_module.db.collection(DAILY_COLLECTION).document(_today()).get()
        cur = int((doc.to_dict() or {}).get("correct_total", 0)) if doc.exists else 0
    except Exception:
        cur = 0
    return {"current": cur, "goal": DAILY_GOAL, "reached": cur >= DAILY_GOAL}


async def _finalize(game: Game) -> Dict[str, Any]:
    """Award XP, advance the daily streak, feed the desk goal, return progression."""
    final: Dict[str, Any] = {
        "score": game.score, "correct": game.correct, "total": game.total,
        "best_streak": game.best_streak,
    }
    try:
        ref, prof = await _load_profile(game.uid, game.username)
        today = _today()

        daily_bonus = 0
        if prof.get("last_played") != today:
            if prof.get("last_played") == _yesterday():
                prof["day_streak"] = int(prof.get("day_streak", 0)) + 1
            else:
                prof["day_streak"] = 1
            daily_bonus = min(prof["day_streak"], 7) * 10
            prof["last_played"] = today

        prev_level = _level_for_xp(int(prof.get("xp", 0)))
        # A losing session still earns something for showing up, but never
        # negative XP — the score itself is where a bad game shows up.
        xp_earned = max(0, game.score) // 10 + 5 * game.correct + daily_bonus
        prof["xp"] = int(prof.get("xp", 0)) + xp_earned
        new_best = game.score > int(prof.get("best_score", 0))
        prof["best_score"] = max(int(prof.get("best_score", 0)), game.score)
        prof["games_played"] = int(prof.get("games_played", 0)) + 1
        prof["total_correct"] = int(prof.get("total_correct", 0)) + game.correct
        prof["username"] = game.username
        prof["updated_at"] = dt.datetime.utcnow()
        await ref.set(prof)

        # shared daily desk goal
        await db_module.db.collection(DAILY_COLLECTION).document(today).set(
            {"goal": DAILY_GOAL, "correct_total": gfs.Increment(game.correct)}, merge=True)

        view = _profile_view(prof)
        final.update({
            "xp_earned": xp_earned,
            "daily_bonus": daily_bonus,
            "day_streak": prof["day_streak"],
            "new_best": new_best,
            "leveled_up": view["level"] > prev_level,
            "level": view["level"],
            "level_pct": view["level_pct"],
            "level_into": view["level_into"],
            "level_span": view["level_span"],
            "tier": view["tier"],
            "daily_goal": await _daily_goal_state(),
        })
    except Exception:
        log.warning("Crash Ledger finalize failed for %s", game.uid, exc_info=True)
        final["xp_earned"] = 0

    await scores.record_result(
        "crash_ledger", game.uid, game.username, game.score,
        game_id=game.id,
        detail={"correct": game.correct, "total": game.total,
                "best_streak": game.best_streak},
    )
    return final


# ---- Request schemas ----
class QuoteRequest(BaseModel):
    bid: float
    ask: float


def _validate_quote(rnd: Dict[str, Any], req: QuoteRequest) -> Tuple[float, float]:
    """A market must be two-sided, the right way round, and on the scale shown."""
    bid, ask = float(req.bid), float(req.ask)
    if not (math.isfinite(bid) and math.isfinite(ask)):
        raise HTTPException(status_code=400, detail="Both sides of your market must be numbers")
    if ask < bid:
        raise HTTPException(status_code=400, detail="Your ask has to be at or above your bid")
    lo, hi = rnd["lo"], rnd["hi"]
    if bid < lo or ask > hi:
        raise HTTPException(
            status_code=400,
            detail=f"Keep your market inside the {lo:g} to {hi:g}{rnd['unit']} scale",
        )
    return bid, ask


# ─────────────────────────────────────────────────────────────────────────────
# Head-to-head rooms
# ─────────────────────────────────────────────────────────────────────────────
# The solo loop adapts difficulty per player, which is right for practice but
# useless for a contest: two people would answer different questions. A room
# therefore fixes one set of rounds on a rising difficulty ramp and serves it
# to everybody, so scores are directly comparable. Rooms live in memory on the
# single pinned instance, like the solo games.

ROOM_DIFF_START = 0.25
ROOM_DIFF_END = 0.85
MAX_ROOMS = 200
ROOM_TTL_SEC = 3 * 60 * 60


class Room:
    def __init__(self, rid: str, code: str, host_id: str, host_name: str):
        self.id = rid
        self.code = code
        self.host_id = host_id
        self.host_name = host_name
        self.status = "lobby"           # lobby → active → finished
        self.created = time.monotonic()
        self.scored = False
        self.players: Dict[str, Dict[str, Any]] = {}

        rng = random.Random()
        self.rounds: List[Dict[str, Any]] = []
        span = max(1, ROUNDS_PER_GAME - 1)
        for i in range(ROUNDS_PER_GAME):
            diff = ROOM_DIFF_START + (ROOM_DIFF_END - ROOM_DIFF_START) * (i / span)
            rnd = _make_round(rng, diff)
            rnd["difficulty"] = diff
            self.rounds.append(rnd)

    # -- players --
    def join(self, uid: str, username: str) -> Dict[str, Any]:
        if uid not in self.players:
            self.players[uid] = {
                "username": username, "idx": 0, "score": 0, "correct": 0,
                "streak": 0, "best_streak": 0, "done": False,
            }
        else:
            self.players[uid]["username"] = username
        return self.players[uid]

    @property
    def all_done(self) -> bool:
        return bool(self.players) and all(p["done"] for p in self.players.values())

    # -- views --
    def round_view(self, uid: str) -> Optional[Dict[str, Any]]:
        p = self.players.get(uid)
        if not p or p["done"] or self.status != "active":
            return None
        return _round_view(self.rounds[p["idx"]], p["idx"], len(self.rounds))

    def standings(self) -> List[Dict[str, Any]]:
        rows = [{
            "user_id": uid,
            "username": p["username"],
            "score": p["score"],
            "correct": p["correct"],
            "progress": p["idx"],
            "total": len(self.rounds),
            "done": p["done"],
            "streak": p["streak"],
            "is_host": uid == self.host_id,
        } for uid, p in self.players.items()]
        rows.sort(key=lambda r: (-r["score"], -r["correct"], r["username"].lower()))
        for i, r in enumerate(rows, start=1):
            r["rank"] = i
        return rows

    # -- play --
    def quote(self, uid: str, bid: float, ask: float) -> Dict[str, Any]:
        p = self.players[uid]
        r = self.rounds[p["idx"]]
        res = score_quote(r, bid, ask, p["streak"])

        if res["held"] and res["tradeable"]:
            p["streak"] += 1
            p["best_streak"] = max(p["best_streak"], p["streak"])
            p["correct"] += 1
        else:
            p["streak"] = 0
        p["score"] += res["points"]

        p["idx"] += 1
        p["done"] = p["idx"] >= len(self.rounds)

        out: Dict[str, Any] = {
            **res,
            "bid": bid, "ask": ask,
            "label": r["label"], "unit": r["unit"],
            "score": p["score"], "streak": p["streak"],
            "milestone": p["streak"] if p["streak"] in (3, 5, 7, 10) else 0,
            "done": p["done"],
            "difficulty_tag": _difficulty_tag(r["difficulty"]),
        }
        if not p["done"]:
            out["round"] = self.round_view(uid)
        if self.all_done:
            self.status = "finished"
        return out


_rooms: Dict[str, Room] = {}


def _prune_rooms() -> None:
    now = time.monotonic()
    stale = [rid for rid, r in _rooms.items() if now - r.created > ROOM_TTL_SEC]
    for rid in stale:
        _rooms.pop(rid, None)
    if len(_rooms) > MAX_ROOMS:
        for rid in sorted(_rooms, key=lambda r: _rooms[r].created)[: len(_rooms) - MAX_ROOMS]:
            _rooms.pop(rid, None)


def _new_room_code() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # no look-alike characters
    while True:
        code = "".join(random.choice(alphabet) for _ in range(5))
        if not any(r.code == code for r in _rooms.values()):
            return code


def _find_room_by_code(code: str) -> Optional[Room]:
    code = (code or "").strip().upper()
    for r in _rooms.values():
        if r.code == code:
            return r
    return None


class _RoomResult:
    """Adapter letting a room result reuse the solo progression/scoring path."""

    def __init__(self, room: Room, uid: str, p: Dict[str, Any]):
        self.id = room.id
        self.uid = uid
        self.username = p["username"]
        self.score = p["score"]
        self.correct = p["correct"]
        self.total = len(room.rounds)
        self.best_streak = p["best_streak"]


async def _finalize_room(room: Room) -> None:
    """Award XP and file ratings for every player, once."""
    if room.scored or not room.all_done:
        return
    room.scored = True
    for uid, p in room.players.items():
        try:
            await _finalize(_RoomResult(room, uid, p))
        except Exception:
            log.warning("Crash Ledger room finalize failed for %s", uid, exc_info=True)


def _room_state(room: Room, uid: str) -> Dict[str, Any]:
    return {
        "room_id": room.id,
        "code": room.code,
        "my_id": uid,
        "status": room.status,
        "host_id": room.host_id,
        "is_host": uid == room.host_id,
        "joined": uid in room.players,
        "total_rounds": len(room.rounds),
        "standings": room.standings(),
        "round": room.round_view(uid),
        "me": room.players.get(uid),
    }


class RoomJoinRequest(BaseModel):
    code: str


@router.post("/room/create")
async def room_create(user: User = Depends(current_user)):
    """Open a room and take the host seat."""
    _prune_rooms()
    rid = str(uuid.uuid4())
    room = Room(rid, _new_room_code(), str(user.id), user.username)
    room.join(str(user.id), user.username)
    _rooms[rid] = room
    return _room_state(room, str(user.id))


@router.post("/room/join")
async def room_join(req: RoomJoinRequest, user: User = Depends(current_user)):
    room = _find_room_by_code(req.code)
    if room is None:
        raise HTTPException(status_code=404, detail="No room with that code")
    uid = str(user.id)
    if room.status != "lobby" and uid not in room.players:
        raise HTTPException(status_code=400, detail="That room has already started")
    room.join(uid, user.username)
    return _room_state(room, uid)


@router.post("/room/{room_id}/start")
async def room_start(room_id: str, user: User = Depends(current_user)):
    room = _rooms.get(room_id)
    if room is None:
        raise HTTPException(status_code=404, detail="Room not found (it may have expired)")
    if str(user.id) != room.host_id and not user.is_admin:
        raise HTTPException(status_code=403, detail="Only the host can start the round")
    if room.status == "lobby":
        room.status = "active"
    return _room_state(room, str(user.id))


@router.get("/room/{room_id}/state")
async def room_state(room_id: str, user: User = Depends(current_user)):
    room = _rooms.get(room_id)
    if room is None:
        raise HTTPException(status_code=404, detail="Room not found (it may have expired)")
    if room.status == "finished":
        await _finalize_room(room)
    return _room_state(room, str(user.id))


@router.post("/room/{room_id}/answer")
async def room_answer(room_id: str, req: QuoteRequest, user: User = Depends(current_user)):
    room = _rooms.get(room_id)
    if room is None:
        raise HTTPException(status_code=404, detail="Room not found (it may have expired)")
    uid = str(user.id)
    if uid not in room.players:
        raise HTTPException(status_code=403, detail="You're not in this room")
    if room.status != "active":
        raise HTTPException(status_code=400, detail="This room isn't running")
    if room.players[uid]["done"]:
        raise HTTPException(status_code=400, detail="You've finished all your rounds")

    bid, ask = _validate_quote(room.rounds[room.players[uid]["idx"]], req)
    out = room.quote(uid, bid, ask)
    out["standings"] = room.standings()
    if room.status == "finished":
        await _finalize_room(room)
    return out


# ---- Page ----
def _load_universe() -> Dict[str, Any]:
    """The full constituent list, grouped by the crash simulator's cohorts.

    The curated cards above cover the notable names; this is the other 450-odd,
    so the "493 tickers tracked" headline is something you can actually browse.
    """
    path = BASE_DIR / "data" / "sp500_cohorts.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {"cohorts": [], "stocks": []}
    return data


@router.get("", include_in_schema=False)
async def crash_ledger_page(request: Request):
    universe = _load_universe()
    return templates.TemplateResponse("crash_ledger.html", {
        "request": request,
        "app_name": "AlphaBook",
        "stock_count": len(_STOCKS),
        "rounds_per_game": ROUNDS_PER_GAME,
        "daily_goal": DAILY_GOAL,
        "universe_json": json.dumps(universe),
        "universe_count": len(universe.get("stocks", [])),
    })


# ---- Profile (start-screen state) ----
@router.get("/profile")
async def profile(user: User = Depends(current_user)):
    ref, prof = await _load_profile(str(user.id), user.username)
    view = _profile_view(prof)
    view["daily_available"] = prof.get("last_played") != _today()
    view["daily_goal"] = await _daily_goal_state()
    view["tiers"] = TIERS
    return view


# ---- Game ----
@router.post("/game/start")
async def start_game(user: User = Depends(current_user)):
    gid = str(uuid.uuid4())
    game = Game(gid, str(user.id), user.username, random.Random())
    _games[gid] = game
    _prune()
    return {
        "game_id": gid, "total": game.total, "round": game.round_view(),
        "difficulty": round(game.difficulty, 2), "difficulty_tag": _difficulty_tag(game.difficulty),
    }


@router.post("/game/{game_id}/answer")
async def answer(game_id: str, req: QuoteRequest, user: User = Depends(current_user)):
    game = _games.get(game_id)
    if game is None:
        raise HTTPException(status_code=404, detail="Game not found (it may have expired) — start a new one")
    if game.uid != str(user.id):
        raise HTTPException(status_code=403, detail="Not your game")
    if game.done:
        raise HTTPException(status_code=400, detail="This game is already finished")

    bid, ask = _validate_quote(game.current, req)
    out = game.quote(bid, ask)
    if out["done"]:
        out["final"] = await _finalize(game)
    return out


# ---- Tiered leaderboard ----
@router.get("/leaderboard")
async def leaderboard(user: User = Depends(current_user)):
    uid = str(user.id)
    try:
        docs = await db_module.db.collection(PROFILES_COLLECTION) \
            .order_by("xp", direction="DESCENDING").limit(300).get()
    except Exception:
        log.warning("Crash Ledger leaderboard query failed", exc_info=True)
        return {"rows": [], "me": None, "tier": TIERS[0], "tiers": TIERS}

    rows = []
    for d in docs:
        data = d.to_dict() or {}
        lvl = _level_for_xp(int(data.get("xp", 0)))
        rows.append({
            "user_id": d.id,
            "username": data.get("username", "player"),
            "xp": int(data.get("xp", 0)),
            "level": lvl,
            "best_score": int(data.get("best_score", 0)),
            "day_streak": int(data.get("day_streak", 0)),
            "tier": _tier_for_level(lvl),
        })

    my = next((r for r in rows if r["user_id"] == uid), None)
    my_tier = my["tier"] if my else _tier_for_level(_level_for_xp(NEW_PLAYER_XP))
    tier_rows = [r for r in rows if r["tier"]["key"] == my_tier["key"]]  # already xp-desc
    for rank, r in enumerate(tier_rows, start=1):
        r["rank"] = rank
        r["is_me"] = r["user_id"] == uid
    return {
        "tier": my_tier,
        "tiers": TIERS,
        "rows": tier_rows[:25],
        "me": next((r for r in tier_rows if r["is_me"]), None),
    }
