"""
Approx: an Estimathon for AlphaBook
===================================

A host picks estimation questions (from the bank in approx_questions, or
their own with the answer they know), opens a room and gets a join code.
Teams join with the code. When the host starts, every team sees all the
questions and the clock, and submits intervals [low, high]: an interval is
good if it contains the true answer.

Scoring, as in the Estimathon rules (and the scoring spreadsheet):

    score = (10 + sum over good intervals of floor(high / low)) * 2 ** (N - good)

where N is the number of questions. Lowest score wins. Each team has N + 5
submissions in all (18 for 13 questions); only the last one for a question
counts. A team sees its score straight away after each submission; the host
sees the leaderboard. The game ends when every team has finished (pressed
"I'm done" or used every submission) or the clock runs out.

Everything for one game lives in one Firestore document. The server runs as
a single instance, so a per-game lock serialises the read-modify-write.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import math
import re
import secrets
import string
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app import db as db_module
from app.approx_questions import BANK, BY_ID, CATEGORIES
from app.auth import current_user
from app.models import User

router = APIRouter(prefix="/approx", tags=["approx"])
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

COLLECTION = "approx_games"
LOBBY, PLAYING, FINISHED = "lobby", "playing", "finished"
MIN_QUESTIONS, MAX_QUESTIONS = 1, 30
MIN_MINUTES, MAX_MINUTES, DEFAULT_MINUTES = 1, 120, 30
EXTRA_ATTEMPTS = 5            # N questions, N + 5 submissions (13 -> 18)
TEAM_NAME_MAX = 40
QUESTION_MAX = 300
# The spreadsheet's way of showing a hopeless score: past ten million it's
# a phrase, not a number.
SCORE_WORDS = [(1e17, "∞ squared"), (1e15, "∞ + 1"), (1e13, "∞?"), (1e11, "almost ∞"),
               (1e9, "a TON"), (1e7, "a lot")]

# What a team's page reports while the game runs, for the host to see. The
# page blocks copy, cut, paste and the copy/paste shortcuts, keeps the game in
# full screen, and counts every attempt to do otherwise.
FLAG_KINDS = {
    "fullscreen_exit": "left full screen",
    "no_fullscreen": "no full screen",
    "left_page": "switched tab or window",
    "odd_keys": "irregular keys",
    "shortcut": "copy/paste shortcuts",
    "copy": "copy attempts",
    "paste": "paste attempts",
    "right_click": "right-clicks",
}
FLAG_MAX_PER_REPORT = 100

_locks: Dict[str, asyncio.Lock] = {}


def _lock(game_id: str) -> asyncio.Lock:
    return _locks.setdefault(game_id, asyncio.Lock())


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _as_utc(value: Any) -> Optional[dt.datetime]:
    if not isinstance(value, dt.datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)


# ── Numbers ──────────────────────────────────────────────────────────────────

_SCI = re.compile(r"^(?:(?P<m>\d+(?:\.\d*)?|\.\d+)\s*(?:[x×*·]\s*))?10\s*\^\s*(?P<e>[+-]?\d+)$")


def parse_quantity(raw: Any) -> float:
    """A positive number as people write it: 1234, 1,234, 1.5e6, 3x10^6,
    3·10^6, 10^7. Nothing more complicated (the rules allow scientific
    notation and no more). Raises ValueError otherwise."""
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        value = float(raw)
    else:
        text = str(raw or "").strip().lower().replace(",", "").replace("_", "").replace(" ", "")
        text = text.replace("×", "x").replace("·", "x").replace("*", "x")
        m = _SCI.match(text)
        if m:
            value = float(m.group("m") or 1) * 10 ** int(m.group("e"))
        else:
            value = float(text)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("must be a positive number")
    return value


def ratio_points(low: float, high: float) -> int:
    """floor(high / low), with a hair of slack so 3 to 9 is 3, not 2."""
    return int(math.floor(high / low * (1 + 1e-12)))


def score_of(n_questions: int, good_ratios: List[int]) -> float:
    return (10 + sum(good_ratios)) * 2 ** (n_questions - len(good_ratios))


def score_label(score: float) -> str:
    for threshold, words in SCORE_WORDS:
        if score >= threshold:
            return words
    return f"{int(score):,}" if score == int(score) else f"{score:,.1f}"


# ── A team's standing ────────────────────────────────────────────────────────

def standing(game: dict, team: dict) -> dict:
    """Score, good intervals, submissions left and each question's latest
    interval, for one team. Only the last submission per question counts."""
    questions = game.get("questions") or []
    n = len(questions)
    latest: Dict[int, dict] = {}
    for s in team.get("submissions") or []:
        latest[int(s["q"])] = s
    per_q, good_ratios = [], []
    for i, q in enumerate(questions):
        s = latest.get(i)
        good = bool(s) and s["low"] <= q["answer"] <= s["high"]
        if good:
            good_ratios.append(ratio_points(s["low"], s["high"]))
        per_q.append({"low": s["low"], "high": s["high"], "good": good,
                      "points": ratio_points(s["low"], s["high"]) if good else None} if s else None)
    used = len(team.get("submissions") or [])
    score = score_of(n, good_ratios)
    return {"score": score, "score_label": score_label(score), "good": len(good_ratios),
            "attempts_used": used, "attempts_left": max(0, int(game.get("attempts", n + EXTRA_ATTEMPTS)) - used),
            "per_question": per_q, "finished": bool(team.get("finished"))}


def leaderboard(game: dict) -> List[dict]:
    rows = []
    for uid, team in (game.get("teams") or {}).items():
        st = standing(game, team)
        last = max((_as_utc(s.get("at")) for s in team.get("submissions") or [] if _as_utc(s.get("at"))),
                   default=None)
        flags = {k: int(v) for k, v in (team.get("flags") or {}).items() if k in FLAG_KINDS and v}
        rows.append({"user_id": uid, "name": team.get("name") or "Team", **{k: st[k] for k in (
            "score", "score_label", "good", "attempts_used", "attempts_left", "finished")},
            "flags": flags, "flags_text": [f"{FLAG_KINDS[k]}: {n}" for k, n in flags.items()],
            "_last": last or _as_utc(team.get("joined_at")) or _now()})
    # Lowest score wins; then more good intervals; then whoever got there first.
    rows.sort(key=lambda r: (r["score"], -r["good"], r["_last"]))
    for i, r in enumerate(rows, start=1):
        r["rank"] = i
        r.pop("_last")
    return rows


# ── Game lifecycle ───────────────────────────────────────────────────────────

def _deadline(game: dict) -> Optional[dt.datetime]:
    started = _as_utc(game.get("started_at"))
    return started + dt.timedelta(seconds=int(game.get("duration_s") or 0)) if started else None


def _maybe_finish(game: dict) -> Optional[str]:
    """Why the game is over now (time / everyone finished), if it is."""
    if game.get("status") != PLAYING:
        return None
    deadline = _deadline(game)
    if deadline and _now() >= deadline:
        return "time"
    teams = game.get("teams") or {}
    if teams and all(t.get("finished") for t in teams.values()):
        return "all_finished"
    return None


async def _load(game_id: str) -> tuple:
    ref = db_module.db.collection(COLLECTION).document(game_id)
    doc = await ref.get()
    if not doc.exists:
        raise HTTPException(404, "Game not found")
    return ref, doc.to_dict() or {}


async def _close_if_over(ref, game: dict) -> None:
    reason = _maybe_finish(game)
    if reason:
        game.update({"status": FINISHED, "ended_at": _now(), "end_reason": reason})
        await ref.update({"status": FINISHED, "ended_at": game["ended_at"], "end_reason": reason})


def _is_host(game: dict, user: User) -> bool:
    return game.get("created_by") == str(user.id)


def _join_code() -> str:
    alphabet = "".join(c for c in string.ascii_uppercase + string.digits if c not in "O0I1")
    return "".join(secrets.choice(alphabet) for _ in range(6))


# ── Pages ────────────────────────────────────────────────────────────────────

@router.get("", include_in_schema=False)
async def approx_page(request: Request):
    """Rules, hosting a game and joining one."""
    return templates.TemplateResponse("approx.html", {"request": request, "app_name": "AlphaBook"})


@router.get("/game/{game_id}", include_in_schema=False)
async def approx_game_page(game_id: str, request: Request):
    await _load(game_id)
    return templates.TemplateResponse("approx_game.html", {"request": request, "app_name": "AlphaBook",
                                                           "game_id": game_id})


# ── API ──────────────────────────────────────────────────────────────────────

@router.get("/bank")
async def question_bank(user: User = Depends(current_user)):
    """The question bank, without answers: a host picks questions by id and
    the server fills the answer in, so the bank can't be read for answers."""
    return {"questions": [{"id": q["id"], "text": q["text"], "category": q.get("category", "")} for q in BANK],
            "categories": list(CATEGORIES)}


class QuestionIn(BaseModel):
    bank_id: Optional[str] = None
    text: Optional[str] = None
    answer: Optional[str] = None


class CreateGame(BaseModel):
    questions: List[QuestionIn]
    minutes: int = DEFAULT_MINUTES
    attempts: Optional[int] = None


@router.post("/create")
async def create_game(req: CreateGame, user: User = Depends(current_user)):
    """Open a room. Whoever creates it hosts it (and doesn't play)."""
    if not MIN_QUESTIONS <= len(req.questions) <= MAX_QUESTIONS:
        raise HTTPException(400, f"Choose between {MIN_QUESTIONS} and {MAX_QUESTIONS} questions")
    questions = []
    for i, q in enumerate(req.questions, start=1):
        if q.bank_id:
            banked = BY_ID.get(q.bank_id)
            if not banked:
                raise HTTPException(400, f"Question {i}: not in the question bank")
            questions.append({"text": banked["text"], "answer": float(banked["answer"]), "bank_id": banked["id"],
                              "note": banked.get("note") or ""})
            continue
        text = " ".join((q.text or "").split())
        if not text:
            raise HTTPException(400, f"Question {i}: write the question")
        if len(text) > QUESTION_MAX:
            raise HTTPException(400, f"Question {i}: keep it under {QUESTION_MAX} characters")
        try:
            answer = parse_quantity(q.answer)
        except ValueError:
            raise HTTPException(400, f"Question {i}: the answer must be a positive number (e.g. 1500, 1.5e3, 3x10^6)")
        questions.append({"text": text, "answer": answer})
    n = len(questions)
    minutes = max(MIN_MINUTES, min(MAX_MINUTES, int(req.minutes or DEFAULT_MINUTES)))
    attempts = int(req.attempts) if req.attempts else n + EXTRA_ATTEMPTS
    attempts = max(n, min(3 * n + 10, attempts))

    coll = db_module.db.collection(COLLECTION)
    for _ in range(10):
        code = _join_code()
        if not [d for d in await coll.where("join_code", "==", code).get()
                if (d.to_dict() or {}).get("status") != FINISHED]:
            break
    game = {
        "join_code": code, "status": LOBBY, "created_by": str(user.id),
        "host_name": user.username, "created_at": _now(),
        "questions": questions, "duration_s": minutes * 60, "attempts": attempts, "teams": {},
    }
    ref = coll.document()
    await ref.set(game)
    return {"ok": True, "game_id": ref.id, "join_code": code}


class JoinGame(BaseModel):
    join_code: str
    team_name: Optional[str] = None


@router.post("/join")
async def join_game(req: JoinGame, user: User = Depends(current_user)):
    """Join with the host's code, as a team (named, or by username)."""
    code = (req.join_code or "").strip().upper()
    docs = [d for d in await db_module.db.collection(COLLECTION).where("join_code", "==", code).get()
            if (d.to_dict() or {}).get("status") != FINISHED]
    if not docs:
        raise HTTPException(404, "No open game with that code")
    game_id = docs[0].id
    async with _lock(game_id):
        ref, game = await _load(game_id)
        await _close_if_over(ref, game)
        if game["status"] == FINISHED:
            raise HTTPException(400, "That game has finished")
        uid = str(user.id)
        if uid == game.get("created_by"):
            return {"ok": True, "game_id": game_id, "host": True}
        teams = game.get("teams") or {}
        if uid in teams:
            return {"ok": True, "game_id": game_id}
        name = " ".join((req.team_name or "").split())[:TEAM_NAME_MAX] or user.username
        if any((t.get("name") or "").lower() == name.lower() for t in teams.values()):
            raise HTTPException(409, f"There's already a team called {name}. Pick another name.")
        teams[uid] = {"name": name, "joined_at": _now(), "submissions": [], "finished": False}
        await ref.update({"teams": teams})
    return {"ok": True, "game_id": game_id}


@router.post("/game/{game_id}/start")
async def start_game(game_id: str, user: User = Depends(current_user)):
    async with _lock(game_id):
        ref, game = await _load(game_id)
        if not _is_host(game, user):
            raise HTTPException(403, "Only the host can start the game")
        if game["status"] != LOBBY:
            raise HTTPException(400, "The game has already started")
        if not game.get("teams"):
            raise HTTPException(400, "Wait for at least one team to join")
        await ref.update({"status": PLAYING, "started_at": _now()})
    return {"ok": True}


@router.post("/game/{game_id}/end")
async def end_game(game_id: str, user: User = Depends(current_user)):
    """The host can call time early."""
    async with _lock(game_id):
        ref, game = await _load(game_id)
        if not _is_host(game, user):
            raise HTTPException(403, "Only the host can end the game")
        if game["status"] == FINISHED:
            return {"ok": True}
        await ref.update({"status": FINISHED, "ended_at": _now(), "end_reason": "host"})
    return {"ok": True}


class Submission(BaseModel):
    question: int        # 0-based
    low: str
    high: str


def _team_or_403(game: dict, user: User) -> dict:
    team = (game.get("teams") or {}).get(str(user.id))
    if team is None:
        raise HTTPException(403, "You're not in this game")
    return team


@router.post("/game/{game_id}/submit")
async def submit_interval(game_id: str, req: Submission, user: User = Depends(current_user)):
    """Submit [low, high] for one question. Returns the team's new standing."""
    async with _lock(game_id):
        ref, game = await _load(game_id)
        await _close_if_over(ref, game)
        team = _team_or_403(game, user)
        if game["status"] == LOBBY:
            raise HTTPException(400, "The game hasn't started yet")
        if game["status"] == FINISHED:
            raise HTTPException(400, "Time's up: the game has finished")
        if team.get("finished"):
            raise HTTPException(400, "You've finished: no more submissions")
        n = len(game.get("questions") or [])
        if not 0 <= req.question < n:
            raise HTTPException(400, "No such question")
        try:
            low, high = parse_quantity(req.low), parse_quantity(req.high)
        except ValueError:
            raise HTTPException(400, "Both ends must be positive numbers (e.g. 1500, 1.5e3, 3x10^6)")
        if low > high:
            raise HTTPException(400, "The lower end must not be above the upper end")
        st = standing(game, team)
        if st["attempts_left"] <= 0:
            raise HTTPException(400, "You've used all your submissions")
        team.setdefault("submissions", []).append({"q": req.question, "low": low, "high": high, "at": _now()})
        st = standing(game, team)
        if st["attempts_left"] <= 0:
            team["finished"] = True          # nothing more they can do
        teams = game["teams"]
        teams[str(user.id)] = team
        await ref.update({"teams": teams})
        game["teams"] = teams
        await _close_if_over(ref, game)
    return {"ok": True, "standing": standing(game, team), "status": game["status"]}


@router.post("/game/{game_id}/finish")
async def finish_team(game_id: str, user: User = Depends(current_user)):
    """A team locks in its answers. When every team has, the game ends."""
    async with _lock(game_id):
        ref, game = await _load(game_id)
        team = _team_or_403(game, user)
        if game["status"] != PLAYING:
            raise HTTPException(400, "The game isn't running")
        team["finished"] = True
        teams = game["teams"]
        teams[str(user.id)] = team
        await ref.update({"teams": teams})
        game["teams"] = teams
        await _close_if_over(ref, game)
    return {"ok": True, "status": game["status"]}


class FlagReport(BaseModel):
    counts: Dict[str, int]


@router.post("/game/{game_id}/flags")
async def report_flags(game_id: str, req: FlagReport, user: User = Depends(current_user)):
    """A team's page reporting what it blocked or saw since its last report
    (left full screen, switched tab, irregular keys, copy/paste attempts),
    added to the team's running counts for the host. Only while playing."""
    async with _lock(game_id):
        ref, game = await _load(game_id)
        team = _team_or_403(game, user)
        if game["status"] != PLAYING:
            return {"ok": True, "ignored": True}
        flags = dict(team.get("flags") or {})
        for kind, n in (req.counts or {}).items():
            if kind in FLAG_KINDS and isinstance(n, int) and n > 0:
                flags[kind] = int(flags.get(kind, 0)) + min(n, FLAG_MAX_PER_REPORT)
        team["flags"], team["flags_at"] = flags, _now()
        teams = game["teams"]
        teams[str(user.id)] = team
        await ref.update({"teams": teams})
    return {"ok": True}


@router.get("/game/{game_id}/state")
async def game_state(game_id: str, user: User = Depends(current_user)):
    """What this person's screen needs: the host sees every team; a team sees
    its own standing. Answers only once the game has finished."""
    async with _lock(game_id):
        ref, game = await _load(game_id)
        await _close_if_over(ref, game)
    uid = str(user.id)
    is_host = _is_host(game, user)
    team = (game.get("teams") or {}).get(uid)
    if not is_host and team is None:
        raise HTTPException(403, "You're not in this game. Join it with the code first.")
    status = game["status"]
    deadline = _deadline(game)
    questions = game.get("questions") or []
    out: Dict[str, Any] = {
        "game_id": game_id, "status": status, "is_host": is_host, "join_code": game.get("join_code"),
        "host_name": game.get("host_name"), "n_questions": len(questions),
        "attempts": game.get("attempts"), "duration_s": game.get("duration_s"),
        "seconds_left": max(0, int((deadline - _now()).total_seconds())) if deadline and status == PLAYING else None,
        "end_reason": game.get("end_reason"),
        "teams_joined": [t.get("name") for t in (game.get("teams") or {}).values()],
    }
    if status != LOBBY or is_host:
        # The host isn't playing, so they see the answers throughout; teams
        # only once the game is over.
        reveal = status == FINISHED or is_host
        out["questions"] = [{"text": q["text"], **({"answer": q["answer"], "note": q.get("note") or ""} if reveal else {})}
                            for q in questions]
    if is_host or status == FINISHED:
        out["leaderboard"] = leaderboard(game)
    if team is not None:
        out["team_name"] = team.get("name")
        out["standing"] = standing(game, team)
    return out
