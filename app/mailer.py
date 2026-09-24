"""
Outbound email — application confirmations and admin reminders.
================================================================

A thin wrapper over SMTP, configured entirely by environment variables so it
works with whatever mail account or relay AlphaBook is pointed at (Gmail,
Oxford's own SMTP, a transactional-email provider's SMTP endpoint — all speak
SMTP). There is no vendor SDK and no new account created by this code; the
operator supplies credentials for a mailbox they already control.

Also sends the sign-up verification email: the link comes from Firebase, but
the email around it is ours (see ``auth._send_branded_verification_email``).

Two ways out, tried in order:

1. **Gmail API, as oxfordalphafund@gmail.com** — using the same Google
   connection that mints Meet links (:mod:`app.gcal`), when that connection
   was granted the ``gmail.send`` permission. No app password needed.
2. **SMTP** — whatever mailbox ``SMTP_*`` points at. Used if the Gmail route
   isn't set up, isn't permitted, or fails for a given message, so an email
   is never lost just because one route is down.

If neither is configured, ``send_email`` logs and returns False instead of
raising. That keeps local dev and the test suite working with no mail server
configured, and keeps a misconfigured mailer from taking down the request
that triggered the email — a candidate finishing their OA should never see a
500 because a reminder email couldn't be sent.
"""
from __future__ import annotations

import asyncio
import base64
import datetime as dt
import logging
import os
import smtplib
from email import utils as email_utils
from email.message import EmailMessage
from email.utils import formataddr
from typing import List, Optional, Tuple

import httpx

from app import gcal

log = logging.getLogger("uvicorn.error")

SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM = os.getenv("SMTP_FROM") or SMTP_USER
SMTP_FROM_NAME = os.getenv("SMTP_FROM_NAME", "AlphaBook")
# STARTTLS is right for the common case (port 587); an implicit-TLS relay on
# 465 sets this to false and connects over SSL from the start instead.
SMTP_USE_STARTTLS = os.getenv("SMTP_USE_STARTTLS", "true").lower() not in ("0", "false", "no")

CONFIGURED = bool(SMTP_HOST and SMTP_FROM)



def _wrap(title: str, body_html: str, cta_label: Optional[str] = None, cta_url: Optional[str] = None) -> str:
    """A minimal, inbox-safe HTML shell: the title, the body, and an optional
    button — nothing else.

    There's deliberately no logo header. The image is remote, so most clients
    block it by default and show an empty bordered box with alt text in its
    place, which looked broken; the sender name already says who it's from.
    """
    cta = ""
    if cta_label and cta_url:
        cta = f'''
        <p style="margin:28px 0 0;">
          <a href="{cta_url}" style="display:inline-block;background:#1B75BC;color:#fff;
             text-decoration:none;padding:11px 22px;font-weight:600;font-size:14px;">
            {cta_label}
          </a>
        </p>'''
    return f"""
    <div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;
                max-width:520px;margin:0 auto;color:#1a1a1a;">
      <h2 style="margin:0 0 16px;font-size:20px;">{title}</h2>
      <div style="font-size:15px;line-height:1.6;">{body_html}</div>
      {cta}
    </div>"""


def _ics_stamp(when: dt.datetime) -> str:
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    return when.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _ics_escape(text: str) -> str:
    # RFC 5545 §3.3.11: backslash, semicolon, comma and newline are escaped.
    return (text or "").replace("\\", "\\\\").replace(";", "\\;") \
        .replace(",", "\\,").replace("\n", "\\n")


def build_ics_invite(
    *, uid: str, summary: str, description: str, start: dt.datetime, end: dt.datetime,
    organizer_name: str, organizer_email: str, attendee_name: str, attendee_email: str,
    location: str = "Online, details to follow",
    uid_domain: Optional[str] = "alphabook.uk",
    method: str = "REQUEST", sequence: int = 0,
    more_attendees: Optional[List[Tuple[str, str]]] = None,
) -> bytes:
    """
    A minimal RFC 5545 VEVENT, valid enough for Gmail/Outlook/Apple Calendar
    to offer an "Add to calendar" prompt. No external library — the format
    is simple enough that hand-writing it is less risk than a new dependency
    for one small feature.

    ``uid_domain=None`` uses ``uid`` exactly as given, for when it's a real
    Google Calendar event's iCalUID (which already carries its own domain).
    """
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//AlphaBook//Interview Scheduling//EN",
        "CALSCALE:GREGORIAN",
        f"METHOD:{method}",
        "BEGIN:VEVENT",
        f"UID:{uid}@{uid_domain}" if uid_domain else f"UID:{uid}",
        f"DTSTAMP:{_ics_stamp(dt.datetime.now(dt.timezone.utc))}",
        f"DTSTART:{_ics_stamp(start)}",
        f"DTEND:{_ics_stamp(end)}",
        f"SUMMARY:{_ics_escape(summary)}",
        f"DESCRIPTION:{_ics_escape(description)}",
        f"LOCATION:{_ics_escape(location)}",
        f"ORGANIZER;CN={_ics_escape(organizer_name)}:mailto:{organizer_email}",
        f"ATTENDEE;CN={_ics_escape(attendee_name)};ROLE=REQ-PARTICIPANT:mailto:{attendee_email}",
        *(f"ATTENDEE;CN={_ics_escape(n)};ROLE=REQ-PARTICIPANT:mailto:{e}"
          for n, e in (more_attendees or []) if e),
        "STATUS:CONFIRMED",
        f"SEQUENCE:{int(sequence)}",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")


def _build_message(sender: str, to: str, subject: str, html: str, text: str,
                   ics: Optional[bytes] = None, cc: Optional[str] = None) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((SMTP_FROM_NAME, sender))
    msg["To"] = to
    if cc:
        # A real Cc header, not a second send — smtplib delivers to every
        # address it finds across To/Cc/Bcc from one send_message() call, and
        # having both people actually on the same message is the point: it's
        # one thread either of them can reply-all on to reach the other.
        msg["Cc"] = cc
    # Gmail's own relay fills these in for mail sent through its web/app
    # clients, but smtplib doesn't add them for us — and their absence is
    # itself a spam signal, since every legitimate mail server stamps both.
    msg["Date"] = email_utils.formatdate(localtime=True)
    msg["Message-ID"] = email_utils.make_msgid(domain=sender.split("@")[-1] or "alphabook.uk")
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")

    if ics:
        msg.add_attachment(ics, maintype="text", subtype="calendar", filename="interview.ics")
        part = msg.get_payload()[-1]
        part.set_param("method", "REQUEST")
        part.set_param("name", "interview.ics")
    return msg


def _send_sync(to: str, subject: str, html: str, text: str,
                ics: Optional[bytes] = None, cc: Optional[str] = None) -> bool:
    msg = _build_message(SMTP_FROM, to, subject, html, text, ics, cc)
    try:
        if SMTP_USE_STARTTLS:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as s:
                s.starttls()
                if SMTP_USER:
                    s.login(SMTP_USER, SMTP_PASSWORD)
                s.send_message(msg)
        else:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=10) as s:
                if SMTP_USER:
                    s.login(SMTP_USER, SMTP_PASSWORD)
                s.send_message(msg)
        return True
    except Exception:
        log.exception("mailer: failed to send %r to %s", subject, to)
        return False


GMAIL_SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
# Flips on the first "not allowed" answer from Google (the connection was
# granted calendar access only) so every later email goes straight to SMTP
# instead of asking again. Reset by a redeploy, which is what updating the
# token env var causes anyway.
_gmail_state = {"refused": False}


def gmail_enabled() -> bool:
    return gcal.CONFIGURED and not _gmail_state["refused"]


async def _send_via_gmail(to: str, subject: str, html: str, text: str,
                          ics: Optional[bytes], cc: Optional[str]) -> bool:
    if not gmail_enabled():
        return False
    token = await gcal.access_token()
    if not token:
        return False
    msg = _build_message(gcal.ACCOUNT_EMAIL, to, subject, html, text, ics, cc)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(GMAIL_SEND_URL, headers={"Authorization": f"Bearer {token}"},
                                  json={"raw": raw})
        if r.status_code in (401, 403):
            _gmail_state["refused"] = True
            log.warning("mailer: Google refused gmail.send (HTTP %s) — the connection has no "
                        "email permission; using SMTP from now on", r.status_code)
            return False
        r.raise_for_status()
        return True
    except Exception:
        log.exception("mailer: Gmail API send failed for %r to %s — falling back to SMTP", subject, to)
        return False


def sender() -> Optional[str]:
    """The address emails are currently going out from, or None if neither
    route is set up. The Gmail route is reported optimistically until Google
    has actually refused it once."""
    if gmail_enabled():
        return gcal.ACCOUNT_EMAIL
    return SMTP_FROM if CONFIGURED else None


async def deliver(to: str, subject: str, title: str, body_html: str,
                  cta_label: Optional[str] = None, cta_url: Optional[str] = None,
                  ics: Optional[bytes] = None, cc: Optional[str] = None) -> Optional[str]:
    """Send one email; return the address it went out from, or None if it
    didn't. Never raises."""
    if not to or "@" not in to:
        log.warning("mailer: refusing to send %r to invalid address %r", subject, to)
        return None
    if not CONFIGURED and not gmail_enabled():
        log.warning("mailer: no email route configured — skipping %r to %s", subject, to)
        return None

    html = _wrap(title, body_html, cta_label, cta_url)
    # A plain-text fallback derived from the label/body, not a full HTML strip
    # — every caller's body_html here is short enough that this reads fine.
    # Entities are decoded after the tags go, so a name escaped for the HTML
    # part (see applications.py) reads normally here rather than as "&lt;".
    import re
    from html import unescape
    text = re.sub(r"<[^>]+>", " ", body_html)
    text = unescape(re.sub(r"\s+", " ", text).strip())
    if cta_label and cta_url:
        text += f"\n\n{cta_label}: {cta_url}"

    if await _send_via_gmail(to, subject, html, text, ics, cc):
        return gcal.ACCOUNT_EMAIL
    if not CONFIGURED:
        return None
    return SMTP_FROM if await asyncio.to_thread(_send_sync, to, subject, html, text, ics, cc) else None


async def send_email(to: str, subject: str, title: str, body_html: str,
                     cta_label: Optional[str] = None, cta_url: Optional[str] = None,
                     ics: Optional[bytes] = None, cc: Optional[str] = None) -> bool:
    """Send one email. Never raises — returns whether it actually went out."""
    return await deliver(to, subject, title, body_html, cta_label, cta_url, ics, cc) is not None
