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
import random
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Tuple

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.templating import Jinja2Templates
from google.cloud import firestore as gfs
from pydantic import BaseModel

from app import db as db_module
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
SAMPLE_K = 14               # candidate pairs considered per round

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

PROMPTS: List[Dict[str, Any]] = [
    {"key": "worst_drawdown", "pick": "min", "gap": 6.0,
     "q": "Which one fell harder at its worst?", "label": "worst drawdown"},
    {"key": "volatility", "pick": "max", "gap": 1.2,
     "q": "Which one was more volatile day to day?", "label": "avg daily volatility"},
    {"key": "avg_return", "pick": "max", "gap": 7.0,
     "q": "Which one held up better on average?", "label": "avg crash return"},
    {"key": "avg_return", "pick": "min", "gap": 7.0,
     "q": "Which one crashed harder on average?", "label": "avg crash return"},
    {"key": "total_return", "pick": "max", "gap": 12.0,
     "q": "Which one ended further ahead across all crashes?", "label": "total crash-period return"},
    {"key": "worst_period", "pick": "min", "gap": 7.0,
     "q": "Which one had the uglier single worst period?", "label": "worst single period"},
]


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
    """Build a round whose pair-closeness matches ``difficulty`` (0 easy → 1 hard).

    Candidate pairs are gathered (all clearly-different enough to have a real
    answer), sorted easiest-first by gap, and the one at the difficulty
    percentile is served — so a strong run gets tighter calls.
    """
    prompt = rng.choice(PROMPTS)
    cands: List[Tuple[float, Dict, Dict, float, float]] = []
    for _ in range(80):
        a, b = rng.sample(_STOCKS, 2)
        va, vb = a.get(prompt["key"]), b.get(prompt["key"])
        if va is None or vb is None:
            continue
        gap = abs(va - vb)
        if gap < prompt["gap"]:
            continue
        cands.append((gap, a, b, va, vb))
        if len(cands) >= SAMPLE_K:
            break

    if not cands:  # pathological fallback
        a, b = rng.sample(_STOCKS, 2)
        va, vb = a[prompt["key"]], b[prompt["key"]]
        cands = [(abs(va - vb), a, b, va, vb)]

    cands.sort(key=lambda c: c[0], reverse=True)   # biggest gap (easiest) first
    idx = min(len(cands) - 1, int(round(difficulty * (len(cands) - 1))))
    gap, a, b, va, vb = cands[idx]
    if prompt["pick"] == "max":
        answer = "a" if va > vb else "b"
    else:
        answer = "a" if va < vb else "b"
    return {
        "prompt": prompt["key"], "pick": prompt["pick"], "q": prompt["q"],
        "label": prompt["label"], "a": a, "b": b, "va": va, "vb": vb, "answer": answer,
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
        r = self.current
        return {
            "index": self.idx,
            "total": self.total,
            "question": r["q"],
            "label": r["label"],
            "a": _public_stock(r["a"]),
            "b": _public_stock(r["b"]),
        }

    def base_points(self) -> int:
        return int(70 + 80 * self.difficulty)

    def answer(self, pick: str) -> Dict[str, Any]:
        r = self.current
        correct = pick == r["answer"]
        points = 0
        milestone = 0
        if correct:
            self.streak += 1
            self.best_streak = max(self.best_streak, self.streak)
            self.correct += 1
            points = self.base_points() + 25 * (self.streak - 1)
            self.score += points
            self.difficulty = min(DIFF_MAX, self.difficulty + DIFF_UP)
            if self.streak in (3, 5, 7, 10):
                milestone = self.streak
        else:
            self.streak = 0
            self.difficulty = max(DIFF_MIN, self.difficulty - DIFF_DOWN)

        self.idx += 1
        self.done = self.idx >= self.total
        out: Dict[str, Any] = {
            "correct": correct,
            "answer": r["answer"],
            "a_value": r["va"],
            "b_value": r["vb"],
            "label": r["label"],
            "points": points,
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
        xp_earned = game.score // 10 + 5 * game.correct + daily_bonus
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
    return final


# ---- Request schemas ----
class AnswerRequest(BaseModel):
    pick: str


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
async def answer(game_id: str, req: AnswerRequest, user: User = Depends(current_user)):
    game = _games.get(game_id)
    if game is None:
        raise HTTPException(status_code=404, detail="Game not found (it may have expired) — start a new one")
    if game.uid != str(user.id):
        raise HTTPException(status_code=403, detail="Not your game")
    if game.done:
        raise HTTPException(status_code=400, detail="This game is already finished")
    if req.pick not in ("a", "b"):
        raise HTTPException(status_code=400, detail="pick must be 'a' or 'b'")

    out = game.answer(req.pick)
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
