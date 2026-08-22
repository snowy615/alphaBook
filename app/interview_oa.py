"""
Interview OA — a password-gated, timed probability/EV screen.
===============================================================

One candidate, one attempt, one question on screen at a time, 20 seconds on
the clock per question. The point of the timer isn't really the clock — it's
that 20 seconds is enough to think through a probability or expected-value
question but not enough to type it into a chatbot and wait for an answer.

Design notes:

* **Login is required before the password gate.** The shared password
  ("interview" by default, overridable via ``INTERVIEW_OA_PASSWORD``) just
  unlocks the assessment for an already-authenticated AlphaBook account, so
  every attempt is tied to a real user, not an anonymous link.
* **One attempt.** A session document is keyed by user id. Once it reaches
  "finished" it stays finished — reloading the page shows a confirmation
  screen, never the questions again, so seeing the questions once (or
  discussing them with someone who already has) doesn't buy a second try.
* **The clock lives on the server.** Each question stores the timestamp it
  was served, and `/state` — polled by the client — resolves an expired
  question into a recorded timeout and advances on its own, the same
  "resolve on read" approach `toxic_flow` uses for its audit window. That
  way a candidate who closes the tab right as the timer hits zero still gets
  an honest, un-strandable result instead of a stuck session.
* **Answers are free-typed numbers** (decimals, fractions like "3/8", or a
  trailing "%"), auto-graded against a tolerance per question. Grading is a
  convenience for the reviewer, not a pass/fail gate — the admin view shows
  every raw answer next to the key.
* **Results are admin-only.** A candidate never sees their own score; they
  see a plain "submitted" screen. That keeps candidates from comparing notes
  on which questions they nailed.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import random
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app import db as db_module
from app.admin import require_admin
from app.auth import current_user
from app.models import User

log = logging.getLogger("uvicorn.error")

router = APIRouter(prefix="/interview-oa", tags=["interview-oa"])
BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

COLLECTION = "interview_oa_sessions"
PASSWORD = os.getenv("INTERVIEW_OA_PASSWORD", "interview")
TIME_PER_QUESTION = 20        # seconds
ANSWER_GRACE = 3              # seconds of network slack before a late /answer is ignored
QUESTIONS_PER_SESSION = 10

# ── Question bank ──────────────────────────────────────────────────────────
# Every question has one clean numeric answer so it can be auto-graded on a
# tolerance. "note" is the one-line justification shown only to the admin.
QUESTION_BANK: List[Dict[str, Any]] = [
    {"id": "coin3_2h", "kind": "probability",
     "prompt": "A fair coin is flipped 3 times. What's the probability of getting exactly 2 heads?",
     "answer": 3 / 8, "tol": 0.01, "note": "C(3,2)/2^3 = 3/8"},
    {"id": "die_ev", "kind": "expectation",
     "prompt": "You roll a fair six-sided die once. What's its expected value?",
     "answer": 3.5, "tol": 0.05, "note": "(1+2+3+4+5+6)/6"},
    {"id": "dice_sum7", "kind": "probability",
     "prompt": "You roll two fair six-sided dice. What's the probability their sum is 7?",
     "answer": 1 / 6, "tol": 0.01, "note": "6 of 36 outcomes"},
    {"id": "coin_first_heads", "kind": "expectation",
     "prompt": "You flip a fair coin repeatedly until it lands heads. What's the expected number of flips?",
     "answer": 2.0, "tol": 0.05, "note": "1/p, p=0.5"},
    {"id": "deck_face_card", "kind": "probability",
     "prompt": "You draw one card from a standard 52-card deck. What's the probability it's a face card (J, Q, or K)?",
     "answer": 12 / 52, "tol": 0.01, "note": "12 face cards / 52"},
    {"id": "biased_coin_atleast1", "kind": "probability",
     "prompt": "A coin lands heads with probability 0.3. You flip it 4 times. What's the probability of at least one heads?",
     "answer": 1 - 0.7 ** 4, "tol": 0.01, "note": "1 - 0.7^4"},
    {"id": "two_aces", "kind": "probability",
     "prompt": "You draw 2 cards without replacement from a standard 52-card deck. What's the probability both are aces?",
     "answer": (4 / 52) * (3 / 51), "tol": 0.002, "note": "(4/52)(3/51)"},
    {"id": "rolls_to_six", "kind": "expectation",
     "prompt": "You roll a fair six-sided die repeatedly until you see a 6. What's the expected number of rolls?",
     "answer": 6.0, "tol": 0.1, "note": "1/p, p=1/6"},
    {"id": "balls_both_red", "kind": "probability",
     "prompt": "A jar has 3 red and 2 blue balls. You draw 2 balls without replacement. What's the probability both are red?",
     "answer": 0.3, "tol": 0.01, "note": "(3/5)(2/4)"},
    {"id": "five_coins_heads", "kind": "expectation",
     "prompt": "You flip 5 fair coins. What's the expected number of heads?",
     "answer": 2.5, "tol": 0.05, "note": "n*p, n=5 p=0.5"},
    {"id": "two_dice_sum_ev", "kind": "expectation",
     "prompt": "You roll two fair six-sided dice. What's the expected value of their sum?",
     "answer": 7.0, "tol": 0.1, "note": "2 * 3.5"},
    {"id": "ten_coins_5heads", "kind": "probability",
     "prompt": "You flip a fair coin 10 times. What's the probability of exactly 5 heads?",
     "answer": 252 / 1024, "tol": 0.01, "note": "C(10,5)/2^10"},
    {"id": "freethrows_ev", "kind": "expectation",
     "prompt": "A basketball player makes 70% of free throws. They shoot 2 free throws. What's the expected number made?",
     "answer": 1.4, "tol": 0.05, "note": "n*p, n=2 p=0.7"},
    {"id": "heart_card", "kind": "probability",
     "prompt": "You draw one card from a standard 52-card deck. What's the probability it's a heart?",
     "answer": 0.25, "tol": 0.01, "note": "13/52"},
    {"id": "two_dice_even_sum", "kind": "probability",
     "prompt": "You roll two fair six-sided dice. What's the probability their sum is even?",
     "answer": 0.5, "tol": 0.01, "note": "symmetry"},
    {"id": "white_balls_ev", "kind": "expectation",
     "prompt": "A box has 4 white and 6 black balls. You draw 3 balls without replacement. What's the expected number of white balls drawn?",
     "answer": 1.2, "tol": 0.05, "note": "hypergeometric: n*K/N = 3*4/10"},
    {"id": "second_heads_ev", "kind": "expectation",
     "prompt": "You flip a fair coin repeatedly until it lands heads for the second time. What's the expected number of flips?",
     "answer": 4.0, "tol": 0.1, "note": "r/p, r=2 p=0.5"},
    {"id": "uniform_sq_ev", "kind": "expectation",
     "prompt": "X is drawn uniformly at random from [0, 1]. What's E[X^2]?",
     "answer": 1 / 3, "tol": 0.01, "note": "integral of x^2 over [0,1]"},
]
QUESTION_BY_ID = {q["id"]: q for q in QUESTION_BANK}


class UnlockRequest(BaseModel):
    password: str


class AnswerRequest(BaseModel):
    index: int
    value: str = ""


def _parse_answer(raw: str) -> Optional[float]:
    s = (raw or "").strip().replace(",", "")
    if not s:
        return None
    is_pct = s.endswith("%")
    if is_pct:
        s = s[:-1].strip()
    try:
        if "/" in s:
            num_s, _, den_s = s.partition("/")
            val = float(num_s) / float(den_s)
        else:
            val = float(s)
    except (ValueError, ZeroDivisionError):
        return None
    return val / 100 if is_pct else val


def _grade(question: Dict[str, Any], parsed: Optional[float]) -> bool:
    if parsed is None:
        return False
    return abs(parsed - question["answer"]) <= question["tol"]


# ── Session helpers ──────────────────────────────────────────────────────────

async def _load(user_id: str) -> Optional[dict]:
    doc = await db_module.db.collection(COLLECTION).document(user_id).get()
    return doc.to_dict() if doc.exists else None


async def _save(user_id: str, session: dict) -> None:
    await db_module.db.collection(COLLECTION).document(user_id).set(session)


def _deadline(session: dict) -> dt.datetime:
    started = session["question_started_at"]
    if isinstance(started, str):
        started = dt.datetime.fromisoformat(started)
    if started.tzinfo is None:
        started = started.replace(tzinfo=dt.timezone.utc)
    return started + dt.timedelta(seconds=TIME_PER_QUESTION)


def _seconds_left(session: dict) -> float:
    return max(0.0, (_deadline(session) - dt.datetime.now(dt.timezone.utc)).total_seconds())


def _record_answer(session: dict, index: int, raw: Optional[str], timed_out: bool) -> None:
    qid = session["question_ids"][index]
    question = QUESTION_BY_ID[qid]
    parsed = _parse_answer(raw) if raw is not None else None
    elapsed = TIME_PER_QUESTION - _seconds_left(session) if not timed_out else float(TIME_PER_QUESTION)
    session["answers"].append({
        "question_id": qid,
        "raw": raw or "",
        "parsed": parsed,
        "correct": _grade(question, parsed),
        "timed_out": timed_out,
        "time_taken_s": round(min(elapsed, TIME_PER_QUESTION), 1),
    })
    _advance(session)


def _advance(session: dict) -> None:
    session["current_index"] += 1
    if session["current_index"] >= len(session["question_ids"]):
        session["status"] = "finished"
        session["finished_at"] = dt.datetime.utcnow()
        correct = sum(1 for a in session["answers"] if a["correct"])
        total = len(session["answers"])
        session["score"] = {"correct": correct, "total": total,
                             "pct": round(100 * correct / total, 1) if total else 0.0}
    else:
        session["question_started_at"] = dt.datetime.now(dt.timezone.utc)


def resolve_expired(session: dict) -> bool:
    """
    If the current question's clock ran out (past a small grace buffer) with
    no answer recorded, time it out and advance.

    The grace buffer matters: without it, this would fire the instant the
    countdown hits zero, beating an in-flight `/answer` call that was fired
    at the buzzer to the server by a beat — the whole point of "auto-submit
    whatever they've typed" is that a same-tick submission still counts.
    """
    if session.get("status") != "active":
        return False
    if _seconds_left(session) > 0 or _time_since_deadline(session) < ANSWER_GRACE:
        return False
    _record_answer(session, session["current_index"], raw=None, timed_out=True)
    return True


def _time_since_deadline(session: dict) -> float:
    return max(0.0, (dt.datetime.now(dt.timezone.utc) - _deadline(session)).total_seconds())


def _question_view(session: dict) -> Optional[Dict[str, Any]]:
    idx = session["current_index"]
    if idx >= len(session["question_ids"]):
        return None
    q = QUESTION_BY_ID[session["question_ids"][idx]]
    return {
        "index": idx,
        "total": len(session["question_ids"]),
        "kind": q["kind"],
        "prompt": q["prompt"],
        "seconds_left": round(_seconds_left(session), 1),
        "time_per_question": TIME_PER_QUESTION,
    }


# ── Pages ─────────────────────────────────────────────────────────────────────

@router.get("", include_in_schema=False)
async def gate_page(request: Request):
    return templates.TemplateResponse("interview_oa.html", {
        "request": request, "app_name": "AlphaBook",
        "questions_per_session": QUESTIONS_PER_SESSION, "time_per_question": TIME_PER_QUESTION,
    })


# ── Candidate API ─────────────────────────────────────────────────────────────

@router.post("/unlock")
async def unlock(req: UnlockRequest, user: User = Depends(current_user)):
    if req.password.strip() != PASSWORD:
        raise HTTPException(403, "That access code isn't right")

    uid = str(user.id)
    session = await _load(uid)
    if session is None:
        session = {
            "user_id": uid, "username": user.username,
            "status": "ready", "unlocked_at": dt.datetime.utcnow(),
            "question_ids": [], "current_index": 0, "answers": [],
        }
        await _save(uid, session)
    return {"ok": True, "status": session["status"]}


@router.post("/start")
async def start(user: User = Depends(current_user)):
    uid = str(user.id)
    session = await _load(uid)
    if session is None:
        raise HTTPException(400, "Unlock the assessment first")
    if session["status"] != "ready":
        raise HTTPException(400, f"This assessment is already {session['status']}")

    pool = list(QUESTION_BANK)
    random.shuffle(pool)
    chosen = pool[:min(QUESTIONS_PER_SESSION, len(pool))]

    session.update({
        "status": "active",
        "question_ids": [q["id"] for q in chosen],
        "current_index": 0,
        "answers": [],
        "started_at": dt.datetime.utcnow(),
        "question_started_at": dt.datetime.now(dt.timezone.utc),
    })
    await _save(uid, session)
    return {"ok": True, "status": "active"}


@router.get("/state")
async def state(user: User = Depends(current_user)):
    uid = str(user.id)
    session = await _load(uid)
    if session is None:
        return {"status": "locked"}

    changed = resolve_expired(session)
    if changed:
        await _save(uid, session)

    out: Dict[str, Any] = {"status": session["status"]}
    if session["status"] == "active":
        out["question"] = _question_view(session)
    elif session["status"] == "finished":
        out["message"] = "Thanks — your responses have been submitted."
    return out


@router.post("/answer")
async def answer(req: AnswerRequest, user: User = Depends(current_user)):
    uid = str(user.id)
    session = await _load(uid)
    if session is None or session["status"] != "active":
        raise HTTPException(400, "No active question")

    if resolve_expired(session):
        # The clock beat this submission to the server — already recorded as a
        # timeout, so this stale POST is a no-op rather than a double-answer.
        await _save(uid, session)
        return {"ok": True, "status": session["status"]}

    if req.index != session["current_index"]:
        return {"ok": True, "status": session["status"]}   # already moved on

    _record_answer(session, session["current_index"], raw=req.value, timed_out=False)
    await _save(uid, session)
    return {"ok": True, "status": session["status"]}


# ── Admin ─────────────────────────────────────────────────────────────────────

@router.get("/admin", include_in_schema=False)
async def admin_results(request: Request, admin: User = Depends(require_admin)):
    docs = await db_module.db.collection(COLLECTION).get()
    rows = []
    for d in docs:
        s = d.to_dict() or {}
        answered = [{
            "prompt": QUESTION_BY_ID[a["question_id"]]["prompt"],
            "note": QUESTION_BY_ID[a["question_id"]]["note"],
            "answer_key": QUESTION_BY_ID[a["question_id"]]["answer"],
            **a,
        } for a in s.get("answers", [])]
        rows.append({
            "username": s.get("username", "?"),
            "status": s.get("status", "ready"),
            "score": s.get("score"),
            "answers": answered,
            "finished_at": s.get("finished_at"),
        })
    rows.sort(key=lambda r: (-((r["score"] or {}).get("pct", -1)), r["username"]))
    return templates.TemplateResponse("interview_oa_admin.html", {
        "request": request, "app_name": "AlphaBook", "rows": rows,
    })
