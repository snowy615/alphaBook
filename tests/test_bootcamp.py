"""The Bootcamp board: who sees it, who writes it, and the files on it."""
import asyncio
import io
import itertools

import pytest
from fastapi import HTTPException, UploadFile
from starlette.datastructures import Headers

from app import bootcamp as bc
from app import db as db_module
from app import membership as mb
from app.models import User

ADMIN = User(id="adm", username="admin", is_admin=True)
ANALYST = User(id="an1", username="warren")
CAMPER = User(id="bc1", username="amy")
MEMBER = User(id="gm1", username="gus")


# ── A small in-memory Firestore and storage bucket ──────────────────────────

class _Doc:
    def __init__(self, store, doc_id):
        self._store, self.id = store, doc_id

    @property
    def exists(self):
        return self.id in self._store

    def to_dict(self):
        return dict(self._store[self.id]) if self.exists else None


class _Ref:
    def __init__(self, store, doc_id):
        self._store, self.id = store, doc_id

    async def get(self):
        return _Doc(self._store, self.id)

    async def set(self, data):
        self._store[self.id] = dict(data)

    async def update(self, patch):
        self._store[self.id].update(patch)

    async def delete(self):
        self._store.pop(self.id, None)


class _Coll:
    _ids = itertools.count(1)

    def __init__(self, store):
        self._store = store

    def document(self, doc_id=None):
        return _Ref(self._store, doc_id or f"p{next(self._ids)}")

    async def get(self):
        return [_Doc(self._store, k) for k in list(self._store)]


class _DB:
    def __init__(self):
        self.collections = {}

    def collection(self, name):
        return _Coll(self.collections.setdefault(name, {}))


class _Blob:
    def __init__(self, bucket, name):
        self._bucket, self.name = bucket, name

    def upload_from_string(self, data, content_type=None):
        self._bucket.blobs[self.name] = (data, content_type)

    def download_as_bytes(self):
        return self._bucket.blobs[self.name][0]

    def delete(self):
        self._bucket.blobs.pop(self.name)


class _Bucket:
    def __init__(self):
        self.blobs = {}

    def blob(self, name):
        return _Blob(self, name)


@pytest.fixture
def db(monkeypatch):
    fake = _DB()
    fake.collections["users"] = {
        "an1": {"username": "warren", "full_name": "Warren Jin", "membership": mb.M_QUANT_ANALYST},
        "bc1": {"username": "amy", "full_name": "Amy", "membership": mb.M_QUANT_BOOTCAMP},
        "gm1": {"username": "gus", "membership": mb.M_MEMBER},
        "adm": {"username": "admin"},
    }
    monkeypatch.setattr(db_module, "db", fake)
    bucket = _Bucket()
    monkeypatch.setattr(db_module, "bucket", bucket, raising=False)
    fake.bucket = bucket
    return fake


def run(coro):
    return asyncio.run(coro)


def _upload(name, data=b"%PDF-1.4 slides", content_type="application/pdf"):
    return UploadFile(file=io.BytesIO(data), filename=name, headers=Headers({"content-type": content_type}))


def _post(user=ANALYST, kind="lecture", title="Session 1: Intro to ML", body="Slides below."):
    return run(bc.create_post(bc.PostIn(kind=kind, title=title, body=body), user))["post"]


def _body(response):
    async def read():
        return b"".join([chunk async for chunk in response.body_iterator])
    return run(read())


# ── Who sees it ─────────────────────────────────────────────────────────────

class TestAccess:
    def test_analysts_and_admins_write_bootcampers_read_everyone_else_is_out(self, db):
        assert run(bc.access_of(ANALYST)) == bc.ACCESS_EDITOR
        assert run(bc.access_of(ADMIN)) == bc.ACCESS_EDITOR
        assert run(bc.access_of(CAMPER)) == bc.ACCESS_READER
        assert run(bc.access_of(MEMBER)) is None
        assert run(bc.access_of(None)) is None

    def test_general_members_are_turned_away_from_everything(self, db):
        for check in (bc.require_reader, bc.require_editor):
            with pytest.raises(HTTPException) as exc:
                run(check(MEMBER))
            assert exc.value.status_code == 403

    def test_bootcampers_cannot_post(self, db):
        assert run(bc.require_reader(CAMPER)) is CAMPER
        with pytest.raises(HTTPException) as exc:
            run(bc.require_editor(CAMPER))
        assert "Only Analysts" in exc.value.detail

    def test_the_access_endpoint_says_whether_to_show_the_link(self, db):
        assert run(bc.my_access(CAMPER)) == {"access": "reader"}
        assert run(bc.my_access(MEMBER)) == {"access": None}

    def test_a_legacy_bootcamp_track_counts(self, db):
        db.collections["users"]["old"] = {"username": "old", "track": "Quant Bootcamp"}
        assert run(bc.access_of(User(id="old", username="old"))) == bc.ACCESS_READER


# ── Posts ───────────────────────────────────────────────────────────────────

class TestPosts:
    def test_post_and_everyone_on_the_bootcamp_sees_it_newest_first(self, db):
        first = _post(title="Welcome to the Bootcamp", kind="announcement")
        second = _post(title="Session 1: Intro to ML")
        out = run(bc.list_posts(CAMPER))
        assert [p["title"] for p in out["posts"]] == ["Session 1: Intro to ML", "Welcome to the Bootcamp"]
        assert out["can_edit"] is False and run(bc.list_posts(ANALYST))["can_edit"] is True
        assert second["author_name"] == "Warren Jin" and first["kind"] == "announcement"
        assert len(out["sessions"]) == 6

    @pytest.mark.parametrize("req,words", [
        (bc.PostIn(kind="lecture", title="   "), "title"),
        (bc.PostIn(kind="memo", title="X"), "lecture or an announcement"),
        (bc.PostIn(kind="lecture", title="x" * 151), "under 150"),
        (bc.PostIn(kind="lecture", title="X", body="y" * 10_001), "under 10,000")])
    def test_bad_posts_are_refused(self, db, req, words):
        with pytest.raises(HTTPException) as exc:
            run(bc.create_post(req, ANALYST))
        assert words in exc.value.detail

    def test_any_analyst_can_edit_and_the_edit_is_signed(self, db):
        p = _post()
        db.collections["users"]["an2"] = {"username": "hao", "full_name": "Hao", "membership": mb.M_QUANT_ANALYST}
        out = run(bc.update_post(p["id"], bc.PostIn(kind="lecture", title="Session 1 (updated)", body="New"),
                                 User(id="an2", username="hao")))["post"]
        assert out["title"] == "Session 1 (updated)" and out["updated_by_name"] == "Hao"
        assert out["author_name"] == "Warren Jin"           # still theirs

    def test_deleting_a_post_takes_its_files_with_it(self, db):
        p = _post()
        run(bc.attach_file(p["id"], _upload("slides.pdf"), ANALYST))
        assert len(db.bucket.blobs) == 1
        run(bc.delete_post(p["id"], ANALYST))
        assert db.bucket.blobs == {} and run(bc.list_posts(CAMPER))["posts"] == []
        with pytest.raises(HTTPException) as exc:
            run(bc.delete_post(p["id"], ANALYST))
        assert exc.value.status_code == 404


# ── Files ───────────────────────────────────────────────────────────────────

class TestFiles:
    def test_attach_and_a_bootcamper_downloads_it(self, db):
        p = _post()
        post = run(bc.attach_file(p["id"], _upload("Week 1 slides.pdf"), ANALYST))["post"]
        f = post["files"][0]
        assert f["name"] == "Week 1 slides.pdf" and f["size"] == len(b"%PDF-1.4 slides")
        assert "blob" not in f                               # storage paths stay on the server
        resp = run(bc.download_file(p["id"], f["id"], user=CAMPER))
        assert _body(resp) == b"%PDF-1.4 slides"
        assert resp.media_type == "application/pdf"
        assert 'attachment; filename="Week 1 slides.pdf"' in resp.headers["content-disposition"]
        assert resp.headers["cache-control"] == "no-store"

    def test_names_are_made_safe(self, db):
        p = _post()
        post = run(bc.attach_file(p["id"], _upload('../../etc/pass"wd <x>.ipynb', b"{}", "application/json"),
                                  ANALYST))["post"]
        name = post["files"][0]["name"]
        assert name.endswith(".ipynb") and "/" not in name and '"' not in name and "<" not in name
        assert all(path.startswith(f"bootcamp/{p['id']}/") for path in db.bucket.blobs)

    @pytest.mark.parametrize("name,data,words", [
        ("virus.exe", b"MZ", "can't be attached"),
        ("noext", b"x", "can't be attached"),
        ("empty.pdf", b"", "empty")])
    def test_odd_files_are_refused(self, db, name, data, words):
        p = _post()
        with pytest.raises(HTTPException) as exc:
            run(bc.attach_file(p["id"], _upload(name, data), ANALYST))
        assert words in exc.value.detail

    def test_files_over_the_limit_are_refused(self, db, monkeypatch):
        monkeypatch.setattr(bc, "FILE_MAX_BYTES", 10)
        p = _post()
        with pytest.raises(HTTPException) as exc:
            run(bc.attach_file(p["id"], _upload("big.pdf", b"x" * 11), ANALYST))
        assert "up to" in exc.value.detail
        assert db.bucket.blobs == {}

    def test_removing_a_file(self, db):
        p = _post()
        f = run(bc.attach_file(p["id"], _upload("a.pdf"), ANALYST))["post"]["files"][0]
        run(bc.attach_file(p["id"], _upload("b.pdf"), ANALYST))
        post = run(bc.remove_file(p["id"], f["id"], ANALYST))["post"]
        assert [x["name"] for x in post["files"]] == ["b.pdf"] and len(db.bucket.blobs) == 1
        with pytest.raises(HTTPException) as exc:
            run(bc.download_file(p["id"], f["id"], user=CAMPER))
        assert exc.value.status_code == 404


class TestPages:
    def test_the_homepage_and_the_members_schedule_link_to_it(self):
        from pathlib import Path
        root = Path(bc.__file__).parent
        assert 'id="bootcampLink"' in (root / "templates" / "index.html").read_text()
        assert "/bootcamp/access" in (root / "static" / "landing.js").read_text()
        assert 'href="/bootcamp"' in (root / "static" / "apply.js").read_text()

    def test_the_route_is_registered(self):
        from app.main import app
        paths = {getattr(r, "path", "") for r in app.routes}
        assert {"/bootcamp", "/bootcamp/posts", "/bootcamp/posts/{post_id}/files/{file_id}"} <= paths


# ── Emailing a post ─────────────────────────────────────────────────────────

@pytest.fixture
def outbox(db, monkeypatch):
    sent = []

    async def fake_send(to, subject, title, body_html, cta_label=None, cta_url=None, **_):
        sent.append({"to": to, "subject": subject, "title": title, "body": body_html, "cta": cta_url})
        return "fail" not in to
    monkeypatch.setattr(bc.mailer, "send_email", fake_send)
    bc._email_locks.clear()
    users = db.collections["users"]
    for i in range(1, 11):          # ten more Bootcamp members
        users[f"m{i}"] = {"username": f"m{i}", "full_name": f"Member {i}", "email": f"m{i}@gmail.com",
                          "membership": mb.M_QUANT_BOOTCAMP}
    users["bc1"]["email"] = "amy@gmail.com"
    db.collections.setdefault("applications", {})["bc1"] = {"oxford_email": "amy@ox.ac.uk"}
    return sent


def _email_all(post_id, again=False):
    out, first = None, True
    while True:
        out = run(bc.email_post(post_id, bc.EmailRequest(again=again and first), ANALYST))
        first = False
        if not out["remaining"]:
            return out


class TestEmail:
    def test_every_member_gets_it_once_in_batches(self, db, outbox):
        p = _post(body="Read chapter 1.\n\nNotebook: https://colab.research.google.com/x")
        first = run(bc.email_post(p["id"], bc.EmailRequest(), ANALYST))
        assert len(outbox) == bc.EMAIL_BATCH and first["remaining"] == 11 - bc.EMAIL_BATCH
        out = _email_all(p["id"])
        assert len(outbox) == 11                                   # Amy and ten more, nobody twice
        assert len({m["to"] for m in outbox}) == 11
        assert out["post"]["email"]["done"] and out["post"]["email"]["sent"] == 11
        assert all("gus" not in m["to"] and "warren" not in m["to"] for m in outbox)   # members only

    def test_the_email_itself(self, db, outbox):
        p = _post(body="Read chapter 1.\n\nNotebook: https://colab.research.google.com/x")
        run(bc.attach_file(p["id"], _upload("slides.pdf"), ANALYST))
        _email_all(p["id"])
        amy = next(m for m in outbox if "amy" in m["to"])
        assert amy["to"] == "amy@ox.ac.uk, amy@gmail.com"          # Oxford and personal
        assert amy["subject"] == "Quant Bootcamp: Session 1: Intro to ML" and amy["title"] == "New lecture"
        assert "<p>Hi Amy,</p>" in amy["body"] and "slides.pdf" in amy["body"]
        assert '<a href="https://colab.research.google.com/x">' in amy["body"]
        assert amy["cta"].endswith("/bootcamp")

    def test_opted_out_addresses_are_left_off(self, db, outbox):
        db.collections["users"]["bc1"]["email_opt_out"] = ["amy@gmail.com"]
        _email_all(_post()["id"])
        assert next(m for m in outbox if "amy" in m["to"])["to"] == "amy@ox.ac.uk"

    def test_a_second_send_needs_again_and_failures_are_counted(self, db, outbox):
        db.collections["users"]["m3"]["email"] = "fail@gmail.com"
        p = _post()
        out = _email_all(p["id"])
        assert out["post"]["email"]["failed"] == 1 and out["post"]["email"]["sent"] == 10
        with pytest.raises(HTTPException) as exc:
            run(bc.email_post(p["id"], bc.EmailRequest(), ANALYST))
        assert exc.value.status_code == 409
        outbox.clear()
        _email_all(p["id"], again=True)
        assert len(outbox) == 11

    def test_only_analysts_see_the_email_record(self, db, outbox):
        p = _post()
        _email_all(p["id"])
        assert "email" not in run(bc.list_posts(CAMPER))["posts"][0]
        staff = run(bc.list_posts(ANALYST))
        assert staff["posts"][0]["email"]["sent"] == 11 and staff["member_count"] == 11


# ── Viewing without downloading ─────────────────────────────────────────────

class TestView:
    def test_which_files_open_where(self):
        assert bc.viewer_of("slides.pdf", 1) == "browser" and bc.viewer_of("plot.PNG", 1) == "browser"
        assert bc.viewer_of("deck.pptx", 2_700_000) == "office" and bc.viewer_of("sheet.xlsx", 1) == "office"
        assert bc.viewer_of("deck.pptx", 11 * 1024 * 1024) is None          # too big for the Office viewer
        assert bc.viewer_of("sheet.xlsx", 6 * 1024 * 1024) is None
        assert bc.viewer_of("intro.ipynb", 1) is None and bc.viewer_of("code.zip", 1) is None

    def test_a_pdf_opens_in_the_browser(self, db):
        p = _post()
        f = run(bc.attach_file(p["id"], _upload("slides.pdf"), ANALYST))["post"]["files"][0]
        assert f["view"] == "browser"
        r = run(bc.view_file(p["id"], f["id"], CAMPER))
        assert r.status_code == 303 and r.headers["location"].endswith(f"/files/{f['id']}?inline=1")
        resp = run(bc.download_file(p["id"], f["id"], inline=True, user=CAMPER))
        assert resp.headers["content-disposition"].startswith("inline;") and resp.media_type == "application/pdf"
        assert resp.headers["x-content-type-options"] == "nosniff"

    def test_text_is_shown_as_text(self, db):
        p = _post()
        f = run(bc.attach_file(p["id"], _upload("model.py", b"print(1)", "text/x-python"), ANALYST))["post"]["files"][0]
        resp = run(bc.download_file(p["id"], f["id"], inline=True, user=CAMPER))
        assert resp.media_type.startswith("text/plain")

    def test_powerpoint_opens_in_the_office_viewer_through_a_short_link(self, db):
        from urllib.parse import parse_qs, unquote, urlparse
        p = _post()
        pptx = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
        f = run(bc.attach_file(p["id"], _upload("Session 1.pptx", b"PK deck", pptx), ANALYST))["post"]["files"][0]
        assert f["view"] == "office"
        r = run(bc.view_file(p["id"], f["id"], CAMPER))
        loc = r.headers["location"]
        assert loc.startswith(bc.OFFICE_VIEWER)
        src = parse_qs(urlparse(loc).query)["src"][0]
        assert src.startswith(f"{bc.BASE_URL}/bootcamp/shared/") and src.endswith("/Session%201.pptx")
        token = src.split("/shared/")[1].split("/")[0]
        resp = run(bc.shared_file(token, unquote(src.rsplit("/", 1)[1])))      # what Microsoft fetches
        assert _body(resp) == b"PK deck" and resp.headers["content-disposition"].startswith("inline;")

    def test_a_view_link_is_for_one_file_and_cannot_sign_anyone_in(self, db, monkeypatch):
        from app import auth
        p = _post()
        f = run(bc.attach_file(p["id"], _upload("a.pptx", b"PK"), ANALYST))["post"]["files"][0]
        token = bc._view_token(p["id"], f["id"])
        assert run(auth.get_user_from_token(token)) is None
        login = auth.create_token("bc1")
        with pytest.raises(HTTPException) as exc:
            run(bc.shared_file(login, "a.pptx"))                  # a sign-in token opens nothing
        assert exc.value.status_code == 404
        with pytest.raises(HTTPException):
            run(bc.shared_file(token[:-3] + "abc", "a.pptx"))     # tampered

    def test_view_links_expire(self, db, monkeypatch):
        import datetime as dt
        p = _post()
        f = run(bc.attach_file(p["id"], _upload("a.pptx", b"PK"), ANALYST))["post"]["files"][0]
        real_now = bc._now
        monkeypatch.setattr(bc, "_now", lambda: real_now() - dt.timedelta(seconds=bc.VIEW_LINK_SECONDS + 60))
        token = bc._view_token(p["id"], f["id"])
        monkeypatch.setattr(bc, "_now", real_now)
        with pytest.raises(HTTPException) as exc:
            run(bc.shared_file(token, "a.pptx"))
        assert "expired" in exc.value.detail

    def test_zips_cannot_be_viewed(self, db):
        p = _post()
        f = run(bc.attach_file(p["id"], _upload("code.zip", b"PK"), ANALYST))["post"]["files"][0]
        with pytest.raises(HTTPException) as exc:
            run(bc.view_file(p["id"], f["id"], CAMPER))
        assert "download it instead" in exc.value.detail


class TestDirectLinks:
    """With credentials that can sign, files come straight from storage."""

    @pytest.fixture
    def signing(self, db, monkeypatch):
        calls = []

        def generate_signed_url(self, **kw):
            calls.append((self.name, kw))
            return f"https://storage.example/{self.name}?sig=1"
        monkeypatch.setattr(_Blob, "generate_signed_url", generate_signed_url, raising=False)
        return calls

    def test_downloads_redirect_to_a_short_lived_storage_link(self, db, signing):
        p = _post()
        f = run(bc.attach_file(p["id"], _upload("Week 1.pdf"), ANALYST))["post"]["files"][0]
        r = run(bc.download_file(p["id"], f["id"], user=CAMPER))
        assert r.status_code == 303 and r.headers["location"].startswith("https://storage.example/bootcamp/")
        assert r.headers["cache-control"] == "no-store"
        kw = signing[-1][1]
        assert kw["response_disposition"].startswith('attachment; filename="Week 1.pdf"')
        assert kw["expiration"].total_seconds() == bc.DIRECT_LINK_SECONDS

    def test_viewing_in_the_browser_asks_storage_to_show_it(self, db, signing):
        p = _post()
        f = run(bc.attach_file(p["id"], _upload("model.py", b"x=1", "text/x-python"), ANALYST))["post"]["files"][0]
        r = run(bc.view_file(p["id"], f["id"], CAMPER))
        assert r.headers["location"].startswith("https://storage.example/")
        kw = signing[-1][1]
        assert kw["response_disposition"].startswith("inline;") and kw["response_type"].startswith("text/plain")

    def test_the_office_viewer_still_gets_the_server_link(self, db, signing):
        p = _post()
        f = run(bc.attach_file(p["id"], _upload("deck.pptx", b"PK"), ANALYST))["post"]["files"][0]
        r = run(bc.view_file(p["id"], f["id"], CAMPER))
        assert r.headers["location"].startswith(bc.OFFICE_VIEWER) and "storage.example" not in r.headers["location"]

    def test_outsiders_never_get_a_link(self, db, signing):
        with pytest.raises(HTTPException):
            run(bc.require_reader(MEMBER))


class TestSpeed:
    def test_loading_the_page_reads_each_collection_once(self, db, monkeypatch):
        reads = []
        real = db.collection

        def counting(name):
            coll = real(name)
            original = coll.document

            def document(doc_id=None):
                ref = original(doc_id)
                if name == "applications":
                    reads.append(doc_id)
                return ref
            coll.document = document
            return coll
        monkeypatch.setattr(db, "collection", counting)
        run(bc.list_posts(ANALYST))
        assert reads == []                     # no read per member


# ── Attendance ──────────────────────────────────────────────────────────────

S1, S2, S3 = "2026-10-25", "2026-11-01", "2026-11-08"


def _mark(session, marks, user=ANALYST):
    return run(bc.mark_attendance(session, bc.AttendanceIn(marks=marks), user))


@pytest.fixture
def roster(db):
    db.collections["users"]["bc2"] = {"username": "ben", "full_name": "Ben Ng", "membership": mb.M_QUANT_BOOTCAMP}
    db.collections.setdefault("applications", {})["bc1"] = {"oxford_email": "amy@ox.ac.uk"}
    return db


class TestAttendance:
    def test_the_percentage(self):
        s = bc.attendance_summary
        assert s({}) == {"attended": 0, "counted": 0, "percent": None}
        assert s({S1: "present", S2: "late", S3: "absent"}) == {"attended": 1.8, "counted": 3, "percent": 60}
        assert s({S1: "late"})["percent"] == 80                                                    # late is 0.8
        assert s({S1: "present", S2: "excused"}) == {"attended": 1, "counted": 1, "percent": 100}   # excused doesn't count

    def test_analysts_mark_and_see_everyone(self, roster):
        _mark(S1, {"bc1": "present", "bc2": "absent"})
        _mark(S2, {"bc1": "late"})
        out = run(bc.attendance(ANALYST))
        assert out["can_edit"] and len(out["sessions"]) == 6
        people = {m["name"]: m for m in out["members"]}
        assert set(people) == {"Amy", "Ben Ng"}                         # Bootcamp members only
        assert people["Amy"]["marks"] == {S1: "present", S2: "late"} and people["Amy"]["percent"] == 90
        assert people["Amy"]["email"] == "amy@ox.ac.uk"
        assert people["Ben Ng"]["percent"] == 0

    def test_a_member_sees_only_their_own(self, roster):
        _mark(S1, {"bc1": "present", "bc2": "absent"})
        _mark(S2, {"bc1": "absent"})
        mine = run(bc.attendance(CAMPER))
        assert mine["can_edit"] is False and "members" not in mine
        assert mine["mine"] == {"marks": {S1: "present", S2: "absent"}, "attended": 1, "counted": 2, "percent": 50}

    def test_members_and_outsiders_cannot_mark(self, roster):
        for who in (CAMPER, MEMBER):
            with pytest.raises(HTTPException) as exc:
                run(bc.require_editor(who))
            assert exc.value.status_code == 403
        with pytest.raises(HTTPException):
            run(bc.require_reader(MEMBER))

    def test_marking_keeps_the_rest_and_can_clear(self, roster):
        _mark(S1, {"bc1": "present", "bc2": "present"})
        out = _mark(S1, {"bc2": "late"})
        assert out["marks"] == {"bc1": "present", "bc2": "late"}
        assert _mark(S1, {"bc1": None})["marks"] == {"bc2": "late"}
        assert db_module.db.collections["bootcamp_attendance"][S1]["updated_by_name"] == "Warren Jin"

    @pytest.mark.parametrize("session,marks,words", [
        ("2026-12-25", {"bc1": "present"}, "No such session"),
        (S1, {"bc1": "here"}, "present, late, absent or excused"),
        (S1, {"gm1": "present"}, "isn't in the Bootcamp")])
    def test_bad_marks_are_refused(self, roster, session, marks, words):
        with pytest.raises(HTTPException) as exc:
            _mark(session, marks)
        assert words in exc.value.detail

    def test_someone_who_leaves_keeps_their_record_on_the_register(self, roster):
        _mark(S1, {"bc2": "present"})
        roster.collections["users"]["bc2"]["membership"] = mb.M_MEMBER
        from app import applications as ap
        ap._collection_cache.clear()
        names = [m["name"] for m in run(bc.attendance(ANALYST))["members"]]
        assert "(no longer in the Bootcamp)" in names
        _mark(S1, {"bc2": None})                       # and their mark can still be cleared
