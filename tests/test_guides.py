"""The /guide page: applicants' guide for everyone, the analyst guide (and
its screenshots) only for Analyst members and admins."""
import asyncio

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app import guides
from app import membership as mb
from app.models import User

from tests.test_applications import _FakeDB


def _request(query=b""):
    return Request({"type": "http", "method": "GET", "path": "/guide", "headers": [],
                    "query_string": query, "server": ("t", 80), "scheme": "http", "root_path": ""})


@pytest.fixture
def people(monkeypatch):
    fake = _FakeDB()
    fake.collections["users"] = {
        "an": {"username": "an", "membership": mb.M_QUANT_ANALYST},
        "boot": {"username": "boot", "membership": mb.M_QUANT_BOOTCAMP},
        "pub": {"username": "pub", "membership": mb.M_PUBLIC},
    }
    monkeypatch.setattr(guides.ap.db_module, "db", fake)
    import app.events as ev
    monkeypatch.setattr(ev.db_module, "db", fake)
    return {"analyst": User(id="an", username="an"), "bootcamp": User(id="boot", username="boot"),
            "public": User(id="pub", username="pub"), "admin": User(id="ad", username="ad", is_admin=True),
            "guest": None}


def _page(user, query=b""):
    r = asyncio.run(guides.guide(_request(query), user))
    return r, r.body.decode()


@pytest.mark.parametrize("who", ["guest", "public", "bootcamp"])
def test_everyone_else_gets_the_applicant_guide(people, who):
    r, html = _page(people[who])
    assert "Applicant guide" in html and "Analyst guide</h2>" not in html
    assert "/guide/img/" not in html and "Automatic shortlist" not in html
    assert 'href="/guide" class="btn ghost">Analyst guide' not in html
    assert r.headers["cache-control"] == "private, no-store"


def test_non_analysts_cant_ask_for_the_analyst_guide(people):
    _, html = _page(people["public"], b"for=analyst")
    assert "Analyst guide</h2>" not in html


@pytest.mark.parametrize("who", ["analyst", "admin"])
def test_analysts_and_admins_get_the_analyst_guide(people, who):
    r, html = _page(people[who])
    assert "Analyst guide</h2>" in html
    assert "out of 18" in html and "/guide/img/analyst-review.jpg" in html
    assert r.headers["cache-control"] == "private, no-store"
    _, applicant = _page(people[who], b"for=applicant")
    assert "Applicant guide</h2>" in applicant and 'href="/guide" class="btn ghost">Analyst guide' in applicant


def test_the_analyst_guide_follows_the_live_rubrics(people):
    _, html = _page(people["analyst"])
    for c in guides.ap.ONLINE_CV_RUBRIC + guides.ap.ONSITE_CV_RUBRIC:
        assert c["label"] in html
    for reason in guides.ap.AUTO_SHORTLIST_REASONS.values():
        assert reason in html
    assert "Hint 1 at 3:00" in html and "Hint 2 at 6:00" in html


def test_analyst_screenshots_are_for_analysts_only(people):
    for who in ("guest", "public", "bootcamp"):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(guides.analyst_guide_image("analyst-review.jpg", people[who]))
        assert exc.value.status_code == 404
    r = asyncio.run(guides.analyst_guide_image("analyst-review.jpg", people["analyst"]))
    assert r.media_type == "image/jpeg"
    for bad in ("../guides.py", "nope.jpg", "..%2Fguides.py"):
        with pytest.raises(HTTPException):
            asyncio.run(guides.analyst_guide_image(bad, people["analyst"]))


def test_no_analyst_material_is_published_as_a_static_file():
    from pathlib import Path
    static = Path(guides.__file__).parent / "static"
    assert not [p for p in static.rglob("analyst-*")]


def test_no_em_or_en_dashes_in_either_guide(people):
    for who in ("public", "analyst"):
        _, html = _page(people[who])
        assert "—" not in html and "–" not in html


def test_the_qr_code_points_at_the_apply_page():
    from pathlib import Path
    svg = Path(guides.__file__).parent / "static" / "guide-assets" / "alphabook-apply-qr.svg"
    assert svg.read_text().startswith("<svg")
