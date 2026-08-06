"""
Guided learning: placement, a levelled path, and what to do next.
=================================================================

Nine modes on one page is a wall, not a curriculum. This module turns the
platform into an ordered route:

* a **placement quiz** at sign-up, four questions, that puts someone at
  beginner / intermediate / advanced rather than making them guess;
* a **path per level** — an ordered list of steps, each pointing at one mode
  with a sentence on why it comes here and what it teaches;
* **progress from real play**. A step is done when the player has a scored
  result in that mode, which `app.scores` already records, so nothing new has
  to be tracked and the path cannot drift out of sync with reality.

Everything except the two Firestore helpers at the bottom is pure, so the
scoring and the path maths are testable without a database.
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Dict, List, Optional, Tuple

from app import db as db_module

LEVELS = ("beginner", "intermediate", "advanced")

LEVEL_META: Dict[str, Dict[str, str]] = {
    "beginner": {
        "label": "Beginner",
        "blurb": "New to trading, or new to doing it under pressure. Start with "
                 "how a market actually clears, then add speed.",
    },
    "intermediate": {
        "label": "Intermediate",
        "blurb": "You know what a bid and an ask are. The work now is pricing, "
                 "calibration and holding a position when it moves against you.",
    },
    "advanced": {
        "label": "Advanced",
        "blurb": "You can price and hedge. The work now is automating it and "
                 "managing risk across a book.",
    },
}


# ── Placement quiz ──────────────────────────────────────────────────────
# Four questions, each option worth points. Deliberately short: a long quiz
# before anyone has seen the product is its own drop-off.
QUESTIONS: List[Dict[str, Any]] = [
    {
        "key": "traded",
        "prompt": "Have you traded before, in any form?",
        "help": "Paper trading, a student fund, an internship, or your own money.",
        "options": [
            {"value": "never", "label": "Never", "points": 0},
            {"value": "dabbled", "label": "A bit — some paper or personal trading", "points": 2},
            {"value": "regular", "label": "Regularly, and I follow markets closely", "points": 4},
            {"value": "professional", "label": "In a professional or fund setting", "points": 6},
        ],
    },
    {
        "key": "orderbook",
        "prompt": "How comfortable are you with a limit order book?",
        "help": "Bids, asks, the spread, and what happens when you cross it.",
        "options": [
            {"value": "new", "label": "I'd need it explained", "points": 0},
            {"value": "basics", "label": "I know bid, ask and spread", "points": 2},
            {"value": "confident", "label": "I can work an order and read depth", "points": 4},
            {"value": "market_maker", "label": "I've quoted two-sided markets", "points": 6},
        ],
    },
    {
        "key": "quant",
        "prompt": "Expected value, variance, and betting on your own estimate?",
        "help": "The maths behind pricing something you can only estimate.",
        "options": [
            {"value": "new", "label": "New to me", "points": 0},
            {"value": "some", "label": "I've covered the basics", "points": 2},
            {"value": "solid", "label": "Comfortable — I'd size a bet on an edge", "points": 4},
            {"value": "strong", "label": "It's my day job or degree", "points": 6},
        ],
    },
    {
        "key": "code",
        "prompt": "Could you write a Python script that reacts to live data?",
        "help": "This decides whether the coding modes are on your path yet.",
        "options": [
            {"value": "none", "label": "I don't code", "points": 0},
            {"value": "learning", "label": "I'm learning", "points": 1},
            {"value": "yes", "label": "Yes, given an API to call", "points": 4},
            {"value": "fluent", "label": "Yes, and I've built trading tooling", "points": 6},
        ],
    },
]

MAX_POINTS = sum(max(o["points"] for o in q["options"]) for q in QUESTIONS)

# Cut-offs on the 0–24 scale. Calibrated against the profile that decides the
# boundary: someone who has dabbled, knows bid/ask/spread, has covered the
# basics of expected value and is learning to code scores 7 — and should not be
# made to sit through the beginner path.
INTERMEDIATE_AT = 7
ADVANCED_AT = 16


def level_for_points(points: int) -> str:
    if points >= ADVANCED_AT:
        return "advanced"
    if points >= INTERMEDIATE_AT:
        return "intermediate"
    return "beginner"


def score_answers(answers: Dict[str, str]) -> Dict[str, Any]:
    """Grade the quiz. Unanswered questions simply score zero.

    Returns the level, the points, and a short rationale naming the answers
    that actually moved the result, so the placement is explainable rather
    than a black box.
    """
    points = 0
    picked: List[Tuple[str, str, int]] = []

    for q in QUESTIONS:
        chosen = answers.get(q["key"])
        option = next((o for o in q["options"] if o["value"] == chosen), None)
        if option is None:
            continue
        points += option["points"]
        picked.append((q["key"], option["label"], option["points"]))

    level = level_for_points(points)

    # Name the strongest and weakest signals; that is what makes it feel fair.
    reasons: List[str] = []
    if picked:
        strongest = max(picked, key=lambda p: p[2])
        weakest = min(picked, key=lambda p: p[2])
        if strongest[2] > 0:
            reasons.append(f"you said “{strongest[1]}”")
        if weakest[2] == 0 and weakest[0] != strongest[0]:
            reasons.append(f"but “{weakest[1]}”")

    return {
        "level": level,
        "points": points,
        "max_points": MAX_POINTS,
        "codes": answers.get("code") in ("yes", "fluent"),
        "reasons": reasons,
    }


# ── The path ────────────────────────────────────────────────────────────
# Each step names one mode and says why it sits here. `mode` matches a key in
# app.scores.MODES, which is how progress is read.
def _step(mode: str, title: str, why: str, teaches: str, minutes: str,
          href: Optional[str] = None) -> Dict[str, Any]:
    return {"mode": mode, "title": title, "why": why, "teaches": teaches,
            "minutes": minutes, "href": href}


PATHS: Dict[str, List[Dict[str, Any]]] = {
    "beginner": [
        _step("mental_math", "Warm up the arithmetic",
              "Everything downstream happens faster than you can reach for a calculator.",
              "Speed and accuracy under a clock", "5 min"),
        _step("market_sim", "Meet a real order book",
              "Before any game makes sense, you need to see bids and asks match.",
              "Bid, ask, spread, and what crossing costs you", "10 min"),
        _step("crash_ledger", "See how names behave when it breaks",
              "Build intuition for which stocks fall hardest before you have to trade one.",
              "Volatility, drawdown, and why beta matters", "10 min"),
        _step("headline", "Trade a story",
              "Your first timed decision: news lands, the price moves, you take a side.",
              "Reacting to information without freezing", "5 min"),
    ],
    "intermediate": [
        _step("fiveos", "Price something you cannot see",
              "The core quant interview exercise: estimate, then back the estimate.",
              "Expected value and calibration", "15 min"),
        _step("poker_auction", "Bid against other people",
              "Second-price auctions punish shading; this is where valuation gets adversarial.",
              "Valuation under competition", "20 min"),
        _step("headline", "Trade a story",
              "Same game, higher bar: size the move, don't just call the direction.",
              "Sizing a position to your confidence", "5 min"),
        _step("risks", "Run a book through a crash",
              "One position is a bet. Several at once is risk management.",
              "Hedging, exposure limits, drawdown", "10 min"),
    ],
    "advanced": [
        _step("risks", "Run a book through a crash",
              "Market-neutral by construction, so the round is pure name selection.",
              "Cross-sectional risk and drawdown control", "10 min"),
        _step("market_sim_py", "Automate it",
              "Take the strategy out of your hands and put it in a bot.",
              "Writing a strategy against a live order API", "15 min"),
        _step("swe_prep", "Ship code under review",
              "The same market, but the server runs your Python — closer to an interview.",
              "Writing correct code against a sandbox", "15 min"),
        _step("market_sim", "Make a two-sided market",
              "Quote both sides against real flow and hold inventory you didn't choose.",
              "Market making and inventory risk", "15 min"),
    ],
}


def path_for(level: str) -> List[Dict[str, Any]]:
    return PATHS.get(level, PATHS["beginner"])


def build_progress(level: str, played_modes: set[str],
                   mode_meta: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, Any]:
    """The player's path with each step marked done, plus what is next.

    `played_modes` is the set of mode keys the player has a scored result in.
    """
    steps = []
    next_index = None
    for i, raw in enumerate(path_for(level)):
        meta = (mode_meta or {}).get(raw["mode"], {})
        done = raw["mode"] in played_modes
        if not done and next_index is None:
            next_index = i
        steps.append({
            **raw,
            "index": i + 1,
            "done": done,
            "label": meta.get("label", raw["mode"]),
            "href": raw.get("href") or meta.get("href", "/"),
        })

    done_count = sum(1 for s in steps if s["done"])
    for i, s in enumerate(steps):
        s["is_next"] = i == next_index

    return {
        "level": level,
        "level_label": LEVEL_META.get(level, {}).get("label", level.title()),
        "level_blurb": LEVEL_META.get(level, {}).get("blurb", ""),
        "steps": steps,
        "done": done_count,
        "total": len(steps),
        "pct": round(done_count / len(steps) * 100) if steps else 0,
        "next": steps[next_index] if next_index is not None else None,
        "complete": next_index is None and bool(steps),
    }


def next_level(level: str) -> Optional[str]:
    try:
        i = LEVELS.index(level)
    except ValueError:
        return None
    return LEVELS[i + 1] if i + 1 < len(LEVELS) else None


# ── Persistence ─────────────────────────────────────────────────────────
async def save_placement(user_id: str, level: str, source: str,
                         answers: Optional[Dict[str, str]] = None,
                         points: Optional[int] = None) -> None:
    """Record a level on the user document."""
    if level not in LEVELS:
        raise ValueError(f"unknown level: {level}")
    patch: Dict[str, Any] = {
        "level": level,
        "level_source": source,
        "level_set_at": dt.datetime.now(dt.timezone.utc),
    }
    if answers is not None:
        patch["placement_answers"] = answers
    if points is not None:
        patch["placement_points"] = int(points)
    await db_module.db.collection("users").document(str(user_id)).update(patch)


async def load_level(user_id: str) -> Tuple[Optional[str], Optional[str]]:
    """(level, source) for a user, or (None, None) if never placed."""
    doc = await db_module.db.collection("users").document(str(user_id)).get()
    if not doc.exists:
        return None, None
    data = doc.to_dict() or {}
    level = data.get("level")
    return (level if level in LEVELS else None), data.get("level_source")
