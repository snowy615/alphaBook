"""
Mental Math Game Router
=======================
Timed mental math quiz game with configurable question types, difficulty, and timer.
"""
import random
import string
import math
from fractions import Fraction
from pathlib import Path
from typing import List, Optional
import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app import db as db_module
from app.auth import current_user
from app.models import User, MentalMathGame

router = APIRouter(prefix="/mental-math", tags=["mental-math"])
BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


# ---- Request schemas ----
class CreateGameRequest(BaseModel):
    question_types: List[str]  # ["addition", "subtraction", ...]
    difficulty: str  # "easy", "medium", "hard"
    num_questions: int  # 5-50
    time_per_question: int  # 5-60 seconds


class JoinRequest(BaseModel):
    join_code: str


class AnswerRequest(BaseModel):
    question_index: int
    answer: str  # string to support fractions like "1/2"


# ---- Question generation ----
def _rand(lo, hi):
    return random.randint(lo, hi)


def generate_question(qtype: str, difficulty: str) -> dict:
    """Generate a single question of the given type and difficulty.
    Returns {text: str, answer: str, type: str}
    """
    gen = GENERATORS.get(qtype)
    if not gen:
        return _gen_addition(difficulty)
    return gen(difficulty)


def _gen_addition(difficulty: str) -> dict:
    if difficulty == "easy":
        a, b = _rand(10, 99), _rand(10, 99)
    elif difficulty == "medium":
        a, b = _rand(100, 999), _rand(100, 999)
    else:
        a, b = _rand(1000, 9999), _rand(1000, 9999)
    return {"text": f"{a} + {b}", "answer": str(a + b), "type": "addition"}


def _gen_subtraction(difficulty: str) -> dict:
    if difficulty == "easy":
        a, b = _rand(10, 99), _rand(10, 99)
    elif difficulty == "medium":
        a, b = _rand(100, 999), _rand(100, 999)
    else:
        a, b = _rand(1000, 9999), _rand(1000, 9999)
    # Ensure a >= b for non-negative results
    if a < b:
        a, b = b, a
    return {"text": f"{a} − {b}", "answer": str(a - b), "type": "subtraction"}


def _gen_multiplication(difficulty: str) -> dict:
    if difficulty == "easy":
        a, b = _rand(2, 9), _rand(2, 9)
    elif difficulty == "medium":
        a, b = _rand(10, 99), _rand(2, 9)
    else:
        a, b = _rand(10, 99), _rand(10, 99)
    return {"text": f"{a} × {b}", "answer": str(a * b), "type": "multiplication"}


def _gen_division(difficulty: str) -> dict:
    if difficulty == "easy":
        b = _rand(2, 9)
        answer = _rand(2, 12)
    elif difficulty == "medium":
        b = _rand(2, 12)
        answer = _rand(10, 50)
    else:
        b = _rand(2, 25)
        answer = _rand(10, 100)
    a = b * answer  # clean division
    return {"text": f"{a} ÷ {b}", "answer": str(answer), "type": "division"}


def _gen_exponent(difficulty: str) -> dict:
    if difficulty == "easy":
        base = _rand(2, 9)
        exp = 2
    elif difficulty == "medium":
        choice = random.choice(["sq", "cube"])
        if choice == "sq":
            base = _rand(10, 20)
            exp = 2
        else:
            base = _rand(2, 7)
            exp = 3
    else:
        choice = random.choice(["sq", "cube"])
        if choice == "sq":
            base = _rand(15, 30)
            exp = 2
        else:
            base = _rand(5, 12)
            exp = 3
    result = base ** exp
    exp_display = "²" if exp == 2 else "³"
    return {"text": f"{base}{exp_display}", "answer": str(result), "type": "exponent"}


def _gen_comparison(difficulty: str) -> dict:
    """Which is bigger? Answer is 'A' or 'B'."""
    if difficulty == "easy":
        a1, a2 = _rand(2, 9), _rand(2, 9)
        b1, b2 = _rand(2, 9), _rand(2, 9)
        val_a = a1 * a2
        val_b = b1 * b2
        text_a = f"{a1} × {a2}"
        text_b = f"{b1} × {b2}"
    elif difficulty == "medium":
        a1, a2 = _rand(10, 30), _rand(2, 9)
        b1, b2 = _rand(10, 30), _rand(2, 9)
        val_a = a1 * a2
        val_b = b1 * b2
        text_a = f"{a1} × {a2}"
        text_b = f"{b1} × {b2}"
    else:
        a1, a2, a3 = _rand(10, 50), _rand(2, 9), _rand(2, 20)
        b1, b2, b3 = _rand(10, 50), _rand(2, 9), _rand(2, 20)
        val_a = a1 * a2 + a3
        val_b = b1 * b2 + b3
        text_a = f"{a1} × {a2} + {a3}"
        text_b = f"{b1} × {b2} + {b3}"
    # Make sure they're not equal
    if val_a == val_b:
        val_b += 1
        b_parts = text_b.split()
        # Simple fix: adjust the expression
        text_b = text_b + " + 1"
    answer = "A" if val_a > val_b else "B"
    return {
        "text": f"Which is bigger?\n\nA: {text_a}\nB: {text_b}",
        "answer": answer,
        "type": "comparison"
    }


def _gen_pattern(difficulty: str) -> dict:
    """Find the next number in a sequence."""
    if difficulty == "easy":
        # Arithmetic sequence
        start = _rand(1, 20)
        step = _rand(2, 8)
        seq = [start + step * i for i in range(5)]
        answer = seq[-1]
        display = ", ".join(str(x) for x in seq[:-1]) + ", ?"
    elif difficulty == "medium":
        pattern_type = random.choice(["arith", "mult", "squares"])
        if pattern_type == "arith":
            start = _rand(5, 50)
            step = _rand(5, 15)
            seq = [start + step * i for i in range(5)]
        elif pattern_type == "mult":
            start = _rand(2, 5)
            factor = _rand(2, 3)
            seq = [start * (factor ** i) for i in range(5)]
        else:
            start = _rand(1, 8)
            seq = [(start + i) ** 2 for i in range(5)]
        answer = seq[-1]
        display = ", ".join(str(x) for x in seq[:-1]) + ", ?"
    else:
        pattern_type = random.choice(["arith", "mult", "alternating"])
        if pattern_type == "arith":
            start = _rand(10, 100)
            step = _rand(7, 25)
            seq = [start + step * i for i in range(6)]
        elif pattern_type == "mult":
            start = _rand(2, 4)
            factor = _rand(2, 4)
            seq = [start * (factor ** i) for i in range(6)]
        else:
            # Alternating: add a, add b, add a, add b...
            start = _rand(1, 20)
            a, b = _rand(2, 10), _rand(3, 12)
            seq = [start]
            for i in range(5):
                seq.append(seq[-1] + (a if i % 2 == 0 else b))
        answer = seq[-1]
        display = ", ".join(str(x) for x in seq[:-1]) + ", ?"
    return {"text": f"What comes next?\n{display}", "answer": str(answer), "type": "pattern"}


def _gen_percentage(difficulty: str) -> dict:
    if difficulty == "easy":
        pct = random.choice([10, 20, 25, 50])
        value = _rand(2, 20) * (100 // pct)  # ensure clean result
    elif difficulty == "medium":
        pct = random.choice([5, 10, 15, 20, 25, 30, 40, 50, 75])
        value = _rand(10, 200)
        # Make result clean
        value = value - (value * pct % 100 != 0) * (value % (100 // math.gcd(pct, 100)))
        if value <= 0:
            value = 100
    else:
        pct = random.choice([5, 8, 12, 15, 20, 25, 30, 35, 40, 60, 75])
        value = _rand(50, 500)
        value = value - (value * pct % 100 != 0) * (value % (100 // math.gcd(pct, 100)))
        if value <= 0:
            value = 200

    result = value * pct // 100
    # Check if clean
    if value * pct % 100 != 0:
        # Use Fraction for non-clean results
        frac = Fraction(value * pct, 100)
        if frac.denominator == 1:
            answer = str(frac.numerator)
        else:
            answer = f"{frac.numerator}/{frac.denominator}"
    else:
        answer = str(result)
    return {"text": f"{pct}% of {value}", "answer": answer, "type": "percentage"}


GENERATORS = {
    "addition": _gen_addition,
    "subtraction": _gen_subtraction,
    "multiplication": _gen_multiplication,
    "division": _gen_division,
    "exponent": _gen_exponent,
    "comparison": _gen_comparison,
    "pattern": _gen_pattern,
    "percentage": _gen_percentage,
}

VALID_TYPES = set(GENERATORS.keys())


def generate_questions(types: List[str], difficulty: str, count: int) -> List[dict]:
    """Generate a list of questions from the given types."""
    questions = []
    for i in range(count):
        qtype = types[i % len(types)]
        q = generate_question(qtype, difficulty)
        questions.append(q)
    # Shuffle so question types are mixed
    random.shuffle(questions)
    return questions


def generate_join_code():
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=6))


def check_answer(submitted: str, correct: str) -> bool:
    """Check if the submitted answer matches the correct answer.
    Handles integers, fractions, and comparison answers.
    """
    submitted = submitted.strip()
    correct = correct.strip()

    # Direct match
    if submitted.upper() == correct.upper():
        return True

    # Try numeric comparison
    try:
        if "/" in submitted:
            sub_val = Fraction(submitted)
        else:
            sub_val = Fraction(int(submitted))

        if "/" in correct:
            cor_val = Fraction(correct)
        else:
            cor_val = Fraction(int(correct))

        return sub_val == cor_val
    except (ValueError, ZeroDivisionError):
        pass

    return False


# ---- Pages ----
@router.get("", include_in_schema=False)
async def rules_page(request: Request):
    """Mental Math rules and join page."""
    return templates.TemplateResponse("mental_math_rules.html", {
        "request": request,
        "app_name": "AlphaBook",
    })


@router.get("/game/{game_id}", include_in_schema=False)
async def game_page(game_id: str, request: Request):
    """Mental Math game page."""
    doc = await db_module.db.collection("mental_math_games").document(game_id).get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Game not found")
    return templates.TemplateResponse("mental_math_game.html", {
        "request": request,
        "app_name": "AlphaBook",
        "game_id": game_id,
    })


# ---- Admin: Create game ----
@router.post("/create")
async def create_game(req: CreateGameRequest, user: User = Depends(current_user)):
    """Admin creates a new Mental Math game."""
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin only")

    # Validate
    types = [t for t in req.question_types if t in VALID_TYPES]
    if not types:
        raise HTTPException(status_code=400, detail="At least one valid question type required")
    if req.difficulty not in ("easy", "medium", "hard"):
        raise HTTPException(status_code=400, detail="Invalid difficulty")
    num_q = max(5, min(50, req.num_questions))
    time_q = max(5, min(60, req.time_per_question))

    # Generate questions
    questions = generate_questions(types, req.difficulty, num_q)

    join_code = generate_join_code()

    game_data = {
        "join_code": join_code,
        "status": "lobby",
        "settings": {
            "question_types": types,
            "difficulty": req.difficulty,
            "num_questions": num_q,
            "time_per_question": time_q,
        },
        "questions": questions,
        "players": [],
        "results": {},
        "created_by": str(user.id),
        "created_at": dt.datetime.utcnow(),
    }

    doc_ref = db_module.db.collection("mental_math_games").document()
    await doc_ref.set(game_data)

    return {"ok": True, "game_id": doc_ref.id, "join_code": join_code}


# ---- Player: Join game ----
@router.post("/join")
async def join_game(req: JoinRequest, user: User = Depends(current_user)):
    """Player joins a Mental Math game with a code."""
    code = req.join_code.strip().upper()

    q = db_module.db.collection("mental_math_games") \
        .where("join_code", "==", code) \
        .where("status", "==", "lobby") \
        .limit(1)
    docs = await q.get()
    if not docs:
        raise HTTPException(status_code=404, detail="Game not found or already started")

    doc = docs[0]
    game_data = doc.to_dict()

    # Check if already joined
    for p in game_data.get("players", []):
        if p["user_id"] == str(user.id):
            return {"ok": True, "game_id": doc.id, "message": "Already joined"}

    # Add player
    players = game_data.get("players", [])
    players.append({
        "user_id": str(user.id),
        "username": user.username,
    })

    await doc.reference.update({"players": players})
    return {"ok": True, "game_id": doc.id}


# ---- Admin: Start game ----
@router.post("/game/{game_id}/start")
async def start_game(game_id: str, user: User = Depends(current_user)):
    """Admin starts the game."""
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin only")

    doc = await db_module.db.collection("mental_math_games").document(game_id).get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Game not found")

    game_data = doc.to_dict()
    if game_data["status"] != "lobby":
        raise HTTPException(status_code=400, detail="Game already started")

    await doc.reference.update({
        "status": "playing",
        "started_at": dt.datetime.utcnow(),
    })

    return {"ok": True, "status": "playing"}


# ---- Player: Submit answer ----
@router.post("/game/{game_id}/answer")
async def submit_answer(game_id: str, req: AnswerRequest, user: User = Depends(current_user)):
    """Player submits an answer for a specific question."""
    doc = await db_module.db.collection("mental_math_games").document(game_id).get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Game not found")

    game_data = doc.to_dict()
    if game_data["status"] != "playing":
        raise HTTPException(status_code=400, detail="Game is not in progress")

    # Verify player is in game
    player_ids = [p["user_id"] for p in game_data.get("players", [])]
    uid = str(user.id)
    if uid not in player_ids:
        raise HTTPException(status_code=403, detail="Not in this game")

    questions = game_data.get("questions", [])
    idx = req.question_index
    if idx < 0 or idx >= len(questions):
        raise HTTPException(status_code=400, detail="Invalid question index")

    correct_answer = questions[idx]["answer"]
    is_correct = check_answer(req.answer, correct_answer)

    # Update results
    results = game_data.get("results", {})
    if uid not in results:
        results[uid] = {"score": 0, "answers": []}

    # Check if already answered this question
    answered_indices = {a["index"] for a in results[uid]["answers"]}
    if idx in answered_indices:
        return {"ok": True, "already_answered": True, "correct": is_correct}

    results[uid]["answers"].append({
        "index": idx,
        "submitted": req.answer,
        "correct": is_correct,
    })
    if is_correct:
        results[uid]["score"] += 1

    await doc.reference.update({"results": results})

    return {"ok": True, "correct": is_correct}


# ---- Player: Finish (all questions done) ----
@router.post("/game/{game_id}/finish")
async def finish_player(game_id: str, user: User = Depends(current_user)):
    """Mark a player as finished."""
    doc = await db_module.db.collection("mental_math_games").document(game_id).get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Game not found")

    game_data = doc.to_dict()
    uid = str(user.id)

    results = game_data.get("results", {})
    if uid not in results:
        results[uid] = {"score": 0, "answers": []}
    results[uid]["finished"] = True

    # Check if all players finished
    players = game_data.get("players", [])
    all_finished = all(
        results.get(p["user_id"], {}).get("finished", False)
        for p in players
    )

    update_data = {"results": results}
    if all_finished:
        update_data["status"] = "finished"

    await doc.reference.update(update_data)

    return {"ok": True, "all_finished": all_finished}


# ---- Game state (polling) ----
@router.get("/game/{game_id}/state")
async def game_state(game_id: str, user: User = Depends(current_user)):
    """Get current game state for a player."""
    doc = await db_module.db.collection("mental_math_games").document(game_id).get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Game not found")

    game_data = doc.to_dict()
    uid = str(user.id)
    is_admin = user.is_admin
    status = game_data["status"]
    players = game_data.get("players", [])
    settings = game_data.get("settings", {})

    # Verify participation (unless admin)
    player_ids = [p["user_id"] for p in players]
    if uid not in player_ids and not is_admin:
        raise HTTPException(status_code=403, detail="Not in this game")

    result = {
        "game_id": game_id,
        "status": status,
        "players": players,
        "join_code": game_data.get("join_code", ""),
        "is_admin": is_admin,
        "settings": settings,
    }

    if status in ("playing", "finished"):
        # Send questions WITHOUT answers during play
        questions = game_data.get("questions", [])
        if status == "playing":
            result["questions"] = [
                {"text": q["text"], "type": q["type"]}
                for q in questions
            ]
        else:
            # Show answers after game is finished
            result["questions"] = questions

        # Player's own results
        results = game_data.get("results", {})
        if uid in results:
            result["my_results"] = results[uid]

        # In finished state, show all scores
        if status == "finished":
            scoreboard = []
            for p in players:
                pid = p["user_id"]
                r = results.get(pid, {"score": 0, "answers": []})
                scoreboard.append({
                    "user_id": pid,
                    "username": p["username"],
                    "score": r.get("score", 0),
                    "total": len(questions),
                })
            scoreboard.sort(key=lambda x: x["score"], reverse=True)
            result["scoreboard"] = scoreboard

    return result
