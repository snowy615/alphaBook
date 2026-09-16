"""Tests for app.auth — the Firebase-backed signup/login endpoint.

Pinned here: a brand-new email/password signup gets our own branded
verification email (not Firebase's generic default), a Google sign-in
(already verified) never gets one, and signing back in as an existing user
never re-sends one either.
"""

import asyncio

from app import auth
from app import db as db_module
from app import mailer


class _FakeDoc:
    def __init__(self, data, id=None):
        self._data = data
        self.id = id

    @property
    def exists(self):
        return self._data is not None

    def to_dict(self):
        return dict(self._data) if self._data else None


class _FakeDocRef:
    def __init__(self, store, key):
        self._store, self._key = store, key

    async def get(self):
        return _FakeDoc(self._store.get(self._key), id=self._key)

    async def set(self, data):
        self._store[self._key] = dict(data)

    async def update(self, patch):
        self._store.setdefault(self._key, {}).update(patch)


class _FakeQuery:
    """Only what auth.py needs: .where(field, "==", value).limit(n).get()."""

    def __init__(self, store, field=None, value=None):
        self._store, self._field, self._value = store, field, value

    def where(self, field, _op, value):
        return _FakeQuery(self._store, field, value)

    def limit(self, _n):
        return self

    async def get(self):
        if self._field is None:
            return [_FakeDoc(v, id=k) for k, v in self._store.items()]
        return [_FakeDoc(v, id=k) for k, v in self._store.items() if v.get(self._field) == self._value]


class _FakeCollection(_FakeQuery):
    def __init__(self, store):
        super().__init__(store)

    def document(self, doc_id):
        return _FakeDocRef(self._store, doc_id)


class _FakeDB:
    def __init__(self):
        self.collections: dict = {}

    def collection(self, name):
        self.collections.setdefault(name, {})
        return _FakeCollection(self.collections[name])


class _FakeURL:
    scheme = "https"


class _FakeRequest:
    """Only what auth_firebase's _is_https() needs from a real Request."""
    headers: dict = {}
    url = _FakeURL()


def _decoded(uid="fb_uid_1", email="jo@example.com", email_verified=False):
    return {"uid": uid, "email": email, "email_verified": email_verified}


class TestVerificationEmailOnSignup:
    def _patch(self, monkeypatch, decoded, *, send_ok=True):
        fake_db = _FakeDB()
        monkeypatch.setattr(db_module, "db", fake_db)
        monkeypatch.setattr(auth.fb_auth, "verify_id_token", lambda token: decoded)

        sent = []

        async def fake_send(to, subject, title, body_html, cta_label=None, cta_url=None, ics=None):
            sent.append({"to": to, "subject": subject, "cta_url": cta_url})
            return send_ok

        monkeypatch.setattr(mailer, "send_email", fake_send)
        monkeypatch.setattr(
            auth.fb_auth, "generate_email_verification_link",
            lambda email, action_code_settings=None: f"https://alphabook.uk/verify?for={email}",
        )
        return fake_db, sent

    def test_fresh_email_password_signup_gets_a_branded_verification_email(self, monkeypatch):
        decoded = _decoded(email_verified=False)
        fake_db, sent = self._patch(monkeypatch, decoded)

        result = asyncio.run(auth.auth_firebase(_FakeRequest(), id_token="tok", username="jo"))

        assert result.status_code == 200
        assert len(sent) == 1
        assert sent[0]["to"] == "jo@example.com"
        assert sent[0]["subject"] == "Verify your AlphaBook email"
        assert sent[0]["cta_url"] == "https://alphabook.uk/verify?for=jo@example.com"
        assert fake_db.collections["users"]["fb_uid_1"]["username"] == "jo"

    def test_google_sign_in_is_already_verified_so_no_email_is_sent(self, monkeypatch):
        decoded = _decoded(uid="fb_uid_2", email="jo@gmail.com", email_verified=True)
        fake_db, sent = self._patch(monkeypatch, decoded)

        result = asyncio.run(auth.auth_firebase(_FakeRequest(), id_token="tok", username="jo"))

        assert result.status_code == 200
        assert sent == []

    def test_signing_back_in_as_an_existing_user_never_resends_it(self, monkeypatch):
        decoded = _decoded(uid="fb_uid_3", email="jo@example.com", email_verified=False)
        fake_db, sent = self._patch(monkeypatch, decoded)
        fake_db.collections.setdefault("users", {})["fb_uid_3"] = {
            "username": "jo", "firebase_uid": "fb_uid_3", "email": "jo@example.com",
            "balance": 10000.0, "is_admin": False, "is_blacklisted": False,
        }

        result = asyncio.run(auth.auth_firebase(_FakeRequest(), id_token="tok", username=None))

        assert result.status_code == 200
        assert sent == []

    def test_a_failed_link_generation_does_not_block_account_creation(self, monkeypatch):
        decoded = _decoded(email_verified=False)
        fake_db, sent = self._patch(monkeypatch, decoded)

        def boom(email, action_code_settings=None):
            raise RuntimeError("firebase is down")

        monkeypatch.setattr(auth.fb_auth, "generate_email_verification_link", boom)

        result = asyncio.run(auth.auth_firebase(_FakeRequest(), id_token="tok", username="jo"))

        assert result.status_code == 200
        assert sent == []   # never got as far as sending, but the account still exists
        assert "fb_uid_1" in fake_db.collections["users"]
