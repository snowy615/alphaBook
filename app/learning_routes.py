"""Routes for placement, the learning path, and the level a page adapts to."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app import learning, scores
from app.auth import current_user
from app.models import User

log = logging.getLogger("uvicorn.error")

router = APIRouter(tags=["learning"])
BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


class PlacementRequest(BaseModel):
    answers: Dict[str, str] = {}


class LevelRequest(BaseModel):
    level: str


async def _played_modes(user_id: str) -> set[str]:
    """Mode keys the player has a scored result in."""
    try:
        card = await scores.scorecard(str(user_id))
    except Exception as exc:                       # a rating outage must not
        log.warning("learning: scorecard failed: %s", exc)   # break the path
        return set()
    if not card.get("found"):
        return set()
    return {row["key"] for row in card.get("modes", []) if row.get("games", 0) > 0}


async def _state_for(user: User) -> Dict[str, Any]:
    level, source = await learning.load_level(str(user.id))
    played = await _played_modes(str(user.id))

    progress = learning.build_progress(level or "beginner", played, scores.MODES)
    progress["placed"] = level is not None
    progress["level_source"] = source

    # Once the path is finished, point at the next tier rather than a dead end.
    if progress["complete"]:
        nxt = learning.next_level(progress["level"])
        progress["suggest_level"] = nxt
        progress["suggest_label"] = (
            learning.LEVEL_META.get(nxt, {}).get("label") if nxt else None
        )
    return progress


# ---- Pages ----
@router.get("/welcome", include_in_schema=False)
async def welcome_page(request: Request):
    """Placement quiz. Shown right after sign-up, and retakeable any time."""
    return templates.TemplateResponse("welcome.html", {
        "request": request,
        "app_name": "AlphaBook",
        "questions": learning.QUESTIONS,
    })


@router.get("/learn", include_in_schema=False)
async def learn_page(request: Request):
    return templates.TemplateResponse("learn.html", {
        "request": request,
        "app_name": "AlphaBook",
        "levels": [
            {"key": k, **learning.LEVEL_META[k], "steps": len(learning.path_for(k))}
            for k in learning.LEVELS
        ],
    })


# ---- API ----
@router.get("/learning/state")
async def learning_state(user: User = Depends(current_user)):
    return await _state_for(user)


@router.get("/learning/questions")
async def learning_questions():
    return {"questions": learning.QUESTIONS}


@router.post("/learning/placement")
async def submit_placement(req: PlacementRequest, user: User = Depends(current_user)):
    result = learning.score_answers(req.answers or {})
    await learning.save_placement(
        str(user.id), result["level"], "quiz",
        answers=req.answers or {}, points=result["points"],
    )
    state = await _state_for(user)
    return {"ok": True, **result, "progress": state}


@router.post("/learning/level")
async def set_level(req: LevelRequest, user: User = Depends(current_user)):
    """Manual override — someone who knows better than the quiz."""
    if req.level not in learning.LEVELS:
        raise HTTPException(status_code=400, detail="Unknown level")
    await learning.save_placement(str(user.id), req.level, "manual")
    return {"ok": True, "progress": await _state_for(user)}


@router.get("/learning/modes")
async def learning_modes(user: Optional[User] = Depends(current_user)):
    """The modes to surface on the landing board, ordered for this player.

    Signed-out visitors get everything; a placed player gets their path first
    so the board opens on what they should do next rather than all nine.
    """
    level, _ = await learning.load_level(str(user.id))
    played = await _played_modes(str(user.id))
    progress = learning.build_progress(level or "beginner", played, scores.MODES)

    on_path: List[str] = [s["mode"] for s in progress["steps"]]
    return {
        "level": progress["level"],
        "placed": level is not None,
        "on_path": on_path,
        "next_mode": progress["next"]["mode"] if progress["next"] else None,
        "progress": progress,
    }
