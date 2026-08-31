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
import logging
import os
import smtplib
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


def _wrap(title: str, body_html: str, cta_label: Optional[str] = None, cta_url: Optional[str] = None) -> str:
    """A minimal, inbox-safe HTML shell — table-free is fine here since this
    is read in modern mail clients, not Outlook 2007."""
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
      <div style="font-weight:700;font-size:13px;letter-spacing:0.04em;
                  text-transform:uppercase;color:#1B75BC;margin-bottom:18px;">
        AlphaBook &middot; Alpha Fund
      </div>
      <h2 style="margin:0 0 16px;font-size:20px;">{title}</h2>
      <div style="font-size:15px;line-height:1.6;">{body_html}</div>
      {cta}
      <p style="margin-top:36px;padding-top:16px;border-top:1px solid #e5e5e5;
                font-size:12px;color:#888;">
        Sent by AlphaBook. If this doesn't apply to you, you can ignore it.
      </p>
    </div>"""


def _send_sync(to: str, subject: str, html: str, text: str) -> bool:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((SMTP_FROM_NAME, SMTP_FROM))
    msg["To"] = to
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")

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
                      cta_label: Optional[str] = None, cta_url: Optional[str] = None) -> bool:
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

    return await asyncio.to_thread(_send_sync, to, subject, html, text)
