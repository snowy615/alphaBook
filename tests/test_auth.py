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


def _req(origin=None, path="/auth/email-link", forwarded_for=None):
    headers = [(b"host", b"alphabook.uk"), (b"x-forwarded-proto", b"https")]
    if forwarded_for:
        headers.append((b"x-forwarded-for", f"{forwarded_for}, 130.211.0.1".encode()))
    if origin:
        headers.append((b"origin", origin.encode()))
    return _Request({"type": "http", "method": "POST", "path": path, "headers": headers,
                     "query_string": b"", "server": ("alphabook.uk", 443), "scheme": "https", "root_path": ""})


class TestEmailSignInLink:
    def _setup(self, monkeypatch, users=None, applications=None, fb_verified=True, fb_disabled=False):
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

        state = {"verified": fb_verified, "disabled": fb_disabled}

        class _FbUser:
            def __init__(self, uid):
                self.email_verified = state["verified"]
                self.disabled = state["disabled"]
                self.email = (fake_db.collections["users"].get(uid) or {}).get("email")
        monkeypatch.setattr(auth.fb_auth, "get_user", lambda uid: _FbUser(uid))
        self.fb_state = state
        monkeypatch.setattr(auth.fb_auth, "update_user", lambda uid, **kw: verified_calls.append((uid, kw)))
        return fake_db, sent, verified_calls

    def _ask(self, email, next="/apply", ip="81.2.69.160"):
        res = asyncio.run(auth.request_email_link(_req(forwarded_for=ip), email=email, next=next))
        return {**json.loads(res.body), "_code": res.status_code}

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

    def test_an_unverified_account_gets_no_link_and_nothing_is_verified(self, monkeypatch):
        """A throwaway sign-up has to verify the normal way; the link is no shortcut."""
        _, sent, verified = self._setup(monkeypatch, users={"u1": {"username": "t", "email": "t@temp-mail.io"}},
                                        fb_verified=False)
        assert self._ask("t@temp-mail.io")["message"] == auth.LOGIN_LINK_SENT
        assert sent == [] and verified == []

    def test_an_unverified_account_gets_none_via_its_oxford_email_either(self, monkeypatch):
        _, sent, _ = self._setup(
            monkeypatch, users={"u1": {"username": "t", "email": "t@temp-mail.io"}},
            applications={"u1": {"oxford_email": "t@ox.ac.uk", "oxford_email_verified": True}}, fb_verified=False)
        self._ask("t@ox.ac.uk")
        assert sent == []

    def test_a_disabled_account_gets_no_link(self, monkeypatch):
        _, sent, _ = self._setup(monkeypatch, users={"u1": {"username": "t", "email": "t@gmail.com"}},
                                 fb_disabled=True)
        self._ask("t@gmail.com")
        assert sent == []

    def test_an_account_suspended_after_the_link_was_sent_cant_use_it(self, monkeypatch):
        fake_db, sent, _ = self._setup(monkeypatch, users={"u1": {"username": "t", "email": "t@gmail.com"}})
        self._ask("t@gmail.com")
        fake_db.collections["users"]["u1"]["is_blacklisted"] = True
        assert asyncio.run(auth.confirm_email_link(_req(), t=self._token(sent[0]))).status_code == 400
        fake_db.collections["users"]["u1"]["is_blacklisted"] = False
        self.fb_state["disabled"] = True
        assert asyncio.run(auth.confirm_email_link(_req(), t=self._token(sent[0]))).status_code == 400

    def test_at_most_five_links_a_day_per_address(self, monkeypatch):
        fake_db, sent, _ = self._setup(monkeypatch, users={"u1": {"username": "t", "email": "t@gmail.com"}})
        for i in range(8):
            self._ask("t@gmail.com", ip=f"10.0.0.{i}")
            # step past the one-a-minute gap
            doc = fake_db.collections[auth.LOGIN_LINK_REQUESTS]["email_" + auth._hash("t@gmail.com")]
            doc["times"] = [t - _dt.timedelta(minutes=2) for t in doc["times"]]
        assert len(sent) == auth.LOGIN_LINKS_PER_DAY

    def test_one_connection_cant_spray_requests(self, monkeypatch):
        users = {f"u{i}": {"username": f"t{i}", "email": f"t{i}@gmail.com"} for i in range(25)}
        _, sent, _ = self._setup(monkeypatch, users=users)
        codes = [self._ask(f"t{i}@gmail.com", ip="81.2.69.160")["_code"] for i in range(25)]
        assert len(sent) == auth.LOGIN_LINK_REQUESTS_PER_HOUR
        assert codes[-1] == 429
        assert self._ask("t24@gmail.com", ip="81.2.69.161")["_code"] == 200   # someone else isn't blocked

    @pytest.mark.parametrize("given, expected", [
        ("/apply", "/apply"), ("/apply?x=1", "/apply?x=1"), ("", "/"), (None, "/"),
        ("//evil.com", "/"), ("https://evil.com", "/"), ("/\\evil.com", "/"), ("apply", "/"),
    ])
    def test_only_same_site_paths_are_followed(self, given, expected):
        assert auth.safe_next(given) == expected


class TestNoSecondAccountForAnAppliedEmail:
    """Signing up (password or Google) with an email that's already the
    Oxford email on someone's application doesn't make a second account."""

    def _setup(self, monkeypatch, created_minutes_ago=1):
        fake_db = _FakeDB()
        fake_db.collections["applications"] = {"gmail": {"oxford_email": "grace.xu@cs.ox.ac.uk", "status": "shortlisted"}}
        fake_db.collections["users"] = {"gmail": {"username": "grace", "email": "grace.rd.xu@gmail.com"}}
        monkeypatch.setattr(db_module, "db", fake_db)
        deleted = []
        created = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=created_minutes_ago)).timestamp() * 1000

        class _FbUser:
            def __init__(self, uid):
                self.email = "grace.rd.xu@gmail.com" if uid == "gmail" else "grace.xu@cs.ox.ac.uk"
                self.provider_data = [type("P", (), {"provider_id": "google.com" if uid == "gmail" else "password"})()]
                self.user_metadata = type("M", (), {"creation_timestamp": created})()
        monkeypatch.setattr(auth.fb_auth, "get_user", lambda uid: _FbUser(uid))
        monkeypatch.setattr(auth.fb_auth, "delete_user", lambda uid: deleted.append(uid))
        monkeypatch.setattr(auth.fb_auth, "verify_id_token",
                            lambda token: {"uid": "newpw", "email": "grace.xu@cs.ox.ac.uk", "email_verified": False})
        return fake_db, deleted

    def test_the_sign_up_is_refused_and_points_to_the_existing_account(self, monkeypatch):
        fake_db, deleted = self._setup(monkeypatch)
        res = asyncio.run(auth.auth_firebase(_FakeRequest(), id_token="t", username="grace2"))
        body = json.loads(res.body)
        assert res.status_code == 409 and body["status"] == "linked"
        assert "Google sign-in, g*********u@gmail.com" in body["message"]
        assert "grace.rd.xu@gmail.com" not in body["message"]
        assert "newpw" not in fake_db.collections["users"]
        assert deleted == ["newpw"]           # the moments-old login doesn't linger

    def test_an_older_login_is_refused_but_not_deleted(self, monkeypatch):
        _, deleted = self._setup(monkeypatch, created_minutes_ago=60 * 24)
        res = asyncio.run(auth.auth_firebase(_FakeRequest(), id_token="t", username="grace2"))
        assert res.status_code == 409 and deleted == []

    @pytest.mark.parametrize("addr, masked", [
        ("sherrytsai1218@gmail.com", "s************8@gmail.com"),
        ("ab@ox.ac.uk", "a*b@ox.ac.uk"), ("a@ox.ac.uk", "a*@ox.ac.uk"), ("", "another address"),
    ])
    def test_masking(self, addr, masked):
        assert auth.mask_email(addr) == masked


class TestSignInLinkForOlderApplications:
    def test_an_application_from_before_the_code_check_still_gets_a_link(self, monkeypatch):
        t = TestEmailSignInLink()
        _, sent, _ = t._setup(monkeypatch, users={"u1": {"username": "sherry", "email": "sherrytsai1218@gmail.com"}},
                              applications={"u1": {"oxford_email": "sant7183@ox.ac.uk"}})   # no flag at all
        t._ask("sant7183@ox.ac.uk")
        assert len(sent) == 1 and sent[0]["to"] == "sant7183@ox.ac.uk"


class TestRenewingAnOldEmailButton:
    def _setup(self, monkeypatch):
        t = TestEmailSignInLink()
        fake_db, sent, _ = t._setup(monkeypatch, users={"u1": {"username": "sherry", "email": "s@gmail.com"}})
        url = asyncio.run(auth.account_link("u1", "sant7183@ox.ac.uk", "/apply"))
        return fake_db, sent, url.split("t=", 1)[1]

    def test_a_used_button_offers_a_fresh_link_to_the_same_inbox(self, monkeypatch):
        fake_db, sent, token = self._setup(monkeypatch)
        asyncio.run(auth.confirm_email_link(_req(), t=token))             # used once
        page = asyncio.run(auth.email_link_page(_req(), t=token)).body.decode()
        assert "Email me a new link" in page
        res = asyncio.run(auth.renew_email_link(_req(), t=token))
        assert auth.mask_email("sant7183@ox.ac.uk") in res.body.decode()
        assert len(sent) == 1 and sent[0]["to"] == "sant7183@ox.ac.uk"
        fresh = sent[0]["cta_url"].split("t=", 1)[1]
        assert fake_db.collections[auth.LOGIN_LINKS][auth._hash(fresh)]["uid"] == "u1"

    def test_a_week_old_button_still_renews_but_not_a_very_old_one(self, monkeypatch):
        fake_db, sent, token = self._setup(monkeypatch)
        doc = fake_db.collections[auth.LOGIN_LINKS][auth._hash(token)]
        doc["expires_at"] = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=1)
        assert "Email me a new link" in asyncio.run(auth.email_link_page(_req(), t=token)).body.decode()
        doc["created_at"] = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=90)
        assert asyncio.run(auth.renew_email_link(_req(), t=token)).status_code == 400
        assert sent == []

    def test_an_unknown_token_renews_nothing(self, monkeypatch):
        _, sent, _ = self._setup(monkeypatch)
        assert asyncio.run(auth.renew_email_link(_req(), t="made-up")).status_code == 400
        assert sent == []


class TestMergedAccounts:
    """A merged extra account leads to the person's main account by every route."""

    def _setup(self, monkeypatch):
        fake_db = _FakeDB()
        fake_db.collections["users"] = {
            "main": {"username": "sherry", "email": "sherrytsai1218@gmail.com"},
            "extra": {"username": "SherryCai", "email": "x.mfe26@said.oxford.edu", "merged_into": "main"}}
        fake_db.collections["applications"] = {"main": {"oxford_email": "sant7183@ox.ac.uk", "status": "shortlisted"}}
        monkeypatch.setattr(db_module, "db", fake_db)

        class _FbUser:
            email_verified, disabled = True, False
        monkeypatch.setattr(auth.fb_auth, "get_user", lambda uid: _FbUser())
        return fake_db

    def test_an_old_session_for_the_extra_account_opens_the_main_one(self, monkeypatch):
        self._setup(monkeypatch)
        user = asyncio.run(auth.get_user_from_token(auth.create_token("extra")))
        assert user.id == "main" and user.username == "sherry"

    def test_signing_in_with_the_extra_login_gives_the_main_accounts_session(self, monkeypatch):
        self._setup(monkeypatch)
        monkeypatch.setattr(auth.fb_auth, "verify_id_token",
                            lambda t: {"uid": "extra", "email": "x.mfe26@said.oxford.edu", "email_verified": True})
        res = asyncio.run(auth.auth_firebase(_FakeRequest(), id_token="t", username=None))
        session = _re.search(r"__session=([^;]+)", res.headers["set-cookie"]).group(1)
        assert auth.jwt.decode(session, auth.SECRET_KEY, algorithms=[auth.ALGORITHM])["sub"] == "main"

    def test_a_sign_in_link_for_the_extra_address_leads_to_the_main_account(self, monkeypatch):
        fake_db = self._setup(monkeypatch)
        sent = []

        async def fake_send(to, subject, title, body_html, cta_label=None, cta_url=None, ics=None, cc=None):
            sent.append(cta_url)
            return True
        monkeypatch.setattr(auth.mailer, "send_email", fake_send)
        asyncio.run(auth.request_email_link(_req(forwarded_for="1.2.3.4"), email="x.mfe26@said.oxford.edu", next="/apply"))
        token = sent[0].split("t=", 1)[1]
        assert fake_db.collections[auth.LOGIN_LINKS][auth._hash(token)]["uid"] == "main"

    def test_the_oxford_email_offers_one_link_not_two(self, monkeypatch):
        fake_db = self._setup(monkeypatch)
        fake_db.collections["applications"]["extra"] = {"oxford_email": "sant7183@ox.ac.uk", "oxford_email_verified": True}
        sent = []

        async def fake_send(to, subject, title, body_html, cta_label=None, cta_url=None, ics=None, cc=None):
            sent.append((cta_url, body_html))
            return True
        monkeypatch.setattr(auth.mailer, "send_email", fake_send)
        asyncio.run(auth.request_email_link(_req(forwarded_for="1.2.3.5"), email="sant7183@ox.ac.uk", next="/apply"))
        assert len(sent) == 1 and sent[0][0] is not None      # a single button, not a choice of accounts
