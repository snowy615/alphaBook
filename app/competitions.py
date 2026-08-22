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

STATUS_DRAFT = "draft"          # created, no start time set yet
STATUS_SCHEDULED = "scheduled"  # opens by itself at starts_at
STATUS_RUNNING = "running"      # joinable and scoring
STATUS_FINISHED = "finished"    # closed, board frozen

# Statuses a competition can be in before it has opened.
PRE_OPEN = (STATUS_DRAFT, STATUS_SCHEDULED)


def mode_settings_spec() -> Dict[str, List[Dict[str, Any]]]:
    """
    What a host can fix about each mode when setting up an event.

    Built from each mode's own create-request options rather than a second
    hard-coded list, so a scenario or question type added to a game shows up
    here without anyone remembering to mirror it.
    """
    spec: Dict[str, List[Dict[str, Any]]] = {}

    try:
        from app import mental_math as mm

        spec["mental_math"] = [
            {"key": "difficulty", "label": "Difficulty", "type": "select",
             "options": [{"value": d, "label": d.title()} for d in ("easy", "medium", "hard")],
             "default": "medium"},
            {"key": "num_questions", "label": "Questions", "type": "number",
             "min": 5, "max": 50, "default": 10},
            {"key": "time_per_question", "label": "Seconds each", "type": "number",
             "min": 5, "max": 60, "default": 15},
            {"key": "question_types", "label": "Question types", "type": "multi",
             "options": [{"value": k, "label": k.replace("_", " ").title()}
                         for k in sorted(mm.VALID_TYPES)],
             "default": ["addition", "multiplication", "percentage"]},
        ]
    except Exception:
        log.warning("mental math settings unavailable", exc_info=True)

    try:
        from app import headline as hl

        spec["headline"] = [
            {"key": "template", "label": "Scenario", "type": "select",
             "options": [{"value": k, "label": v["name"]} for k, v in hl.TEMPLATES.items()],
             "default": next(iter(hl.TEMPLATES), "")},
        ]
    except Exception:
        log.warning("headline settings unavailable", exc_info=True)

    try:
        from app import risk_episodes as ep

        universes = [{"value": "", "label": "Any universe"}]
        universes += [{"value": u["universe"], "label": u["label"]} for u in ep.universes()]
        spec["risks"] = [
            {"key": "universe", "label": "Universe", "type": "select",
             "options": universes, "default": ""},
            {"key": "seconds_per_day", "label": "Seconds per day", "type": "number",
             "min": ep.MIN_SECONDS_PER_DAY, "max": ep.MAX_SECONDS_PER_DAY,
             "default": ep.DEFAULT_SECONDS_PER_DAY},
        ]
    except Exception:
        log.warning("risks settings unavailable", exc_info=True)

    return spec


def _clean_settings(modes: List[str], raw: Dict[str, Any]) -> Dict[str, Any]:
    """Keep only settings the spec knows about, coerced to their declared type."""
    spec = mode_settings_spec()
    out: Dict[str, Any] = {}
    for mode in modes:
        fields = spec.get(mode)
        if not fields:
            continue
        given = (raw or {}).get(mode) or {}
        chosen: Dict[str, Any] = {}
        for f in fields:
            val = given.get(f["key"], f.get("default"))
            if f["type"] == "number":
                try:
                    val = int(val)
                except (TypeError, ValueError):
                    val = f.get("default")
                lo, hi = f.get("min"), f.get("max")
                if lo is not None:
                    val = max(lo, val)
                if hi is not None:
                    val = min(hi, val)
            elif f["type"] == "select":
                allowed = {o["value"] for o in f["options"]}
                if val not in allowed:
                    val = f.get("default")
            elif f["type"] == "multi":
                allowed = {o["value"] for o in f["options"]}
                val = [v for v in (val or []) if v in allowed] or list(f.get("default") or [])
            chosen[f["key"]] = val
        out[mode] = chosen
    return out


def _parse_when(value: Any) -> Optional[dt.datetime]:
    """Accept an ISO string from the form; always return tz-aware UTC or None."""
    if not value:
        return None
    if isinstance(value, dt.datetime):
        when = value
    else:
        try:
            when = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(400, "That start time isn't a valid date and time")
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    return when.astimezone(dt.timezone.utc)


def _as_utc(value: Any) -> Optional[dt.datetime]:
    """Firestore hands back tz-aware datetimes; local writes may not be."""
    if not value:
        return None
    if isinstance(value, str):
        return _parse_when(value)
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


def sync_schedule(data: Dict[str, Any]) -> bool:
    """
    Move a competition through its own timetable.

    Scheduled events open themselves and timed ones close themselves, resolved
    when someone reads rather than by a background task — the same approach the
    timed games use, so nothing depends on a process staying alive.
    """
    now = dt.datetime.now(dt.timezone.utc)
    changed = False
    status = data.get("status")

    if status == STATUS_SCHEDULED:
        starts = _as_utc(data.get("starts_at"))
        if starts and now >= starts:
            data["status"] = STATUS_RUNNING
            data["started_at"] = now
            status = STATUS_RUNNING
            changed = True

    if status == STATUS_RUNNING:
        ends = _as_utc(data.get("ends_at"))
        if ends and now >= ends:
            data["status"] = STATUS_FINISHED
            data["finished_at"] = now
            changed = True

    return changed

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # no look-alike characters


def _new_code() -> str:
    return "".join(random.choice(CODE_ALPHABET) for _ in range(6))


def _require_host(user: User) -> None:
    if not mb.can_host({"is_admin": user.is_admin, "role": getattr(user, "role", None)}):
        raise HTTPException(403, "Only a host or admin can run competitions")


def _iso(value: Any) -> Optional[str]:
    when = _as_utc(value)
    return when.isoformat() if when else None


def _view(doc_id: str, data: Dict[str, Any], *, joined: bool = False) -> Dict[str, Any]:
    modes = data.get("modes") or []
    return {
        "id": doc_id,
        "name": data.get("name", ""),
        "code": data.get("code", ""),
        "status": data.get("status", STATUS_DRAFT),
        "modes": modes,
        "mode_labels": [scores.mode_meta(m)["label"] for m in modes],
        "settings": data.get("settings") or {},
        "starts_at": _iso(data.get("starts_at")),
        "ends_at": _iso(data.get("ends_at")),
        "created_by": data.get("created_by", ""),
        "host_name": data.get("host_name", ""),
        "created_at": data.get("created_at"),
        "started_at": data.get("started_at"),
        "finished_at": data.get("finished_at"),
        "entrants": len(data.get("entrants") or []),
        "joined": joined,
    }


async def _load(comp_id: str):
    """Load a competition, first bringing it up to date with its own timetable."""
    ref = db_module.db.collection(COLLECTION).document(comp_id)
    doc = await ref.get()
    if not doc.exists:
        raise HTTPException(404, "Competition not found")
    data = doc.to_dict() or {}
    if sync_schedule(data):
        await ref.set(data)
    return ref, data


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
        if sync_schedule(data):
            await cdoc.reference.set(data)
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
    settings: Dict[str, Any] = Field(default_factory=dict)   # {mode: {key: value}}
    start_now: bool = False
    starts_at: Optional[str] = None                  # ISO; ignored when start_now
    ends_at: Optional[str] = None                    # ISO; optional auto-close


class UpdateRequest(BaseModel):
    """Edits allowed only while an event has not opened yet."""
    name: Optional[str] = Field(default=None, max_length=80)
    modes: Optional[List[str]] = None
    settings: Optional[Dict[str, Any]] = None
    starts_at: Optional[str] = None
    ends_at: Optional[str] = None
    clear_schedule: bool = False


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


@router.get("/format/{mode}")
async def format_for_mode(mode: str, user: User = Depends(current_user)):
    """
    The settings a room in this mode has to use right now.

    A mode page calls this before showing its own setup form: if the player is
    in a running competition that scores this mode and the host fixed a format,
    the form is pre-filled and locked so every entrant plays the same game.
    """
    comp = await active_for_user(str(user.id))
    if not comp:
        return {"locked": False}
    modes = comp.get("modes") or []
    if modes and mode not in modes:
        # In a competition, but this mode isn't part of it — play it as practice.
        return {"locked": False, "in_competition": True, "counts": False}

    settings = (comp.get("settings") or {}).get(mode) or {}
    return {
        "locked": bool(settings),
        "in_competition": True,
        "counts": True,
        "competition": comp.get("name", ""),
        "settings": settings,
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
    rows = []
    for d in docs:
        data = d.to_dict() or {}
        # A scheduled event opens itself the first time anyone looks at the list.
        if sync_schedule(data):
            await d.reference.set(data)
        rows.append(_view(d.id, data, joined=uid in (data.get("entrants") or [])))
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

@router.get("/spec")
async def spec(user: User = Depends(current_user)):
    """Everything the setup form needs: the modes, and what each one can fix."""
    _require_host(user)
    return {
        "modes": [{"key": k, "label": v["label"], "blurb": v["blurb"]}
                  for k, v in scores.MODES.items()],
        "settings": mode_settings_spec(),
    }


@router.post("/create")
async def create(req: CreateRequest, user: User = Depends(current_user)):
    _require_host(user)
    modes = [m for m in req.modes if m in scores.MODES]
    settings = _clean_settings(modes, req.settings)

    starts = None if req.start_now else _parse_when(req.starts_at)
    ends = _parse_when(req.ends_at)
    now = dt.datetime.now(dt.timezone.utc)

    if ends and ends <= (starts or now):
        raise HTTPException(400, "The finish time has to be after the start")

    if req.start_now:
        status, started = STATUS_RUNNING, now
    elif starts:
        if starts <= now:
            # A time already past means "open it", not "schedule the past".
            status, started, starts = STATUS_RUNNING, now, None
        else:
            status, started = STATUS_SCHEDULED, None
    else:
        status, started = STATUS_DRAFT, None

    comp_id = str(uuid.uuid4())
    data = {
        "name": (req.name or "Competition").strip()[:80],
        "code": _new_code(),
        "status": status,
        "modes": modes,
        "settings": settings,
        "entrants": [],
        "created_by": str(user.id),
        "host_name": user.username,
        "created_at": dt.datetime.utcnow(),
        "starts_at": starts,
        "ends_at": ends,
        "started_at": started,
        "finished_at": None,
    }
    await db_module.db.collection(COLLECTION).document(comp_id).set(data)
    return {"ok": True, "competition": _view(comp_id, data)}


@router.put("/{comp_id}")
async def update(comp_id: str, req: UpdateRequest, user: User = Depends(current_user)):
    """Adjust an event that hasn't opened. Once it's live the format is fixed."""
    _require_host(user)
    ref, data = await _load(comp_id)
    _require_owner(data, user)
    if data.get("status") not in PRE_OPEN:
        raise HTTPException(400, "This competition has already opened — its format is fixed")

    if req.name is not None:
        data["name"] = req.name.strip()[:80] or data["name"]
    if req.modes is not None:
        data["modes"] = [m for m in req.modes if m in scores.MODES]
    if req.settings is not None or req.modes is not None:
        data["settings"] = _clean_settings(data["modes"], req.settings or data.get("settings") or {})

    if req.clear_schedule:
        data["starts_at"] = None
        data["status"] = STATUS_DRAFT
    elif req.starts_at is not None:
        starts = _parse_when(req.starts_at)
        data["starts_at"] = starts
        data["status"] = STATUS_SCHEDULED if starts else STATUS_DRAFT

    if req.ends_at is not None:
        data["ends_at"] = _parse_when(req.ends_at)

    ends, starts = _as_utc(data.get("ends_at")), _as_utc(data.get("starts_at"))
    if ends and starts and ends <= starts:
        raise HTTPException(400, "The finish time has to be after the start")

    await ref.set(data)
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
