"""Tests for app.gcal — minting Google Meet links via the Calendar API.

Pinned here: every function no-ops safely when unconfigured or when Google's
API fails, the access token is refreshed once and then cached, and a created
event's Meet link is pulled out of the response correctly. All HTTP is
stubbed; nothing here touches the network.
"""
import asyncio
import datetime as dt

import pytest

from app import gcal


def run(coro):
    return asyncio.run(coro)


class _FakeResponse:
    def __init__(self, status_code=200, json_data=None):
        self.status_code = status_code
        self._json = json_data or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._json


class _FakeAsyncClient:
    """Records every call and hands back queued responses in order."""
    calls = []
    responses = []

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, **kwargs):
        _FakeAsyncClient.calls.append(("post", url, kwargs))
        return _FakeAsyncClient.responses.pop(0)

    async def patch(self, url, **kwargs):
        _FakeAsyncClient.calls.append(("patch", url, kwargs))
        return _FakeAsyncClient.responses.pop(0)


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setattr(gcal, "CLIENT_ID", "cid")
    monkeypatch.setattr(gcal, "CLIENT_SECRET", "secret")
    monkeypatch.setattr(gcal, "REFRESH_TOKEN", "refresh")
    monkeypatch.setattr(gcal, "CONFIGURED", True)
    monkeypatch.setattr(gcal, "_token_cache", {"access_token": None, "expires_at": None})
    _FakeAsyncClient.calls = []
    _FakeAsyncClient.responses = []
    monkeypatch.setattr(gcal.httpx, "AsyncClient", _FakeAsyncClient)
    yield


def _queue_token(access_token="tok-1", expires_in=3600):
    _FakeAsyncClient.responses.append(
        _FakeResponse(200, {"access_token": access_token, "expires_in": expires_in}))


class TestUnconfigured:
    def test_create_is_a_safe_noop(self, monkeypatch):
        monkeypatch.setattr(gcal, "CONFIGURED", False)
        start = dt.datetime.now(dt.timezone.utc)
        result = run(gcal.create_meet_event(
            summary="x", description="y", start=start, end=start, attendee_emails=[]))
        assert result is None
        assert _FakeAsyncClient.calls == []

    def test_update_is_a_safe_noop(self, monkeypatch):
        monkeypatch.setattr(gcal, "CONFIGURED", False)
        start = dt.datetime.now(dt.timezone.utc)
        assert run(gcal.update_event_time("ev1", start, start)) is False


class TestCreateMeetEvent:
    def test_creates_an_event_and_returns_the_meet_link(self):
        _queue_token()
        _FakeAsyncClient.responses.append(_FakeResponse(200, {
            "id": "ev1", "hangoutLink": "https://meet.google.com/abc-defg-hij",
        }))
        start = dt.datetime.now(dt.timezone.utc)
        end = start + dt.timedelta(minutes=30)

        result = run(gcal.create_meet_event(
            summary="Alpha Fund interview — Jo", description="Quant Analyst interview.",
            start=start, end=end, attendee_emails=["jo@ox.ac.uk", "priya@ox.ac.uk"]))

        assert result == {"event_id": "ev1", "meet_link": "https://meet.google.com/abc-defg-hij"}
        method, url, kwargs = _FakeAsyncClient.calls[-1]
        assert method == "post"
        assert kwargs["json"]["attendees"] == [{"email": "jo@ox.ac.uk"}, {"email": "priya@ox.ac.uk"}]
        assert kwargs["params"]["sendUpdates"] == "none"
        assert kwargs["headers"]["Authorization"] == "Bearer tok-1"

    def test_falls_back_to_entry_points_when_no_hangout_link(self):
        _queue_token()
        _FakeAsyncClient.responses.append(_FakeResponse(200, {
            "id": "ev2",
            "conferenceData": {"entryPoints": [
                {"entryPointType": "phone", "uri": "tel:+441234"},
                {"entryPointType": "video", "uri": "https://meet.google.com/xyz-uvwx-rst"},
            ]},
        }))
        start = dt.datetime.now(dt.timezone.utc)

        result = run(gcal.create_meet_event(
            summary="x", description="y", start=start, end=start, attendee_emails=[]))

        assert result["meet_link"] == "https://meet.google.com/xyz-uvwx-rst"

    def test_no_conference_data_at_all_returns_none(self):
        _queue_token()
        _FakeAsyncClient.responses.append(_FakeResponse(200, {"id": "ev3"}))
        start = dt.datetime.now(dt.timezone.utc)

        assert run(gcal.create_meet_event(
            summary="x", description="y", start=start, end=start, attendee_emails=[])) is None

    def test_a_failed_token_refresh_is_a_safe_none(self):
        _FakeAsyncClient.responses.append(_FakeResponse(401, {"error": "invalid_grant"}))
        start = dt.datetime.now(dt.timezone.utc)

        assert run(gcal.create_meet_event(
            summary="x", description="y", start=start, end=start, attendee_emails=[])) is None

    def test_a_failed_event_creation_is_a_safe_none(self):
        _queue_token()
        _FakeAsyncClient.responses.append(_FakeResponse(500, {}))
        start = dt.datetime.now(dt.timezone.utc)

        assert run(gcal.create_meet_event(
            summary="x", description="y", start=start, end=start, attendee_emails=[])) is None

    def test_the_access_token_is_cached_across_calls(self):
        _queue_token()
        _FakeAsyncClient.responses.append(_FakeResponse(200, {"id": "ev1", "hangoutLink": "https://meet.google.com/a"}))
        _FakeAsyncClient.responses.append(_FakeResponse(200, {"id": "ev2", "hangoutLink": "https://meet.google.com/b"}))
        start = dt.datetime.now(dt.timezone.utc)

        run(gcal.create_meet_event(summary="x", description="y", start=start, end=start, attendee_emails=[]))
        run(gcal.create_meet_event(summary="x", description="y", start=start, end=start, attendee_emails=[]))

        # Only one token exchange for both event creations.
        assert sum(1 for c in _FakeAsyncClient.calls if c[1] == gcal.TOKEN_URL) == 1


class TestUpdateEventTime:
    def test_moves_an_existing_event(self):
        _queue_token()
        _FakeAsyncClient.responses.append(_FakeResponse(200, {}))
        start = dt.datetime.now(dt.timezone.utc)

        assert run(gcal.update_event_time("ev1", start, start)) is True
        method, url, kwargs = _FakeAsyncClient.calls[-1]
        assert method == "patch"
        assert url.endswith("/ev1")
        assert kwargs["params"]["sendUpdates"] == "none"

    def test_no_event_id_is_a_safe_noop(self):
        start = dt.datetime.now(dt.timezone.utc)
        assert run(gcal.update_event_time("", start, start)) is False
        assert _FakeAsyncClient.calls == []

    def test_a_failed_move_returns_false(self):
        _queue_token()
        _FakeAsyncClient.responses.append(_FakeResponse(404, {}))
        start = dt.datetime.now(dt.timezone.utc)

        assert run(gcal.update_event_time("gone", start, start)) is False
