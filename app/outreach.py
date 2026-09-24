"""
The Quant Outreach event — the one event that's tied to the application.
=========================================================================

Everything else on the events page is a plain event with plain sign-up
(:mod:`app.events`). Quant Outreach is different: its sign-up doubles as the
first step of the Quant Bootcamp application, and comes with two tickets —

* **CV clinic + Fast-Track** — capped (``FAST_TRACK_CAPACITY``). An analyst
  reviews the CV in person and the applicant skips the written assessment.
* **General attendance** — no cap, no Fast-Track.

…or neither ("not attending"), which is simply no sign-up.

The sign-up itself is the source of truth, stored like any other event
sign-up (``event_signups/{event}_{user}``, with a ``ticket`` field) so it
exists independently of any application: someone can register on the events
page first and the application then picks it up, or choose on the
application and have it show on the events page. This module holds the
pieces both sides share so neither has to import the other.
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Dict, List, Optional, Set
from zoneinfo import ZoneInfo

from fastapi import HTTPException

from app import db as db_module

OUTREACH_EVENT_ID = "quant-outreach"
EVENTS = "events"
SIGNUPS = "event_signups"
APPLICATIONS = "applications"
LONDON_TZ = ZoneInfo("Europe/London")

EVENT_TICKET_NONE = "none"
EVENT_TICKET_GENERAL = "general"
EVENT_TICKET_FAST_TRACK = "fast_track"
EVENT_TICKETS = {EVENT_TICKET_NONE, EVENT_TICKET_GENERAL, EVENT_TICKET_FAST_TRACK}
FAST_TRACK_CAPACITY = 50

TICKET_LABELS = {
    EVENT_TICKET_FAST_TRACK: "CV clinic + Fast-Track",
    EVENT_TICKET_GENERAL: "General attendance",
}

_DESCRIPTION = (
    "The Quant Outreach Event is designed to introduce students to quantitative finance, "
    "showcase how involvement in Oxford Alpha Fund can support their professional "
    "development, and provide an accelerated pathway into the fund. The event involves "
    "senior OAF members sharing their experiences in securing internships, the skills and "
    "lessons they gained through their involvement in the fund, and how working with other "
    "members contributed to their development.\n\n"
    "The event also includes a CV review and fast-track recruitment component, where "
    "participants receive direct feedback on their CVs and speak with OAF members through "
    "a condensed assessment process. Candidates who perform strongly may receive an "
    "accelerated recruitment decision, while other promising candidates may be placed "
    "under further consideration. Participants progressing through this route can bypass "
    "the initial online application and assessment stages of the standard recruitment "
    "process."
)
_LOCATION = "Fitzhugh Auditorium, Cohen Quad, Exeter College"


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _clock(t: dt.datetime) -> str:
    """5:30, 7:00 — 12-hour, no am/pm (time_label adds that)."""
    return f"{t.hour % 12 or 12}:{t.minute:02d}"


def date_label(starts_at: dt.datetime) -> str:
    """"Sunday 11 October 2026" — the event's own (UK) calendar date."""
    s = starts_at.astimezone(LONDON_TZ)
    return f"{s:%A} {s.day} {s:%B %Y}"


def time_label(starts_at: dt.datetime, ends_at: dt.datetime) -> str:
    """"5:30–7:00 pm", or "11:00 am–1:00 pm" across noon. Shown as the
    wall-clock time in Oxford, with no time-zone suffix: every event is
    in person here, and a bare "London" next to the time read as if the
    event itself were in London."""
    s, e = starts_at.astimezone(LONDON_TZ), ends_at.astimezone(LONDON_TZ)
    s_half, e_half = ("am" if s.hour < 12 else "pm"), ("am" if e.hour < 12 else "pm")
    if s_half == e_half:
        return f"{_clock(s)}–{_clock(e)} {e_half}"
    return f"{_clock(s)} {s_half}–{_clock(e)} {e_half}"


def when_label(starts_at: dt.datetime, ends_at: dt.datetime) -> str:
    """Date and time on one line, for places with room for only one."""
    return f"{date_label(starts_at)}, {time_label(starts_at, ends_at)}"


def _ref(uid: str):
    return db_module.db.collection(SIGNUPS).document(f"{OUTREACH_EVENT_ID}_{uid}")


# Bumped whenever the details above are deliberately changed in code and the
# live event should pick them up. ensure_event() applies them once to an
# event stored at an older revision, then leaves it to admins again.
CONTENT_REVISION = 2


def _content() -> Dict[str, Any]:
    day = dt.date(2026, 10, 11)
    starts = dt.datetime.combine(day, dt.time(17, 30), tzinfo=LONDON_TZ).astimezone(dt.timezone.utc)
    ends = dt.datetime.combine(day, dt.time(19, 0), tzinfo=LONDON_TZ).astimezone(dt.timezone.utc)
    return {
        "title": "Quant Outreach",
        "description": _DESCRIPTION,
        "location": _LOCATION,
        "date": day.isoformat(),
        "start_time": "17:30",
        "end_time": "19:00",
        "starts_at": starts,
        "ends_at": ends,
        "content_revision": CONTENT_REVISION,
    }


async def ensure_event() -> None:
    """Create the event if it doesn't exist yet. Otherwise an admin owns the
    details (title, description, location, date and times) and they're left
    alone — except once per CONTENT_REVISION bump, when the details written
    here replace whatever is stored. That's how a change made in code reaches
    the live event without anyone re-typing it on the Events page. Sign-ups
    live in their own collection, so they're untouched either way."""
    ref = db_module.db.collection(EVENTS).document(OUTREACH_EVENT_ID)
    doc = await ref.get()
    if doc.exists:
        if int((doc.to_dict() or {}).get("content_revision") or 1) < CONTENT_REVISION:
            await ref.update({**_content(), "updated_at": _now()})
        return
    await ref.set({
        "kind": "outreach",
        **_content(),
        "capacity": None,
        "show_attendance": False,
        "signup_mode": "first_come",
        "created_by": "system",
        "created_at": _now(),
    })


def _as_utc(value: Any) -> Optional[dt.datetime]:
    """Firestore hands back an aware datetime; tests and older docs may hold
    a naive one or an ISO string. Anything unreadable is None."""
    if isinstance(value, str):
        try:
            value = dt.datetime.fromisoformat(value)
        except ValueError:
            return None
    if not isinstance(value, dt.datetime):
        return None
    return value.replace(tzinfo=dt.timezone.utc) if value.tzinfo is None else value


def _has_ended(data: Dict[str, Any]) -> bool:
    ends = _as_utc(data.get("ends_at"))
    return bool(ends and ends <= _now())


async def event_has_ended() -> bool:
    """True once the event's ``ends_at`` has passed. Both tickets are only
    worth anything at the event itself — Fast-Track in particular is the CV
    clinic *on the night* — so once it's over, "not attending" is the only
    honest choice left, on the application just as on the events page. An
    event that doesn't exist (or has no end time) never counts as ended."""
    doc = await db_module.db.collection(EVENTS).document(OUTREACH_EVENT_ID).get()
    return doc.exists and _has_ended(doc.to_dict() or {})


async def event_summary() -> Optional[Dict[str, Any]]:
    """Title and time of the event, for the application to show alongside the
    choice. None if it doesn't exist (the application then just omits it).
    ``is_past`` lets the choice screen stop offering tickets for an event
    that has already happened."""
    doc = await db_module.db.collection(EVENTS).document(OUTREACH_EVENT_ID).get()
    if not doc.exists:
        return None
    data = doc.to_dict() or {}
    starts, ends = data.get("starts_at"), data.get("ends_at")
    return {
        "title": data.get("title", "Quant Outreach"),
        "when_label": when_label(starts, ends) if starts and ends else "",
        "date_label": date_label(starts) if starts else "",
        "time_label": time_label(starts, ends) if starts and ends else "",
        "location": data.get("location", ""),
        "is_past": _has_ended(data),
    }


async def confirmed_signups() -> List[Dict[str, Any]]:
    # A scan rather than a query: the collection is small (one society's
    # events), and it matches how the Fast-Track count has always been read.
    docs = await db_module.db.collection(SIGNUPS).get()
    return [s for s in (d.to_dict() or {} for d in docs)
            if s.get("event_id") == OUTREACH_EVENT_ID and s.get("status") == "confirmed"]


async def ticket_of(uid: str) -> Optional[str]:
    """This person's ticket ("fast_track" / "general"), or None if they
    haven't signed up."""
    doc = await _ref(uid).get()
    if not doc.exists:
        return None
    data = doc.to_dict() or {}
    return data.get("ticket") if data.get("status") == "confirmed" else None


async def fast_track_holders() -> Set[str]:
    """Everyone currently holding a Fast-Track place: an outreach sign-up
    with that ticket, or an application whose ticket says so. Either alone
    counts, so a place is never lost or double-counted while the two sides
    are catching up with each other."""
    holders = {s.get("user_id") for s in await confirmed_signups() if s.get("ticket") == EVENT_TICKET_FAST_TRACK}
    for d in await db_module.db.collection(APPLICATIONS).get():
        if (d.to_dict() or {}).get("event_ticket") == EVENT_TICKET_FAST_TRACK:
            holders.add(d.id)
    holders.discard(None)
    return holders


async def set_ticket(uid: str, username: str, ticket: str) -> None:
    """Make the sign-up match ``ticket``: create or change it, or remove it
    for "none". Claiming a *new* Fast-Track place is the only thing that
    competes for capacity — re-confirming your own, or moving off it, never
    does."""
    if ticket not in EVENT_TICKETS:
        raise HTTPException(400, "Unknown ticket type")
    ref = _ref(uid)

    if ticket == EVENT_TICKET_NONE:
        await ref.delete()
        return

    if ticket == EVENT_TICKET_FAST_TRACK:
        holders = await fast_track_holders()
        if uid not in holders and len(holders) >= FAST_TRACK_CAPACITY:
            raise HTTPException(400, "CV clinic + Fast-Track is full — choose General attendance instead")

    if (await ref.get()).exists:
        await ref.update({"ticket": ticket, "status": "confirmed", "updated_at": _now()})
        return

    udoc = await db_module.db.collection("users").document(uid).get()
    udata = (udoc.to_dict() or {}) if udoc.exists else {}
    await ref.set({
        "event_id": OUTREACH_EVENT_ID,
        "user_id": uid,
        "username": username,
        "full_name": udata.get("full_name") or "",
        "email": udata.get("email") or "",
        "status": "confirmed",
        "ticket": ticket,
        "created_at": _now(),
    })
