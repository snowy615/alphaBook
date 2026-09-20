"""Tests for app.mailer — the SMTP wrapper behind application emails.

Pinned here: send_email never raises regardless of configuration or address
validity, it no-ops (rather than erroring) when SMTP isn't configured, and it
actually calls the SMTP layer — with the right recipient and subject — when
it is. The real smtplib call is stubbed out; nothing here touches a network.
"""
import asyncio
import datetime as dt
from email import message_from_bytes

from app import mailer


def run(coro):
    return asyncio.run(coro)


class TestUnconfigured:
    def test_send_is_a_safe_noop_without_smtp_host(self, monkeypatch):
        monkeypatch.setattr(mailer, "CONFIGURED", False)
        calls = []
        monkeypatch.setattr(mailer, "_send_sync", lambda *a: calls.append(a) or True)

        sent = run(mailer.send_email("jo@merton.ox.ac.uk", "Subject", "Title", "<p>hi</p>"))

        assert sent is False
        assert calls == []   # never even reaches the SMTP layer


class TestAddressValidation:
    def test_rejects_a_missing_or_malformed_address(self, monkeypatch):
        monkeypatch.setattr(mailer, "CONFIGURED", True)
        for bad in ("", None, "not-an-email"):
            assert run(mailer.send_email(bad, "Subject", "Title", "<p>hi</p>")) is False


class TestConfigured:
    def _patch_sync(self, monkeypatch, result=True):
        calls = []

        def fake_send_sync(to, subject, html, text, ics=None):
            calls.append({"to": to, "subject": subject, "html": html, "text": text, "ics": ics})
            return result

        monkeypatch.setattr(mailer, "CONFIGURED", True)
        monkeypatch.setattr(mailer, "_send_sync", fake_send_sync)
        return calls

    def test_calls_through_with_the_right_recipient_and_subject(self, monkeypatch):
        calls = self._patch_sync(monkeypatch)

        sent = run(mailer.send_email(
            "jo@merton.ox.ac.uk", "Alpha Fund — you're in", "Application received",
            "<p>Congratulations.</p>",
        ))

        assert sent is True
        assert len(calls) == 1
        assert calls[0]["to"] == "jo@merton.ox.ac.uk"
        assert calls[0]["subject"] == "Alpha Fund — you're in"
        assert "Congratulations." in calls[0]["html"]
        assert "Congratulations." in calls[0]["text"]

    def test_html_body_carries_the_title(self, monkeypatch):
        calls = self._patch_sync(monkeypatch)
        run(mailer.send_email("jo@merton.ox.ac.uk", "Subject", "A Clear Title", "<p>body</p>"))
        assert "A Clear Title" in calls[0]["html"]

    def test_cta_appears_in_both_html_and_text(self, monkeypatch):
        calls = self._patch_sync(monkeypatch)
        run(mailer.send_email(
            "jo@merton.ox.ac.uk", "Subject", "Title", "<p>body</p>",
            cta_label="Continue your application", cta_url="https://alphabook.uk/apply",
        ))
        assert "https://alphabook.uk/apply" in calls[0]["html"]
        assert "Continue your application" in calls[0]["text"]
        assert "https://alphabook.uk/apply" in calls[0]["text"]

    def test_a_failed_send_is_reported_but_does_not_raise(self, monkeypatch):
        self._patch_sync(monkeypatch, result=False)
        sent = run(mailer.send_email("jo@merton.ox.ac.uk", "Subject", "Title", "<p>hi</p>"))
        assert sent is False

    def test_text_fallback_strips_markup(self, monkeypatch):
        calls = self._patch_sync(monkeypatch)
        run(mailer.send_email(
            "jo@merton.ox.ac.uk", "Subject", "Title",
            "<p>Hi <strong>Jo</strong>, welcome.</p>",
        ))
        assert "<strong>" not in calls[0]["text"]
        assert "Jo" in calls[0]["text"] and "welcome" in calls[0]["text"]


class TestMessageHeaders:
    """Date and Message-ID were missing entirely — smtplib doesn't add them
    for you, and their absence is itself a spam signal most filters weigh,
    Microsoft's especially."""

    def test_the_raw_message_carries_date_and_message_id(self, monkeypatch):
        sent_holder = {}

        class _CaptureSMTP:
            def __init__(self, *a, **k): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def starttls(self): pass
            def login(self, *a): pass
            def send_message(self, msg): sent_holder["msg"] = msg

        monkeypatch.setattr(mailer.smtplib, "SMTP", _CaptureSMTP)
        monkeypatch.setattr(mailer, "CONFIGURED", True)

        mailer._send_sync("jo@merton.ox.ac.uk", "Subject", "<p>hi</p>", "hi")

        msg = sent_holder["msg"]
        assert msg["Date"] is not None
        assert msg["Message-ID"] is not None
        assert msg["Message-ID"].strip().startswith("<")


class TestIcsInvite:
    def _build(self, **overrides):
        start = dt.datetime(2026, 9, 15, 14, 0, tzinfo=dt.timezone.utc)
        kwargs = dict(
            uid="interview-u1-123", summary="Alpha Fund interview — Jo Bloggs",
            description="Quant Analyst interview with Priya Patel.",
            start=start, end=start + dt.timedelta(minutes=30),
            organizer_name="Priya Patel", organizer_email="priya@ox.ac.uk",
            attendee_name="Jo Bloggs", attendee_email="jo@merton.ox.ac.uk",
        )
        kwargs.update(overrides)
        return mailer.build_ics_invite(**kwargs)

    def test_produces_a_valid_vevent_block(self):
        ics = self._build().decode()
        assert ics.startswith("BEGIN:VCALENDAR\r\n")
        assert ics.endswith("END:VCALENDAR\r\n")
        assert "BEGIN:VEVENT" in ics and "END:VEVENT" in ics
        assert "METHOD:REQUEST" in ics

    def test_carries_the_right_times_organizer_and_attendee(self):
        ics = self._build().decode()
        assert "DTSTART:20260915T140000Z" in ics
        assert "DTEND:20260915T143000Z" in ics
        assert "ORGANIZER;CN=Priya Patel:mailto:priya@ox.ac.uk" in ics
        assert "ATTENDEE;CN=Jo Bloggs;ROLE=REQ-PARTICIPANT:mailto:jo@merton.ox.ac.uk" in ics

    def test_naive_datetimes_are_treated_as_utc(self):
        naive = dt.datetime(2026, 9, 15, 14, 0)
        ics = self._build(start=naive, end=naive + dt.timedelta(minutes=30)).decode()
        assert "DTSTART:20260915T140000Z" in ics

    def test_special_characters_are_escaped(self):
        ics = self._build(description="Bring; a laptop, and notes\nplease").decode()
        assert "Bring\\; a laptop\\, and notes\\nplease" in ics

    def test_attaches_to_the_email_with_calendar_content_type(self, monkeypatch):
        calls = []

        def fake_send_sync(to, subject, html, text, ics=None):
            calls.append((to, subject, ics))
            return True

        monkeypatch.setattr(mailer, "CONFIGURED", True)
        monkeypatch.setattr(mailer, "_send_sync", fake_send_sync)

        ics_bytes = self._build()
        asyncio.run(mailer.send_email(
            "jo@merton.ox.ac.uk", "Interview confirmed", "Title", "<p>body</p>", ics=ics_bytes,
        ))

        assert calls[0][2] == ics_bytes

    def test_ics_attachment_survives_a_real_mime_round_trip(self, monkeypatch):
        # Build the actual MIME message (skipping the network call) and parse
        # it back, the way a mail client would — catches a bad Content-Type
        # or a truncated attachment that string checks alone would miss.
        ics_bytes = self._build()
        sent_holder = {}

        class _CaptureSMTP:
            def __init__(self, *a, **k): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def starttls(self): pass
            def login(self, *a): pass
            def send_message(self, msg): sent_holder["msg"] = msg

        monkeypatch.setattr(mailer.smtplib, "SMTP", _CaptureSMTP)
        monkeypatch.setattr(mailer, "CONFIGURED", True)

        mailer._send_sync("jo@merton.ox.ac.uk", "Interview confirmed", "<p>hi</p>", "hi", ics_bytes)

        raw = sent_holder["msg"].as_bytes()
        parsed = message_from_bytes(raw)
        ics_parts = [p for p in parsed.walk() if p.get_content_type() == "text/calendar"]
        assert len(ics_parts) == 1
        assert ics_parts[0].get_payload(decode=True) == ics_bytes
