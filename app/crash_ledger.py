"""
Crash Ledger — "Crash Call", a duel game over real crash-period stock data.
==========================================================================

The page keeps its original job (browse how S&P 500 names behaved in past
crashes, with a live chart per ticker) and adds a game on top: each round shows
two real stocks and asks which one behaved a certain way during past crashes —
fell harder, was more volatile, held up better. You can open either stock's
chart before you call it. Guess right to build a streak; the score lands on a
leaderboard.

The metrics come from ``crash_data.json`` (parsed from the ledger cards), so the
answers are grounded in the same numbers the reference section shows. Games are
short and solo, so they live in memory on the single pinned instance; only final
scores are persisted to Firestore.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import random
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app import db as db_module
from app.auth import current_user
from app.models import User

log = logging.getLogger("uvicorn.error")

router = APIRouter(prefix="/crash-ledger", tags=["crash-ledger"])
BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

SCORES_COLLECTION = "crash_ledger_scores"
ROUNDS_PER_GAME = 10
MAX_GAMES = 500          # in-flight games kept before the oldest are pruned

# The stock dataset (ticker, name, exchange + crash metrics).
_STOCKS: List[Dict[str, Any]] = json.loads((BASE_DIR / "crash_data.json").read_text())

# Each prompt: the metric it compares, the question, and which side is the
# answer — "max" means the stock with the higher value, "min" the lower (most
# negative). ``gap`` is the minimum difference required so a round is never a
# near-tie coin-flip.
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


def _public_stock(s: Dict[str, Any]) -> Dict[str, Any]:
    """Only the fields the client needs — never the metric being guessed."""
    return {"ticker": s["ticker"], "name": s["name"], "exchange": s["exchange"]}


def _make_round(rng: random.Random) -> Dict[str, Any]:
    """Build one round: a prompt, two clearly-different stocks, and the answer."""
    for _ in range(300):
        prompt = rng.choice(PROMPTS)
        a, b = rng.sample(_STOCKS, 2)
        va, vb = a.get(prompt["key"]), b.get(prompt["key"])
        if va is None or vb is None or abs(va - vb) < prompt["gap"]:
            continue
        if prompt["pick"] == "max":
            answer = "a" if va > vb else "b"
        else:
            answer = "a" if va < vb else "b"
        return {
            "prompt": prompt["key"], "pick": prompt["pick"], "q": prompt["q"],
            "label": prompt["label"], "a": a, "b": b, "va": va, "vb": vb, "answer": answer,
        }
    # Extremely unlikely fallback: any distinct pair on the first prompt.
    prompt = PROMPTS[0]
    a, b = rng.sample(_STOCKS, 2)
    va, vb = a[prompt["key"]], b[prompt["key"]]
    answer = "a" if va < vb else "b"
    return {"prompt": prompt["key"], "pick": prompt["pick"], "q": prompt["q"],
            "label": prompt["label"], "a": a, "b": b, "va": va, "vb": vb, "answer": answer}


class Game:
    def __init__(self, gid: str, uid: str, username: str, rng: random.Random):
        self.id = gid
        self.uid = uid
        self.username = username
        self.rounds = [_make_round(rng) for _ in range(ROUNDS_PER_GAME)]
        self.idx = 0
        self.score = 0
        self.streak = 0
        self.best_streak = 0
        self.correct = 0
        self.done = False
        self.created = time.monotonic()

    def round_view(self) -> Dict[str, Any]:
        r = self.rounds[self.idx]
        return {
            "index": self.idx,
            "total": len(self.rounds),
            "question": r["q"],
            "label": r["label"],
            "a": _public_stock(r["a"]),
            "b": _public_stock(r["b"]),
        }


_games: Dict[str, Game] = {}


def _prune() -> None:
    if len(_games) <= MAX_GAMES:
        return
    for gid in sorted(_games, key=lambda g: _games[g].created)[: len(_games) - MAX_GAMES]:
        _games.pop(gid, None)


# ---- Request schemas ----
class AnswerRequest(BaseModel):
    pick: str  # "a" | "b"


# ---- Page ----
@router.get("", include_in_schema=False)
async def crash_ledger_page(request: Request):
    return templates.TemplateResponse("crash_ledger.html", {
        "request": request,
        "app_name": "AlphaBook",
        "stock_count": len(_STOCKS),
        "rounds_per_game": ROUNDS_PER_GAME,
    })


# ---- Game ----
@router.post("/game/start")
async def start_game(user: User = Depends(current_user)):
    gid = str(uuid.uuid4())
    game = Game(gid, str(user.id), user.username, random.Random())
    _games[gid] = game
    _prune()
    return {"game_id": gid, "total": len(game.rounds), "round": game.round_view()}


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

    r = game.rounds[game.idx]
    correct = req.pick == r["answer"]
    points = 0
    if correct:
        game.streak += 1
        game.best_streak = max(game.best_streak, game.streak)
        game.correct += 1
        points = 100 + 25 * (game.streak - 1)   # streak bonus rewards a hot run
        game.score += points
    else:
        game.streak = 0

    game.idx += 1
    game.done = game.idx >= len(game.rounds)

    resp: Dict[str, Any] = {
        "correct": correct,
        "answer": r["answer"],
        "a_value": r["va"],
        "b_value": r["vb"],
        "label": r["label"],
        "points": points,
        "score": game.score,
        "streak": game.streak,
        "done": game.done,
    }
    if game.done:
        resp["final"] = {
            "score": game.score,
            "correct": game.correct,
            "total": len(game.rounds),
            "best_streak": game.best_streak,
        }
        await _persist_score(game)
    else:
        resp["round"] = game.round_view()
    return resp


async def _persist_score(game: Game) -> None:
    """Keep each user's best score on the board."""
    try:
        ref = db_module.db.collection(SCORES_COLLECTION).document(game.uid)
        doc = await ref.get()
        prev = (doc.to_dict() or {}).get("score", -1) if doc.exists else -1
        if game.score > prev:
            await ref.set({
                "user_id": game.uid,
                "username": game.username,
                "score": game.score,
                "correct": game.correct,
                "total": len(game.rounds),
                "best_streak": game.best_streak,
                "updated_at": dt.datetime.utcnow(),
            })
    except Exception:
        log.warning("Failed to persist Crash Ledger score for %s", game.uid, exc_info=True)


@router.get("/leaderboard")
async def leaderboard(limit: int = 20, user: User = Depends(current_user)):
    try:
        docs = await db_module.db.collection(SCORES_COLLECTION) \
            .order_by("score", direction="DESCENDING") \
            .limit(max(1, min(limit, 50))) \
            .get()
    except Exception:
        log.warning("Crash Ledger leaderboard query failed", exc_info=True)
        return {"leaderboard": [], "me": None}

    rows = []
    me: Optional[Dict[str, Any]] = None
    uid = str(user.id)
    for rank, d in enumerate(docs, start=1):
        data = d.to_dict() or {}
        row = {
            "rank": rank,
            "user_id": d.id,
            "username": data.get("username", "player"),
            "score": data.get("score", 0),
            "correct": data.get("correct", 0),
            "total": data.get("total", ROUNDS_PER_GAME),
            "best_streak": data.get("best_streak", 0),
            "is_me": d.id == uid,
        }
        rows.append(row)
        if row["is_me"]:
            me = row
    return {"leaderboard": rows, "me": me}
