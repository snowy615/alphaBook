"""Deleting a user must also free the email address.

The bug this covers: the endpoint removed the Firestore document and reported
success, but left the Firebase Auth record — so signing up again with the same
email failed with "email already exists" while the admin had been told the
account was gone.
"""
import asyncio

import pytest

from app import admin
from app.models import User


# ── Minimal async Firestore stand-ins ───────────────────────────────────
class FakeDoc:
    def __init__(self, doc_id, data, store=None, coll=None):
        self.id = doc_id
        self._data = data
        self.exists = data is not None
        self._store = store
        self._coll = coll

    def to_dict(self):
        return dict(self._data) if self._data is not None else None

    @property
    def reference(self):
        return self

    async def get(self):
        """A document reference and its snapshot are the same object here."""
        return self

    async def delete(self):
        if self._store is not None:
            self._store.setdefault("deleted", []).append((self._coll, self.id))
            self._store.get(self._coll, {}).pop(self.id, None)


class FakeQuery:
    def __init__(self, docs):
        self._docs = docs

    def where(self, *args, **kwargs):
        return self

    async def get(self):
        return self._docs


class FakeCollection:
    def __init__(self, name, store):
        self.name = name
        self._store = store

    def document(self, doc_id):
        data = self._store.get(self.name, {}).get(doc_id)
        return FakeDoc(doc_id, data, self._store, self.name)

    def where(self, field=None, _op=None, value=None, **kwargs):
        rows = self._store.get(self.name, {})
        return FakeQuery([FakeDoc(k, v, self._store, self.name) for k, v in rows.items()
                          if field is None or v.get(field) == value])


class FakeDB:
    def __init__(self, store):
        self._store = store

    def collection(self, name):
        return FakeCollection(name, self._store)


class FakeUserNotFound(Exception):
    pass


class FakeBlob:
    def __init__(self, name, bucket_calls):
        self._name, self._calls = name, bucket_calls

    def delete(self):
        self._calls.append(self._name)


class FakeBucket:
    """Records every blob name asked to be deleted, sync .delete() like the
    real google-cloud-storage Blob — the endpoint wraps it in to_thread."""

    def __init__(self):
        self.deleted: list = []

    def blob(self, name):
        return FakeBlob(name, self.deleted)


@pytest.fixture
def store():
    return {
        "users": {
            "uid-123": {"username": "alice", "is_admin": False,
                        "firebase_uid": "uid-123", "email": "alice@example.com"},
            "admin-1": {"username": "root", "is_admin": True},
        },
        "applications": {},
        "event_signups": {},
        "orders": {},
        "trades": {},
        "player_scores": {"uid-123": {"overall": 61}},
        "crash_ledger_profiles": {"uid-123": {"xp": 400}},
    }


@pytest.fixture
def auth_accounts():
    """Which uids exist in Firebase Auth, independent of Firestore."""
    return {"uid-123", "other-uid"}


@pytest.fixture
def wired(monkeypatch, store, auth_accounts):
    """Point the endpoint at fakes and record what it asks Firebase to delete."""
    calls = {"auth_deleted": [], "cache_invalidated": 0}

    monkeypatch.setattr(admin.db_module, "db", FakeDB(store), raising=False)
    bucket = FakeBucket()
    monkeypatch.setattr(admin.db_module, "bucket", bucket, raising=False)
    calls["bucket"] = bucket

    def fake_delete_user(uid):
        if uid not in auth_accounts:
            raise FakeUserNotFound(uid)
        auth_accounts.discard(uid)
        calls["auth_deleted"].append(uid)

    monkeypatch.setattr(admin.fb_auth, "delete_user", fake_delete_user, raising=False)
    monkeypatch.setattr(admin.fb_auth, "UserNotFoundError", FakeUserNotFound, raising=False)
    monkeypatch.setattr(admin.scores, "invalidate_cache",
                        lambda: calls.__setitem__("cache_invalidated",
                                                  calls["cache_invalidated"] + 1))
    return calls


def run(coro):
    return asyncio.run(coro)


ADMIN = User(id="admin-1", username="root", is_admin=True)


class TestDeleteUser:
    def test_the_firebase_record_is_deleted_so_the_email_frees_up(self, wired, store):
        result = run(admin.delete_user("uid-123", admin=ADMIN))
        assert result["ok"] is True
        assert wired["auth_deleted"] == ["uid-123"], \
            "the Firebase Auth record must be deleted or the email stays taken"
        assert result["auth_removed"] is True
        assert "free to reuse" in result["message"]

    def test_the_firestore_document_is_still_deleted(self, wired, store):
        run(admin.delete_user("uid-123", admin=ADMIN))
        assert "uid-123" not in store["users"]

    def test_ratings_and_xp_go_too_so_no_ghost_on_the_leaderboard(self, wired, store):
        run(admin.delete_user("uid-123", admin=ADMIN))
        assert "uid-123" not in store["player_scores"]
        assert "uid-123" not in store["crash_ledger_profiles"]

    def test_the_leaderboard_cache_is_invalidated(self, wired):
        run(admin.delete_user("uid-123", admin=ADMIN))
        assert wired["cache_invalidated"] == 1

    def test_it_uses_the_stored_firebase_uid_when_it_differs(self, wired, store):
        store["users"]["uid-123"]["firebase_uid"] = "other-uid"
        run(admin.delete_user("uid-123", admin=ADMIN))
        assert wired["auth_deleted"] == ["other-uid"]

    def test_an_account_with_no_firebase_record_still_deletes_cleanly(self, wired, store):
        store["users"]["local-1"] = {"username": "bob", "is_admin": False}
        result = run(admin.delete_user("local-1", admin=ADMIN))
        assert result["ok"] is True
        assert result["auth_removed"] is False
        assert "no Firebase sign-in record" in result["auth_note"]
        assert "local-1" not in store["users"]

    def test_a_firebase_failure_is_reported_not_swallowed(self, monkeypatch, wired, store):
        def boom(uid):
            raise RuntimeError("network down")
        monkeypatch.setattr(admin.fb_auth, "delete_user", boom, raising=False)

        result = run(admin.delete_user("uid-123", admin=ADMIN))
        # The Firestore side still completes, but the admin is told the truth.
        assert result["ok"] is True
        assert result["auth_removed"] is False
        assert "could NOT be removed" in result["auth_note"]
        assert "uid-123" not in store["users"]

    def test_admins_cannot_be_deleted(self, wired, store):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc:
            run(admin.delete_user("admin-1", admin=ADMIN))
        assert exc.value.status_code == 400
        assert "admin-1" in store["users"]

    def test_missing_user_is_a_404(self, wired):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc:
            run(admin.delete_user("nope", admin=ADMIN))
        assert exc.value.status_code == 404

    def test_deleting_a_user_also_deletes_their_application(self, wired, store):
        # This is the whole point for a tester registering Fast-Track: without
        # this, the application (and the capacity it holds) outlives the
        # account, and the outreach event spot is never actually freed.
        store["applications"]["uid-123"] = {
            "user_id": "uid-123", "status": "cv", "event_ticket": "fast_track",
        }
        result = run(admin.delete_user("uid-123", admin=ADMIN))
        assert "uid-123" not in store["applications"]
        assert result["application_removed"] is True
        assert "programme application" in result["message"]

    def test_a_user_with_no_application_is_still_deleted_cleanly(self, wired, store):
        result = run(admin.delete_user("uid-123", admin=ADMIN))
        assert result["ok"] is True
        assert result["application_removed"] is False
        assert "programme application" not in result["message"]

    def test_the_applicants_cv_blob_is_removed_from_storage(self, wired, store):
        store["applications"]["uid-123"] = {
            "user_id": "uid-123", "status": "submitted", "cv_blob_path": "cvs/2027/uid-123.pdf",
        }
        run(admin.delete_user("uid-123", admin=ADMIN))
        assert "cvs/2027/uid-123.pdf" in wired["bucket"].deleted

    def test_a_profile_cv_with_no_application_is_still_removed(self, wired, store):
        store["users"]["uid-123"]["cv_blob_path"] = "cvs/2027/uid-123.pdf"
        run(admin.delete_user("uid-123", admin=ADMIN))
        assert "cvs/2027/uid-123.pdf" in wired["bucket"].deleted

    def test_the_same_cv_path_on_both_profile_and_application_is_only_deleted_once(self, wired, store):
        store["users"]["uid-123"]["cv_blob_path"] = "cvs/2027/uid-123.pdf"
        store["applications"]["uid-123"] = {
            "user_id": "uid-123", "status": "submitted", "cv_blob_path": "cvs/2027/uid-123.pdf",
        }
        run(admin.delete_user("uid-123", admin=ADMIN))
        assert wired["bucket"].deleted.count("cvs/2027/uid-123.pdf") == 1

    def test_their_event_signups_go_too_so_a_place_is_freed(self, wired, store):
        # A confirmed place would otherwise keep counting against the event's
        # capacity after the account is gone.
        store["event_signups"]["ev1_uid-123"] = {"event_id": "ev1", "user_id": "uid-123", "status": "confirmed"}
        store["event_signups"]["ev1_other"] = {"event_id": "ev1", "user_id": "other", "status": "confirmed"}
        run(admin.delete_user("uid-123", admin=ADMIN))
        assert list(store["event_signups"]) == ["ev1_other"]


class TestIntegrationsCheck:
    """The admin "Run check" button: a real Meet link is made and cleaned up,
    and a test email reports which address it actually went out from."""

    def _patch(self, monkeypatch, created=True, sent_from="oxfordalphafund@gmail.com"):
        deleted, emails = [], []

        async def fake_create(**kwargs):
            return {"event_id": "ev1", "meet_link": "https://meet.google.com/abc-defg-hij"} if created else None

        async def fake_delete(event_id):
            deleted.append(event_id)
            return True

        async def fake_deliver(to, subject, title, body_html, *a, **k):
            emails.append(to)
            return sent_from

        monkeypatch.setattr(admin.gcal, "CONFIGURED", True)
        monkeypatch.setattr(admin.gcal, "ACCOUNT_EMAIL", "oxfordalphafund@gmail.com")
        monkeypatch.setattr(admin.gcal, "create_meet_event", fake_create)
        monkeypatch.setattr(admin.gcal, "delete_event", fake_delete)
        monkeypatch.setattr(admin.mailer, "deliver", fake_deliver)
        return deleted, emails

    def test_everything_working(self, monkeypatch):
        deleted, emails = self._patch(monkeypatch)
        result = run(admin.check_integrations(admin=ADMIN))
        assert result["meet"]["ok"] and result["meet"]["sample_link"].startswith("https://meet.google.com/")
        assert deleted == ["ev1"]                       # the test event doesn't linger
        assert emails == ["oxfordalphafund@gmail.com"]  # never sent to an applicant
        assert result["email"]["ok"] and result["email"]["sent_from"] == "oxfordalphafund@gmail.com"
        assert "still going out" not in result["email"]["detail"]

    def test_says_so_when_email_is_still_on_the_old_address(self, monkeypatch):
        self._patch(monkeypatch, sent_from="yansnow615@gmail.com")
        result = run(admin.check_integrations(admin=ADMIN))
        assert result["email"]["ok"] is True
        assert "still going out from yansnow615@gmail.com" in result["email"]["detail"]

    def test_a_refused_google_connection_is_reported(self, monkeypatch):
        deleted, _ = self._patch(monkeypatch, created=False)
        result = run(admin.check_integrations(admin=ADMIN))
        assert result["meet"]["ok"] is False and deleted == []

    def test_unconfigured_is_reported_not_attempted(self, monkeypatch):
        self._patch(monkeypatch)
        monkeypatch.setattr(admin.gcal, "CONFIGURED", False)
        result = run(admin.check_integrations(admin=ADMIN))
        assert result["meet"]["ok"] is False and "Not configured" in result["meet"]["detail"]
