"""Tests for app.applications — the programme application and its OA.

Pinned here: the clock machinery that runs a sitting — the motivation
question (MOTIVATION_SECONDS) expiring into the estimation one
(ESTIMATION_SECONDS), and the overall session hard stop (SESSION_SECONDS),
including the case where the estimation clock runs out on its own before the
session clock would (motivation finished early). Also Bootcamp/Analyst/
Fundamental/Quant eligibility (including which programmes are currently
disabled), the Oxford email and student checks, reviewer scoring, interview
scheduling and the admin escape hatches (redo, delete). All pure functions
over plain dicts, same approach as test_interview_oa.py — no Firestore
involved.
"""

import asyncio
import datetime as dt

import pytest
from fastapi import HTTPException

from app import applications as ap
from app import membership as mb
from app.models import User


# ── A minimal Firestore stand-in for the tests below that exercise the
# actual endpoint functions (require_reviewer, decide, submit_score) rather
# than the pure state-machine helpers above. Async to match the real client.
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
        return _FakeDoc(self._store.get(self._key))

    async def set(self, data):
        self._store[self._key] = dict(data)

    async def update(self, patch):
        self._store.setdefault(self._key, {}).update(patch)

    async def delete(self):
        self._store.pop(self._key, None)


class _FakeCollection:
    def __init__(self, store):
        self._store = store

    def document(self, doc_id):
        return _FakeDocRef(self._store, doc_id)

    async def get(self):
        return [_FakeDoc(data, id=doc_id) for doc_id, data in self._store.items()]


class _FakeDB:
    def __init__(self):
        self.collections: dict = {}

    def collection(self, name):
        self.collections.setdefault(name, {})
        return _FakeCollection(self.collections[name])


def ago(seconds: float) -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=seconds)


def make_app(
    *,
    section="motivation",
    started_seconds_ago=0.0,
    estimation_seconds_ago=0.0,
    motivation_text="",
    estimation_text="",
    status=ap.S_OA_ACTIVE,
):
    """An application mid-assessment, with both clocks set explicitly."""
    oa = {
        "started_at": ago(started_seconds_ago),
        "section": section,
        "motivation": {"prompt": ap.MOTIVATION_PROMPT, "text": motivation_text},
        "estimation": {"prompt": ap.ESTIMATION_PROMPT, "text": estimation_text},
    }
    if section == "estimation":
        oa["estimation_started_at"] = ago(estimation_seconds_ago)
    return {
        "user_id": "u1", "username": "cand", "programme": mb.M_QUANT_ANALYST,
        "status": status, "oa": oa, "flags": {"paste": 0, "left_page": 0},
    }


class TestMotivationSection:
    def test_stays_put_while_the_clock_runs(self):
        application = make_app(started_seconds_ago=60)
        assert ap.resolve(application) is False
        assert application["oa"]["section"] == "motivation"

    def test_expires_into_the_estimation_section(self):
        application = make_app(started_seconds_ago=ap.MOTIVATION_SECONDS + 1,
                               motivation_text="half an answer")
        assert ap.resolve(application) is True
        oa = application["oa"]
        assert oa["section"] == "estimation"
        # Whatever was autosaved is banked, not discarded.
        assert oa["motivation"]["text"] == "half an answer"
        assert oa["motivation"]["submitted_at"] is not None
        assert oa["estimation_started_at"] is not None

    def test_closing_early_opens_the_estimation_section(self):
        application = make_app(started_seconds_ago=90, motivation_text="done early")
        ap._close_motivation(application["oa"])
        oa = application["oa"]
        assert oa["section"] == "estimation"
        assert oa["motivation"]["word_count"] == 2
        # Finishing the first answer early buys no extra time on the second —
        # its own clock simply starts now, with the full ten minutes on it.
        assert round(ap._left(oa["estimation_started_at"], ap.ESTIMATION_SECONDS)) == \
            ap.ESTIMATION_SECONDS


class TestEstimationSection:
    def test_stays_put_while_the_clock_runs(self):
        application = make_app(section="estimation", started_seconds_ago=310,
                               estimation_seconds_ago=60)
        assert ap.resolve(application) is False
        assert application["oa"]["section"] == "estimation"

    def test_live_section_is_left_alone(self):
        application = make_app(section="estimation", started_seconds_ago=310,
                               estimation_seconds_ago=10)
        assert ap.resolve(application) is False
        assert application["status"] == ap.S_OA_ACTIVE

    def test_running_out_finishes_the_whole_sitting_before_the_session_clock_would(self):
        # Motivation was finished early (used only 60s of its allotment), so
        # estimation opened early too. Its own clock can then run out well
        # before the overall session clock would — proving the estimation
        # timeout is a real, independently-reachable branch, not just the
        # session hard stop under another name.
        application = make_app(
            section="estimation", started_seconds_ago=300, estimation_seconds_ago=240,
            estimation_text="got most of the way there")
        assert ap._left(application["oa"]["started_at"], ap.SESSION_SECONDS) > 0  # session not over

        assert ap.resolve(application) is True

        oa = application["oa"]
        assert application["status"] == ap.S_SUBMITTED
        assert oa["section"] == "done"
        assert oa["estimation"]["text"] == "got most of the way there"
        assert oa["finish_reason"] == "completed"

    def test_finish_banks_the_estimation_answer(self):
        application = make_app(section="estimation", started_seconds_ago=800,
                               estimation_seconds_ago=30, estimation_text="my reasoning")
        ap._finish(application, "completed")
        oa = application["oa"]
        assert application["status"] == ap.S_SUBMITTED
        assert oa["section"] == "done"
        assert oa["estimation"]["text"] == "my reasoning"
        assert oa["estimation"]["word_count"] == 2
        assert oa["estimation"]["submitted_at"] is not None


class TestSessionDeadline:
    def test_the_session_length_is_a_hard_stop(self):
        application = make_app(section="estimation",
                               started_seconds_ago=ap.SESSION_SECONDS + 1,
                               estimation_seconds_ago=5, estimation_text="in progress")
        assert ap.resolve(application) is True
        assert application["status"] == ap.S_SUBMITTED
        assert application["oa"]["finish_reason"] == "session_expired"
        assert application["oa"]["estimation"]["text"] == "in progress"

    def test_an_expired_sitting_left_in_the_motivation_section_still_closes(self):
        application = make_app(started_seconds_ago=ap.SESSION_SECONDS + 30,
                               motivation_text="only ever wrote this")
        assert ap.resolve(application) is True
        assert application["status"] == ap.S_SUBMITTED
        oa = application["oa"]
        assert oa["motivation"]["text"] == "only ever wrote this"
        # Both sections are banked even though estimation was never opened.
        assert oa["estimation"]["text"] == ""
        assert oa["estimation"]["submitted_at"] is not None

    def test_a_finished_application_is_never_reopened(self):
        application = make_app(section="done", status=ap.S_SUBMITTED)
        assert ap.resolve(application) is False
        assert application["status"] == ap.S_SUBMITTED


class TestEligibility:
    def test_general_public_may_apply(self):
        assert mb.can_apply({"membership": mb.M_PUBLIC}) is True

    def test_general_member_may_apply(self):
        assert mb.can_apply({"membership": mb.M_MEMBER}) is True

    def test_an_account_with_no_membership_set_may_apply(self):
        assert mb.can_apply({}) is True

    def test_analyst_is_the_ceiling_for_both_tracks(self):
        assert mb.can_apply({"membership": mb.M_QUANT_ANALYST}) is False
        assert mb.can_apply({"membership": mb.M_FUND_ANALYST}) is False

    def test_quant_bootcamp_can_still_apply_on_to_quant_analyst(self):
        assert mb.can_apply({"membership": mb.M_QUANT_BOOTCAMP}) is True
        assert mb.apply_programmes_for(mb.M_QUANT_BOOTCAMP) == [mb.M_QUANT_ANALYST]

    def test_fundamental_bootcamp_can_still_apply_on_to_fundamental_analyst(self):
        assert mb.can_apply({"membership": mb.M_FUND_BOOTCAMP}) is True
        assert mb.apply_programmes_for(mb.M_FUND_BOOTCAMP) == [mb.M_FUND_ANALYST]

    def test_recruiters_and_hosts_are_on_the_other_side_of_the_table(self):
        assert mb.can_apply({"membership": mb.M_PUBLIC, "role": mb.ROLE_RECRUITER}) is False
        assert mb.can_apply({"membership": mb.M_PUBLIC, "role": mb.ROLE_HOST}) is False
        assert mb.can_apply({"membership": mb.M_PUBLIC, "is_admin": True}) is False

    def test_new_applicants_see_exactly_the_four_tracks(self):
        # No combined "both at once" option — apply to each separately.
        programmes = mb.apply_programmes_for(mb.M_PUBLIC)
        assert set(programmes) == {
            mb.M_FUND_BOOTCAMP, mb.M_QUANT_BOOTCAMP,
            mb.M_FUND_ANALYST, mb.M_QUANT_ANALYST,
        }


class TestOxfordEmail:
    def test_recognises_college_and_department_subdomains(self):
        assert ap.is_oxford_email("jo@merton.ox.ac.uk") is True
        assert ap.is_oxford_email("jo@admin.ox.ac.uk") is True
        assert ap.is_oxford_email("jo@ox.ac.uk") is True

    def test_case_and_whitespace_do_not_matter(self):
        assert ap.is_oxford_email("  Jo@Merton.OX.AC.UK  ") is True

    def test_rejects_lookalikes_and_other_domains(self):
        # Anchored to the end of the string, so a domain that merely contains
        # "ox.ac.uk" earlier on does not slip through.
        assert ap.is_oxford_email("jo@ox.ac.uk.evil.com") is False
        assert ap.is_oxford_email("jo@notox.ac.uk") is False
        assert ap.is_oxford_email("jo@gmail.com") is False
        assert ap.is_oxford_email("") is False
        assert ap.is_oxford_email(None) is False

    def test_an_oxford_account_needs_no_prompt(self):
        assert ap._resolve_oxford_email("jo@merton.ox.ac.uk", None) == "jo@merton.ox.ac.uk"
        # The account's own address wins even if a different one was passed in —
        # the field only exists to fill the gap for a personal account.
        assert ap._resolve_oxford_email("jo@merton.ox.ac.uk", "ignored@gmail.com") == "jo@merton.ox.ac.uk"

    def test_a_personal_account_must_supply_a_valid_oxford_address(self):
        with pytest.raises(HTTPException):
            ap._resolve_oxford_email("jo@gmail.com", None)
        with pytest.raises(HTTPException):
            ap._resolve_oxford_email("jo@gmail.com", "still-not-oxford@gmail.com")
        with pytest.raises(HTTPException):
            ap._resolve_oxford_email("jo@gmail.com", "not even an email")

    def test_a_personal_account_with_a_valid_oxford_address_is_accepted(self):
        assert ap._resolve_oxford_email("jo@gmail.com", " JO@Merton.OX.AC.UK ") == "jo@merton.ox.ac.uk"


class TestSubmissionEmail:
    """
    The one email every candidate gets fires exactly once, however many times
    the submission gets noticed — /apply/state is polled once a second while
    the OA is live, so the same completed sitting gets resolved over and over.
    """

    def _patch_io(self, monkeypatch):
        saved, sent = [], []

        async def fake_save(uid, application):
            saved.append(uid)

        async def fake_send(to, subject, title, body_html, cta_label=None, cta_url=None):
            sent.append({"to": to, "subject": subject})
            return True

        monkeypatch.setattr(ap, "_save", fake_save)
        monkeypatch.setattr(ap.mailer, "send_email", fake_send)
        return saved, sent

    def test_resolve_and_notify_sends_once_across_repeated_polls(self, monkeypatch):
        saved, sent = self._patch_io(monkeypatch)
        application = make_app(
            section="estimation", started_seconds_ago=ap.SESSION_SECONDS + 1,
            estimation_seconds_ago=5, estimation_text="in progress")
        application["oxford_email"] = "jo@merton.ox.ac.uk"

        assert asyncio.run(ap._resolve_and_notify("u1", application)) is True
        assert application["status"] == ap.S_SUBMITTED
        assert len(sent) == 1
        assert sent[0]["to"] == "jo@merton.ox.ac.uk"

        # Polled again after it has already closed out: nothing left to
        # resolve, so no second email and no second save.
        saves_before = len(saved)
        assert asyncio.run(ap._resolve_and_notify("u1", application)) is False
        assert len(sent) == 1
        assert len(saved) == saves_before

    def test_finish_and_persist_sends_once_even_if_called_twice(self, monkeypatch):
        saved, sent = self._patch_io(monkeypatch)
        application = make_app(section="estimation", started_seconds_ago=800,
                               estimation_seconds_ago=30, estimation_text="my reasoning")
        application["oxford_email"] = "jo@merton.ox.ac.uk"

        asyncio.run(ap._finish_and_persist("u1", application, "completed"))
        assert application["status"] == ap.S_SUBMITTED
        assert len(sent) == 1

        # A second call (e.g. a retried request) must not send twice.
        asyncio.run(ap._finish_and_persist("u1", application, "completed"))
        assert len(sent) == 1

    def test_falls_back_to_the_account_email_with_no_oxford_email_on_file(self, monkeypatch):
        saved, sent = self._patch_io(monkeypatch)
        application = make_app(section="estimation", started_seconds_ago=800,
                               estimation_seconds_ago=30, estimation_text="my reasoning")
        application["email"] = "jo@merton.ox.ac.uk"   # no oxford_email key at all

        asyncio.run(ap._finish_and_persist("u1", application, "completed"))
        assert sent[0]["to"] == "jo@merton.ox.ac.uk"

    def test_no_address_on_file_sends_nothing_and_does_not_raise(self, monkeypatch):
        saved, sent = self._patch_io(monkeypatch)
        application = make_app(section="estimation", started_seconds_ago=800,
                               estimation_seconds_ago=30, estimation_text="my reasoning")

        asyncio.run(ap._finish_and_persist("u1", application, "completed"))
        assert sent == []
        assert application["status"] == ap.S_SUBMITTED


class TestReviewSummary:
    def test_averages_only_the_reviewers_who_gave_that_score(self):
        application = {"reviews": {
            "r1": {"reviewer_name": "Alice", "cv_score": 8, "written_score": 6},
            "r2": {"reviewer_name": "Bob", "cv_score": 10, "written_score": None},
        }}
        summary = ap._review_summary(application, viewer_id="r1")
        assert summary["count"] == 2
        assert summary["cv_avg"] == 9.0
        assert summary["written_avg"] == 6.0
        assert summary["mine"]["reviewer_name"] == "Alice"

    def test_no_reviews_yet(self):
        summary = ap._review_summary({}, viewer_id="r1")
        assert summary == {
            "count": 0, "cv_avg": None, "written_avg": None, "interview_avg": None,
            "entries": [], "mine": None,
        }


class TestRequireReviewer:
    def test_admin_is_always_a_reviewer(self, monkeypatch):
        monkeypatch.setattr(ap.db_module, "db", _FakeDB())
        admin = User(id="a1", username="root", is_admin=True)
        assert asyncio.run(ap.require_reviewer(admin)) is admin

    def test_quant_analyst_member_is_a_reviewer(self, monkeypatch):
        fake_db = _FakeDB()
        fake_db.collections["users"] = {"u2": {"membership": mb.M_QUANT_ANALYST}}
        monkeypatch.setattr(ap.db_module, "db", fake_db)
        user = User(id="u2", username="alice")
        assert asyncio.run(ap.require_reviewer(user)) is user

    def test_general_member_is_refused(self, monkeypatch):
        fake_db = _FakeDB()
        fake_db.collections["users"] = {"u3": {"membership": mb.M_PUBLIC}}
        monkeypatch.setattr(ap.db_module, "db", fake_db)
        user = User(id="u3", username="bob")
        with pytest.raises(HTTPException):
            asyncio.run(ap.require_reviewer(user))

    def test_bootcamp_member_is_refused(self, monkeypatch):
        fake_db = _FakeDB()
        fake_db.collections["users"] = {"u4": {"membership": mb.M_QUANT_BOOTCAMP}}
        monkeypatch.setattr(ap.db_module, "db", fake_db)
        user = User(id="u4", username="cara")
        with pytest.raises(HTTPException):
            asyncio.run(ap.require_reviewer(user))


class TestScoring:
    def _patch(self, monkeypatch, application):
        store = {"u1": application}

        async def fake_load(uid):
            return store.get(uid)

        async def fake_save(uid, app_):
            store[uid] = app_

        monkeypatch.setattr(ap, "_load", fake_load)
        monkeypatch.setattr(ap, "_save", fake_save)
        return store

    def _base(self, status=ap.S_SUBMITTED):
        return {"user_id": "u1", "username": "jo", "status": status, "reviews": {}}

    def test_rejects_an_out_of_range_score(self, monkeypatch):
        self._patch(monkeypatch, self._base())
        reviewer = User(id="r1", username="alice")
        with pytest.raises(HTTPException):
            asyncio.run(ap.submit_score("u1", ap.ReviewScore(cv_score=11), reviewer))
        with pytest.raises(HTTPException):
            asyncio.run(ap.submit_score("u1", ap.ReviewScore(written_score=0), reviewer))

    def test_requires_at_least_one_score(self, monkeypatch):
        self._patch(monkeypatch, self._base())
        reviewer = User(id="r1", username="alice")
        with pytest.raises(HTTPException):
            asyncio.run(ap.submit_score("u1", ap.ReviewScore(), reviewer))

    def test_cannot_score_before_the_assessment_is_submitted(self, monkeypatch):
        self._patch(monkeypatch, self._base(status=ap.S_OA_ACTIVE))
        reviewer = User(id="r1", username="alice")
        with pytest.raises(HTTPException):
            asyncio.run(ap.submit_score("u1", ap.ReviewScore(cv_score=8), reviewer))

    def test_two_independent_reviewers_are_averaged(self, monkeypatch):
        self._patch(monkeypatch, self._base())
        alice = User(id="r1", username="alice")
        bob = User(id="r2", username="bob")
        asyncio.run(ap.submit_score("u1", ap.ReviewScore(cv_score=8, written_score=6), alice))
        result = asyncio.run(ap.submit_score("u1", ap.ReviewScore(cv_score=10, written_score=8), bob))
        assert result["review"]["count"] == 2
        assert result["review"]["cv_avg"] == 9.0
        assert result["review"]["written_avg"] == 7.0

    def test_resubmitting_updates_your_own_score_not_a_new_one(self, monkeypatch):
        self._patch(monkeypatch, self._base())
        alice = User(id="r1", username="alice")
        asyncio.run(ap.submit_score("u1", ap.ReviewScore(cv_score=8, written_score=6), alice))
        result = asyncio.run(ap.submit_score("u1", ap.ReviewScore(cv_score=9), alice))
        assert result["review"]["count"] == 1
        assert result["review"]["cv_avg"] == 9.0
        # Not touched by the second call, so it's kept rather than wiped.
        assert result["review"]["written_avg"] == 6.0

    def test_can_still_be_scored_after_a_decision(self, monkeypatch):
        self._patch(monkeypatch, self._base(status=ap.S_ACCEPTED))
        reviewer = User(id="r1", username="alice")
        result = asyncio.run(ap.submit_score("u1", ap.ReviewScore(cv_score=7), reviewer))
        assert result["review"]["cv_avg"] == 7.0

    def test_interview_score_out_of_range_is_rejected(self, monkeypatch):
        self._patch(monkeypatch, self._base())
        reviewer = User(id="r1", username="alice")
        with pytest.raises(HTTPException):
            asyncio.run(ap.submit_score("u1", ap.ReviewScore(interview_score=0), reviewer))
        with pytest.raises(HTTPException):
            asyncio.run(ap.submit_score("u1", ap.ReviewScore(interview_score=11), reviewer))

    def test_an_interview_score_alone_is_enough_to_submit(self, monkeypatch):
        self._patch(monkeypatch, self._base())
        reviewer = User(id="r1", username="alice")
        result = asyncio.run(ap.submit_score("u1", ap.ReviewScore(interview_score=9), reviewer))
        assert result["review"]["interview_avg"] == 9.0

    def test_interview_scores_from_multiple_reviewers_are_averaged(self, monkeypatch):
        self._patch(monkeypatch, self._base())
        alice = User(id="r1", username="alice")
        bob = User(id="r2", username="bob")
        asyncio.run(ap.submit_score("u1", ap.ReviewScore(interview_score=8), alice))
        result = asyncio.run(ap.submit_score("u1", ap.ReviewScore(interview_score=6), bob))
        assert result["review"]["interview_avg"] == 7.0

    def test_a_fast_tracked_applicant_can_still_get_an_interview_score(self, monkeypatch):
        # Written is skipped for Fast-Track, but everyone sits an interview.
        application = self._base()
        application["event_ticket"] = ap.EVENT_TICKET_FAST_TRACK
        self._patch(monkeypatch, application)
        reviewer = User(id="r1", username="alice")
        result = asyncio.run(ap.submit_score("u1", ap.ReviewScore(interview_score=10), reviewer))
        assert result["review"]["interview_avg"] == 10.0


class TestRankedRows:
    def test_combined_score_folds_in_the_interview_average(self, monkeypatch):
        fake_db = _FakeDB()
        fake_db.collections[ap.COLLECTION] = {
            "u1": {
                "user_id": "u1", "username": "jo", "status": ap.S_SHORTLISTED,
                "reviews": {"r1": {"cv_score": 8, "written_score": 6, "interview_score": 10}},
            },
        }
        monkeypatch.setattr(ap.db_module, "db", fake_db)
        rows = asyncio.run(ap._ranked_rows(viewer_id="r1"))
        assert rows[0]["review"]["interview_avg"] == 10.0
        assert rows[0]["combined_score"] == 8.0   # mean of 8, 6, 10

    def test_an_unscored_interview_does_not_drag_the_average_down(self, monkeypatch):
        fake_db = _FakeDB()
        fake_db.collections[ap.COLLECTION] = {
            "u1": {
                "user_id": "u1", "username": "jo", "status": ap.S_SUBMITTED,
                "event_ticket": ap.EVENT_TICKET_FAST_TRACK,
                "reviews": {"r1": {"cv_score": 9}},
            },
        }
        monkeypatch.setattr(ap.db_module, "db", fake_db)
        rows = asyncio.run(ap._ranked_rows(viewer_id="r1"))
        assert rows[0]["combined_score"] == 9.0   # not scored yet, so it's simply excluded


class TestDecideFlow:
    def _patch(self, monkeypatch, application):
        store = {"u1": application}

        async def fake_load(uid):
            return store.get(uid)

        async def fake_save(uid, app_):
            store[uid] = app_

        sent = []

        async def fake_send(to, subject, title, body_html, cta_label=None, cta_url=None):
            sent.append((to, subject))
            return True

        fake_db = _FakeDB()
        monkeypatch.setattr(ap, "_load", fake_load)
        monkeypatch.setattr(ap, "_save", fake_save)
        monkeypatch.setattr(ap.mailer, "send_email", fake_send)
        monkeypatch.setattr(ap.db_module, "db", fake_db)
        return store, sent, fake_db

    def _base(self, status):
        return {
            "user_id": "u1", "username": "jo", "full_name": "Jo Bloggs",
            "email": "jo@example.com", "oxford_email": "jo@merton.ox.ac.uk",
            "programme": mb.M_QUANT_ANALYST, "status": status,
        }

    def test_submitted_can_be_shortlisted_and_emailed(self, monkeypatch):
        store, sent, _ = self._patch(monkeypatch, self._base(ap.S_SUBMITTED))
        admin = User(id="a1", username="root", is_admin=True)
        result = asyncio.run(ap.decide("u1", ap.Decision(decision="shortlist"), admin))
        assert result["status"] == ap.S_SHORTLISTED
        assert store["u1"]["status"] == ap.S_SHORTLISTED
        assert sent == [("jo@merton.ox.ac.uk", ap._DECISION_COPY[ap.S_SHORTLISTED]["subject"])]

    def test_cannot_accept_a_submitted_application_that_was_never_shortlisted(self, monkeypatch):
        _, sent, _ = self._patch(monkeypatch, self._base(ap.S_SUBMITTED))
        admin = User(id="a1", username="root", is_admin=True)
        with pytest.raises(HTTPException):
            asyncio.run(ap.decide("u1", ap.Decision(decision="accept"), admin))
        assert sent == []   # no email fired for a decision that was refused

    def test_shortlisted_can_be_accepted_and_membership_is_granted(self, monkeypatch):
        store, sent, fake_db = self._patch(monkeypatch, self._base(ap.S_SHORTLISTED))
        admin = User(id="a1", username="root", is_admin=True)
        result = asyncio.run(ap.decide("u1", ap.Decision(decision="accept"), admin))
        assert result["status"] == ap.S_ACCEPTED
        assert fake_db.collections["users"]["u1"]["membership"] == mb.M_QUANT_ANALYST
        assert sent[-1] == ("jo@merton.ox.ac.uk", ap._DECISION_COPY[ap.S_ACCEPTED]["subject"])

    @pytest.mark.parametrize("status", [ap.S_SUBMITTED, ap.S_SHORTLISTED])
    def test_reject_is_allowed_from_submitted_or_shortlisted(self, monkeypatch, status):
        store, sent, _ = self._patch(monkeypatch, self._base(status))
        admin = User(id="a1", username="root", is_admin=True)
        result = asyncio.run(ap.decide("u1", ap.Decision(decision="reject"), admin))
        assert result["status"] == ap.S_REJECTED
        assert sent[-1][1] == ap._DECISION_COPY[ap.S_REJECTED]["subject"]

    @pytest.mark.parametrize("status", [ap.S_CV, ap.S_OA_READY, ap.S_OA_ACTIVE])
    def test_no_decision_is_possible_before_submission(self, monkeypatch, status):
        self._patch(monkeypatch, self._base(status))
        admin = User(id="a1", username="root", is_admin=True)
        for decision in ("shortlist", "accept", "reject"):
            with pytest.raises(HTTPException):
                asyncio.run(ap.decide("u1", ap.Decision(decision=decision), admin))

    def test_a_fast_tracked_applicant_can_be_shortlisted_just_like_anyone_else(self, monkeypatch):
        # Fast-Track skips the written assessment, not the interview — the
        # shortlist/accept/reject pipeline (and the scheduling that follows
        # it) has to work exactly the same for them.
        application = self._base(ap.S_SUBMITTED)
        application["event_ticket"] = ap.EVENT_TICKET_FAST_TRACK
        store, sent, _ = self._patch(monkeypatch, application)
        admin = User(id="a1", username="root", is_admin=True)
        result = asyncio.run(ap.decide("u1", ap.Decision(decision="shortlist"), admin))
        assert result["status"] == ap.S_SHORTLISTED
        assert store["u1"]["status"] == ap.S_SHORTLISTED

    def test_a_decided_application_cannot_be_decided_again(self, monkeypatch):
        self._patch(monkeypatch, self._base(ap.S_ACCEPTED))
        admin = User(id="a1", username="root", is_admin=True)
        with pytest.raises(HTTPException):
            asyncio.run(ap.decide("u1", ap.Decision(decision="reject"), admin))

    def test_a_non_admin_analyst_can_shortlist(self, monkeypatch):
        # The bug this covers: shortlisting was accidentally as narrow as
        # accept/reject, so an Analyst reviewer — who can already score CVs
        # and schedule interviews — couldn't move a submitted application
        # into the interview stage at all.
        store, sent, _ = self._patch(monkeypatch, self._base(ap.S_SUBMITTED))
        analyst = User(id="r1", username="priya", is_admin=False)
        result = asyncio.run(ap.decide("u1", ap.Decision(decision="shortlist"), analyst))
        assert result["status"] == ap.S_SHORTLISTED
        assert store["u1"]["status"] == ap.S_SHORTLISTED
        assert store["u1"]["shortlisted_by"] == "priya"

    def test_a_non_admin_analyst_can_accept(self, monkeypatch):
        # Accept/reject are open to any reviewer too — the safeguard against
        # a bad call is the client-side "are you sure, do you have approval"
        # confirmation plus every decision being attributed, not a 403.
        store, sent, _ = self._patch(monkeypatch, self._base(ap.S_SHORTLISTED))
        analyst = User(id="r1", username="priya", is_admin=False)
        result = asyncio.run(ap.decide("u1", ap.Decision(decision="accept"), analyst))
        assert result["status"] == ap.S_ACCEPTED
        assert store["u1"]["decided_by"] == "priya"

    def test_a_non_admin_analyst_can_reject(self, monkeypatch):
        store, sent, _ = self._patch(monkeypatch, self._base(ap.S_SUBMITTED))
        analyst = User(id="r1", username="priya", is_admin=False)
        result = asyncio.run(ap.decide("u1", ap.Decision(decision="reject"), analyst))
        assert result["status"] == ap.S_REJECTED
        assert store["u1"]["decided_by"] == "priya"


class TestRejectionEmailWording:
    """Analyst rejections read differently from Bootcamp ones — Analyst
    recruiting runs in cycles, so "next cycle" is the accurate term, while
    Bootcamp keeps the more casual "a future round" wording."""

    def _patch(self, monkeypatch, application):
        store = {"u1": application}

        async def fake_load(uid):
            return store.get(uid)

        async def fake_save(uid, app_):
            store[uid] = app_

        sent = []

        async def fake_send(to, subject, title, body_html, cta_label=None, cta_url=None):
            sent.append(body_html)
            return True

        fake_db = _FakeDB()
        monkeypatch.setattr(ap, "_load", fake_load)
        monkeypatch.setattr(ap, "_save", fake_save)
        monkeypatch.setattr(ap.mailer, "send_email", fake_send)
        monkeypatch.setattr(ap.db_module, "db", fake_db)
        return sent

    def _base(self, programme, status=ap.S_SUBMITTED):
        return {
            "user_id": "u1", "username": "jo", "full_name": "Jo Bloggs",
            "email": "jo@example.com", "oxford_email": "jo@merton.ox.ac.uk",
            "programme": programme, "status": status,
        }

    def test_an_analyst_rejection_mentions_the_next_cycle(self, monkeypatch):
        sent = self._patch(monkeypatch, self._base(mb.M_QUANT_ANALYST))
        admin = User(id="a1", username="root", is_admin=True)
        asyncio.run(ap.decide("u1", ap.Decision(decision="reject"), admin))
        assert "next cycle" in sent[0]
        assert "future round" not in sent[0]

    def test_a_bootcamp_rejection_keeps_the_original_wording(self, monkeypatch):
        sent = self._patch(monkeypatch, self._base(mb.M_QUANT_BOOTCAMP))
        admin = User(id="a1", username="root", is_admin=True)
        asyncio.run(ap.decide("u1", ap.Decision(decision="reject"), admin))
        assert "future round" in sent[0]
        assert "next cycle" not in sent[0]


class TestOxfordStudentConfirmation:
    def _patch(self, monkeypatch, user_data):
        store: dict = {}

        async def fake_user_data(uid):
            return user_data

        async def fake_load(uid):
            return store.get(uid)

        async def fake_save(uid, app_):
            store[uid] = app_

        monkeypatch.setattr(ap, "_user_data", fake_user_data)
        monkeypatch.setattr(ap, "_load", fake_load)
        monkeypatch.setattr(ap, "_save", fake_save)
        return store

    def test_general_public_must_confirm_to_start(self, monkeypatch):
        self._patch(monkeypatch, {"email": "jo@gmail.com", "membership": mb.M_PUBLIC})
        user = User(id="u1", username="jo")
        with pytest.raises(HTTPException):
            asyncio.run(ap.start_application(
                ap.StartApplication(programme=mb.M_QUANT_ANALYST, oxford_email="jo@merton.ox.ac.uk",
                                     confirms_oxford_student=False),
                user))

    def test_general_public_confirmed_is_recorded(self, monkeypatch):
        store = self._patch(monkeypatch, {"email": "jo@gmail.com", "membership": mb.M_PUBLIC})
        user = User(id="u1", username="jo")
        result = asyncio.run(ap.start_application(
            ap.StartApplication(programme=mb.M_QUANT_ANALYST, oxford_email="jo@merton.ox.ac.uk",
                                 confirms_oxford_student=True),
            user))
        assert result["ok"] is True
        assert store["u1"]["confirmed_oxford_student"] is True
        assert store["u1"]["applicant_category"] == mb.M_PUBLIC

    def test_general_alpha_fund_member_does_not_need_to_confirm(self, monkeypatch):
        store = self._patch(monkeypatch, {"email": "jo@merton.ox.ac.uk", "membership": mb.M_MEMBER})
        user = User(id="u1", username="jo")
        result = asyncio.run(ap.start_application(ap.StartApplication(programme=mb.M_QUANT_ANALYST), user))
        assert result["ok"] is True
        assert store["u1"]["confirmed_oxford_student"] is False
        assert store["u1"]["applicant_category"] == mb.M_MEMBER


class TestDisabledProgrammeRejection:
    """The Fundamental side (and the combined "Both" option) is listed as a
    choice but not actually open yet — /apply/start has to refuse it even
    though apply_programmes_for() still includes it, so a form submitted
    around the disabled UI (or a stale page) can't sneak one through."""

    def _patch(self, monkeypatch, user_data):
        store: dict = {}

        async def fake_user_data(uid):
            return user_data

        async def fake_load(uid):
            return store.get(uid)

        async def fake_save(uid, app_):
            store[uid] = app_

        monkeypatch.setattr(ap, "_user_data", fake_user_data)
        monkeypatch.setattr(ap, "_load", fake_load)
        monkeypatch.setattr(ap, "_save", fake_save)
        return store

    def test_fundamental_bootcamp_is_refused(self, monkeypatch):
        self._patch(monkeypatch, {"email": "jo@merton.ox.ac.uk", "membership": mb.M_MEMBER})
        user = User(id="u1", username="jo")
        with pytest.raises(HTTPException):
            asyncio.run(ap.start_application(ap.StartApplication(programme=mb.M_FUND_BOOTCAMP), user))

    def test_quant_bootcamp_still_works(self, monkeypatch):
        store = self._patch(monkeypatch, {"email": "jo@merton.ox.ac.uk", "membership": mb.M_MEMBER})
        user = User(id="u1", username="jo")
        result = asyncio.run(ap.start_application(ap.StartApplication(programme=mb.M_QUANT_BOOTCAMP), user))
        assert result["ok"] is True
        assert store["u1"]["programme"] == mb.M_QUANT_BOOTCAMP


class TestNewApplicationAlwaysStartsAtCv:
    """
    Regression coverage for a real bug: a brand-new application used to jump
    straight to oa_ready whenever the account's profile already had a CV on
    file (common for anyone who applied before) — skipping the "would you
    like to update it?" question entirely, and skipping the stamp of this
    application's own CV snapshot along with it, since only cv-confirm does
    that. Every new application must start at the CV step regardless of
    what's already on the profile.
    """

    def _patch(self, monkeypatch, user_data):
        store: dict = {}

        async def fake_user_data(uid):
            return user_data

        async def fake_load(uid):
            return store.get(uid)

        async def fake_save(uid, app_):
            store[uid] = app_

        monkeypatch.setattr(ap, "_user_data", fake_user_data)
        monkeypatch.setattr(ap, "_load", fake_load)
        monkeypatch.setattr(ap, "_save", fake_save)
        return store

    def test_a_profile_with_an_existing_cv_still_starts_at_the_cv_step(self, monkeypatch):
        store = self._patch(monkeypatch, {
            "email": "jo@merton.ox.ac.uk", "membership": mb.M_MEMBER,
            "cv_blob_path": "cvs/2027/Quant/u1.pdf",   # a CV from an earlier application
        })
        user = User(id="u1", username="jo")

        result = asyncio.run(ap.start_application(ap.StartApplication(programme=mb.M_QUANT_ANALYST), user))

        assert result["status"] == ap.S_CV
        stored = store["u1"]
        assert stored["status"] == ap.S_CV
        # And critically: this application has not snapshotted a CV of its
        # own yet — only cv-confirm does that, and it has not run.
        assert "cv_blob_path" not in stored

    def test_only_cv_confirm_stamps_the_applications_own_snapshot(self, monkeypatch):
        store = self._patch(monkeypatch, {
            "email": "jo@merton.ox.ac.uk", "membership": mb.M_MEMBER,
            "cv_blob_path": "cvs/2027/Quant/u1.pdf",
        })
        user = User(id="u1", username="jo")
        asyncio.run(ap.start_application(ap.StartApplication(programme=mb.M_QUANT_ANALYST), user))

        asyncio.run(ap.confirm_cv(
            ap.ConfirmCv(college="Merton", degree="Computer Science", year_of_study="2nd year"), user))

        assert store["u1"]["status"] == ap.S_OA_READY
        assert store["u1"]["cv_blob_path"] == "cvs/2027/Quant/u1.pdf"


class TestRedoAndDelete:
    """Admin-only escape hatches: retake the assessment, or wipe the record."""

    def _patch(self, monkeypatch, application):
        fake_db = _FakeDB()
        fake_db.collections[ap.COLLECTION] = {"u1": application}

        async def fake_load(uid):
            return fake_db.collections[ap.COLLECTION].get(uid)

        async def fake_save(uid, app_):
            fake_db.collections[ap.COLLECTION][uid] = app_

        monkeypatch.setattr(ap, "_load", fake_load)
        monkeypatch.setattr(ap, "_save", fake_save)
        monkeypatch.setattr(ap.db_module, "db", fake_db)
        return fake_db

    def _submitted_application(self):
        return {
            "user_id": "u1", "username": "jo", "full_name": "Jo Bloggs",
            "email": "jo@example.com", "oxford_email": "jo@merton.ox.ac.uk",
            "programme": mb.M_QUANT_ANALYST, "status": ap.S_SUBMITTED,
            "cv_blob_path": "cvs/x.pdf",
            "oa": {"section": "done", "answers": []},
            "reviews": {"r1": {"cv_score": 8}},
            "submitted_at": dt.datetime.now(dt.timezone.utc),
            "flags": {"paste": 1, "left_page": 0},
        }

    def test_redo_clears_oa_and_reviews_but_keeps_identity(self, monkeypatch):
        fake_db = self._patch(monkeypatch, self._submitted_application())
        admin = User(id="admin1", username="root", is_admin=True)

        result = asyncio.run(ap.redo_application("u1", admin))

        assert result["status"] == ap.S_OA_READY
        stored = fake_db.collections[ap.COLLECTION]["u1"]
        assert "oa" not in stored
        assert "reviews" not in stored
        assert "submitted_at" not in stored
        assert stored["flags"] == {"paste": 0, "left_page": 0}
        # Identity, CV and programme survive a redo untouched.
        assert stored["cv_blob_path"] == "cvs/x.pdf"
        assert stored["programme"] == mb.M_QUANT_ANALYST
        assert stored["oxford_email"] == "jo@merton.ox.ac.uk"

    def test_redo_refuses_before_the_assessment_has_started(self, monkeypatch):
        application = self._submitted_application()
        application["status"] = ap.S_OA_READY
        self._patch(monkeypatch, application)
        admin = User(id="admin1", username="root", is_admin=True)

        with pytest.raises(HTTPException):
            asyncio.run(ap.redo_application("u1", admin))

    def test_redo_is_available_after_a_decision_too(self, monkeypatch):
        # An admin might want to give someone another shot even after
        # rejecting them — the redo itself doesn't re-decide anything.
        application = self._submitted_application()
        application["status"] = ap.S_REJECTED
        application["decision_note"] = "not this time"
        fake_db = self._patch(monkeypatch, application)
        admin = User(id="admin1", username="root", is_admin=True)

        result = asyncio.run(ap.redo_application("u1", admin))

        assert result["status"] == ap.S_OA_READY
        assert "decision_note" not in fake_db.collections[ap.COLLECTION]["u1"]

    def test_redo_without_a_cv_on_file_falls_back_to_the_cv_step(self, monkeypatch):
        application = self._submitted_application()
        application.pop("cv_blob_path")
        self._patch(monkeypatch, application)
        admin = User(id="admin1", username="root", is_admin=True)

        result = asyncio.run(ap.redo_application("u1", admin))

        assert result["status"] == ap.S_CV

    def test_delete_removes_the_application_entirely(self, monkeypatch):
        fake_db = self._patch(monkeypatch, self._submitted_application())
        admin = User(id="admin1", username="root", is_admin=True)

        result = asyncio.run(ap.delete_application("u1", admin))

        assert result == {"ok": True}
        assert "u1" not in fake_db.collections[ap.COLLECTION]

    def test_delete_a_missing_application_404s(self, monkeypatch):
        self._patch(monkeypatch, self._submitted_application())
        admin = User(id="admin1", username="root", is_admin=True)

        with pytest.raises(HTTPException):
            asyncio.run(ap.delete_application("does-not-exist", admin))


class TestAdminInterviewView:
    def test_normalises_datetimes_and_passes_through_other_fields(self):
        when = dt.datetime.now(dt.timezone.utc)
        interview = {"interviewer_name": "Priya", "when": when, "scheduled_at": when,
                     "responded_at": None, "status": "proposed"}

        view = ap._admin_interview_view(interview)

        assert view["interviewer_name"] == "Priya"
        assert view["when"] == when
        assert view["responded_at"] is None

    def test_none_in_none_out(self):
        assert ap._admin_interview_view(None) is None


class TestMyPendingInterview:
    """The admin page's "My pending interviews" tab: an interview assigned
    to the viewer that they haven't yet logged a score for."""

    def _application(self, **interview_over):
        interview = {
            "interviewer_id": "rev1", "interviewer_name": "Priya", "interviewer_email": "p@ox.ac.uk",
            "when": dt.datetime.now(dt.timezone.utc), "status": ap.INTERVIEW_PROPOSED,
            "message": "", "scheduled_at": dt.datetime.now(dt.timezone.utc), "responded_at": None,
        }
        interview.update(interview_over)
        return {"username": "jo", "programme": mb.M_QUANT_ANALYST, "status": ap.S_SHORTLISTED,
                "interview": interview}

    def test_assigned_and_unscored_is_pending(self):
        row = ap._review_row("u1", self._application(), viewer_id="rev1")
        assert row["is_my_pending_interview"] is True

    def test_someone_elses_interview_is_not_mine(self):
        row = ap._review_row("u1", self._application(), viewer_id="rev2")
        assert row["is_my_pending_interview"] is False

    def test_declined_is_not_pending(self):
        row = ap._review_row("u1", self._application(status=ap.INTERVIEW_DECLINED), viewer_id="rev1")
        assert row["is_my_pending_interview"] is False

    def test_already_scored_by_me_is_not_pending(self):
        application = self._application()
        application["reviews"] = {"rev1": {"reviewer_name": "Priya", "interview_score": 8}}
        row = ap._review_row("u1", application, viewer_id="rev1")
        assert row["is_my_pending_interview"] is False

    def test_no_interview_is_not_pending(self):
        row = ap._review_row("u1", {"username": "jo", "status": ap.S_SHORTLISTED}, viewer_id="rev1")
        assert row["is_my_pending_interview"] is False


class TestInterviewScheduling:
    """
    The full loop: a reviewer proposes a time, the candidate confirms
    (calendar invites both ways) or declines (the interviewer is emailed to
    sort out something else directly).
    """

    def _patch(self, monkeypatch, application, users=None):
        fake_db = _FakeDB()
        fake_db.collections[ap.COLLECTION] = {"u1": application}
        fake_db.collections["users"] = users or {}

        async def fake_load(uid):
            return fake_db.collections[ap.COLLECTION].get(uid)

        async def fake_save(uid, app_):
            fake_db.collections[ap.COLLECTION][uid] = app_

        sent = []

        async def fake_send(to, subject, title, body_html, cta_label=None, cta_url=None, ics=None, cc=None):
            sent.append({"to": to, "cc": cc, "subject": subject, "has_ics": ics is not None,
                         "body_html": body_html, "ics": ics})
            return True

        monkeypatch.setattr(ap, "_load", fake_load)
        monkeypatch.setattr(ap, "_save", fake_save)
        monkeypatch.setattr(ap.db_module, "db", fake_db)
        monkeypatch.setattr(ap.mailer, "send_email", fake_send)
        return fake_db, sent

    def _shortlisted_application(self):
        return {
            "user_id": "u1", "username": "jo", "full_name": "Jo Bloggs",
            "email": "jo@example.com", "oxford_email": "jo@merton.ox.ac.uk",
            "programme": mb.M_QUANT_ANALYST, "status": ap.S_SHORTLISTED,
        }

    def _reviewer_users(self):
        return {
            "qa1": {"username": "priya", "full_name": "Priya Patel",
                    "email": "priya@ox.ac.uk", "membership": mb.M_QUANT_ANALYST},
            "admin1": {"username": "root", "full_name": "Root Admin",
                       "email": "root@ox.ac.uk", "is_admin": True},
            "noemail": {"username": "sam", "membership": mb.M_QUANT_ANALYST},   # no email — excluded
            "general": {"username": "bob", "email": "bob@ox.ac.uk"},            # not a reviewer — excluded
        }

    def test_list_reviewers_includes_admins_and_quant_analysts_with_email(self, monkeypatch):
        self._patch(monkeypatch, self._shortlisted_application(), self._reviewer_users())
        reviewers = asyncio.run(ap._list_reviewers())
        assert {r["id"] for r in reviewers} == {"qa1", "admin1"}

    def test_schedule_interview_requires_shortlisted_status(self, monkeypatch):
        application = self._shortlisted_application()
        application["status"] = ap.S_SUBMITTED
        self._patch(monkeypatch, application, self._reviewer_users())
        reviewer = User(id="qa1", username="priya")
        when = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=2)

        with pytest.raises(HTTPException):
            asyncio.run(ap.schedule_interview(
                "u1", ap.ScheduleInterview(interviewer_id="qa1", when=when), reviewer))

    def test_schedule_interview_rejects_an_unknown_interviewer(self, monkeypatch):
        self._patch(monkeypatch, self._shortlisted_application(), self._reviewer_users())
        reviewer = User(id="qa1", username="priya")
        when = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=2)

        with pytest.raises(HTTPException):
            asyncio.run(ap.schedule_interview(
                "u1", ap.ScheduleInterview(interviewer_id="ghost", when=when), reviewer))

    def test_schedule_interview_stores_and_emails_the_candidate(self, monkeypatch):
        fake_db, sent = self._patch(monkeypatch, self._shortlisted_application(), self._reviewer_users())
        reviewer = User(id="qa1", username="priya")
        when = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=2)

        result = asyncio.run(ap.schedule_interview(
            "u1", ap.ScheduleInterview(interviewer_id="qa1", when=when,
                                        message="Looking forward to it"), reviewer))

        stored = fake_db.collections[ap.COLLECTION]["u1"]["interview"]
        assert stored["status"] == ap.INTERVIEW_PROPOSED
        assert stored["interviewer_email"] == "priya@ox.ac.uk"
        assert stored["message"] == "Looking forward to it"
        assert len(sent) == 1
        assert sent[0]["to"] == "jo@merton.ox.ac.uk"
        # The candidate gets a calendar invite (and the interviewer's email
        # address, in the body) as soon as a time is proposed, not just once
        # they confirm — so they can hold the slot while they decide.
        assert sent[0]["has_ics"] is True
        assert result["interview"]["status"] == ap.INTERVIEW_PROPOSED

    def test_a_fast_tracked_candidate_gets_scheduled_the_same_way(self, monkeypatch):
        application = self._shortlisted_application()
        application["event_ticket"] = ap.EVENT_TICKET_FAST_TRACK
        fake_db, sent = self._patch(monkeypatch, application, self._reviewer_users())
        reviewer = User(id="qa1", username="priya")
        when = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=2)

        asyncio.run(ap.schedule_interview(
            "u1", ap.ScheduleInterview(interviewer_id="qa1", when=when), reviewer))

        stored = fake_db.collections[ap.COLLECTION]["u1"]["interview"]
        assert stored["status"] == ap.INTERVIEW_PROPOSED
        assert len(sent) == 1

    def test_schedule_interview_attaches_a_meet_link_when_gcal_is_configured(self, monkeypatch):
        fake_db, sent = self._patch(monkeypatch, self._shortlisted_application(), self._reviewer_users())
        created = []

        async def fake_create(**kwargs):
            created.append(kwargs)
            return {"event_id": "ev1", "meet_link": "https://meet.google.com/abc-defg-hij"}

        monkeypatch.setattr(ap.gcal, "create_meet_event", fake_create)
        reviewer = User(id="qa1", username="priya")
        when = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=2)

        result = asyncio.run(ap.schedule_interview(
            "u1", ap.ScheduleInterview(interviewer_id="qa1", when=when), reviewer))

        stored = fake_db.collections[ap.COLLECTION]["u1"]["interview"]
        assert stored["meet_link"] == "https://meet.google.com/abc-defg-hij"
        assert stored["gcal_event_id"] == "ev1"
        assert result["interview"]["meet_link"] == "https://meet.google.com/abc-defg-hij"
        assert created[0]["attendee_emails"] == ["jo@merton.ox.ac.uk", "priya@ox.ac.uk"]
        # The link is in the proposal email body and in the attached .ics.
        assert "https://meet.google.com/abc-defg-hij" in sent[0]["body_html"]
        assert b"https://meet.google.com/abc-defg-hij" in sent[0]["ics"]

    def test_schedule_interview_without_gcal_configured_has_no_meet_link(self, monkeypatch):
        fake_db, sent = self._patch(monkeypatch, self._shortlisted_application(), self._reviewer_users())
        monkeypatch.setattr(ap.gcal, "CONFIGURED", False)
        reviewer = User(id="qa1", username="priya")
        when = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=2)

        result = asyncio.run(ap.schedule_interview(
            "u1", ap.ScheduleInterview(interviewer_id="qa1", when=when), reviewer))

        assert result["interview"]["meet_link"] is None
        assert "meet.google.com" not in sent[0]["body_html"]

    def test_re_proposing_moves_the_existing_event_instead_of_making_a_new_one(self, monkeypatch):
        application = self._shortlisted_application()
        application["interview"] = {
            "interviewer_id": "qa1", "interviewer_name": "Priya Patel", "interviewer_email": "priya@ox.ac.uk",
            "message": "", "when": dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=2),
            "status": ap.INTERVIEW_PROPOSED, "scheduled_by": "priya",
            "scheduled_at": dt.datetime.now(dt.timezone.utc), "responded_at": None, "candidate_note": None,
            "meet_link": "https://meet.google.com/existing-link", "gcal_event_id": "ev-existing",
        }
        fake_db, sent = self._patch(monkeypatch, application, self._reviewer_users())
        moved = []

        async def fake_update(event_id, start, end):
            moved.append(event_id)
            return True

        async def fake_create(**kwargs):
            raise AssertionError("should not create a fresh event when moving an existing one")

        monkeypatch.setattr(ap.gcal, "update_event_time", fake_update)
        monkeypatch.setattr(ap.gcal, "create_meet_event", fake_create)
        reviewer = User(id="qa1", username="priya")
        new_when = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=5)

        result = asyncio.run(ap.schedule_interview(
            "u1", ap.ScheduleInterview(interviewer_id="qa1", when=new_when), reviewer))

        assert moved == ["ev-existing"]
        assert result["interview"]["meet_link"] == "https://meet.google.com/existing-link"
        stored = fake_db.collections[ap.COLLECTION]["u1"]["interview"]
        assert stored["gcal_event_id"] == "ev-existing"

    def _proposed_application(self):
        application = self._shortlisted_application()
        application["interview"] = {
            "interviewer_id": "qa1", "interviewer_name": "Priya Patel", "interviewer_email": "priya@ox.ac.uk",
            "message": "", "when": dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=2),
            "status": ap.INTERVIEW_PROPOSED, "scheduled_by": "priya",
            "scheduled_at": dt.datetime.now(dt.timezone.utc), "responded_at": None, "candidate_note": None,
        }
        return application

    def test_confirm_requires_a_pending_proposal(self, monkeypatch):
        self._patch(monkeypatch, self._shortlisted_application())
        user = User(id="u1", username="jo")

        with pytest.raises(HTTPException):
            asyncio.run(ap.confirm_interview(user))

    def test_confirm_moves_to_confirmed_and_emails_both_sides_together(self, monkeypatch):
        # One email on a shared thread, not two separate copies — so either
        # side can reply-all and actually reach the other one directly.
        fake_db, sent = self._patch(monkeypatch, self._proposed_application())
        user = User(id="u1", username="jo")

        result = asyncio.run(ap.confirm_interview(user))

        assert result["interview"]["status"] == ap.INTERVIEW_CONFIRMED
        stored = fake_db.collections[ap.COLLECTION]["u1"]["interview"]
        assert stored["status"] == ap.INTERVIEW_CONFIRMED
        assert stored["responded_at"] is not None
        assert len(sent) == 1
        assert sent[0]["has_ics"] is True
        assert sent[0]["to"] == "jo@merton.ox.ac.uk"
        assert sent[0]["cc"] == "priya@ox.ac.uk"

    def test_confirm_still_emails_whichever_side_has_an_address(self, monkeypatch):
        # No interviewer email on file: still just one email, to the
        # candidate, with nothing cc'd rather than a silent no-op.
        application = self._proposed_application()
        application["interview"]["interviewer_email"] = ""
        fake_db, sent = self._patch(monkeypatch, application)
        user = User(id="u1", username="jo")

        asyncio.run(ap.confirm_interview(user))

        assert len(sent) == 1
        assert sent[0]["to"] == "jo@merton.ox.ac.uk"
        assert sent[0]["cc"] is None

    def test_decline_requires_a_pending_proposal(self, monkeypatch):
        self._patch(monkeypatch, self._shortlisted_application())
        user = User(id="u1", username="jo")

        with pytest.raises(HTTPException):
            asyncio.run(ap.decline_interview(ap.InterviewDecline(note="busy"), user))

    def test_decline_moves_to_declined_and_emails_only_the_interviewer(self, monkeypatch):
        fake_db, sent = self._patch(monkeypatch, self._proposed_application())
        user = User(id="u1", username="jo")

        result = asyncio.run(ap.decline_interview(ap.InterviewDecline(note="Can we do next week?"), user))

        assert result["interview"]["status"] == ap.INTERVIEW_DECLINED
        stored = fake_db.collections[ap.COLLECTION]["u1"]["interview"]
        assert stored["candidate_note"] == "Can we do next week?"
        assert len(sent) == 1
        assert sent[0]["to"] == "priya@ox.ac.uk"
        assert "different interview time" in sent[0]["subject"]

    def test_redo_also_clears_a_pending_interview(self, monkeypatch):
        application = self._proposed_application()
        application["cv_blob_path"] = "cvs/x.pdf"
        fake_db, _ = self._patch(monkeypatch, application)
        admin = User(id="admin1", username="root", is_admin=True)

        result = asyncio.run(ap.redo_application("u1", admin))

        assert result["status"] == ap.S_OA_READY
        assert "interview" not in fake_db.collections[ap.COLLECTION]["u1"]


class TestStateEndpoint:
    """
    /apply/state decides three very different screens: the ceiling message
    for a Quant Analyst (regardless of any old application on file), the
    live application flow, and a decided application with or without a way
    back in.
    """

    def _patch(self, monkeypatch, users=None, applications=None):
        fake_db = _FakeDB()
        fake_db.collections["users"] = users or {}
        fake_db.collections[ap.COLLECTION] = applications or {}

        async def fake_load(uid):
            return fake_db.collections[ap.COLLECTION].get(uid)

        monkeypatch.setattr(ap, "_load", fake_load)
        monkeypatch.setattr(ap.db_module, "db", fake_db)
        return fake_db

    def test_a_quant_analyst_sees_the_ceiling_regardless_of_old_applications(self, monkeypatch):
        self._patch(
            monkeypatch,
            users={"u1": {"username": "jo", "membership": mb.M_QUANT_ANALYST, "email": "jo@merton.ox.ac.uk"}},
            applications={"u1": {"status": ap.S_ACCEPTED, "programme": mb.M_QUANT_ANALYST}},
        )
        user = User(id="u1", username="jo")

        result = asyncio.run(ap.state(user))

        assert result["status"] == "analyst"
        assert result["is_reviewer"] is True
        assert "programme" not in result   # the old application is not surfaced at all

    def test_an_admin_is_flagged_as_a_reviewer_too(self, monkeypatch):
        self._patch(monkeypatch, users={"u1": {"username": "root"}})
        user = User(id="u1", username="root", is_admin=True)

        result = asyncio.run(ap.state(user))

        assert result["is_reviewer"] is True
        assert result["eligible"] is False   # admins don't apply, they review

    def test_no_application_on_file_is_plain_none(self, monkeypatch):
        self._patch(monkeypatch, users={"u1": {"username": "jo", "membership": mb.M_PUBLIC}})
        user = User(id="u1", username="jo")

        result = asyncio.run(ap.state(user))

        assert result["status"] == "none"
        assert result["programmes"] == mb.APPLY_PROGRAMMES
        assert set(result["disabled_programmes"]) == mb.DISABLED_PROGRAMMES

    def test_bootcamp_member_only_sees_analyst_as_a_choice(self, monkeypatch):
        self._patch(monkeypatch, users={"u1": {"username": "jo", "membership": mb.M_QUANT_BOOTCAMP}})
        user = User(id="u1", username="jo")

        result = asyncio.run(ap.state(user))

        assert result["programmes"] == [mb.M_QUANT_ANALYST]

    def test_accepted_into_bootcamp_can_apply_again_for_analyst(self, monkeypatch):
        self._patch(
            monkeypatch,
            users={"u1": {"username": "jo", "membership": mb.M_QUANT_BOOTCAMP, "email": "jo@merton.ox.ac.uk"}},
            applications={"u1": {"status": ap.S_ACCEPTED, "programme": mb.M_QUANT_BOOTCAMP,
                                 "oxford_email": "jo@merton.ox.ac.uk"}},
        )
        user = User(id="u1", username="jo")

        result = asyncio.run(ap.state(user))

        assert result["status"] == ap.S_ACCEPTED
        assert result["decision"] == ap.S_ACCEPTED
        assert result["can_apply_again"] is True

    def test_rejected_general_public_can_try_again(self, monkeypatch):
        self._patch(
            monkeypatch,
            users={"u1": {"username": "jo", "membership": mb.M_PUBLIC, "email": "jo@merton.ox.ac.uk"}},
            applications={"u1": {"status": ap.S_REJECTED, "programme": mb.M_QUANT_BOOTCAMP,
                                 "oxford_email": "jo@merton.ox.ac.uk"}},
        )
        user = User(id="u1", username="jo")

        result = asyncio.run(ap.state(user))

        assert result["can_apply_again"] is True

    def test_a_live_application_is_shown_as_is_with_no_reapply_flag(self, monkeypatch):
        self._patch(
            monkeypatch,
            users={"u1": {"username": "jo", "membership": mb.M_PUBLIC}},
            applications={"u1": {"status": ap.S_CV, "programme": mb.M_QUANT_BOOTCAMP}},
        )
        user = User(id="u1", username="jo")

        result = asyncio.run(ap.state(user))

        assert result["status"] == ap.S_CV
        assert "can_apply_again" not in result


class TestReapplyAfterDecision:
    def _patch(self, monkeypatch, users, applications):
        fake_db = _FakeDB()
        fake_db.collections["users"] = users
        fake_db.collections[ap.COLLECTION] = applications

        async def fake_load(uid):
            return fake_db.collections[ap.COLLECTION].get(uid)

        async def fake_save(uid, app_):
            fake_db.collections[ap.COLLECTION][uid] = app_

        monkeypatch.setattr(ap, "_load", fake_load)
        monkeypatch.setattr(ap, "_save", fake_save)
        monkeypatch.setattr(ap.db_module, "db", fake_db)
        return fake_db

    def test_bootcamp_member_can_start_a_fresh_analyst_application(self, monkeypatch):
        fake_db = self._patch(
            monkeypatch,
            users={"u1": {"username": "jo", "membership": mb.M_QUANT_BOOTCAMP,
                          "email": "jo@merton.ox.ac.uk", "cv_blob_path": "cvs/old.pdf"}},
            applications={"u1": {"status": ap.S_ACCEPTED, "programme": mb.M_QUANT_BOOTCAMP,
                                 "decided_at": dt.datetime.now(dt.timezone.utc)}},
        )
        user = User(id="u1", username="jo")

        result = asyncio.run(ap.start_application(
            ap.StartApplication(programme=mb.M_QUANT_ANALYST), user))

        assert result["ok"] is True
        stored = fake_db.collections[ap.COLLECTION]["u1"]
        assert stored["programme"] == mb.M_QUANT_ANALYST
        # Always the CV step, even though their profile already has one on
        # file from the Bootcamp application — that's what makes the "would
        # you like to update it?" question actually get asked, and stamps
        # this new application's own CV snapshot via cv-confirm.
        assert stored["status"] == ap.S_CV
        assert stored["previous_application"]["programme"] == mb.M_QUANT_BOOTCAMP
        assert stored["previous_application"]["status"] == ap.S_ACCEPTED

    def test_bootcamp_member_cannot_choose_bootcamp_again(self, monkeypatch):
        self._patch(
            monkeypatch,
            users={"u1": {"username": "jo", "membership": mb.M_QUANT_BOOTCAMP, "email": "jo@merton.ox.ac.uk"}},
            applications={},
        )
        user = User(id="u1", username="jo")

        with pytest.raises(HTTPException):
            asyncio.run(ap.start_application(
                ap.StartApplication(programme=mb.M_QUANT_BOOTCAMP), user))

    def test_a_live_application_still_cannot_be_restarted(self, monkeypatch):
        self._patch(
            monkeypatch,
            users={"u1": {"username": "jo", "membership": mb.M_PUBLIC, "email": "jo@merton.ox.ac.uk"}},
            applications={"u1": {"status": ap.S_SUBMITTED, "programme": mb.M_QUANT_BOOTCAMP}},
        )
        user = User(id="u1", username="jo")

        with pytest.raises(HTTPException):
            asyncio.run(ap.start_application(
                ap.StartApplication(programme=mb.M_QUANT_ANALYST), user))


class TestAvailabilityWindow:
    """The pure grid math: a fixed London-time window for this admissions
    cycle (Oct 1-23), floored at the real 'today' so nobody is ever offered
    a slot in the past. Oct 2026 is entirely inside British Summer Time
    (it ends 25 Oct 2026), so London is a stable UTC+1 throughout — every
    slot below is written as 08:00 UTC = 09:00 London for that reason."""

    def _freeze(self, monkeypatch, when: dt.datetime) -> None:
        monkeypatch.setattr(ap, "_now", lambda: when)

    def test_window_start_floors_at_the_configured_start_date(self, monkeypatch):
        self._freeze(monkeypatch, dt.datetime(2026, 9, 20, 12, 0, tzinfo=dt.timezone.utc))
        start, end = ap._availability_window()
        assert start == ap.AVAILABILITY_WINDOW_START
        assert end == ap.AVAILABILITY_WINDOW_END

    def test_window_start_advances_with_todays_london_date(self, monkeypatch):
        self._freeze(monkeypatch, dt.datetime(2026, 10, 10, 12, 0, tzinfo=dt.timezone.utc))
        start, end = ap._availability_window()
        assert start == dt.date(2026, 10, 10)
        assert end == ap.AVAILABILITY_WINDOW_END

    def test_keeps_a_well_formed_whole_hour_london_slot(self, monkeypatch):
        self._freeze(monkeypatch, dt.datetime(2026, 9, 20, 12, 0, tzinfo=dt.timezone.utc))
        out = ap._valid_availability_slots(["2026-10-05T08:00:00+00:00"])
        assert out == ["2026-10-05T08:00:00+00:00"]

    def test_drops_a_slot_not_on_the_hour(self, monkeypatch):
        self._freeze(monkeypatch, dt.datetime(2026, 9, 20, 12, 0, tzinfo=dt.timezone.utc))
        out = ap._valid_availability_slots(["2026-10-05T08:30:00+00:00"])
        assert out == []

    def test_drops_a_slot_outside_7am_7pm_london(self, monkeypatch):
        self._freeze(monkeypatch, dt.datetime(2026, 9, 20, 12, 0, tzinfo=dt.timezone.utc))
        out = ap._valid_availability_slots([
            "2026-10-05T05:00:00+00:00",   # 06:00 London — before the window opens
            "2026-10-05T18:00:00+00:00",   # 19:00 London — after it closes
        ])
        assert out == []

    def test_drops_a_slot_before_october_1_or_after_october_23(self, monkeypatch):
        self._freeze(monkeypatch, dt.datetime(2026, 9, 20, 12, 0, tzinfo=dt.timezone.utc))
        too_early = "2026-09-30T08:00:00+00:00"
        too_late = "2026-10-24T08:00:00+00:00"
        in_window = "2026-10-05T08:00:00+00:00"
        out = ap._valid_availability_slots([too_early, too_late, in_window])
        assert out == [in_window]

    def test_a_slot_today_or_earlier_is_dropped_once_the_window_has_advanced(self, monkeypatch):
        self._freeze(monkeypatch, dt.datetime(2026, 10, 10, 12, 0, tzinfo=dt.timezone.utc))
        yesterday = "2026-10-09T08:00:00+00:00"
        today = "2026-10-10T08:00:00+00:00"
        out = ap._valid_availability_slots([yesterday, today])
        assert out == [today]

    def test_deduplicates_and_sorts(self, monkeypatch):
        self._freeze(monkeypatch, dt.datetime(2026, 9, 20, 12, 0, tzinfo=dt.timezone.utc))
        out = ap._valid_availability_slots([
            "2026-10-06T09:00:00+00:00", "2026-10-05T08:00:00+00:00", "2026-10-05T08:00:00+00:00",
        ])
        assert out == ["2026-10-05T08:00:00+00:00", "2026-10-06T09:00:00+00:00"]

    def test_ignores_garbage_values_instead_of_raising(self, monkeypatch):
        self._freeze(monkeypatch, dt.datetime(2026, 9, 20, 12, 0, tzinfo=dt.timezone.utc))
        out = ap._valid_availability_slots(["not-a-date", "", "2026-10-05T08:00:00+00:00"])
        assert out == ["2026-10-05T08:00:00+00:00"]

    def test_caps_at_the_maximum_slot_count(self, monkeypatch):
        self._freeze(monkeypatch, dt.datetime(2026, 9, 20, 12, 0, tzinfo=dt.timezone.utc))
        monkeypatch.setattr(ap, "AVAILABILITY_MAX_SLOTS", 3)
        raw = [f"2026-10-0{d}T08:00:00+00:00" for d in range(1, 10)]
        out = ap._valid_availability_slots(raw)
        assert len(out) == 3


class TestAvailabilityEndpoint:
    """POST /apply/availability — only while shortlisted, locked once the
    interview itself is confirmed."""

    def _patch(self, monkeypatch, application):
        store = {"u1": application}

        async def fake_load(uid):
            return store.get(uid)

        async def fake_save(uid, app_):
            store[uid] = app_

        monkeypatch.setattr(ap, "_load", fake_load)
        monkeypatch.setattr(ap, "_save", fake_save)
        return store

    def _shortlisted(self, **extra):
        base = {
            "user_id": "u1", "username": "jo", "status": ap.S_SHORTLISTED,
            "shortlisted_at": dt.datetime(2026, 9, 16, 12, 0, tzinfo=dt.timezone.utc),
        }
        base.update(extra)
        return base

    def test_requires_an_application_on_file(self, monkeypatch):
        self._patch(monkeypatch, None)
        user = User(id="u1", username="jo")
        with pytest.raises(HTTPException):
            asyncio.run(ap.submit_availability(ap.AvailabilitySubmit(slots=[]), user))

    def test_requires_shortlisted_status(self, monkeypatch):
        self._patch(monkeypatch, self._shortlisted(status=ap.S_SUBMITTED))
        user = User(id="u1", username="jo")
        with pytest.raises(HTTPException):
            asyncio.run(ap.submit_availability(ap.AvailabilitySubmit(slots=[]), user))

    def test_locked_once_the_interview_is_confirmed(self, monkeypatch):
        application = self._shortlisted(interview={"status": ap.INTERVIEW_CONFIRMED})
        self._patch(monkeypatch, application)
        user = User(id="u1", username="jo")
        with pytest.raises(HTTPException):
            asyncio.run(ap.submit_availability(ap.AvailabilitySubmit(slots=[]), user))

    def test_still_editable_while_an_interview_is_only_proposed(self, monkeypatch):
        monkeypatch.setattr(ap, "_now", lambda: dt.datetime(2026, 9, 20, 12, 0, tzinfo=dt.timezone.utc))
        application = self._shortlisted(interview={"status": ap.INTERVIEW_PROPOSED})
        store = self._patch(monkeypatch, application)
        user = User(id="u1", username="jo")
        slots = ["2026-10-05T08:00:00+00:00"]

        result = asyncio.run(ap.submit_availability(ap.AvailabilitySubmit(slots=slots), user))

        assert result["availability"] == slots
        assert store["u1"]["availability"] == slots

    def test_saves_and_filters_out_of_window_slots(self, monkeypatch):
        monkeypatch.setattr(ap, "_now", lambda: dt.datetime(2026, 9, 20, 12, 0, tzinfo=dt.timezone.utc))
        application = self._shortlisted()
        store = self._patch(monkeypatch, application)
        user = User(id="u1", username="jo")
        slots = ["2026-10-05T08:00:00+00:00", "2026-10-05T08:30:00+00:00", "not-a-date"]

        result = asyncio.run(ap.submit_availability(ap.AvailabilitySubmit(slots=slots), user))

        assert result["availability"] == ["2026-10-05T08:00:00+00:00"]
        assert "availability_updated_at" in store["u1"]


class TestAvailabilityInState:
    """/apply/state surfaces the candidate's own picks once shortlisted, and
    locks them out once the interview is confirmed."""

    def _patch(self, monkeypatch, application):
        fake_db = _FakeDB()
        fake_db.collections["users"] = {"u1": {"username": "jo", "membership": mb.M_PUBLIC}}
        fake_db.collections[ap.COLLECTION] = {"u1": application}

        async def fake_load(uid):
            return fake_db.collections[ap.COLLECTION].get(uid)

        monkeypatch.setattr(ap, "_load", fake_load)
        monkeypatch.setattr(ap.db_module, "db", fake_db)
        return fake_db

    def test_shortlisted_state_carries_availability(self, monkeypatch):
        shortlisted_at = dt.datetime(2026, 9, 16, 12, 0, tzinfo=dt.timezone.utc)
        application = {
            "status": ap.S_SHORTLISTED, "programme": mb.M_QUANT_ANALYST,
            "shortlisted_at": shortlisted_at, "availability": ["2026-10-05T08:00:00+00:00"],
        }
        self._patch(monkeypatch, application)
        user = User(id="u1", username="jo")

        result = asyncio.run(ap.state(user))

        assert result["availability"] == ["2026-10-05T08:00:00+00:00"]
        assert result["availability_locked"] is False

    def test_locked_once_confirmed(self, monkeypatch):
        application = {
            "status": ap.S_SHORTLISTED, "programme": mb.M_QUANT_ANALYST,
            "shortlisted_at": dt.datetime.now(dt.timezone.utc),
            "interview": {"status": ap.INTERVIEW_CONFIRMED},
        }
        self._patch(monkeypatch, application)
        user = User(id="u1", username="jo")

        result = asyncio.run(ap.state(user))

        assert result["availability_locked"] is True

    def test_not_carried_for_other_statuses(self, monkeypatch):
        application = {"status": ap.S_SUBMITTED, "programme": mb.M_QUANT_ANALYST}
        self._patch(monkeypatch, application)
        user = User(id="u1", username="jo")

        result = asyncio.run(ap.state(user))

        assert "availability" not in result
        assert "shortlisted_at" not in result


class TestExportApplications:
    """GET /apply/admin/export.xlsx — the same ranked rows as the admin page,
    flattened into one spreadsheet."""

    def _patch(self, monkeypatch, applications):
        fake_db = _FakeDB()
        fake_db.collections[ap.COLLECTION] = applications
        monkeypatch.setattr(ap.db_module, "db", fake_db)
        return fake_db

    def _application(self, **extra):
        base = {
            "user_id": "u1", "username": "jo", "full_name": "Jo Bloggs",
            "email": "jo@example.com", "oxford_email": "jo@merton.ox.ac.uk",
            "applicant_category": mb.M_PUBLIC, "programme": mb.M_QUANT_ANALYST,
            "status": ap.S_SHORTLISTED,
            "shortlisted_at": dt.datetime(2026, 9, 16, 12, 0, tzinfo=dt.timezone.utc),
            "availability": ["2026-09-16T09:00:00+00:00"],
            "oa": {
                "motivation": {"text": "Because quant finance.", "word_count": 3, "seconds_used": 120},
                "estimation": {"text": "Roughly a million.", "word_count": 3, "seconds_used": 300},
            },
            "reviews": {"r1": {"reviewer_name": "Priya", "cv_score": 8, "written_score": 7, "note": "Strong"}},
        }
        base.update(extra)
        return base

    def test_export_produces_a_workbook_with_one_row_per_applicant(self, monkeypatch):
        from openpyxl import load_workbook
        self._patch(monkeypatch, {"u1": self._application()})
        reviewer = User(id="admin1", username="root", is_admin=True)

        response = asyncio.run(ap.export_applications(reviewer))
        wb = load_workbook(_read_streaming(response))

        ws = wb.active
        headers = [c.value for c in next(ws.iter_rows(min_row=1, max_row=1))]
        assert "Motivation text" in headers
        assert "Availability submitted" in headers
        assert ws.max_row == 2   # header + one applicant

        row = {headers[i]: c.value for i, c in enumerate(next(ws.iter_rows(min_row=2, max_row=2)))}
        assert row["Username"] == "jo"
        assert row["Motivation text"] == "Because quant finance."
        assert row["CV avg"] == 8
        assert "16 Sep 2026" in row["Availability submitted"]
        assert "London" in row["Availability submitted"]

    def test_export_handles_an_applicant_with_no_availability(self, monkeypatch):
        from openpyxl import load_workbook
        self._patch(monkeypatch, {"u1": self._application(availability=[], reviews={})})
        reviewer = User(id="admin1", username="root", is_admin=True)

        response = asyncio.run(ap.export_applications(reviewer))
        wb = load_workbook(_read_streaming(response))

        ws = wb.active
        headers = [c.value for c in next(ws.iter_rows(min_row=1, max_row=1))]
        row = {headers[i]: c.value for i, c in enumerate(next(ws.iter_rows(min_row=2, max_row=2)))}
        assert row["Availability submitted"] == "None submitted"


def _read_streaming(response):
    """Drain a StreamingResponse's async body iterator into a seekable buffer."""
    async def _drain():
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk)
        return b"".join(chunks)
    import io as _io
    return _io.BytesIO(asyncio.run(_drain()))


class TestEventTicketEndpoint:
    """POST /apply/event-ticket — General Attendance, Fast-Track CV Clinic
    (capped), or not attending, chosen once before the CV step."""

    def _patch(self, monkeypatch, application, other_applications=None):
        fake_db = _FakeDB()
        fake_db.collections[ap.COLLECTION] = {"u1": application, **(other_applications or {})}

        async def fake_load(uid):
            return fake_db.collections[ap.COLLECTION].get(uid)

        async def fake_save(uid, app_):
            fake_db.collections[ap.COLLECTION][uid] = app_

        monkeypatch.setattr(ap, "_load", fake_load)
        monkeypatch.setattr(ap, "_save", fake_save)
        monkeypatch.setattr(ap.db_module, "db", fake_db)
        return fake_db

    def _cv_stage_application(self, **extra):
        base = {"user_id": "u1", "username": "jo", "programme": mb.M_QUANT_ANALYST, "status": ap.S_CV}
        base.update(extra)
        return base

    def test_requires_an_application_on_file(self, monkeypatch):
        self._patch(monkeypatch, None)
        user = User(id="u1", username="jo")
        with pytest.raises(HTTPException):
            asyncio.run(ap.choose_event_ticket(ap.EventTicketChoice(ticket="general"), user))

    def test_only_available_at_the_cv_stage(self, monkeypatch):
        self._patch(monkeypatch, self._cv_stage_application(status=ap.S_OA_READY))
        user = User(id="u1", username="jo")
        with pytest.raises(HTTPException):
            asyncio.run(ap.choose_event_ticket(ap.EventTicketChoice(ticket="general"), user))

    def test_unknown_ticket_is_rejected(self, monkeypatch):
        self._patch(monkeypatch, self._cv_stage_application())
        user = User(id="u1", username="jo")
        with pytest.raises(HTTPException):
            asyncio.run(ap.choose_event_ticket(ap.EventTicketChoice(ticket="vip"), user))

    def test_general_attendance_is_recorded(self, monkeypatch):
        fake_db = self._patch(monkeypatch, self._cv_stage_application())
        user = User(id="u1", username="jo")

        result = asyncio.run(ap.choose_event_ticket(ap.EventTicketChoice(ticket="general"), user))

        assert result["event_ticket"] == "general"
        assert fake_db.collections[ap.COLLECTION]["u1"]["event_ticket"] == "general"
        assert "event_registered_at" in fake_db.collections[ap.COLLECTION]["u1"]

    def test_not_attending_is_recorded_as_none(self, monkeypatch):
        fake_db = self._patch(monkeypatch, self._cv_stage_application())
        user = User(id="u1", username="jo")

        asyncio.run(ap.choose_event_ticket(ap.EventTicketChoice(ticket="none"), user))

        assert fake_db.collections[ap.COLLECTION]["u1"]["event_ticket"] == "none"

    def test_fast_track_succeeds_under_capacity(self, monkeypatch):
        fake_db = self._patch(monkeypatch, self._cv_stage_application())
        user = User(id="u1", username="jo")

        result = asyncio.run(ap.choose_event_ticket(ap.EventTicketChoice(ticket="fast_track"), user))

        assert result["event_ticket"] == "fast_track"
        assert fake_db.collections[ap.COLLECTION]["u1"]["event_ticket"] == "fast_track"

    def test_fast_track_is_refused_once_capacity_is_reached(self, monkeypatch):
        others = {
            f"other{i}": {"user_id": f"other{i}", "status": ap.S_CV, "event_ticket": "fast_track"}
            for i in range(ap.FAST_TRACK_CAPACITY)
        }
        self._patch(monkeypatch, self._cv_stage_application(), other_applications=others)
        user = User(id="u1", username="jo")

        with pytest.raises(HTTPException):
            asyncio.run(ap.choose_event_ticket(ap.EventTicketChoice(ticket="fast_track"), user))

    def test_reconfirming_your_own_fast_track_spot_does_not_need_a_free_place(self, monkeypatch):
        # Capacity is exactly full, but u1 already holds one of those spots —
        # re-submitting the same ticket must not be refused for lack of room.
        others = {
            f"other{i}": {"user_id": f"other{i}", "status": ap.S_CV, "event_ticket": "fast_track"}
            for i in range(ap.FAST_TRACK_CAPACITY - 1)
        }
        fake_db = self._patch(
            monkeypatch, self._cv_stage_application(event_ticket="fast_track"), other_applications=others)
        user = User(id="u1", username="jo")

        result = asyncio.run(ap.choose_event_ticket(ap.EventTicketChoice(ticket="fast_track"), user))

        assert result["event_ticket"] == "fast_track"
        assert fake_db.collections[ap.COLLECTION]["u1"]["event_ticket"] == "fast_track"

    def test_switching_off_fast_track_then_back_is_re_checked_against_capacity(self, monkeypatch):
        others = {
            f"other{i}": {"user_id": f"other{i}", "status": ap.S_CV, "event_ticket": "fast_track"}
            for i in range(ap.FAST_TRACK_CAPACITY)
        }
        self._patch(monkeypatch, self._cv_stage_application(event_ticket="general"), other_applications=others)
        user = User(id="u1", username="jo")

        with pytest.raises(HTTPException):
            asyncio.run(ap.choose_event_ticket(ap.EventTicketChoice(ticket="fast_track"), user))


class TestConfirmCvWithApplicantInfo:
    """POST /apply/cv-confirm now also collects college/degree/year/LinkedIn,
    and a Fast-Track applicant skips straight to submitted from here."""

    def _patch(self, monkeypatch, application, user_data=None):
        fake_db = _FakeDB()
        fake_db.collections[ap.COLLECTION] = {"u1": application}

        async def fake_load(uid):
            return fake_db.collections[ap.COLLECTION].get(uid)

        async def fake_save(uid, app_):
            fake_db.collections[ap.COLLECTION][uid] = app_

        async def fake_user_data(uid):
            return user_data or {"cv_blob_path": "cvs/x.pdf", "email": "jo@example.com"}

        sent = []

        async def fake_send(to, subject, title, body_html, cta_label=None, cta_url=None, ics=None):
            sent.append({"to": to, "subject": subject, "title": title})
            return True

        monkeypatch.setattr(ap, "_load", fake_load)
        monkeypatch.setattr(ap, "_save", fake_save)
        monkeypatch.setattr(ap, "_user_data", fake_user_data)
        monkeypatch.setattr(ap.mailer, "send_email", fake_send)
        monkeypatch.setattr(ap.db_module, "db", fake_db)
        return fake_db, sent

    def _application(self, **extra):
        base = {
            "user_id": "u1", "username": "jo", "programme": mb.M_QUANT_ANALYST,
            "status": ap.S_CV, "oxford_email": "jo@merton.ox.ac.uk",
        }
        base.update(extra)
        return base

    def test_requires_college_degree_and_year(self, monkeypatch):
        self._patch(monkeypatch, self._application())
        user = User(id="u1", username="jo")
        with pytest.raises(HTTPException):
            asyncio.run(ap.confirm_cv(ap.ConfirmCv(college="", degree="CS", year_of_study="2nd year"), user))

    def test_general_ticket_stores_info_and_moves_to_oa_ready(self, monkeypatch):
        fake_db, sent = self._patch(monkeypatch, self._application(event_ticket="general"))
        user = User(id="u1", username="jo")

        result = asyncio.run(ap.confirm_cv(
            ap.ConfirmCv(college="Merton", degree="Computer Science", year_of_study="2nd year",
                         linkedin="https://linkedin.com/in/jo"),
            user))

        assert result["status"] == ap.S_OA_READY
        stored = fake_db.collections[ap.COLLECTION]["u1"]
        assert stored["status"] == ap.S_OA_READY
        assert stored["college"] == "Merton"
        assert stored["degree"] == "Computer Science"
        assert stored["year_of_study"] == "2nd year"
        assert stored["linkedin"] == "https://linkedin.com/in/jo"
        assert sent == []   # no confirmation email yet — the OA hasn't been sat

    def test_fast_track_skips_straight_to_submitted_with_its_own_email(self, monkeypatch):
        fake_db, sent = self._patch(monkeypatch, self._application(event_ticket="fast_track"))
        user = User(id="u1", username="jo")

        result = asyncio.run(ap.confirm_cv(
            ap.ConfirmCv(college="Merton", degree="Computer Science", year_of_study="2nd year"), user))

        assert result["status"] == ap.S_SUBMITTED
        stored = fake_db.collections[ap.COLLECTION]["u1"]
        assert stored["status"] == ap.S_SUBMITTED
        assert "submitted_at" in stored
        assert "oa" not in stored   # never sat one
        assert len(sent) == 1
        assert sent[0]["to"] == "jo@merton.ox.ac.uk"
        assert "no written assessment" in sent[0]["title"].lower() or "received" in sent[0]["title"].lower()

    def test_fast_track_confirmation_email_is_sent_only_once(self, monkeypatch):
        fake_db, sent = self._patch(monkeypatch, self._application(event_ticket="fast_track"))
        user = User(id="u1", username="jo")
        payload = ap.ConfirmCv(college="Merton", degree="Computer Science", year_of_study="2nd year")

        asyncio.run(ap.confirm_cv(payload, user))
        # A second call while still "submitted" is a no-op per the existing
        # (status not in (cv, oa_ready)) guard — confirms it doesn't re-send.
        asyncio.run(ap.confirm_cv(payload, user))

        assert len(sent) == 1

    def test_general_and_none_tickets_both_require_the_written_assessment(self, monkeypatch):
        for ticket in ("general", "none", None):
            fake_db, _ = self._patch(monkeypatch, self._application(event_ticket=ticket))
            user = User(id="u1", username="jo")
            result = asyncio.run(ap.confirm_cv(
                ap.ConfirmCv(college="Merton", degree="CS", year_of_study="1st year"), user))
            assert result["status"] == ap.S_OA_READY


class TestEventRegistrationInState:
    """/apply/state carries the event ticket, Fast-Track capacity, and the
    college/degree/year/LinkedIn fields while at the cv stage, and keeps
    is_fast_tracked available at every later stage too."""

    def _patch(self, monkeypatch, users=None, applications=None):
        fake_db = _FakeDB()
        fake_db.collections["users"] = users or {}
        fake_db.collections[ap.COLLECTION] = applications or {}

        async def fake_load(uid):
            return fake_db.collections[ap.COLLECTION].get(uid)

        monkeypatch.setattr(ap, "_load", fake_load)
        monkeypatch.setattr(ap.db_module, "db", fake_db)
        return fake_db

    def test_cv_stage_exposes_ticket_capacity_and_info_fields(self, monkeypatch):
        self._patch(
            monkeypatch,
            users={"u1": {"username": "jo", "membership": mb.M_PUBLIC}},
            applications={"u1": {
                "status": ap.S_CV, "programme": mb.M_QUANT_ANALYST, "event_ticket": "fast_track",
                "college": "Merton", "degree": "CS", "year_of_study": "2nd year",
            }},
        )
        user = User(id="u1", username="jo")

        result = asyncio.run(ap.state(user))

        assert result["event_ticket"] == "fast_track"
        assert result["is_fast_tracked"] is True
        assert result["fast_track_remaining"] == ap.FAST_TRACK_CAPACITY - 1
        assert result["fast_track_capacity"] == ap.FAST_TRACK_CAPACITY
        assert result["college"] == "Merton"
        assert result["year_of_study_options"] == ap.YEAR_OF_STUDY_OPTIONS

    def test_is_fast_tracked_survives_into_submitted(self, monkeypatch):
        self._patch(
            monkeypatch,
            users={"u1": {"username": "jo", "membership": mb.M_PUBLIC}},
            applications={"u1": {
                "status": ap.S_SUBMITTED, "programme": mb.M_QUANT_ANALYST, "event_ticket": "fast_track",
                "submitted_at": dt.datetime.now(dt.timezone.utc),
            }},
        )
        user = User(id="u1", username="jo")

        result = asyncio.run(ap.state(user))

        assert result["is_fast_tracked"] is True
        assert "fast_track_remaining" not in result   # only meaningful during ticket choice

    def test_no_ticket_chosen_yet_is_falsy(self, monkeypatch):
        self._patch(
            monkeypatch,
            users={"u1": {"username": "jo", "membership": mb.M_PUBLIC}},
            applications={"u1": {"status": ap.S_CV, "programme": mb.M_QUANT_ANALYST}},
        )
        user = User(id="u1", username="jo")

        result = asyncio.run(ap.state(user))

        assert result["event_ticket"] is None
        assert result["is_fast_tracked"] is False


class TestChoiceIsTheEventSignup:
    """The application's event step and the events page are one sign-up."""

    def _patch(self, monkeypatch, application, signups=None):
        fake_db = _FakeDB()
        fake_db.collections[ap.COLLECTION] = {"u1": application}
        fake_db.collections["event_signups"] = signups or {}
        fake_db.collections["users"] = {"u1": {"full_name": "Jo Bloggs", "email": "jo@example.com"}}
        monkeypatch.setattr(ap.db_module, "db", fake_db)
        return fake_db

    def _cv_stage(self, **extra):
        return {"user_id": "u1", "username": "jo", "status": ap.S_CV, "programme": mb.M_QUANT_BOOTCAMP, **extra}

    def test_choosing_in_the_application_creates_the_event_signup(self, monkeypatch):
        fake_db = self._patch(monkeypatch, self._cv_stage())
        user = User(id="u1", username="jo")
        asyncio.run(ap.choose_event_ticket(ap.EventTicketChoice(ticket="fast_track"), user))
        signup = fake_db.collections["event_signups"]["quant-outreach_u1"]
        assert signup["ticket"] == "fast_track" and signup["email"] == "jo@example.com"

    def test_not_attending_removes_an_earlier_signup(self, monkeypatch):
        fake_db = self._patch(monkeypatch, self._cv_stage(), {"quant-outreach_u1": {
            "event_id": "quant-outreach", "user_id": "u1", "status": "confirmed", "ticket": "general"}})
        user = User(id="u1", username="jo")
        asyncio.run(ap.choose_event_ticket(ap.EventTicketChoice(ticket="none"), user))
        assert "quant-outreach_u1" not in fake_db.collections["event_signups"]

    def test_the_state_reflects_a_signup_made_on_the_events_page(self, monkeypatch):
        self._patch(monkeypatch, self._cv_stage(), {"quant-outreach_u1": {
            "event_id": "quant-outreach", "user_id": "u1", "status": "confirmed", "ticket": "fast_track"}})
        user = User(id="u1", username="jo")
        result = asyncio.run(ap.state(user))
        assert result["event_signup"] == "fast_track"
        assert not result["event_ticket"]   # not yet confirmed on the application itself

    def test_the_state_says_nothing_signed_up_when_there_is_no_signup(self, monkeypatch):
        self._patch(monkeypatch, self._cv_stage())
        user = User(id="u1", username="jo")
        assert asyncio.run(ap.state(user))["event_signup"] is None

    def test_someone_holding_a_place_by_signup_can_keep_it_when_it_looks_full(self, monkeypatch):
        others = {f"quant-outreach_x{i}": {"event_id": "quant-outreach", "user_id": f"x{i}",
                                           "status": "confirmed", "ticket": "fast_track"}
                  for i in range(ap.FAST_TRACK_CAPACITY - 1)}
        others["quant-outreach_u1"] = {"event_id": "quant-outreach", "user_id": "u1",
                                       "status": "confirmed", "ticket": "fast_track"}
        self._patch(monkeypatch, self._cv_stage(), others)
        user = User(id="u1", username="jo")
        result = asyncio.run(ap.choose_event_ticket(ap.EventTicketChoice(ticket="fast_track"), user))
        assert result["event_ticket"] == "fast_track"


# ── Regression tests for the application-flow bug sweep ─────────────────────
# Each class below pins one fix. They share one fake-Firestore setup: the real
# _load/_save go through it, and every outbound side effect (email, Calendar)
# is captured instead of sent.

def _flow_db(monkeypatch, applications=None, users=None, events=None, signups=None):
    fake_db = _FakeDB()
    fake_db.collections[ap.COLLECTION] = applications or {}
    fake_db.collections["users"] = users or {}
    fake_db.collections["events"] = events or {}
    fake_db.collections["event_signups"] = signups or {}
    monkeypatch.setattr(ap.db_module, "db", fake_db)

    sent, gcal_calls = [], []

    async def fake_send(to, subject, title, body_html, cta_label=None, cta_url=None, ics=None, cc=None):
        sent.append({"to": to, "cc": cc, "subject": subject, "body_html": body_html})
        return True

    async def fake_create(**kwargs):
        gcal_calls.append(("create", kwargs))
        return {"event_id": f"ev{len(gcal_calls)}", "meet_link": f"https://meet.google.com/new-{len(gcal_calls)}"}

    async def fake_update(event_id, start, end):
        gcal_calls.append(("update", event_id))
        return True

    async def fake_delete(event_id):
        gcal_calls.append(("delete", event_id))
        return True

    monkeypatch.setattr(ap.mailer, "send_email", fake_send)
    monkeypatch.setattr(ap.gcal, "create_meet_event", fake_create)
    monkeypatch.setattr(ap.gcal, "update_event_time", fake_update)
    monkeypatch.setattr(ap.gcal, "delete_event", fake_delete)
    return fake_db, sent, gcal_calls


class _Clock:
    """A hand-wound ap._now, so a test can step across the motivation
    deadline to the tenth of a second."""

    def __init__(self, monkeypatch):
        self.now = dt.datetime(2026, 9, 20, 12, 0, tzinfo=dt.timezone.utc)
        monkeypatch.setattr(ap, "_now", lambda: self.now)

    def advance(self, seconds):
        self.now += dt.timedelta(seconds=seconds)


class TestStalePartOneSubmit:
    """Fix 1: a part-one submit that reaches the server after a poll has
    already moved the sitting on to part two must not become part two's
    answer — let alone finish the assessment."""

    def _start(self, monkeypatch):
        clock = _Clock(monkeypatch)
        fake_db, sent, _ = _flow_db(
            monkeypatch,
            applications={"u1": {"user_id": "u1", "username": "jo", "programme": mb.M_QUANT_ANALYST,
                                 "status": ap.S_OA_READY, "oxford_email": "jo@merton.ox.ac.uk",
                                 "flags": {"paste": 0, "left_page": 0}}},
            users={"u1": {"username": "jo", "cv_blob_path": "cvs/jo.pdf"}},
        )
        user = User(id="u1", username="jo")
        asyncio.run(ap.start_oa(user))
        return clock, fake_db, sent, user

    def _written(self, user, text, final, section):
        return asyncio.run(ap.submit_written(ap.WrittenSubmit(text=text, final=final, section=section), user))

    def test_the_reported_race_keeps_part_two_empty_and_live(self, monkeypatch):
        clock, fake_db, sent, user = self._start(monkeypatch)
        clock.advance(292)
        self._written(user, "draft", False, "motivation")
        clock.advance(8.3)
        asyncio.run(ap.oa_state(user))   # the poll wins the race and opens part two

        result = self._written(user, "draft plus more", True, "motivation")

        stored = fake_db.collections[ap.COLLECTION]["u1"]
        assert stored["status"] == ap.S_OA_ACTIVE
        assert stored["oa"]["section"] == "estimation"
        assert stored["oa"]["estimation"]["text"] == ""
        assert result["section"] == "estimation" and result["stale"] is True
        assert sent == []   # nothing was finished, so no confirmation email

    def test_a_submit_inside_the_grace_period_keeps_the_last_sentence(self, monkeypatch):
        clock, fake_db, _, user = self._start(monkeypatch)
        clock.advance(292)
        self._written(user, "draft", False, "motivation")
        clock.advance(8.3)
        asyncio.run(ap.oa_state(user))
        estimation_started = fake_db.collections[ap.COLLECTION]["u1"]["oa"]["estimation_started_at"]

        self._written(user, "draft plus more", True, "motivation")

        oa = fake_db.collections[ap.COLLECTION]["u1"]["oa"]
        assert oa["motivation"]["text"] == "draft plus more"
        assert oa["motivation"]["word_count"] == 3
        # No clock moves: part one still used exactly its allotment, and
        # part two's started where the poll started it.
        assert oa["motivation"]["seconds_used"] == ap.MOTIVATION_SECONDS
        assert oa["estimation_started_at"] == estimation_started

    def test_a_submit_after_the_grace_period_is_ignored(self, monkeypatch):
        clock, fake_db, _, user = self._start(monkeypatch)
        clock.advance(292)
        self._written(user, "draft", False, "motivation")
        clock.advance(8 + ap.LATE_SUBMIT_GRACE_SECONDS + 1)
        asyncio.run(ap.oa_state(user))

        self._written(user, "draft plus much later", True, "motivation")

        oa = fake_db.collections[ap.COLLECTION]["u1"]["oa"]
        assert oa["motivation"]["text"] == "draft"
        assert oa["estimation"]["text"] == ""

    def test_a_stale_draft_never_lands_in_the_part_two_box(self, monkeypatch):
        clock, fake_db, _, user = self._start(monkeypatch)
        clock.advance(ap.MOTIVATION_SECONDS + 1)
        asyncio.run(ap.oa_state(user))

        self._written(user, "part one words", False, "motivation")

        oa = fake_db.collections[ap.COLLECTION]["u1"]["oa"]
        assert oa["estimation"]["text"] == ""
        assert fake_db.collections[ap.COLLECTION]["u1"]["status"] == ap.S_OA_ACTIVE

    def test_a_late_draft_cannot_overwrite_an_answer_submitted_early(self, monkeypatch):
        # Submitted part one early: that *is* the answer. A draft still in
        # flight from before the click mustn't replace it.
        clock, fake_db, _, user = self._start(monkeypatch)
        clock.advance(100)
        self._written(user, "my considered final answer", True, "motivation")
        clock.advance(1)

        self._written(user, "an older draft", False, "motivation")

        oa = fake_db.collections[ap.COLLECTION]["u1"]["oa"]
        assert oa["motivation"]["text"] == "my considered final answer"
        assert oa["estimation"]["text"] == ""

    def test_a_normal_final_submit_still_works_for_both_sections(self, monkeypatch):
        clock, fake_db, sent, user = self._start(monkeypatch)
        clock.advance(120)
        result = self._written(user, "why me", True, "motivation")
        assert result["section"] == "estimation"
        clock.advance(60)
        result = self._written(user, "about a million", True, "estimation")

        stored = fake_db.collections[ap.COLLECTION]["u1"]
        assert result["status"] == ap.S_SUBMITTED
        assert stored["oa"]["motivation"]["text"] == "why me"
        assert stored["oa"]["estimation"]["text"] == "about a million"
        assert stored["oa"]["estimation"]["seconds_used"] == 60
        assert len(sent) == 1

    def test_an_old_client_without_a_section_behaves_as_before(self, monkeypatch):
        clock, fake_db, _, user = self._start(monkeypatch)
        clock.advance(30)
        self._written(user, "no section sent", False, None)
        assert fake_db.collections[ap.COLLECTION]["u1"]["oa"]["motivation"]["text"] == "no section sent"


class TestApplyJsClientFixes:
    """Fixes 1, 2 and 12 on the client. There's no JS test runner in this
    repo, so these pin the specific lines rather than the behaviour."""

    @pytest.fixture(scope="class")
    def js(self):
        from pathlib import Path
        return (Path(ap.__file__).parent / "static" / "apply.js").read_text()

    def test_left_page_is_flagged_during_either_question(self, js):
        assert 'drawnKey === "motivation" || drawnKey === "estimation"' in js
        assert 'drawnKey.startsWith("q")' not in js

    def test_written_submits_say_which_question_they_are_for(self, js):
        assert js.count("section: drawnKey") == 2

    def test_the_submitted_screen_no_longer_mentions_a_score(self, js):
        assert "and your score" not in js

    def test_the_event_choice_handles_a_past_event_and_a_held_place(self, js):
        assert "ev.is_past" in js
        assert "Your place is held" in js


class TestEventTicketAfterTheEvent:
    """Fix 3: once Quant Outreach is over, the application offers no ticket
    — in particular no Fast-Track past the assessment."""

    def _events(self, ended):
        ends = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=-1 if ended else 10)
        return {"quant-outreach": {"title": "Quant Outreach", "starts_at": ends - dt.timedelta(hours=1),
                                   "ends_at": ends, "location": "Cohen Quad"}}

    def _cv_stage(self):
        return {"u1": {"user_id": "u1", "username": "jo", "programme": mb.M_QUANT_ANALYST, "status": ap.S_CV}}

    @pytest.mark.parametrize("ticket", ["fast_track", "general"])
    def test_attending_is_refused_once_the_event_has_ended(self, monkeypatch, ticket):
        fake_db, _, _ = _flow_db(monkeypatch, applications=self._cv_stage(), events=self._events(True))
        with pytest.raises(HTTPException) as exc:
            asyncio.run(ap.choose_event_ticket(ap.EventTicketChoice(ticket=ticket), User(id="u1", username="jo")))
        assert exc.value.status_code == 400
        assert "already taken place" in exc.value.detail
        assert "event_ticket" not in fake_db.collections[ap.COLLECTION]["u1"]

    def test_not_attending_still_works_after_the_event(self, monkeypatch):
        fake_db, _, _ = _flow_db(monkeypatch, applications=self._cv_stage(), events=self._events(True))
        asyncio.run(ap.choose_event_ticket(ap.EventTicketChoice(ticket="none"), User(id="u1", username="jo")))
        assert fake_db.collections[ap.COLLECTION]["u1"]["event_ticket"] == "none"

    def test_fast_track_still_works_before_the_event(self, monkeypatch):
        fake_db, _, _ = _flow_db(monkeypatch, applications=self._cv_stage(), events=self._events(False))
        asyncio.run(ap.choose_event_ticket(ap.EventTicketChoice(ticket="fast_track"), User(id="u1", username="jo")))
        assert fake_db.collections[ap.COLLECTION]["u1"]["event_ticket"] == "fast_track"

    def test_the_state_says_whether_the_event_is_past(self, monkeypatch):
        _flow_db(monkeypatch, applications=self._cv_stage(), events=self._events(True))
        assert asyncio.run(ap.state(User(id="u1", username="jo")))["event"]["is_past"] is True
        _flow_db(monkeypatch, applications=self._cv_stage(), events=self._events(False))
        assert asyncio.run(ap.state(User(id="u1", username="jo")))["event"]["is_past"] is False


class TestFastTrackOnlyOnce:
    """Fix 4: a rejected Fast-Track applicant who reapplies sits the
    assessment — the surviving outreach sign-up doesn't carry them past it."""

    def _rejected_fast_track(self, monkeypatch):
        fake_db, _, _ = _flow_db(
            monkeypatch,
            applications={"u1": {"user_id": "u1", "status": ap.S_REJECTED, "programme": mb.M_QUANT_BOOTCAMP,
                                 "event_ticket": "fast_track", "decided_at": dt.datetime.now(dt.timezone.utc)}},
            users={"u1": {"username": "jo", "membership": mb.M_PUBLIC, "email": "jo@merton.ox.ac.uk"}},
            signups={"quant-outreach_u1": {"event_id": "quant-outreach", "user_id": "u1",
                                           "status": "confirmed", "ticket": "fast_track"}},
        )
        user = User(id="u1", username="jo")
        asyncio.run(ap.start_application(
            ap.StartApplication(programme=mb.M_QUANT_BOOTCAMP, confirms_oxford_student=True), user))
        return fake_db, user

    def test_the_breadcrumb_remembers_the_old_ticket(self, monkeypatch):
        fake_db, _ = self._rejected_fast_track(monkeypatch)
        assert fake_db.collections[ap.COLLECTION]["u1"]["previous_application"]["event_ticket"] == "fast_track"

    def test_fast_track_is_refused_on_the_reapplication(self, monkeypatch):
        fake_db, user = self._rejected_fast_track(monkeypatch)
        with pytest.raises(HTTPException) as exc:
            asyncio.run(ap.choose_event_ticket(ap.EventTicketChoice(ticket="fast_track"), user))
        assert exc.value.status_code == 400
        assert "only be used once" in exc.value.detail
        asyncio.run(ap.choose_event_ticket(ap.EventTicketChoice(ticket="general"), user))
        assert fake_db.collections[ap.COLLECTION]["u1"]["event_ticket"] == "general"

    def test_the_state_tells_the_client_fast_track_is_used(self, monkeypatch):
        _, user = self._rejected_fast_track(monkeypatch)
        assert asyncio.run(ap.state(user))["fast_track_used"] is True

    def test_a_first_application_can_still_use_it(self, monkeypatch):
        _flow_db(monkeypatch, applications={"u1": {"user_id": "u1", "status": ap.S_CV,
                                                    "programme": mb.M_QUANT_BOOTCAMP}})
        user = User(id="u1", username="jo")
        assert asyncio.run(ap.state(user))["fast_track_used"] is False
        asyncio.run(ap.choose_event_ticket(ap.EventTicketChoice(ticket="fast_track"), user))


class TestReProposeWithADifferentInterviewer:
    """Fix 6: moving an event only changes its time, so a new interviewer
    gets a fresh event and the old one is removed."""

    def _proposed(self, interviewer_id):
        return {"u1": {
            "user_id": "u1", "username": "jo", "full_name": "Jo Bloggs", "oxford_email": "jo@merton.ox.ac.uk",
            "programme": mb.M_QUANT_ANALYST, "status": ap.S_SHORTLISTED,
            "interview": {"interviewer_id": interviewer_id, "status": ap.INTERVIEW_PROPOSED,
                          "when": dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=2),
                          "meet_link": "https://meet.google.com/old", "gcal_event_id": "ev-old"},
        }}

    def _users(self):
        return {"qaA": {"username": "priya", "full_name": "Priya A", "email": "a@ox.ac.uk",
                        "membership": mb.M_QUANT_ANALYST},
                "qaB": {"username": "ben", "full_name": "Ben B", "email": "b@ox.ac.uk",
                        "membership": mb.M_QUANT_ANALYST}}

    def test_a_new_interviewer_gets_a_new_event_and_the_old_one_goes(self, monkeypatch):
        fake_db, _, calls = _flow_db(monkeypatch, applications=self._proposed("qaA"), users=self._users())
        when = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=4)

        result = asyncio.run(ap.schedule_interview(
            "u1", ap.ScheduleInterview(interviewer_id="qaB", when=when), User(id="qaA", username="priya")))

        assert calls[0] == ("delete", "ev-old")
        kind, kwargs = calls[1]
        assert kind == "create" and "b@ox.ac.uk" in kwargs["attendee_emails"]
        assert "a@ox.ac.uk" not in kwargs["attendee_emails"]
        assert len(calls) == 2   # no update_event_time on the old event
        assert result["interview"]["meet_link"] != "https://meet.google.com/old"
        assert fake_db.collections[ap.COLLECTION]["u1"]["interview"]["gcal_event_id"] != "ev-old"

    def test_the_same_interviewer_still_just_moves_the_event(self, monkeypatch):
        _, _, calls = _flow_db(monkeypatch, applications=self._proposed("qaA"), users=self._users())
        when = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=4)
        asyncio.run(ap.schedule_interview(
            "u1", ap.ScheduleInterview(interviewer_id="qaA", when=when), User(id="qaA", username="priya")))
        assert calls == [("update", "ev-old")]


class TestInterviewEventCleanup:
    """Fix 7: reject, redo and delete take the Meet event off the calendar;
    accept leaves it alone."""

    def _app(self, status=ap.S_SHORTLISTED):
        return {"u1": {"user_id": "u1", "username": "jo", "oxford_email": "jo@merton.ox.ac.uk",
                       "programme": mb.M_QUANT_ANALYST, "status": status, "cv_blob_path": "cvs/jo.pdf",
                       "interview": {"status": ap.INTERVIEW_CONFIRMED, "gcal_event_id": "ev-live"}}}

    ADMIN = User(id="admin1", username="root", is_admin=True)

    def test_reject_removes_the_event(self, monkeypatch):
        _, _, calls = _flow_db(monkeypatch, applications=self._app())
        asyncio.run(ap.decide("u1", ap.Decision(decision="reject"), self.ADMIN))
        assert calls == [("delete", "ev-live")]

    def test_accept_leaves_the_event(self, monkeypatch):
        _, _, calls = _flow_db(monkeypatch, applications=self._app(), users={"u1": {"username": "jo"}})
        asyncio.run(ap.decide("u1", ap.Decision(decision="accept"), self.ADMIN))
        assert calls == []

    def test_redo_removes_the_event(self, monkeypatch):
        _, _, calls = _flow_db(monkeypatch, applications=self._app())
        asyncio.run(ap.redo_application("u1", self.ADMIN))
        assert calls == [("delete", "ev-live")]

    def test_delete_removes_the_event(self, monkeypatch):
        _, _, calls = _flow_db(monkeypatch, applications=self._app())
        asyncio.run(ap.delete_application("u1", self.ADMIN))
        assert calls == [("delete", "ev-live")]

    def test_a_calendar_failure_does_not_block_the_decision(self, monkeypatch):
        fake_db, _, _ = _flow_db(monkeypatch, applications=self._app())

        async def boom(event_id):
            raise RuntimeError("google is down")

        monkeypatch.setattr(ap.gcal, "delete_event", boom)
        asyncio.run(ap.decide("u1", ap.Decision(decision="reject"), self.ADMIN))
        assert fake_db.collections[ap.COLLECTION]["u1"]["status"] == ap.S_REJECTED

    def test_no_event_means_nothing_to_remove(self, monkeypatch):
        apps = self._app()
        apps["u1"].pop("interview")
        _, _, calls = _flow_db(monkeypatch, applications=apps)
        asyncio.run(ap.decide("u1", ap.Decision(decision="reject"), self.ADMIN))
        assert calls == []


class TestRedoOnAFastTrackApplication:
    """Fix 8: after a redo the applicant has written answers, and reviewers
    must see them rather than "Fast-tracked — no written assessment"."""

    def test_the_answers_show_after_a_redo(self, monkeypatch):
        from openpyxl import load_workbook
        fake_db, _, _ = _flow_db(
            monkeypatch,
            applications={"u1": {"user_id": "u1", "username": "jo", "programme": mb.M_QUANT_ANALYST,
                                 "status": ap.S_SUBMITTED, "event_ticket": "fast_track",
                                 "cv_blob_path": "cvs/jo.pdf"}},
        )
        admin = User(id="admin1", username="root", is_admin=True)
        asyncio.run(ap.redo_application("u1", admin))

        stored = fake_db.collections[ap.COLLECTION]["u1"]
        assert stored["event_ticket"] == "general"
        assert stored["redo_previous_ticket"] == "fast_track"

        # They now sit the assessment...
        stored.update({"status": ap.S_SUBMITTED, "oa": {
            "motivation": {"text": "My motivation", "seconds_used": 200},
            "estimation": {"text": "My estimate", "seconds_used": 100}}})

        row = ap._review_row("u1", stored)
        assert row["is_fast_tracked"] is False
        assert row["motivation_text"] == "My motivation"

        wb = load_workbook(_read_streaming(asyncio.run(ap.export_applications(admin))))
        ws = wb.active
        headers = [c.value for c in next(ws.iter_rows(min_row=1, max_row=1))]
        values = {headers[i]: c.value for i, c in enumerate(next(ws.iter_rows(min_row=2, max_row=2)))}
        assert values["Motivation text"] == "My motivation"
        assert values["Estimation text"] == "My estimate"

    def test_a_redone_fast_track_application_cannot_pick_fast_track_again(self, monkeypatch):
        # Redo with no CV on file lands back at the CV step, where the ticket
        # endpoint is open again — it must not become a second Fast-Track.
        fake_db, _, _ = _flow_db(
            monkeypatch,
            applications={"u1": {"user_id": "u1", "username": "jo", "programme": mb.M_QUANT_ANALYST,
                                 "status": ap.S_SUBMITTED, "event_ticket": "fast_track"}},
        )
        admin = User(id="admin1", username="root", is_admin=True)
        assert asyncio.run(ap.redo_application("u1", admin))["status"] == ap.S_CV
        with pytest.raises(HTTPException):
            asyncio.run(ap.choose_event_ticket(ap.EventTicketChoice(ticket="fast_track"),
                                               User(id="u1", username="jo")))


EVIL_NAME = 'Zed<a href="https://evil.example">click to verify</a>'


class TestEmailEscaping:
    """Fix 9: names, notes and messages typed by users or reviewers can't
    become markup in an email sent from our own address."""

    def _assert_escaped(self, body):
        assert "&lt;a href=" in body
        assert '<a href="https://evil.example">' not in body

    def test_submission_confirmation(self, monkeypatch):
        _, sent, _ = _flow_db(monkeypatch)
        asyncio.run(ap._send_submission_confirmation(
            {"full_name": EVIL_NAME, "oxford_email": "z@ox.ac.uk", "programme": mb.M_QUANT_BOOTCAMP}))
        self._assert_escaped(sent[0]["body_html"])

    def test_fast_track_confirmation(self, monkeypatch):
        _, sent, _ = _flow_db(monkeypatch)
        asyncio.run(ap._send_fast_track_confirmation(
            {"full_name": EVIL_NAME, "oxford_email": "z@ox.ac.uk", "programme": mb.M_QUANT_BOOTCAMP}))
        self._assert_escaped(sent[0]["body_html"])

    def test_decision_email(self, monkeypatch):
        _, sent, _ = _flow_db(monkeypatch)
        asyncio.run(ap._send_decision_email(
            {"full_name": EVIL_NAME, "oxford_email": "z@ox.ac.uk", "programme": mb.M_QUANT_BOOTCAMP},
            ap.S_REJECTED))
        self._assert_escaped(sent[0]["body_html"])

    def test_reminder_escapes_the_name_and_the_admin_note(self, monkeypatch):
        _, sent, _ = _flow_db(monkeypatch, applications={"u1": {
            "full_name": EVIL_NAME, "oxford_email": "z@ox.ac.uk", "status": ap.S_CV}})
        asyncio.run(ap.remind("u1", ap.RemindRequest(note="<b>soon</b>"),
                              User(id="admin1", username="root", is_admin=True)))
        body = sent[0]["body_html"]
        self._assert_escaped(body)
        assert "&lt;b&gt;soon&lt;/b&gt;" in body

    def test_interview_emails_escape_names_messages_and_notes(self, monkeypatch):
        _, sent, _ = _flow_db(monkeypatch)
        application = {"user_id": "u1", "full_name": EVIL_NAME, "oxford_email": "z@ox.ac.uk",
                       "programme": mb.M_QUANT_ANALYST}
        interview = {"interviewer_name": "Eve<script>x</script>", "interviewer_email": "e@ox.ac.uk",
                     "message": '<img src=x onerror="alert(1)">', "candidate_note": EVIL_NAME,
                     "when": dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=2)}
        asyncio.run(ap._send_interview_proposal_email(application, interview))
        asyncio.run(ap._send_interview_confirmed_emails(application, interview))
        asyncio.run(ap._send_interview_declined_email(application, interview))

        proposal, confirmed, declined = (m["body_html"] for m in sent)
        for body in (proposal, confirmed, declined):
            assert "<script>" not in body
        assert "<img" not in proposal and "&lt;img" in proposal
        self._assert_escaped(confirmed)
        self._assert_escaped(declined)

    def test_the_plain_text_part_reads_normally(self, monkeypatch):
        # mailer.deliver derives a text part from the HTML; the escaped name
        # should come out as the literal characters, not as entities.
        from app import mailer
        captured = {}

        async def fake_gmail(to, subject, html_body, text, ics, cc):
            captured["text"] = text
            return True

        monkeypatch.setattr(mailer, "gmail_enabled", lambda: True)
        monkeypatch.setattr(mailer, "_send_via_gmail", fake_gmail)
        asyncio.run(mailer.deliver("z@ox.ac.uk", "s", "t", "<p>Hi Zed&lt;b&gt;</p>"))
        assert captured["text"] == "Hi Zed<b>"


class TestExportNoneScores:
    """Fix 10: a score stored as None reads as a dash, not "None"."""

    def test_reviewer_notes_show_a_dash(self, monkeypatch):
        from openpyxl import load_workbook
        _flow_db(monkeypatch, applications={"u1": {
            "user_id": "u1", "username": "jo", "programme": mb.M_QUANT_ANALYST, "status": ap.S_SUBMITTED,
            "reviews": {"r1": {"reviewer_name": "Priya", "cv_score": 8,
                               "written_score": None, "interview_score": None}}}})
        wb = load_workbook(_read_streaming(asyncio.run(
            ap.export_applications(User(id="admin1", username="root", is_admin=True)))))
        ws = wb.active
        headers = [c.value for c in next(ws.iter_rows(min_row=1, max_row=1))]
        notes = next(ws.iter_rows(min_row=2, max_row=2))[headers.index("Reviewer notes")].value
        assert "None" not in notes
        assert notes == "Priya: CV 8, written —, interview —"


class TestWordingFixes:
    """Fix 12, server side."""

    def test_the_cv_reminder_no_longer_claims_there_is_no_cv(self):
        body = ap._REMINDER_COPY[ap.S_CV]["body"]
        assert "don't have a CV on file" not in body
        assert "finish the CV step of your application" in body

    def test_the_state_says_whether_the_viewer_is_an_admin(self, monkeypatch):
        _flow_db(monkeypatch, users={"admin1": {"username": "root"}})
        assert asyncio.run(ap.state(User(id="admin1", username="root", is_admin=True)))["is_admin"] is True
