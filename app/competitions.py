"""
Competitions — a scored event with a join code.
===============================================

Everyone plays in **practice** by default: results feed personal ratings,
feedback and the site leaderboard exactly as before. A host opens a
**competition**, students join it with a code, and from then until the host
closes it every scored game they finish is *also* tagged to that competition
and ranked on its own board.

Practice therefore never goes quiet, and a competition is a clean, shareable
event rather than a mode switch that hides someone's normal progress.

Ranking inside a competition reuses the site's rating maths (see
:mod:`app.scores`) but scoped to the event: a player's percentile *among the
entrants*, per mode, averaged. That keeps modes with incomparable units — P&L,
accuracy, points — combinable into one table.

Enrolment is a single field on the user (`active_competition`), so a player is
in at most one competition at a time and leaving is instant.
"""
from __future__ import annotations

import datetime as dt
import logging
import random
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from app import db as db_module
from app import membership as mb
from app import scores
from app.auth import current_user
from app.models import User

log = logging.getLogger("uvicorn.error")

router = APIRouter(prefix="/competitions", tags=["competitions"])
BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

COLLECTION = "competitions"

STATUS_DRAFT = "draft"        # created, not accepting play yet
STATUS_RUNNING = "running"    # joinable and scoring
STATUS_FINISHED = "finished"  # closed, board frozen

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # no look-alike characters


def _new_code() -> str:
    return "".join(random.choice(CODE_ALPHABET) for _ in range(6))


def _require_host(user: User) -> None:
    if not mb.can_host({"is_admin": user.is_admin, "role": getattr(user, "role", None)}):
        raise HTTPException(403, "Only a host or admin can run competitions")


def _view(doc_id: str, data: Dict[str, Any], *, joined: bool = False) -> Dict[str, Any]:
    return {
        "id": doc_id,
        "name": data.get("name", ""),
        "code": data.get("code", ""),
        "status": data.get("status", STATUS_DRAFT),
        "modes": data.get("modes") or [],
        "created_by": data.get("created_by", ""),
        "host_name": data.get("host_name", ""),
        "created_at": data.get("created_at"),
        "started_at": data.get("started_at"),
        "finished_at": data.get("finished_at"),
        "entrants": len(data.get("entrants") or []),
        "joined": joined,
    }


async def _load(comp_id: str):
    ref = db_module.db.collection(COLLECTION).document(comp_id)
    doc = await ref.get()
    if not doc.exists:
        raise HTTPException(404, "Competition not found")
    return ref, (doc.to_dict() or {})


async def _find_by_code(code: str) -> Optional[Any]:
    docs = await db_module.db.collection(COLLECTION) \
        .where("code", "==", (code or "").strip().upper()).limit(1).get()
    return docs[0] if docs else None


# ─────────────────────────────────────────────────────────────────────────────
# Enrolment — what the rest of the app asks about
# ─────────────────────────────────────────────────────────────────────────────

async def active_for_user(user_id: str) -> Optional[Dict[str, Any]]:
    """
    The competition this player's results should count toward, or None.

    Returns None unless the competition still exists and is running, so a
    finished or deleted event can never keep swallowing results.
    """
    try:
        doc = await db_module.db.collection("users").document(str(user_id)).get()
        comp_id = (doc.to_dict() or {}).get("active_competition")
        if not comp_id:
            return None
        cdoc = await db_module.db.collection(COLLECTION).document(comp_id).get()
        if not cdoc.exists:
            return None
        data = cdoc.to_dict() or {}
        if data.get("status") != STATUS_RUNNING:
            return None
        return {"id": comp_id, **data}
    except Exception:
        log.warning("competition lookup failed for %s", user_id, exc_info=True)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Request schemas
# ─────────────────────────────────────────────────────────────────────────────

class CreateRequest(BaseModel):
    name: str = Field(default="Competition", max_length=80)
    modes: List[str] = Field(default_factory=list)   # empty = every mode counts


class JoinRequest(BaseModel):
    code: str


# ─────────────────────────────────────────────────────────────────────────────
# Pages
# ─────────────────────────────────────────────────────────────────────────────

@router.get("", include_in_schema=False)
async def competitions_page(request: Request):
    return templates.TemplateResponse("competitions.html", {
        "request": request,
        "app_name": "AlphaBook",
        "modes": scores.MODES,
    })


# ─────────────────────────────────────────────────────────────────────────────
# Player endpoints
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/mine")
async def my_state(user: User = Depends(current_user)):
    """What the header and profile need: am I in practice or a competition?"""
    doc = await db_module.db.collection("users").document(str(user.id)).get()
    data = doc.to_dict() or {}
    comp_id = data.get("active_competition")

    active = None
    if comp_id:
        cdoc = await db_module.db.collection(COLLECTION).document(comp_id).get()
        if cdoc.exists:
            cdata = cdoc.to_dict() or {}
            active = _view(comp_id, cdata, joined=True)
            # A finished competition stops counting; say so rather than
            # implying results are still being recorded.
            active["scoring"] = cdata.get("status") == STATUS_RUNNING

    return {
        "mode": "competition" if (active and active["scoring"]) else "practice",
        "competition": active,
        "can_host": mb.can_host({"is_admin": user.is_admin, "role": data.get("role")}),
    }


@router.post("/join")
async def join(req: JoinRequest, user: User = Depends(current_user)):
    """Enter a competition with its code."""
    doc = await _find_by_code(req.code)
    if doc is None:
        raise HTTPException(404, "No competition with that code")

    data = doc.to_dict() or {}
    if data.get("status") == STATUS_FINISHED:
        raise HTTPException(400, "That competition has finished")
    if data.get("status") != STATUS_RUNNING:
        raise HTTPException(400, "That competition hasn't started yet")

    uid = str(user.id)
    entrants = list(data.get("entrants") or [])
    if uid not in entrants:
        entrants.append(uid)
        await doc.reference.update({"entrants": entrants})

    await db_module.db.collection("users").document(uid).update(
        {"active_competition": doc.id})
    return {"ok": True, "competition": _view(doc.id, {**data, "entrants": entrants}, joined=True)}


@router.post("/leave")
async def leave(user: User = Depends(current_user)):
    """Go back to practice. Results already recorded stay on the board."""
    await db_module.db.collection("users").document(str(user.id)).update(
        {"active_competition": None})
    return {"ok": True, "mode": "practice"}


@router.get("/list")
async def list_competitions(user: User = Depends(current_user)):
    """Running and recently finished competitions."""
    docs = await db_module.db.collection(COLLECTION).get()
    uid = str(user.id)
    rows = [_view(d.id, d.to_dict() or {}, joined=uid in ((d.to_dict() or {}).get("entrants") or []))
            for d in docs]
    rows.sort(key=lambda r: (r["status"] != STATUS_RUNNING,
                             r.get("created_at") or dt.datetime.min.replace(tzinfo=dt.timezone.utc)),
              reverse=False)
    return {"competitions": rows}


@router.get("/{comp_id}/leaderboard")
async def competition_leaderboard(comp_id: str, user: User = Depends(current_user)):
    """
    The event's own board.

    Built from the score events tagged to this competition, ranked with the
    same percentile maths the site uses — but among the entrants only, so a
    competition is judged on its own field.
    """
    _, data = await _load(comp_id)

    try:
        docs = await db_module.db.collection(scores.EVENTS_COLLECTION) \
            .where("competition_id", "==", comp_id).get()
    except Exception:
        log.warning("competition events query failed for %s", comp_id, exc_info=True)
        docs = []

    # Rebuild the per-player, per-mode aggregates compute_ratings expects.
    players: Dict[str, Dict[str, Any]] = {}
    for d in docs:
        e = d.to_dict() or {}
        uid, mode = e.get("user_id"), e.get("mode")
        if not uid or mode not in scores.MODES:
            continue
        p = players.setdefault(uid, {"user_id": uid, "username": e.get("username", ""), "modes": {}})
        agg = p["modes"].setdefault(mode, {"games": 0, "sum": 0.0, "best": None,
                                           "last": None, "recent": []})
        value = float(e.get("value", 0.0))
        agg["games"] += 1
        agg["sum"] += value
        agg["best"] = value if agg["best"] is None else max(agg["best"], value)
        agg["last"] = value
        agg["recent"].append(value)
        if e.get("username"):
            p["username"] = e["username"]

    board = scores.compute_ratings(list(players.values()))
    my_id = str(user.id)

    def strip(rows):
        out = []
        for r in rows:
            row = {k: v for k, v in r.items() if k not in ("last_feedback", "recent")}
            if isinstance(row.get("ratings"), dict):
                row["ratings"] = {m: {k: v for k, v in rv.items()
                                      if k not in ("last_feedback", "recent")}
                                  for m, rv in row["ratings"].items()}
            row["is_me"] = row.get("user_id") == my_id
            out.append(row)
        return out

    return {
        "competition": _view(comp_id, data, joined=my_id in (data.get("entrants") or [])),
        "modes": [{"key": k, **v} for k, v in scores.MODES.items()],
        "overall": strip(board["players"]),
        "mode_boards": {k: strip(v) for k, v in board["mode_boards"].items()},
        "played": sum(1 for _ in docs),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Host endpoints
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/create")
async def create(req: CreateRequest, user: User = Depends(current_user)):
    _require_host(user)
    modes = [m for m in req.modes if m in scores.MODES]
    comp_id = str(uuid.uuid4())
    data = {
        "name": (req.name or "Competition").strip()[:80],
        "code": _new_code(),
        "status": STATUS_DRAFT,
        "modes": modes,
        "entrants": [],
        "created_by": str(user.id),
        "host_name": user.username,
        "created_at": dt.datetime.utcnow(),
        "started_at": None,
        "finished_at": None,
    }
    await db_module.db.collection(COLLECTION).document(comp_id).set(data)
    return {"ok": True, "competition": _view(comp_id, data)}


def _require_owner(data: Dict[str, Any], user: User) -> None:
    if data.get("created_by") != str(user.id) and not user.is_admin:
        raise HTTPException(403, "Only the host who created this competition can manage it")


@router.post("/{comp_id}/start")
async def start(comp_id: str, user: User = Depends(current_user)):
    """Open the competition: the code starts working and results start counting."""
    _require_host(user)
    ref, data = await _load(comp_id)
    _require_owner(data, user)
    if data.get("status") == STATUS_FINISHED:
        raise HTTPException(400, "That competition has already finished")
    await ref.update({"status": STATUS_RUNNING, "started_at": dt.datetime.utcnow()})
    return {"ok": True, "status": STATUS_RUNNING}


@router.post("/{comp_id}/finish")
async def finish(comp_id: str, user: User = Depends(current_user)):
    """Close it. The board freezes and entrants drop back to practice."""
    _require_host(user)
    ref, data = await _load(comp_id)
    _require_owner(data, user)
    await ref.update({"status": STATUS_FINISHED, "finished_at": dt.datetime.utcnow()})

    # Clear enrolment so nobody keeps playing "in" a closed event.
    for uid in (data.get("entrants") or []):
        try:
            await db_module.db.collection("users").document(uid).update(
                {"active_competition": None})
        except Exception:
            log.warning("could not clear competition for %s", uid, exc_info=True)

    return {"ok": True, "status": STATUS_FINISHED}


@router.delete("/{comp_id}")
async def delete(comp_id: str, user: User = Depends(current_user)):
    _require_host(user)
    ref, data = await _load(comp_id)
    _require_owner(data, user)
    for uid in (data.get("entrants") or []):
        try:
            await db_module.db.collection("users").document(uid).update(
                {"active_competition": None})
        except Exception:
            pass
    await ref.delete()
    return {"ok": True}
