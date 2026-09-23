"""
Google Meet links for interviews, via the Calendar API.
=========================================================

A Calendar event on oxfordalphafund@gmail.com's own calendar is created (or,
on a reschedule, moved) purely to mint a Meet link. Attendees are never sent
Google's own invite for it (``sendUpdates=none``) — AlphaBook's own email
with its own .ics attachment is the actual invite; this just supplies the
link that goes inside it.

Authenticated as that one mailbox via a long-lived refresh token: a personal
Gmail account has no service-account / domain-wide-delegation equivalent, so
real user consent is required. The token is obtained once, outside this app
(see the setup script), and stored like any other secret (``SMTP_PASSWORD``
is configured the same way).

Every function here fails soft: unconfigured, or a bad response from Google,
just means no Meet link — an interview still gets scheduled, exactly like
the mailer no-ops without SMTP configured.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import uuid
from typing import Any, Dict, List, Optional

import httpx

log = logging.getLogger("uvicorn.error")

CLIENT_ID = os.getenv("GOOGLE_OAUTH_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", "")
REFRESH_TOKEN = os.getenv("GOOGLE_OAUTH_REFRESH_TOKEN", "")
# The calendar the event is created on — "primary" is the mailbox's own
# calendar, which is all a single Gmail account has.
CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID", "primary")

CONFIGURED = bool(CLIENT_ID and CLIENT_SECRET and REFRESH_TOKEN)

TOKEN_URL = "https://oauth2.googleapis.com/token"
_EVENTS_URL = f"https://www.googleapis.com/calendar/v3/calendars/{CALENDAR_ID}/events"

# Module-level so every call in the process reuses the same access token
# until it's about to expire, instead of trading a fresh one for every
# interview scheduled.
_token_cache: Dict[str, Any] = {"access_token": None, "expires_at": None}


async def _access_token() -> Optional[str]:
    now = dt.datetime.now(dt.timezone.utc)
    cached, expires_at = _token_cache["access_token"], _token_cache["expires_at"]
    if cached and expires_at and now < expires_at:
        return cached
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(TOKEN_URL, data={
                "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
                "refresh_token": REFRESH_TOKEN, "grant_type": "refresh_token",
            })
            r.raise_for_status()
            data = r.json()
    except Exception:
        log.exception("gcal: failed to refresh access token")
        return None
    _token_cache["access_token"] = data.get("access_token")
    _token_cache["expires_at"] = now + dt.timedelta(seconds=int(data.get("expires_in", 3600)) - 60)
    return _token_cache["access_token"]


def _rfc3339(when: dt.datetime) -> str:
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    return when.astimezone(dt.timezone.utc).isoformat()


async def create_meet_event(*, summary: str, description: str, start: dt.datetime,
                             end: dt.datetime, attendee_emails: List[str]) -> Optional[Dict[str, str]]:
    """Create a Calendar event with a Google Meet link attached.

    Returns ``{"event_id": ..., "meet_link": ...}``, or ``None`` if this
    isn't configured, or Google refused/failed the request.
    """
    if not CONFIGURED:
        return None
    token = await _access_token()
    if not token:
        return None
    body = {
        "summary": summary,
        "description": description,
        "start": {"dateTime": _rfc3339(start)},
        "end": {"dateTime": _rfc3339(end)},
        "attendees": [{"email": e} for e in attendee_emails if e],
        "conferenceData": {
            "createRequest": {
                "requestId": uuid.uuid4().hex,
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            },
        },
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                _EVENTS_URL, params={"conferenceDataVersion": 1, "sendUpdates": "none"},
                headers={"Authorization": f"Bearer {token}"}, json=body,
            )
            r.raise_for_status()
            data = r.json()
    except Exception:
        log.exception("gcal: failed to create event")
        return None
    link = _meet_link_from(data)
    if not link:
        return None
    return {"event_id": data.get("id", ""), "meet_link": link}


def _meet_link_from(event: Dict[str, Any]) -> Optional[str]:
    if event.get("hangoutLink"):
        return event["hangoutLink"]
    for point in (event.get("conferenceData", {}) or {}).get("entryPoints") or []:
        if point.get("entryPointType") == "video" and point.get("uri"):
            return point["uri"]
    return None


async def update_event_time(event_id: str, start: dt.datetime, end: dt.datetime) -> bool:
    """Move an existing event to a new time — used when an interview is
    re-proposed, so a Meet link a candidate may already have stays valid
    instead of a fresh one being minted (and a stray old event left behind)
    on every reschedule."""
    if not CONFIGURED or not event_id:
        return False
    token = await _access_token()
    if not token:
        return False
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.patch(
                f"{_EVENTS_URL}/{event_id}", params={"sendUpdates": "none"},
                headers={"Authorization": f"Bearer {token}"},
                json={"start": {"dateTime": _rfc3339(start)}, "end": {"dateTime": _rfc3339(end)}},
            )
            r.raise_for_status()
        return True
    except Exception:
        log.exception("gcal: failed to move event %s", event_id)
        return False
