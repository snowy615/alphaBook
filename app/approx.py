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

The modified mode (``mode: "modified"``) mixes two kinds of question. Its
normal ("interval") questions play as above, with the score over just those
questions shown live. On the rest each team gives one number, final once
submitted; the closest team gets as many points as there are teams, the next
one fewer, and so on (no answer, no points), and on equal distance whoever
answered first ranks higher. Their answers can come from the game itself
(KINDS): a number the system drew, or one worked out from what the teams
submitted, so they're only final once the game is. At the end teams are
ranked on each part, best getting as many points as there are teams, and the
two are added up: highest total wins.

Everything for one game lives in one Firestore document. The server runs as
a single instance, so a per-game lock serialises the read-modify-write.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import math
import re
import secrets
import statistics
import string
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app import db as db_module
from app.approx_questions import BANK, BY_ID, CATEGORIES, MODIFIED_BANK, MODIFIED_BY_ID, MODIFIED_CATEGORY
from app.auth import current_user
from app.models import User

router = APIRouter(prefix="/approx", tags=["approx"])
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

COLLECTION = "approx_games"
LOBBY, PLAYING, FINISHED = "lobby", "playing", "finished"
NORMAL, MODIFIED = "normal", "modified"
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

# Where a modified-mode question's answer comes from. "interval" is a normal
# Estimathon question (an interval around an answer known up front); "fixed"
# is the host's, for the closest number (set when hosting, or later on the
# game page); "random" is drawn by the system when the game is made; the rest
# are worked out from the teams' answers. The two score kinds use points on
# the other closest-number questions, so no answer depends on itself.
INTERVAL = "interval"
KINDS = {
    INTERVAL: "Normal: an interval around the answer",
    "fixed": "Closest to an answer you set",
    "random": "Random whole number",
    "mean": "Average of the answers to it",
    "median": "Median of the answers to it",
    "sum": "Sum of the answers to it",
    "max_identical": "Most teams giving the same answer to it",
    "distinct": "Number of different answers to it",
    "digit_count": "Times a digit appears in every answer",
    "lowest_score": "Lowest team total",
    "highest_score": "Highest team total",
}
SCORE_KINDS = ("lowest_score", "highest_score")
RANDOM_SPAN_MAX = 10 ** 9


def kind_hint(q: dict) -> str:
    """How a modified question is answered, in a line for the teams."""
    kind, factor = q.get("kind"), q.get("factor") or 1
    if kind == INTERVAL:
        return "Give an interval: it's good if the answer is inside it, as in the normal game."
    if kind == "random":
        return f"The system drew a whole number from {_written_out(q['min'])} to {_written_out(q['max'])}."
    if kind == "mean":
        return ("The answer is the average of every team's answer to this question." if factor == 1 else
                f"The answer is {factor_label(factor)} of the average of every team's answer to this question.")
    return {
        "median": "The answer is the median of every team's answer to this question.",
        "sum": "The answer is the sum of every team's answer to this question.",
        "max_identical": "The answer is the largest number of teams giving exactly the same answer to this question.",
        "distinct": "The answer is how many different numbers the teams give for this question.",
        "digit_count": (f"Counts the digit {q.get('digit')} in every team's answers to every question, each written "
                        "out in full (1e3 is 1000); an interval counts both ends."),
        "lowest_score": "The lowest total any team scores on the other closest-number questions.",
        "highest_score": "The highest total any team scores on the other closest-number questions.",
    }.get(kind, "")


def hint_line(q: dict) -> str:
    """What the teams see under a modified question: how it's answered and
    what range it takes. The bank's own questions already say both."""
    if q.get("bank_id") in MODIFIED_BY_ID or q.get("bank_id") in BY_ID:
        return ""
    lo, hi = q.get("min"), q.get("max")
    span = (f"Answer from {_written_out(lo)} to {_written_out(hi)}." if lo is not None and hi is not None
            else f"Answer at least {_written_out(lo)}." if lo is not None
            else f"Answer at most {_written_out(hi)}." if hi is not None else "")
    return " ".join(x for x in (kind_hint(q), span) if x)


def factor_label(factor: float) -> str:
    for num in range(1, 10):
        for den in range(2, 10):
            if abs(factor - num / den) < 1e-9:
                return f"{num}/{den}"
    return _written_out(factor)

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


def parse_number(raw: Any) -> float:
    """A number as people write it: 1234, 1,234, -5, 0, 1.5e6, 3x10^6,
    3·10^6, 10^7. Nothing more complicated (the rules allow scientific
    notation and no more). Raises ValueError otherwise."""
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        value = float(raw)
    else:
        text = str(raw or "").strip().lower().replace(",", "").replace("_", "").replace(" ", "")
        text = text.replace("×", "x").replace("·", "x").replace("*", "x")
        sign = -1.0 if text[:1] == "-" else 1.0
        text = text[1:] if text[:1] in "+-" else text
        if text[:1] in "+-":
            raise ValueError("not a number")
        m = _SCI.match(text)
        if m:
            value = sign * float(m.group("m") or 1) * 10 ** int(m.group("e"))
        else:
            value = sign * float(text)
    if not math.isfinite(value):
        raise ValueError("not a number")
    return value


def parse_quantity(raw: Any) -> float:
    """A positive number (an Estimathon interval end), written as parse_number reads it."""
    value = parse_number(raw)
    if value <= 0:
        raise ValueError("must be a positive number")
    return value


def parse_factor(raw: Any) -> float:
    """A multiplier for an average: 2/3, 0.5, 1.5. Not zero."""
    text = str(raw if raw is not None else "").strip().replace(" ", "")
    if not text:
        return 1.0
    if "/" in text:
        num, _, den = text.partition("/")
        value = parse_number(num) / parse_number(den)
    else:
        value = parse_number(text)
    if not math.isfinite(value) or value == 0:
        raise ValueError("must be a non-zero number")
    return value


def _written_out(value: float) -> str:
    """A number in full, no exponent: 1e3 -> "1000", 0.25 -> "0.25"."""
    if value == int(value):
        return str(int(value))
    return format(Decimal(repr(value)), "f")


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


# ── Modified mode: answers from the game, points by closeness ────────────────

def _mode(game: dict) -> str:
    return game.get("mode") or NORMAL


def _is_interval(q: dict) -> bool:
    return q.get("kind") == INTERVAL


def _latest_by_team(game: dict) -> Dict[str, Dict[int, dict]]:
    """Each team's latest submission per question (the only one, on a
    closest-number question)."""
    out: Dict[str, Dict[int, dict]] = {}
    for uid, team in (game.get("teams") or {}).items():
        out[uid] = {int(s["q"]): s for s in team.get("submissions") or []}
    return out


def _rank_points(game: dict, i: int, answer: float, latest: Dict[str, Dict[int, dict]]) -> Dict[str, tuple]:
    """(rank, points) on question i for each team that answered it: closest
    first, equal distance going to whoever answered first. As many points
    for first as there are teams in the game, one fewer for each place."""
    n_teams = len(game.get("teams") or {})
    entries = []
    for uid, subs in latest.items():
        s = subs.get(i)
        if s is not None:
            # Rounded so float noise in a computed answer can't split a real tie.
            entries.append((round(abs(s["value"] - answer), 9), _as_utc(s.get("at")) or _now(), uid))
    entries.sort()
    return {uid: (rank, max(0, n_teams - rank + 1)) for rank, (_, _, uid) in enumerate(entries, start=1)}


def _interval_part(game: dict, team: dict) -> dict:
    """A team's Estimathon on a modified game's interval questions: the
    normal score over those questions alone, and what's left to submit."""
    questions = game.get("questions") or []
    idx = [i for i, q in enumerate(questions) if _is_interval(q)]
    subs = [s for s in team.get("submissions") or [] if int(s["q"]) in idx]
    latest = {int(s["q"]): s for s in subs}
    per, good_ratios = {}, []
    for i in idx:
        s = latest.get(i)
        if s is None:
            continue
        good = s["low"] <= questions[i]["answer"] <= s["high"]
        points = ratio_points(s["low"], s["high"]) if good else None
        if good:
            good_ratios.append(points)
        per[i] = {"low": s["low"], "high": s["high"], "good": good, "points": points}
    score = score_of(len(idx), good_ratios)
    return {"score": score, "score_label": score_label(score), "good": len(good_ratios), "used": len(subs),
            "attempts_left": max(0, int(game.get("attempts") or 0) - len(subs)), "per": per, "n": len(idx)}


def _part_points(values: Dict[str, float], lower_is_better: bool) -> Dict[str, int]:
    """Teams ranked on one part, best getting as many points as there are
    teams and one fewer for each team ahead; level teams share a place."""
    n = len(values)
    sign = 1 if lower_is_better else -1
    return {uid: n - sum(1 for w in values.values() if sign * w < sign * v) for uid, v in values.items()}


def _team_done(game: dict, team: dict) -> bool:
    """Nothing left to submit: every closest-number question answered and
    every interval submission used."""
    questions = game.get("questions") or []
    answered = {int(s["q"]) for s in team.get("submissions") or []}
    crowd_left = any(i not in answered for i, q in enumerate(questions) if not _is_interval(q))
    has_interval = any(_is_interval(q) for q in questions)
    return not crowd_left and (not has_interval or _interval_part(game, team)["attempts_left"] <= 0)


def modified_results(game: dict) -> dict:
    """Every question's answer as things stand (None if there isn't one yet:
    a fixed answer not set, or nobody has answered to average), each team's
    (rank, points) per closest-number question, each team's interval score,
    and, with both kinds of question, the combined total."""
    questions = game.get("questions") or []
    teams = game.get("teams") or {}
    latest = _latest_by_team(game)
    values: Dict[int, List[float]] = {i: [] for i in range(len(questions))}
    every_answer: List[str] = []
    for subs in latest.values():
        for i, s in subs.items():
            if i not in values:
                continue
            if _is_interval(questions[i]):
                every_answer += [_written_out(s["low"]), _written_out(s["high"])]
            else:
                values[i].append(s["value"])
                every_answer.append(s.get("shown") or _written_out(s["value"]))
    answers: List[Optional[float]] = []
    for i, q in enumerate(questions):
        kind, vals = q.get("kind") or "fixed", values[i]
        if kind in ("fixed", "random", INTERVAL):
            a = q.get("answer")
        elif kind in SCORE_KINDS:
            a = None                      # below, once the other questions are scored
        elif kind == "digit_count":
            a = float(sum(text.count(q.get("digit") or "1") for text in every_answer))
        elif not vals:
            a = None
        elif kind == "mean":
            a = (q.get("factor") or 1) * statistics.fmean(vals)
        elif kind == "median":
            a = float(statistics.median(vals))
        elif kind == "sum":
            a = float(sum(vals))
        elif kind == "max_identical":
            a = float(max(Counter(vals).values()))
        else:                             # distinct
            a = float(len(set(vals)))
        answers.append(a)
    points: Dict[str, Dict[int, tuple]] = {uid: {} for uid in latest}

    def score(i: int) -> None:
        if answers[i] is not None:
            for uid, rp in _rank_points(game, i, answers[i], latest).items():
                points[uid][i] = rp

    for i, q in enumerate(questions):
        if not _is_interval(q) and q.get("kind") not in SCORE_KINDS:
            score(i)
    totals = [sum(p for _, p in points[uid].values()) for uid in latest]
    for i, q in enumerate(questions):
        if q.get("kind") in SCORE_KINDS and totals:
            answers[i] = float(min(totals) if q["kind"] == "lowest_score" else max(totals))
            score(i)

    has_interval = any(_is_interval(q) for q in questions)
    has_crowd = any(not _is_interval(q) for q in questions)
    interval = {uid: _interval_part(game, team) for uid, team in teams.items()} if has_interval else {}
    crowd = {uid: sum(p for _, p in points.get(uid, {}).values()) for uid in teams}
    parts = None
    if has_interval and has_crowd:
        by_interval = _part_points({uid: v["score"] for uid, v in interval.items()}, lower_is_better=True)
        by_crowd = _part_points(crowd, lower_is_better=False)
        parts = {uid: {"interval": by_interval[uid], "crowd": by_crowd[uid],
                       "total": by_interval[uid] + by_crowd[uid]} for uid in teams}
    return {"answers": answers, "points": points, "latest": latest, "interval": interval, "crowd": crowd,
            "parts": parts, "has_interval": has_interval, "has_crowd": has_crowd}


def modified_standing(game: dict, team_uid: str, results: dict, reveal: bool) -> dict:
    """One team's answers: its intervals and their score as it goes, as in
    the normal game; its closest-number answers, with places and points
    (and the combined total) only once revealed."""
    questions = game.get("questions") or []
    mine = results["latest"].get(team_uid) or {}
    pts = results["points"].get(team_uid) or {}
    team = (game.get("teams") or {}).get(team_uid) or {}
    part = results["interval"].get(team_uid)
    per_q = []
    for i, q in enumerate(questions):
        if _is_interval(q):
            per_q.append(dict(part["per"][i], interval=True) if part and i in part["per"] else None)
            continue
        s = mine.get(i)
        row = {"value": s["value"], "shown": s.get("shown") or _written_out(s["value"])} if s else None
        if row is not None and reveal:
            rank, p = pts.get(i, (None, 0))
            row.update({"rank": rank, "points": p})
        per_q.append(row)
    out = {"answered": sum(1 for i in mine if not _is_interval(questions[i])),
           "n_crowd": sum(1 for q in questions if not _is_interval(q)),
           "per_question": per_q, "finished": bool(team.get("finished")),
           "n_teams": len(game.get("teams") or {})}
    if part is not None:
        out["interval"] = {k: part[k] for k in ("score", "score_label", "good", "attempts_left", "n")}
    if reveal:
        out["points"] = results["crowd"].get(team_uid, 0)
        if results["parts"]:
            out["parts"] = results["parts"][team_uid]
    return out


def modified_leaderboard(game: dict, results: dict) -> List[dict]:
    rows = []
    for uid, team in (game.get("teams") or {}).items():
        pts = results["points"].get(uid) or {}
        subs = team.get("submissions") or []
        part = results["interval"].get(uid)
        last = max((_as_utc(s.get("at")) for s in subs if _as_utc(s.get("at"))), default=None)
        flags = {k: int(v) for k, v in (team.get("flags") or {}).items() if k in FLAG_KINDS and v}
        row = {"user_id": uid, "name": team.get("name") or "Team",
               "points": results["crowd"].get(uid, 0),
               "firsts": sum(1 for r, _ in pts.values() if r == 1),
               "answered": len({int(s["q"]) for s in subs}), "finished": bool(team.get("finished")),
               "flags": flags, "flags_text": [f"{FLAG_KINDS[k]}: {n}" for k, n in flags.items()],
               "_last": last or _as_utc(team.get("joined_at")) or _now()}
        if part is not None:
            row.update({"score": part["score"], "score_label": part["score_label"], "good": part["good"]})
        if results["parts"]:
            row.update(results["parts"][uid])
        rows.append(row)
    if results["parts"]:
        # The two parts' places added up; then closest-number points; then the interval score.
        rows.sort(key=lambda r: (-r["total"], -r["points"], r["score"], r["_last"]))
    elif results["has_interval"]:
        rows.sort(key=lambda r: (r["score"], -r["good"], r["_last"]))
    else:
        # Most points wins; then more questions won outright; then whoever finished answering first.
        rows.sort(key=lambda r: (-r["points"], -r["firsts"], r["_last"]))
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
    the server fills the answer in, so the bank can't be read for answers.
    The modified mode's own questions have no secret answer to keep (the
    random ones are drawn when the game is made), so they come whole."""
    return {"questions": [{"id": q["id"], "text": q["text"], "category": q.get("category", "")} for q in BANK],
            "categories": list(CATEGORIES),
            "modified": [{"id": q["id"], "text": q["text"], "category": MODIFIED_CATEGORY, "kind": q["kind"],
                          "kind_label": KINDS[q["kind"]], "hint": kind_hint(q)} for q in MODIFIED_BANK],
            "modified_category": MODIFIED_CATEGORY,
            "kinds": [{"kind": k, "label": v} for k, v in KINDS.items()]}


class QuestionIn(BaseModel):
    bank_id: Optional[str] = None
    text: Optional[str] = None
    answer: Optional[str] = None
    # The modified mode's own questions (see KINDS).
    kind: Optional[str] = None
    min: Optional[str] = None
    max: Optional[str] = None
    factor: Optional[str] = None
    digit: Optional[str] = None


class CreateGame(BaseModel):
    questions: List[QuestionIn]
    minutes: int = DEFAULT_MINUTES
    attempts: Optional[int] = None
    mode: str = NORMAL


def _optional_number(raw: Optional[str]) -> Optional[float]:
    return parse_number(raw) if raw is not None and str(raw).strip() else None


def _modified_question(i: int, q: QuestionIn) -> dict:
    """One modified-mode question from the host's form: bank (by id, either
    bank) or their own, with where its answer comes from."""
    def bad(words: str) -> HTTPException:
        return HTTPException(400, f"Question {i}: {words}")

    if q.bank_id and q.bank_id in BY_ID:            # an ordinary bank question, played as normal
        b = BY_ID[q.bank_id]
        return {"text": b["text"], "kind": INTERVAL, "answer": float(b["answer"]), "bank_id": b["id"],
                "note": b.get("note") or ""}
    if q.bank_id:
        b = MODIFIED_BY_ID.get(q.bank_id)
        if not b:
            raise bad("not in the question bank")
        out = {"text": b["text"], "kind": b["kind"], "bank_id": b["id"], "note": b.get("note") or "",
               **{k: b[k] for k in ("min", "max", "factor", "digit") if k in b}}
    else:
        text = " ".join((q.text or "").split())
        if not text:
            raise bad("write the question")
        if len(text) > QUESTION_MAX:
            raise bad(f"keep it under {QUESTION_MAX} characters")
        kind = q.kind or "fixed"
        if kind not in KINDS:
            raise bad("choose where its answer comes from")
        out = {"text": text, "kind": kind}
        if kind == INTERVAL:
            try:
                out["answer"] = parse_quantity(q.answer)
            except ValueError:
                raise bad("a normal question needs its answer, a positive number (e.g. 1500, 1.5e3, 3x10^6)")
            return out
        try:
            lo, hi = _optional_number(q.min), _optional_number(q.max)
        except ValueError:
            raise bad("the allowed range must be numbers")
        if lo is not None:
            out["min"] = lo
        if hi is not None:
            out["max"] = hi
        if kind == "mean":
            try:
                out["factor"] = parse_factor(q.factor)
            except ValueError:
                raise bad("the factor must be a number like 2/3 or 0.5")
        if kind == "digit_count":
            digit = (q.digit or "").strip()
            if len(digit) != 1 or not digit.isdigit():
                raise bad("choose one digit, 0 to 9")
            out["digit"] = digit
    if out.get("min") is not None and out.get("max") is not None and out["min"] > out["max"]:
        raise bad("the lowest allowed answer is above the highest")
    if out["kind"] == "random":
        lo, hi = out.get("min"), out.get("max")
        if lo is None or hi is None or lo != int(lo) or hi != int(hi) or hi <= lo or hi - lo > RANDOM_SPAN_MAX:
            raise bad("a random number needs a whole-number range, lowest below highest")
        out["answer"] = float(int(lo) + secrets.randbelow(int(hi) - int(lo) + 1))
    if out["kind"] == "fixed":
        try:
            out["answer"] = _optional_number(q.answer)      # None: the host sets it during or after the game
        except ValueError:
            raise bad("the answer must be a number (e.g. 1500, 1.5e3, 3x10^6)")
    return out


@router.post("/create")
async def create_game(req: CreateGame, user: User = Depends(current_user)):
    """Open a room. Whoever creates it hosts it (and doesn't play)."""
    if not MIN_QUESTIONS <= len(req.questions) <= MAX_QUESTIONS:
        raise HTTPException(400, f"Choose between {MIN_QUESTIONS} and {MAX_QUESTIONS} questions")
    if req.mode not in (NORMAL, MODIFIED):
        raise HTTPException(400, "Choose the normal or the modified mode")
    questions = []
    for i, q in enumerate(req.questions, start=1):
        if req.mode == MODIFIED:
            questions.append(_modified_question(i, q))
            continue
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
    if req.mode == MODIFIED:
        # One answer per closest-number question; the interval questions get
        # the normal game's allowance among themselves.
        k = sum(1 for q in questions if _is_interval(q))
        attempts = k + EXTRA_ATTEMPTS if k else 0

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
        "mode": req.mode,
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
    low: Optional[str] = None
    high: Optional[str] = None
    value: Optional[str] = None   # the modified mode: one number


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
        n = len(game.get("questions") or [])
        if not 0 <= req.question < n:
            raise HTTPException(400, "No such question")
        if _mode(game) == MODIFIED:
            if _is_interval(game["questions"][req.question]):
                return await _submit_modified_interval(ref, game, team, user, req)
            return await _submit_answer(ref, game, team, user, req)
        if team.get("finished"):
            raise HTTPException(400, "You've finished: no more submissions")
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


async def _submit_answer(ref, game: dict, team: dict, user: User, req: Submission) -> dict:
    """The modified mode: one number per question, final once it's in."""
    q = game["questions"][req.question]
    if any(int(s["q"]) == req.question for s in team.get("submissions") or []):
        raise HTTPException(400, f"You've already answered question {req.question + 1}")
    if team.get("finished"):
        raise HTTPException(400, "You've finished: no more submissions")
    try:
        value = parse_number(req.value)
    except ValueError:
        raise HTTPException(400, "Enter a number (e.g. 42, 1500, 1.5e3, 3x10^6)")
    lo, hi = q.get("min"), q.get("max")
    if (lo is not None and value < lo) or (hi is not None and value > hi):
        span = (f"from {_written_out(lo)} to {_written_out(hi)}" if lo is not None and hi is not None
                else f"of at least {_written_out(lo)}" if lo is not None else f"of at most {_written_out(hi)}")
        raise HTTPException(400, f"Question {req.question + 1} takes an answer {span}")
    team.setdefault("submissions", []).append(
        {"q": req.question, "value": value, "shown": _written_out(value), "at": _now()})
    return await _save_modified(ref, game, team, user)


async def _submit_modified_interval(ref, game: dict, team: dict, user: User, req: Submission) -> dict:
    """An interval for one of a modified game's normal questions, as in the
    normal game: the last one for a question counts."""
    if team.get("finished"):
        raise HTTPException(400, "You've finished: no more submissions")
    try:
        low, high = parse_quantity(req.low), parse_quantity(req.high)
    except ValueError:
        raise HTTPException(400, "Both ends must be positive numbers (e.g. 1500, 1.5e3, 3x10^6)")
    if low > high:
        raise HTTPException(400, "The lower end must not be above the upper end")
    if _interval_part(game, team)["attempts_left"] <= 0:
        raise HTTPException(400, "You've used all your interval submissions")
    team.setdefault("submissions", []).append({"q": req.question, "low": low, "high": high, "at": _now()})
    return await _save_modified(ref, game, team, user)


async def _save_modified(ref, game: dict, team: dict, user: User) -> dict:
    if _team_done(game, team):
        team["finished"] = True           # nothing more they can do
    teams = game["teams"]
    teams[str(user.id)] = team
    await ref.update({"teams": teams})
    game["teams"] = teams
    await _close_if_over(ref, game)
    reveal = game["status"] == FINISHED
    return {"ok": True, "standing": modified_standing(game, str(user.id), modified_results(game), reveal),
            "status": game["status"]}


class SetAnswer(BaseModel):
    question: int        # 0-based
    answer: str


@router.post("/game/{game_id}/answer")
async def set_answer(game_id: str, req: SetAnswer, user: User = Depends(current_user)):
    """The host sets (or corrects) a modified question's own answer, at any
    point: before, during or after the game. Only "fixed" ones have one."""
    async with _lock(game_id):
        ref, game = await _load(game_id)
        if not _is_host(game, user):
            raise HTTPException(403, "Only the host can set answers")
        questions = game.get("questions") or []
        if _mode(game) != MODIFIED or not 0 <= req.question < len(questions):
            raise HTTPException(400, "No such question")
        kind = questions[req.question].get("kind")
        if kind not in ("fixed", INTERVAL):
            raise HTTPException(400, "That answer comes from the game itself")
        try:
            questions[req.question]["answer"] = (parse_quantity if kind == INTERVAL else parse_number)(req.answer)
        except ValueError:
            raise HTTPException(400, "The answer must be a number (e.g. 1500, 1.5e3, 3x10^6)"
                                + (", above zero" if kind == INTERVAL else ""))
        await ref.update({"questions": questions})
    return {"ok": True}


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
    out["mode"] = _mode(game)
    if out["mode"] == MODIFIED:
        return _modified_state(game, out, uid, is_host, team)
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


def _modified_state(game: dict, out: dict, uid: str, is_host: bool, team: Optional[dict]) -> dict:
    """game_state for the modified mode. Teams see nobody else's answers, and
    no answers or points, until the game is over: most answers are made of
    everyone's submissions, so even a provisional one would give them away.
    The host sees it all as it stands."""
    status = out["status"]
    questions = game.get("questions") or []
    results = modified_results(game)
    finished = status == FINISHED
    if status != LOBBY or is_host:
        rows = []
        for i, q in enumerate(questions):
            row = {"text": q["text"], "hint": hint_line(q), "min": q.get("min"), "max": q.get("max"),
                   "interval": _is_interval(q)}
            if is_host or finished:
                a = results["answers"][i]
                row.update({"answer": a, "note": q.get("note") or "", "kind": q.get("kind"),
                            "kind_label": "Normal: bank answer" if q.get("bank_id") in BY_ID else KINDS.get(q.get("kind"), ""),
                            # Final when it can't change: the host's own, a drawn one, or a finished game.
                            "provisional": not finished and q.get("kind") not in ("fixed", "random", INTERVAL)})
            if is_host and _is_interval(q):
                parts = results["interval"].values()
                row["answered"] = sum(1 for p in parts if i in p["per"])
                row["good"] = sum(1 for p in parts if (p["per"].get(i) or {}).get("good"))
            elif is_host:
                names = {u: (t.get("name") or "Team") for u, t in (game.get("teams") or {}).items()}
                ranked = sorted(((rp[0], u) for u, pts in results["points"].items() for k, rp in pts.items() if k == i))
                row["closest"] = [{"name": names.get(u, "Team"),
                                   "shown": results["latest"][u][i].get("shown") or _written_out(results["latest"][u][i]["value"])}
                                  for _, u in ranked[:3]]
                row["answered"] = sum(1 for subs in results["latest"].values() if i in subs)
            rows.append(row)
        out["questions"] = rows
    out["has_interval"], out["has_crowd"] = results["has_interval"], results["has_crowd"]
    if is_host or finished:
        out["leaderboard"] = modified_leaderboard(game, results)
    if team is not None:
        out["team_name"] = team.get("name")
        out["standing"] = modified_standing(game, uid, results, reveal=finished)
    return out
