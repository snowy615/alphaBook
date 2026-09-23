"""
Outbound email — application confirmations and admin reminders.
================================================================

A thin wrapper over SMTP, configured entirely by environment variables so it
works with whatever mail account or relay AlphaBook is pointed at (Gmail,
Oxford's own SMTP, a transactional-email provider's SMTP endpoint — all speak
SMTP). There is no vendor SDK and no new account created by this code; the
operator supplies credentials for a mailbox they already control.

Deliberately not used for sign-in verification: Firebase Auth already sends
that email itself (``sendEmailVerification`` in the client SDK, using
Firebase's own templates and deliverability), so duplicating it here would
just be a second, worse copy of the same email.

If ``SMTP_HOST`` is unset, ``send_email`` logs and returns False instead of
raising. That keeps local dev and the test suite working with no mail server
configured, and keeps a misconfigured mailer from taking down the request
that triggered the email — a candidate finishing their OA should never see a
500 because a reminder email couldn't be sent.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import smtplib
from email import utils as email_utils
from email.message import EmailMessage
from email.utils import formataddr
from typing import Optional

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

# Where the logo image is fetched from — must be an absolute URL since email
# clients render with no page context to resolve a relative one against.
# This is a *separate* asset from the one used on the site: the current
# wordmark flattened onto an opaque white background (228x84, 2x the display
# size below) rather than left transparent, because Outlook's desktop
# renderer (the Word engine) is unreliable with alpha-transparent PNGs and
# can fail to display them at all.
BASE_URL = os.getenv("APP_BASE_URL", "https://alphabook.uk").rstrip("/")
LOGO_URL = f"{BASE_URL}/static/alphabook_email.png"
LOGO_W, LOGO_H = 114, 42


def _wrap(title: str, body_html: str, cta_label: Optional[str] = None, cta_url: Optional[str] = None) -> str:
    """A minimal, inbox-safe HTML shell.

    The header is a <table>, not a flex div — Outlook's desktop renderer
    doesn't support flexbox at all, so a flex row can silently collapse or
    reorder there. A <table> with valign is the one layout primitive every
    mail client, Outlook included, has always supported.
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
      <table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin-bottom:22px;">
        <tr>
          <td style="padding:0;">
            <img src="{LOGO_URL}" alt="AlphaBook" width="{LOGO_W}" height="{LOGO_H}"
                 style="width:{LOGO_W}px;height:{LOGO_H}px;display:block;border:0;">
          </td>
          <td style="padding:0 0 0 10px;font-weight:400;font-size:13px;color:#888;vertical-align:middle;">
            &middot; Alpha Fund
          </td>
        </tr>
      </table>
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
    location: str = "Online — details to follow",
) -> bytes:
    """
    A minimal RFC 5545 VEVENT, valid enough for Gmail/Outlook/Apple Calendar
    to offer an "Add to calendar" prompt. No external library — the format
    is simple enough that hand-writing it is less risk than a new dependency
    for one small feature.
    """
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//AlphaBook//Interview Scheduling//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:REQUEST",
        "BEGIN:VEVENT",
        f"UID:{uid}@alphabook.uk",
        f"DTSTAMP:{_ics_stamp(dt.datetime.now(dt.timezone.utc))}",
        f"DTSTART:{_ics_stamp(start)}",
        f"DTEND:{_ics_stamp(end)}",
        f"SUMMARY:{_ics_escape(summary)}",
        f"DESCRIPTION:{_ics_escape(description)}",
        f"LOCATION:{_ics_escape(location)}",
        f"ORGANIZER;CN={_ics_escape(organizer_name)}:mailto:{organizer_email}",
        f"ATTENDEE;CN={_ics_escape(attendee_name)};ROLE=REQ-PARTICIPANT:mailto:{attendee_email}",
        "STATUS:CONFIRMED",
        "SEQUENCE:0",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")


def _send_sync(to: str, subject: str, html: str, text: str,
                ics: Optional[bytes] = None, cc: Optional[str] = None) -> bool:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((SMTP_FROM_NAME, SMTP_FROM))
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
    msg["Message-ID"] = email_utils.make_msgid(domain=SMTP_FROM.split("@")[-1] or "alphabook.uk")
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")

    if ics:
        msg.add_attachment(ics, maintype="text", subtype="calendar", filename="interview.ics")
        part = msg.get_payload()[-1]
        part.set_param("method", "REQUEST")
        part.set_param("name", "interview.ics")

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


async def send_email(to: str, subject: str, title: str, body_html: str,
                      cta_label: Optional[str] = None, cta_url: Optional[str] = None,
                      ics: Optional[bytes] = None, cc: Optional[str] = None) -> bool:
    """Send one email. Never raises — returns whether it actually went out."""
    if not to or "@" not in to:
        log.warning("mailer: refusing to send %r to invalid address %r", subject, to)
        return False
    if not CONFIGURED:
        log.warning("mailer: SMTP_HOST not set — skipping %r to %s", subject, to)
        return False

    html = _wrap(title, body_html, cta_label, cta_url)
    # A plain-text fallback derived from the label/body, not a full HTML strip
    # — every caller's body_html here is short enough that this reads fine.
    import re
    text = re.sub(r"<[^>]+>", " ", body_html)
    text = re.sub(r"\s+", " ", text).strip()
    if cta_label and cta_url:
        text += f"\n\n{cta_label}: {cta_url}"

    return await asyncio.to_thread(_send_sync, to, subject, html, text, ics, cc)
