"""
Events — things Oxford Alpha Fund runs, with sign-up.
=====================================================

An admin creates an event (title, description, location, date and start/end
time in London time, an optional capacity, whether the attendance numbers
are shown to the public, and how sign-up works). Anyone signed in can then
sign up.

Sign-up comes in two modes, chosen per event:

* ``first_come`` — signing up confirms the place immediately, until the
  capacity (if any) is reached.
* ``approval`` — signing up files a request; an admin or Analyst member
  approves or declines it. Approving is what uses up a place.

One event is special: Quant Outreach (:mod:`app.outreach`). Its sign-up is
the same thing as the first step of the Quant Bootcamp application, and comes
with two tickets instead of the plain sign-up — so it has its own endpoint
and is refused by the generic ones.

Storage is two collections: ``events``, and ``event_signups`` with one
document per person per event (id ``{event_id}_{user_id}``), so nobody can
hold two places and cancelling is deleting one document. Capacity is a soft
cap — counted by scan at sign-up time, like the Fast-Track places — which is
plenty for a society's event sizes.
"""
from __future__ import annotations

import datetime as dt
import re
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app import db as db_module
from app import membership as mb
from app import outreach
from app.admin import require_admin
from app.applications import (
    event_ticket_locked, fast_track_refusal, require_reviewer, sync_event_ticket,
)
from app.auth import current_user, http_bearer
from app.models import User

router = APIRouter(prefix="/events", tags=["events"])
BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

EVENTS = "events"
SIGNUPS = "event_signups"
LONDON_TZ = ZoneInfo("Europe/London")

MODE_FIRST_COME = "first_come"
MODE_APPROVAL = "approval"
SIGNUP_MODES = {MODE_FIRST_COME, MODE_APPROVAL}

ST_CONFIRMED = "confirmed"
ST_PENDING = "pending"
ST_DECLINED = "declined"

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class EventPayload(BaseModel):
    title: str
    description: str = ""
    location: str = ""
    date: str            # YYYY-MM-DD, London
    start_time: str      # HH:MM, London
    end_time: str        # HH:MM, London
    capacity: Optional[int] = None    # None / blank = unlimited
    show_attendance: bool = False
    signup_mode: str = MODE_FIRST_COME


class Decision(BaseModel):
    decision: str        # "approve" | "decline"


class TicketChoice(BaseModel):
    ticket: str          # "fast_track" | "general" | "none"


def _is_outreach(event: Dict[str, Any]) -> bool:
    return event.get("kind") == "outreach"


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _as_utc(value: Any) -> Optional[dt.datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


def _parse_event(payload: EventPayload) -> Dict[str, Any]:
    """Validate the form and turn London wall-clock times into UTC instants."""
    title = (payload.title or "").strip()
    if not title:
        raise HTTPException(400, "Give the event a title")
    if not _DATE_RE.match(payload.date or ""):
        raise HTTPException(400, "Pick a date")
    if not _TIME_RE.match(payload.start_time or "") or not _TIME_RE.match(payload.end_time or ""):
        raise HTTPException(400, "Pick a start and end time")
    if not (payload.location or "").strip():
        raise HTTPException(400, "Say where the event is")
    if payload.signup_mode not in SIGNUP_MODES:
        raise HTTPException(400, "Sign-up must be first come first served, or need approval")
    if payload.capacity is not None and payload.capacity < 1:
        raise HTTPException(400, "Capacity must be at least 1 (or leave it blank for no limit)")

    try:
        day = dt.date.fromisoformat(payload.date)
    except ValueError:
        raise HTTPException(400, "That date isn't valid")
    start_t = dt.time.fromisoformat(payload.start_time)
    end_t = dt.time.fromisoformat(payload.end_time)
    if end_t <= start_t:
        raise HTTPException(400, "The end time has to be after the start time")

    starts_at = dt.datetime.combine(day, start_t, tzinfo=LONDON_TZ).astimezone(dt.timezone.utc)
    ends_at = dt.datetime.combine(day, end_t, tzinfo=LONDON_TZ).astimezone(dt.timezone.utc)
    return {
        "title": title[:120],
        "description": (payload.description or "").strip()[:4000],
        "location": (payload.location or "").strip()[:200],
        "date": payload.date,
        "start_time": payload.start_time,
        "end_time": payload.end_time,
        "starts_at": starts_at,
        "ends_at": ends_at,
        "capacity": payload.capacity,
        "show_attendance": bool(payload.show_attendance),
        "signup_mode": payload.signup_mode,
    }


async def _is_reviewer(user: Optional[User]) -> bool:
    """Admins and Analyst members — the people who can see and decide on
    sign-ups. Same definition as the applications review page."""
    if user is None:
        return False
    if user.is_admin:
        return True
    doc = await db_module.db.collection("users").document(str(user.id)).get()
    data = doc.to_dict() if doc.exists else {}
    return mb.membership_of(data) in mb.ANALYST_MEMBERSHIPS


async def optional_user(
    request: Request,
    creds: HTTPAuthorizationCredentials = Depends(http_bearer),
) -> Optional[User]:
    """The signed-in user, or None — the events list is public."""
    try:
        return await current_user(request, creds)
    except HTTPException:
        return None


async def _load_event(event_id: str) -> Dict[str, Any]:
    doc = await db_module.db.collection(EVENTS).document(event_id).get()
    if not doc.exists:
        raise HTTPException(404, "No such event")
    return {"id": doc.id, **(doc.to_dict() or {})}


async def _signups_of(event_id: str) -> List[Dict[str, Any]]:
    docs = await db_module.db.collection(SIGNUPS).where("event_id", "==", event_id).get()
    return [d.to_dict() or {} for d in docs]


def _confirmed(signups: List[Dict[str, Any]]) -> int:
    return sum(1 for s in signups if s.get("status") == ST_CONFIRMED)


def _is_full(event: Dict[str, Any], confirmed: int) -> bool:
    cap = event.get("capacity")
    return bool(cap) and confirmed >= cap


def _event_view(event: Dict[str, Any], signups: List[Dict[str, Any]],
                viewer_id: Optional[str], can_manage: bool,
                fast_track_taken: int = 0) -> Dict[str, Any]:
    starts_at, ends_at = _as_utc(event.get("starts_at")), _as_utc(event.get("ends_at"))
    confirmed = _confirmed(signups)
    mine = next((s for s in signups if viewer_id and s.get("user_id") == viewer_id), None)
    # Numbers are shown to everyone only if the admin chose that; reviewers
    # always see them, since they're the ones managing the list.
    show_numbers = bool(event.get("show_attendance")) or can_manage
    view = {
        "id": event["id"],
        "title": event.get("title", ""),
        "description": event.get("description", ""),
        "location": event.get("location", ""),
        "date": event.get("date"),
        "start_time": event.get("start_time"),
        "end_time": event.get("end_time"),
        "when_label": outreach.when_label(starts_at, ends_at),
        "date_label": outreach.date_label(starts_at),
        "time_label": outreach.time_label(starts_at, ends_at),
        "starts_at": starts_at.isoformat(),
        "is_past": ends_at <= _now(),
        "signup_mode": event.get("signup_mode", MODE_FIRST_COME),
        "show_attendance": bool(event.get("show_attendance")),
        "capacity": event.get("capacity") if show_numbers else None,
        "attending": confirmed if show_numbers else None,
        "pending": sum(1 for s in signups if s.get("status") == ST_PENDING) if can_manage else None,
        "full": _is_full(event, confirmed),
        "my_status": mine.get("status") if mine else None,
    }
    if _is_outreach(event):
        # Two tickets instead of a plain sign-up. How many Fast-Track places
        # are left is shown to everyone regardless of the attendance setting —
        # it's the thing people are deciding on.
        cap = outreach.FAST_TRACK_CAPACITY
        view["tickets"] = [
            {"key": outreach.EVENT_TICKET_FAST_TRACK,
             "label": outreach.TICKET_LABELS[outreach.EVENT_TICKET_FAST_TRACK],
             "capacity": cap, "remaining": max(0, cap - fast_track_taken),
             "full": fast_track_taken >= cap},
            {"key": outreach.EVENT_TICKET_GENERAL,
             "label": outreach.TICKET_LABELS[outreach.EVENT_TICKET_GENERAL]},
        ]
        view["my_ticket"] = mine.get("ticket") if mine and mine.get("status") == ST_CONFIRMED else None
        view["full"] = False
    return view


# ── Page ─────────────────────────────────────────────────────────────────────

@router.get("", include_in_schema=False)
async def events_page(request: Request):
    return templates.TemplateResponse("events.html", {"request": request, "app_name": "AlphaBook"})


# ── Public list ──────────────────────────────────────────────────────────────

@router.get("/list")
async def list_events(user: Optional[User] = Depends(optional_user)):
    can_manage = await _is_reviewer(user)
    viewer_id = str(user.id) if user else None

    by_event: Dict[str, List[Dict[str, Any]]] = {}
    for d in await db_module.db.collection(SIGNUPS).get():
        s = d.to_dict() or {}
        by_event.setdefault(s.get("event_id"), []).append(s)

    event_docs = await db_module.db.collection(EVENTS).get()
    taken = 0
    if any((d.to_dict() or {}).get("kind") == "outreach" for d in event_docs):
        taken = len(await outreach.fast_track_holders())
    events = [
        _event_view({"id": d.id, **(d.to_dict() or {})}, by_event.get(d.id, []), viewer_id, can_manage, taken)
        for d in event_docs
    ]
    # Upcoming first, soonest at the top; finished ones after, most recent first.
    upcoming = sorted((e for e in events if not e["is_past"]), key=lambda e: e["starts_at"])
    past = sorted((e for e in events if e["is_past"]), key=lambda e: e["starts_at"], reverse=True)
    return {
        "events": upcoming + past,
        "viewer": {"username": user.username} if user else None,
        "is_admin": bool(user and user.is_admin),
        "can_manage": can_manage,
    }


# ── Admin: create / edit / delete ────────────────────────────────────────────

@router.post("/create")
async def create_event(payload: EventPayload, admin: User = Depends(require_admin)):
    fields = _parse_event(payload)
    event_id = uuid.uuid4().hex[:10]
    await db_module.db.collection(EVENTS).document(event_id).set({
        **fields, "created_by": admin.username, "created_at": _now(),
    })
    return {"ok": True, "id": event_id}


@router.put("/{event_id}")
async def update_event(event_id: str, payload: EventPayload, admin: User = Depends(require_admin)):
    event = await _load_event(event_id)
    fields = _parse_event(payload)
    if _is_outreach(event):
        # Places and sign-up are the ticket system's, not this form's.
        fields.pop("capacity")
        fields.pop("signup_mode")
    await db_module.db.collection(EVENTS).document(event_id).update(fields)
    return {"ok": True}


@router.delete("/{event_id}")
async def delete_event(event_id: str, admin: User = Depends(require_admin)):
    if _is_outreach(await _load_event(event_id)):
        raise HTTPException(400, "Quant Outreach is tied to the application form, so it can't be deleted — edit it instead")
    for d in await db_module.db.collection(SIGNUPS).where("event_id", "==", event_id).get():
        await d.reference.delete()
    await db_module.db.collection(EVENTS).document(event_id).delete()
    return {"ok": True}


# ── Sign-up ──────────────────────────────────────────────────────────────────

def _signup_ref(event_id: str, user_id: str):
    return db_module.db.collection(SIGNUPS).document(f"{event_id}_{user_id}")


@router.post("/{event_id}/signup")
async def sign_up(event_id: str, user: User = Depends(current_user)):
    event = await _load_event(event_id)
    if _is_outreach(event):
        raise HTTPException(400, "Choose a ticket for this event")
    ends_at = _as_utc(event.get("ends_at"))
    if ends_at and ends_at <= _now():
        raise HTTPException(400, "This event has already finished")

    uid = str(user.id)
    ref = _signup_ref(event_id, uid)
    existing = await ref.get()
    if existing.exists:
        status = (existing.to_dict() or {}).get("status")
        if status == ST_DECLINED:
            raise HTTPException(400, "Your request for this event wasn't approved")
        raise HTTPException(400, "You're already signed up for this event")

    if _is_full(event, _confirmed(await _signups_of(event_id))):
        raise HTTPException(400, "This event is full")

    udoc = await db_module.db.collection("users").document(uid).get()
    udata = udoc.to_dict() if udoc.exists else {}
    status = ST_CONFIRMED if event.get("signup_mode", MODE_FIRST_COME) == MODE_FIRST_COME else ST_PENDING
    await ref.set({
        "event_id": event_id,
        "user_id": uid,
        "username": user.username,
        "full_name": udata.get("full_name") or "",
        "email": udata.get("email") or "",
        "status": status,
        "created_at": _now(),
    })
    return {"ok": True, "status": status}


async def _set_outreach_ticket(user: User, ticket: str) -> None:
    uid = str(user.id)
    # An application that's already been fast-tracked owns that ticket now.
    if ticket != outreach.EVENT_TICKET_FAST_TRACK and await event_ticket_locked(uid):
        raise HTTPException(400, "You've already been fast-tracked through your application, "
                                 "so this ticket can't be changed here")
    # ...and one that took the ordinary route can't grab a Fast-Track place
    # from here partway through (it wouldn't skip anything, only use up one
    # of the 50).
    if ticket == outreach.EVENT_TICKET_FAST_TRACK:
        refusal = await fast_track_refusal(uid)
        if refusal:
            raise HTTPException(400, refusal)
    await outreach.set_ticket(uid, user.username, ticket)
    await sync_event_ticket(uid, ticket)


@router.post("/{event_id}/ticket")
async def choose_ticket(event_id: str, payload: TicketChoice, user: User = Depends(current_user)):
    event = await _load_event(event_id)
    if not _is_outreach(event):
        raise HTTPException(400, "This event doesn't have tickets")
    ends_at = _as_utc(event.get("ends_at"))
    if ends_at and ends_at <= _now():
        raise HTTPException(400, "This event has already finished")
    await _set_outreach_ticket(user, payload.ticket)
    return {"ok": True, "ticket": payload.ticket}


@router.delete("/{event_id}/signup")
async def cancel_signup(event_id: str, user: User = Depends(current_user)):
    if _is_outreach(await _load_event(event_id)):
        await _set_outreach_ticket(user, outreach.EVENT_TICKET_NONE)
        return {"ok": True}
    ref = _signup_ref(event_id, str(user.id))
    if not (await ref.get()).exists:
        raise HTTPException(404, "You're not signed up for this event")
    await ref.delete()
    return {"ok": True}


# ── Reviewers: see and decide on sign-ups ────────────────────────────────────

@router.get("/{event_id}/signups")
async def list_signups(event_id: str, reviewer: User = Depends(require_reviewer)):
    await _load_event(event_id)
    rows = sorted(await _signups_of(event_id), key=lambda s: _as_utc(s.get("created_at")) or _now())
    # Names from profiles as they are now, not as they were at sign-up.
    names = {d.id: ((d.to_dict() or {}).get("full_name") or "").strip()
             for d in await db_module.db.collection("users").get()}
    return {"signups": [{
        "user_id": s.get("user_id"),
        "name": names.get(s.get("user_id")) or s.get("full_name") or s.get("username") or "?",
        "username": s.get("username") or "",
        "email": s.get("email") or "",
        "status": s.get("status"),
        "ticket": outreach.TICKET_LABELS.get(s.get("ticket")) if s.get("ticket") else None,
        "decided_by": s.get("decided_by") or "",
    } for s in rows]}


@router.post("/{event_id}/signups/{user_id}/decision")
async def decide_signup(event_id: str, user_id: str, payload: Decision,
                        reviewer: User = Depends(require_reviewer)):
    if payload.decision not in ("approve", "decline"):
        raise HTTPException(400, "Decision must be approve or decline")
    event = await _load_event(event_id)
    if _is_outreach(event):
        raise HTTPException(400, "Quant Outreach places aren't approved — people choose their own ticket")
    ref = _signup_ref(event_id, user_id)
    doc = await ref.get()
    if not doc.exists:
        raise HTTPException(404, "No such sign-up")

    current = (doc.to_dict() or {}).get("status")
    if payload.decision == "approve":
        if current != ST_CONFIRMED and _is_full(event, _confirmed(await _signups_of(event_id))):
            raise HTTPException(400, "This event is full — there's no place left to give")
        status = ST_CONFIRMED
    else:
        status = ST_DECLINED

    await ref.update({"status": status, "decided_by": reviewer.username, "decided_at": _now()})
    return {"ok": True, "status": status}
