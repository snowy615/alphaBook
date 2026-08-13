"""
Site-wide rewards: daily streaks, XP, levels and badges.
========================================================

Crash Ledger already had its own XP and day streak, but they only counted
that one mode. This is the platform-level layer: a streak for showing up at
all, XP from every scored game in every mode, and badges for the milestones
worth chasing.

Design notes:

* **The streak is for turning up, not for winning.** It advances on the first
  authenticated page load of the day, so a student who opens the site and
  plays one round keeps it alive. That makes it a habit loop rather than
  another thing to be bad at.
* **A missed day costs the streak, not the XP.** XP and badges are permanent;
  only the consecutive-day count resets. Losing weeks of progress for one
  missed day is the part of streak systems people quit over.
* **Badges are earned, never granted.** Each one is recomputed from real
  results, so they can't drift out of step with the leaderboard.

All state lives on the user document, so a read is one lookup and there is no
second collection to keep consistent.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Dict, List, Optional

from app import db as db_module

log = logging.getLogger("uvicorn.error")

# ── XP ────────────────────────────────────────────────────────────────────────
XP_PER_GAME = 20               # any finished game, win or lose
XP_DAILY_BASE = 25             # first visit of the day
XP_STREAK_STEP = 5             # added per consecutive day…
XP_STREAK_CAP = 100            # …up to this
XP_NEW_PLAYER = 30             # a head start, so the first bar isn't empty

# ── Levels ────────────────────────────────────────────────────────────────────
# Quadratic, so each level costs more than the last.
LEVEL_STEP = 120


def level_for_xp(xp: int) -> int:
    return int((max(0, xp) / LEVEL_STEP) ** 0.5) + 1


def xp_for_level(level: int) -> int:
    return LEVEL_STEP * (level - 1) ** 2


def level_progress(xp: int) -> Dict[str, Any]:
    lvl = level_for_xp(xp)
    lo, hi = xp_for_level(lvl), xp_for_level(lvl + 1)
    into, span = xp - lo, max(1, hi - lo)
    return {"level": lvl, "into": into, "span": span, "pct": round(into / span, 4)}


# ── Badges ────────────────────────────────────────────────────────────────────
# Each: key, label, blurb, and a test over the player's stats.
BADGES: List[Dict[str, Any]] = [
    {"key": "first_game", "label": "First blood", "icon": "🎯",
     "blurb": "Finish your first scored game.",
     "test": lambda s: s["total_games"] >= 1},
    {"key": "ten_games", "label": "Regular", "icon": "📈",
     "blurb": "Finish 10 games.",
     "test": lambda s: s["total_games"] >= 10},
    {"key": "fifty_games", "label": "Desk veteran", "icon": "🏛️",
     "blurb": "Finish 50 games.",
     "test": lambda s: s["total_games"] >= 50},
    {"key": "streak_3", "label": "Warming up", "icon": "🔥",
     "blurb": "Three days in a row.",
     "test": lambda s: s["streak_best"] >= 3},
    {"key": "streak_7", "label": "Week straight", "icon": "🔥",
     "blurb": "Seven days in a row.",
     "test": lambda s: s["streak_best"] >= 7},
    {"key": "streak_30", "label": "Iron habit", "icon": "💎",
     "blurb": "Thirty days in a row.",
     "test": lambda s: s["streak_best"] >= 30},
    {"key": "two_modes", "label": "Curious", "icon": "🧭",
     "blurb": "Play two different modes.",
     "test": lambda s: s["modes_played"] >= 2},
    {"key": "five_modes", "label": "All-rounder", "icon": "🎲",
     "blurb": "Play five different modes.",
     "test": lambda s: s["modes_played"] >= 5},
    {"key": "every_mode", "label": "Completionist", "icon": "🗺️",
     "blurb": "Play every mode on the platform.",
     "test": lambda s: s["modes_played"] >= s["total_modes"] > 0},
    {"key": "rated_70", "label": "Sharp", "icon": "⭐",
     "blurb": "Reach an overall rating of 70.",
     "test": lambda s: s["overall"] >= 70},
    {"key": "rated_85", "label": "Serious edge", "icon": "🌟",
     "blurb": "Reach an overall rating of 85.",
     "test": lambda s: s["overall"] >= 85},
    {"key": "mode_top", "label": "Mode leader", "icon": "👑",
     "blurb": "Finish top of any mode's board.",
     "test": lambda s: s["best_mode_rank"] == 1},
    {"key": "podium", "label": "On the podium", "icon": "🥇",
     "blurb": "Reach the top 3 overall.",
     "test": lambda s: 1 <= s["rank"] <= 3},
    {"key": "competitor", "label": "Competitor", "icon": "⚔️",
     "blurb": "Play in a competition.",
     "test": lambda s: s["competitions"] >= 1},
]

BADGE_BY_KEY = {b["key"]: b for b in BADGES}


def _today() -> str:
    return dt.date.today().isoformat()


def _yesterday() -> str:
    return (dt.date.today() - dt.timedelta(days=1)).isoformat()


def daily_bonus(streak: int) -> int:
    """XP for the first visit of the day, worth more the longer the run."""
    return XP_DAILY_BASE + min(XP_STREAK_CAP, XP_STREAK_STEP * max(0, streak - 1))


async def touch_daily(user_id: str) -> Dict[str, Any]:
    """
    Register a visit. Advances the streak once per day and pays the bonus.

    Returns what the UI needs to celebrate, including `awarded` so the client
    only shows the toast on the visit that actually earned it.
    """
    blank = {"streak": 0, "best": 0, "xp": 0, "awarded": 0, "already": True}
    if not user_id:
        return blank

    try:
        ref = db_module.db.collection("users").document(str(user_id))
        doc = await ref.get()
        if not doc.exists:
            return blank
        data = doc.to_dict() or {}

        today, last = _today(), data.get("streak_last")
        xp = int(data.get("xp", 0) or 0)

        if last == today:                      # already counted today
            return {"streak": int(data.get("streak_days", 0) or 0),
                    "best": int(data.get("streak_best", 0) or 0),
                    "xp": xp, "awarded": 0, "already": True}

        streak = int(data.get("streak_days", 0) or 0)
        streak = streak + 1 if last == _yesterday() else 1
        best = max(int(data.get("streak_best", 0) or 0), streak)
        award = daily_bonus(streak)
        xp += award

        await ref.update({
            "streak_days": streak,
            "streak_best": best,
            "streak_last": today,
            "xp": xp,
        })
        return {"streak": streak, "best": best, "xp": xp,
                "awarded": award, "already": False}
    except Exception:
        log.warning("daily streak update failed for %s", user_id, exc_info=True)
        return blank


async def award_xp(user_id: str, amount: int = XP_PER_GAME) -> None:
    """Add XP. Never raises — it runs inside game finish paths."""
    if not user_id or amount <= 0:
        return
    try:
        ref = db_module.db.collection("users").document(str(user_id))
        doc = await ref.get()
        if not doc.exists:
            return
        xp = int((doc.to_dict() or {}).get("xp", 0) or 0)
        await ref.update({"xp": xp + int(amount)})
    except Exception:
        log.warning("xp award failed for %s", user_id, exc_info=True)


def earned_badges(stats: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Every badge, flagged earned or not, so the UI can show what's next."""
    out = []
    for b in BADGES:
        try:
            got = bool(b["test"](stats))
        except Exception:
            got = False
        out.append({"key": b["key"], "label": b["label"], "icon": b["icon"],
                    "blurb": b["blurb"], "earned": got})
    return out


async def summary(user_id: str, scorecard: Optional[Dict[str, Any]] = None,
                  competitions: int = 0) -> Dict[str, Any]:
    """Streak, XP, level and badges for one player."""
    try:
        doc = await db_module.db.collection("users").document(str(user_id)).get()
        data = doc.to_dict() if doc.exists else {}
    except Exception:
        data = {}

    xp = int((data or {}).get("xp", 0) or 0) or XP_NEW_PLAYER
    card = scorecard or {}
    modes = card.get("modes") or []
    best_rank = min((m.get("rank") or 9999) for m in modes) if modes else 9999

    stats = {
        "total_games": card.get("total_games", 0) or 0,
        "modes_played": card.get("modes_played", 0) or 0,
        "total_modes": card.get("total_modes", 0) or 0,
        "overall": card.get("overall", 0) or 0,
        "rank": card.get("rank") or 9999,
        "best_mode_rank": best_rank,
        "streak_best": int((data or {}).get("streak_best", 0) or 0),
        "competitions": competitions,
    }

    badges = earned_badges(stats)
    streak = int((data or {}).get("streak_days", 0) or 0)
    # A streak only counts as live if it was touched today or yesterday.
    if (data or {}).get("streak_last") not in (_today(), _yesterday()):
        streak = 0

    return {
        "streak": streak,
        "streak_best": stats["streak_best"],
        "streak_today": (data or {}).get("streak_last") == _today(),
        "next_bonus": daily_bonus(streak + 1),
        "xp": xp,
        **level_progress(xp),
        "badges": badges,
        "earned_count": sum(1 for b in badges if b["earned"]),
        "badge_count": len(badges),
    }
