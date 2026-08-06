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

    def where(self, *args, **kwargs):
        rows = self._store.get(self.name, {})
        return FakeQuery([FakeDoc(k, v, self._store, self.name) for k, v in rows.items()])


class FakeDB:
    def __init__(self, store):
        self._store = store

    def collection(self, name):
        return FakeCollection(name, self._store)


class FakeUserNotFound(Exception):
    pass


@pytest.fixture
def store():
    return {
        "users": {
            "uid-123": {"username": "alice", "is_admin": False,
                        "firebase_uid": "uid-123", "email": "alice@example.com"},
            "admin-1": {"username": "root", "is_admin": True},
        },
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
