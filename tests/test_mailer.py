"""Tests for app.mailer — the SMTP wrapper behind application emails.

Pinned here: send_email never raises regardless of configuration or address
validity, it no-ops (rather than erroring) when SMTP isn't configured, and it
actually calls the SMTP layer — with the right recipient and subject — when
it is. The real smtplib call is stubbed out; nothing here touches a network.
"""
import asyncio

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

        def fake_send_sync(to, subject, html, text):
            calls.append({"to": to, "subject": subject, "html": html, "text": text})
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
