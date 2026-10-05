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

        # Unverified — the email goes out, but no session yet either.
        assert result.status_code == 403
        assert len(sent) == 1
        assert sent[0]["to"] == "jo@example.com"
        assert sent[0]["subject"] == "Verify your email"
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

        assert result.status_code == 403  # still unverified, still no session
        assert sent == []

    def test_a_failed_link_generation_does_not_block_account_creation(self, monkeypatch):
        decoded = _decoded(email_verified=False)
        fake_db, sent = self._patch(monkeypatch, decoded)

        def boom(email, action_code_settings=None):
            raise RuntimeError("firebase is down")

        monkeypatch.setattr(auth.fb_auth, "generate_email_verification_link", boom)

        result = asyncio.run(auth.auth_firebase(_FakeRequest(), id_token="tok", username="jo"))

        assert result.status_code == 403
        assert sent == []   # never got as far as sending, but the account still exists
        assert "fb_uid_1" in fake_db.collections["users"]


class TestUnverifiedEmailGate:
    """The bug this covers: /auth/firebase set a session cookie and logged
    people straight in regardless of whether they'd clicked the verification
    link — so the app moved on without anyone actually verifying."""

    def _patch(self, monkeypatch, decoded):
        fake_db = _FakeDB()
        monkeypatch.setattr(db_module, "db", fake_db)
        monkeypatch.setattr(auth.fb_auth, "verify_id_token", lambda token: decoded)

        async def fake_send(to, subject, title, body_html, cta_label=None, cta_url=None, ics=None):
            return True

        monkeypatch.setattr(mailer, "send_email", fake_send)
        monkeypatch.setattr(
            auth.fb_auth, "generate_email_verification_link",
            lambda email, action_code_settings=None: f"https://alphabook.uk/verify?for={email}",
        )
        return fake_db

    def test_an_unverified_signup_gets_no_session_cookie(self, monkeypatch):
        decoded = _decoded(email_verified=False)
        self._patch(monkeypatch, decoded)

        result = asyncio.run(auth.auth_firebase(_FakeRequest(), id_token="tok", username="jo"))

        assert result.status_code == 403
        assert "set-cookie" not in {k.lower() for k in result.headers.keys()}

    def test_an_unverified_returning_user_is_also_blocked(self, monkeypatch):
        decoded = _decoded(uid="fb_uid_9", email="jo@example.com", email_verified=False)
        fake_db = self._patch(monkeypatch, decoded)
        fake_db.collections.setdefault("users", {})["fb_uid_9"] = {
            "username": "jo", "firebase_uid": "fb_uid_9", "email": "jo@example.com",
            "balance": 10000.0, "is_admin": False, "is_blacklisted": False,
        }

        result = asyncio.run(auth.auth_firebase(_FakeRequest(), id_token="tok", username=None))

        assert result.status_code == 403
        assert "set-cookie" not in {k.lower() for k in result.headers.keys()}

    def test_a_verified_token_gets_a_normal_session(self, monkeypatch):
        decoded = _decoded(uid="fb_uid_10", email="jo@example.com", email_verified=True)
        self._patch(monkeypatch, decoded)

        result = asyncio.run(auth.auth_firebase(_FakeRequest(), id_token="tok", username="jo"))

        assert result.status_code == 200
        assert "set-cookie" in {k.lower() for k in result.headers.keys()}

    def test_a_sign_in_with_no_email_gets_no_session(self, monkeypatch):
        # Every sign-in method on the site (Google, email and password)
        # carries an email; a token without one has nothing verified behind
        # it, so it doesn't get a session. (Admin's direct login is separate.)
        decoded = {"uid": "fb_uid_11", "email": "", "email_verified": False}
        self._patch(monkeypatch, decoded)

        result = asyncio.run(auth.auth_firebase(_FakeRequest(), id_token="tok", username="jo"))

        assert result.status_code == 403
        assert "set-cookie" not in {k.lower() for k in result.headers.keys()}


class TestResolveUsername:
    """The bug this covers: logging in by username only ever hit the
    admin-only /auth/direct path, so it failed for every real account —
    Firebase itself only knows how to sign in by email."""

    def test_a_known_username_resolves_to_its_email(self, monkeypatch):
        fake_db = _FakeDB()
        fake_db.collections["users"] = {
            "fb_uid_1": {"username": "jo", "email": "jo@example.com"},
        }
        monkeypatch.setattr(db_module, "db", fake_db)

        result = asyncio.run(auth.resolve_username(username="jo"))

        assert result.status_code == 200
        import json
        assert json.loads(result.body)["email"] == "jo@example.com"

    def test_an_unknown_username_is_reported_generically(self, monkeypatch):
        fake_db = _FakeDB()
        fake_db.collections["users"] = {}
        monkeypatch.setattr(db_module, "db", fake_db)

        result = asyncio.run(auth.resolve_username(username="ghost"))

        assert result.status_code == 404

    def test_a_username_with_no_email_on_file_is_reported_the_same_way(self, monkeypatch):
        # The local admin account, and any other emailless account, must not
        # be distinguishable from an unknown username by the response.
        fake_db = _FakeDB()
        fake_db.collections["users"] = {"admin_user_id": {"username": "admin"}}
        monkeypatch.setattr(db_module, "db", fake_db)

        result = asyncio.run(auth.resolve_username(username="admin"))

        assert result.status_code == 404


class TestResendVerification:
    def _patch(self, monkeypatch, decoded, *, username_on_file="jo"):
        fake_db = _FakeDB()
        fake_db.collections.setdefault("users", {})[decoded["uid"]] = {
            "username": username_on_file, "firebase_uid": decoded["uid"], "email": decoded.get("email"),
        }
        monkeypatch.setattr(db_module, "db", fake_db)
        monkeypatch.setattr(auth.fb_auth, "verify_id_token", lambda token: decoded)

        sent = []

        async def fake_send(to, subject, title, body_html, cta_label=None, cta_url=None, ics=None):
            sent.append(to)
            return True

        monkeypatch.setattr(mailer, "send_email", fake_send)
        monkeypatch.setattr(
            auth.fb_auth, "generate_email_verification_link",
            lambda email, action_code_settings=None: f"https://alphabook.uk/verify?for={email}",
        )
        return sent

    def test_resends_to_an_unverified_account(self, monkeypatch):
        decoded = _decoded(uid="fb_uid_20", email="jo@example.com", email_verified=False)
        sent = self._patch(monkeypatch, decoded)

        result = asyncio.run(auth.resend_verification(id_token="tok"))

        assert result.status_code == 200
        assert sent == ["jo@example.com"]

    def test_an_already_verified_account_is_told_so_and_nothing_is_sent(self, monkeypatch):
        decoded = _decoded(uid="fb_uid_21", email="jo@example.com", email_verified=True)
        sent = self._patch(monkeypatch, decoded)

        result = asyncio.run(auth.resend_verification(id_token="tok"))

        assert result.status_code == 200
        assert sent == []

    def test_an_invalid_token_is_rejected(self, monkeypatch):
        def boom(token):
            raise ValueError("bad token")
        monkeypatch.setattr(auth.fb_auth, "verify_id_token", boom)

        result = asyncio.run(auth.resend_verification(id_token="garbage"))

        assert result.status_code == 401


class TestDirectAdminLoginFailsClosed:
    def test_no_admin_password_configured_means_no_admin_login(self, monkeypatch):
        monkeypatch.setattr(auth, "ADMIN_PASSWORD", "")
        for pw in ("", "Alphabook"):
            result = asyncio.run(auth.direct_login(_FakeRequest(), username="admin", password=pw))
            assert result.status_code == 401

    def test_a_wrong_password_is_refused(self, monkeypatch):
        monkeypatch.setattr(auth, "ADMIN_PASSWORD", "the-real-one")
        result = asyncio.run(auth.direct_login(_FakeRequest(), username="admin", password="Alphabook"))
        assert result.status_code == 401


# ── Emailed sign-in links ────────────────────────────────────────────────────

import json
import re as _re
import pytest
import datetime as _dt
from starlette.requests import Request as _Request


def _req(origin=None, path="/auth/email-link"):
    headers = [(b"host", b"alphabook.uk"), (b"x-forwarded-proto", b"https")]
    if origin:
        headers.append((b"origin", origin.encode()))
    return _Request({"type": "http", "method": "POST", "path": path, "headers": headers,
                     "query_string": b"", "server": ("alphabook.uk", 443), "scheme": "https", "root_path": ""})


class TestEmailSignInLink:
    def _setup(self, monkeypatch, users=None, applications=None, fb_verified=True):
        fake_db = _FakeDB()
        fake_db.collections["users"] = users or {}
        fake_db.collections["applications"] = applications or {}
        monkeypatch.setattr(db_module, "db", fake_db)
        sent = []

        async def fake_send(to, subject, title, body_html, cta_label=None, cta_url=None, ics=None, cc=None):
            sent.append({"to": to, "subject": subject, "body": body_html, "cta_url": cta_url})
            return True
        monkeypatch.setattr(auth.mailer, "send_email", fake_send)
        verified_calls = []

        class _FbUser:
            def __init__(self, uid):
                self.email_verified = fb_verified
                self.email = (fake_db.collections["users"].get(uid) or {}).get("email")
        monkeypatch.setattr(auth.fb_auth, "get_user", lambda uid: _FbUser(uid))
        monkeypatch.setattr(auth.fb_auth, "update_user", lambda uid, **kw: verified_calls.append((uid, kw)))
        return fake_db, sent, verified_calls

    def _ask(self, email, next="/apply"):
        res = asyncio.run(auth.request_email_link(email=email, next=next))
        return json.loads(res.body)

    def _token(self, mail):
        url = mail["cta_url"] or _re.search(r'href="([^"]+)"', mail["body"]).group(1)
        return url.split("t=", 1)[1]

    def test_the_sign_in_email_gets_a_link_stored_only_as_a_hash(self, monkeypatch):
        fake_db, sent, _ = self._setup(monkeypatch, users={"u1": {"username": "toby", "email": "toby@gmail.com"}})
        assert self._ask("Toby@gmail.com")["status"] == "ok"
        assert len(sent) == 1 and sent[0]["to"] == "Toby@gmail.com"
        token = self._token(sent[0])
        stored = fake_db.collections[auth.LOGIN_LINKS]
        assert token not in stored and auth._hash(token) in stored
        assert stored[auth._hash(token)]["uid"] == "u1" and stored[auth._hash(token)]["next"] == "/apply"

    def test_a_confirmed_oxford_email_signs_in_to_the_gmail_account(self, monkeypatch):
        _, sent, _ = self._setup(
            monkeypatch, users={"u1": {"username": "toby", "email": "therealtobygu@gmail.com"}},
            applications={"u1": {"oxford_email": "cunji.gu@spc.ox.ac.uk", "oxford_email_verified": True}})
        self._ask("cunji.gu@spc.ox.ac.uk")
        assert len(sent) == 1 and sent[0]["to"] == "cunji.gu@spc.ox.ac.uk"

    def test_an_unconfirmed_oxford_email_gets_nothing_but_the_same_reply(self, monkeypatch):
        _, sent, _ = self._setup(
            monkeypatch, users={"u1": {"username": "x", "email": "x@gmail.com"}},
            applications={"u1": {"oxford_email": "abc123+anything@ox.ac.uk", "oxford_email_verified": False}})
        assert self._ask("abc123+anything@ox.ac.uk")["message"] == auth.LOGIN_LINK_SENT
        assert sent == []

    def test_an_unknown_email_gets_the_same_reply_and_nothing_sent(self, monkeypatch):
        _, sent, _ = self._setup(monkeypatch)
        assert self._ask("nobody@ox.ac.uk")["message"] == auth.LOGIN_LINK_SENT
        assert sent == []

    def test_one_send_per_address_per_minute(self, monkeypatch):
        _, sent, _ = self._setup(monkeypatch, users={"u1": {"username": "t", "email": "t@gmail.com"}})
        self._ask("t@gmail.com")
        self._ask("t@gmail.com")
        assert len(sent) == 1

    def test_suspended_accounts_get_nothing(self, monkeypatch):
        _, sent, _ = self._setup(monkeypatch, users={"u1": {"username": "t", "email": "t@gmail.com",
                                                             "is_blacklisted": True}})
        self._ask("t@gmail.com")
        assert sent == []

    def test_two_accounts_on_one_address_get_a_link_each(self, monkeypatch):
        _, sent, _ = self._setup(
            monkeypatch,
            users={"a": {"username": "chris_ox", "email": "c@kellogg.ox.ac.uk"},
                   "b": {"username": "chris_g", "email": "c@gmail.com"}},
            applications={"b": {"oxford_email": "c@kellogg.ox.ac.uk", "oxford_email_verified": True}})
        self._ask("c@kellogg.ox.ac.uk")
        assert len(sent) == 1 and sent[0]["cta_url"] is None
        assert "Sign in as chris_ox" in sent[0]["body"] and "Sign in as chris_g" in sent[0]["body"]

    def test_opening_the_link_does_not_use_it_up(self, monkeypatch):
        """Mail scanners open links first; only the button press signs in."""
        fake_db, sent, _ = self._setup(monkeypatch, users={"u1": {"username": "toby", "email": "t@gmail.com"}})
        self._ask("t@gmail.com")
        token = self._token(sent[0])
        page = asyncio.run(auth.email_link_page(_req(), t=token)).body.decode()
        assert "Sign me in" in page and "toby" in page
        assert fake_db.collections[auth.LOGIN_LINKS][auth._hash(token)]["used_at"] is None

    def test_the_button_signs_in_once_and_returns_to_the_application(self, monkeypatch):
        fake_db, sent, _ = self._setup(monkeypatch, users={"u1": {"username": "toby", "email": "t@gmail.com"}})
        self._ask("t@gmail.com", next="/apply")
        token = self._token(sent[0])
        res = asyncio.run(auth.confirm_email_link(_req(origin="https://alphabook.uk"), t=token))
        assert res.status_code == 303 and res.headers["location"] == "/apply"
        cookie = res.headers["set-cookie"]
        session = _re.search(r"__session=([^;]+)", cookie).group(1)
        assert auth.jwt.decode(session, auth.SECRET_KEY, algorithms=[auth.ALGORITHM])["sub"] == "u1"
        again = asyncio.run(auth.confirm_email_link(_req(), t=token))
        assert again.status_code == 400 and "already been used" in again.body.decode()

    def test_an_expired_link_is_refused(self, monkeypatch):
        fake_db, sent, _ = self._setup(monkeypatch, users={"u1": {"username": "t", "email": "t@gmail.com"}})
        self._ask("t@gmail.com")
        token = self._token(sent[0])
        fake_db.collections[auth.LOGIN_LINKS][auth._hash(token)]["expires_at"] = \
            _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=1)
        assert asyncio.run(auth.confirm_email_link(_req(), t=token)).status_code == 400

    def test_a_post_from_another_site_is_refused(self, monkeypatch):
        _, sent, _ = self._setup(monkeypatch, users={"u1": {"username": "t", "email": "t@gmail.com"}})
        self._ask("t@gmail.com")
        with pytest.raises(auth.HTTPException):
            asyncio.run(auth.confirm_email_link(_req(origin="https://evil.example"), t=self._token(sent[0])))

    def test_signing_in_from_the_inbox_verifies_an_unverified_sign_in_email(self, monkeypatch):
        _, sent, verified = self._setup(monkeypatch, users={"u1": {"username": "t", "email": "t@ox.ac.uk"}},
                                        fb_verified=False)
        self._ask("t@ox.ac.uk")
        asyncio.run(auth.confirm_email_link(_req(), t=self._token(sent[0])))
        assert verified == [("u1", {"email_verified": True})]

    @pytest.mark.parametrize("given, expected", [
        ("/apply", "/apply"), ("/apply?x=1", "/apply?x=1"), ("", "/"), (None, "/"),
        ("//evil.com", "/"), ("https://evil.com", "/"), ("/\\evil.com", "/"), ("apply", "/"),
    ])
    def test_only_same_site_paths_are_followed(self, given, expected):
        assert auth.safe_next(given) == expected
