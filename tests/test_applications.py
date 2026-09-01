"""Tests for app.applications — the programme application and its 15-minute OA.

Pinned here: whole-number answer parsing, the paper's fixed mix of question
kinds, and the clock machinery that runs a sitting — the 5-minute written
section expiring into the numerical one, per-question timeouts rolling forward
honestly across a closed tab, and the 15-minute hard stop. All pure functions
over plain dicts, same approach as test_interview_oa.py — no Firestore
involved.
"""

import asyncio
import datetime as dt
import random
from collections import Counter

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
    section="written",
    started_seconds_ago=0.0,
    question_seconds_ago=0.0,
    question_ids=None,
    index=0,
    answers=None,
    written_text="",
    status=ap.S_OA_ACTIVE,
):
    """An application mid-assessment, with both clocks set explicitly."""
    qids = question_ids or ["e_rolls_to_six", "n_squares", "p_dice_sum7"]
    oa = {
        "started_at": ago(started_seconds_ago),
        "section": section,
        "written": {"prompt": ap.WRITTEN_PROMPT, "text": written_text},
        "question_ids": qids,
        "current_index": index,
        "answers": list(answers or []),
    }
    if section == "numerical":
        oa["question_started_at"] = ago(question_seconds_ago)
        oa["numerical_started_at"] = ago(question_seconds_ago)
    return {
        "user_id": "u1", "username": "cand", "programme": mb.M_QUANT_ANALYST,
        "status": status, "oa": oa, "flags": {"paste": 0, "left_page": 0},
    }


class TestParseAnswer:
    def test_plain_integer(self):
        assert ap.parse_answer("42") == 42

    def test_negative(self):
        assert ap.parse_answer("-7") == -7

    def test_leading_plus_and_spaces(self):
        assert ap.parse_answer("  +12 ") == 12

    def test_commas_are_typing_habits(self):
        assert ap.parse_answer("1,024") == 1024

    def test_decimal_is_not_an_answer(self):
        # Every answer in the bank is whole, so "3.5" is a different claim
        # rather than something to round into 3 or 4.
        assert ap.parse_answer("3.5") is None
        assert ap.parse_answer("6.0") is None

    def test_fraction_is_not_an_answer(self):
        assert ap.parse_answer("3/8") is None

    def test_blank_is_none(self):
        assert ap.parse_answer("") is None
        assert ap.parse_answer(None) is None
        assert ap.parse_answer("   ") is None

    def test_words_are_none(self):
        assert ap.parse_answer("six") is None
        assert ap.parse_answer("6 rolls") is None


class TestQuestionBank:
    def test_every_answer_is_a_whole_number(self):
        assert all(isinstance(q["answer"], int) for q in ap.QUESTION_BANK)

    def test_ids_are_unique(self):
        assert len({q["id"] for q in ap.QUESTION_BANK}) == len(ap.QUESTION_BANK)

    def test_every_question_has_a_recognised_difficulty(self):
        assert all(q["difficulty"] in ap.DIFFICULTIES for q in ap.QUESTION_BANK)

    def test_every_topic_carries_an_even_four_per_difficulty(self):
        by_topic_difficulty = Counter((q["kind"], q["difficulty"]) for q in ap.QUESTION_BANK)
        topics = {q["kind"] for q in ap.QUESTION_BANK}
        for topic in topics:
            for difficulty in ap.DIFFICULTIES:
                assert by_topic_difficulty[(topic, difficulty)] == 4, (topic, difficulty)

    def test_each_programmes_mix_adds_up_to_the_paper(self):
        for programme, mix in ap.PAPER_MIX.items():
            assert sum(mix.values()) == ap.NUMERICAL_QUESTIONS, programme

    def test_the_bank_can_fill_every_programmes_slots(self):
        by_topic = Counter(q["kind"] for q in ap.QUESTION_BANK)
        for mix in ap.PAPER_MIX.values():
            for kind, needed in mix.items():
                assert by_topic[kind] >= needed

    def test_the_two_sections_fill_the_sitting_exactly(self):
        assert (ap.WRITTEN_SECONDS
                + ap.NUMERICAL_QUESTIONS * ap.SECONDS_PER_QUESTION) == ap.SESSION_SECONDS


class TestEvenSplit:
    def test_splits_as_equally_as_possible(self):
        assert ap._even_split(6) == [2, 2, 2]
        assert ap._even_split(7) == [3, 2, 2]
        assert ap._even_split(4) == [2, 1, 1]
        assert ap._even_split(3) == [1, 1, 1]

    def test_always_sums_back_to_n(self):
        for n in range(0, 30):
            assert sum(ap._even_split(n)) == n


class TestBuildPaper:
    def test_quant_bootcamp_keeps_the_original_three_topics(self):
        paper = ap.build_paper(mb.M_QUANT_BOOTCAMP, random.Random(7))
        assert len(paper) == ap.NUMERICAL_QUESTIONS
        assert len(set(paper)) == ap.NUMERICAL_QUESTIONS   # no repeats
        kinds = Counter(ap.QUESTION_BY_ID[q]["kind"] for q in paper)
        assert kinds == Counter(ap.PAPER_MIX[mb.M_QUANT_BOOTCAMP])
        assert "quant concepts" not in kinds

    def test_quant_analyst_includes_quant_concepts(self):
        paper = ap.build_paper(mb.M_QUANT_ANALYST, random.Random(7))
        assert len(paper) == ap.NUMERICAL_QUESTIONS
        assert len(set(paper)) == ap.NUMERICAL_QUESTIONS
        kinds = Counter(ap.QUESTION_BY_ID[q]["kind"] for q in paper)
        assert kinds == Counter(ap.PAPER_MIX[mb.M_QUANT_ANALYST])
        assert kinds["quant concepts"] == 4

    def test_difficulty_spread_is_the_same_every_time_within_a_programme(self):
        # The specific questions vary, but how many of each difficulty land
        # in the paper is deterministic — that's the whole point of the
        # even split, so a paper never happens to be all-hard by chance.
        for seed in range(10):
            paper = ap.build_paper(mb.M_QUANT_BOOTCAMP, random.Random(seed))
            difficulties = Counter(ap.QUESTION_BY_ID[q]["difficulty"] for q in paper)
            assert difficulties == Counter({"easy": 8, "medium": 6, "hard": 6})

    def test_different_seeds_draw_different_questions(self):
        paper_a = ap.build_paper(mb.M_QUANT_BOOTCAMP, random.Random(1))
        paper_b = ap.build_paper(mb.M_QUANT_BOOTCAMP, random.Random(2))
        assert paper_a != paper_b

    def test_unknown_programme_falls_back_to_bootcamp(self):
        paper = ap.build_paper("Some Other Programme", random.Random(7))
        kinds = Counter(ap.QUESTION_BY_ID[q]["kind"] for q in paper)
        assert kinds == Counter(ap.PAPER_MIX[mb.M_QUANT_BOOTCAMP])


class TestQuantConcepts:
    def test_every_quant_concepts_answer_is_still_a_whole_number(self):
        qc = [q for q in ap.QUESTION_BANK if q["kind"] == "quant concepts"]
        assert len(qc) == 12
        assert all(isinstance(q["answer"], int) for q in qc)

    def test_multiple_choice_answers_are_a_small_option_number(self):
        # A handful are genuinely multiple choice — those answers should read
        # as an option index (1-4), not a computed quantity, so a stray
        # off-by-one in the bank stands out immediately.
        mc_ids = {"qc_delta_def", "qc_long_profit", "qc_gamma_def", "qc_putcall", "qc_replication"}
        for q in ap.QUESTION_BANK:
            if q["id"] in mc_ids:
                assert 1 <= q["answer"] <= 4, q["id"]


class TestGrading:
    def test_exact_match_only(self):
        q = ap.QUESTION_BY_ID["e_rolls_to_six"]      # answer 6
        assert ap.grade(q, 6) is True
        assert ap.grade(q, 5) is False
        assert ap.grade(q, None) is False


class TestWrittenSection:
    def test_stays_put_while_the_clock_runs(self):
        application = make_app(started_seconds_ago=60)
        assert ap.resolve(application) is False
        assert application["oa"]["section"] == "written"

    def test_expires_into_the_numerical_section(self):
        application = make_app(started_seconds_ago=ap.WRITTEN_SECONDS + 1,
                               written_text="half an answer")
        assert ap.resolve(application) is True
        oa = application["oa"]
        assert oa["section"] == "numerical"
        # Whatever was autosaved is banked, not discarded.
        assert oa["written"]["text"] == "half an answer"
        assert oa["written"]["submitted_at"] is not None
        assert oa["current_index"] == 0

    def test_closing_early_opens_the_numerical_section(self):
        application = make_app(started_seconds_ago=90, written_text="done early")
        ap._close_written(application["oa"])
        oa = application["oa"]
        assert oa["section"] == "numerical"
        assert oa["written"]["word_count"] == 2
        # Finishing the essay early buys no extra time on part two — the first
        # question simply starts now, with its own 30 seconds.
        assert round(ap._left(oa["question_started_at"], ap.SECONDS_PER_QUESTION)) == \
            ap.SECONDS_PER_QUESTION


class TestNumericalSection:
    def test_answer_is_graded_and_advances(self):
        application = make_app(section="numerical", started_seconds_ago=310,
                               question_seconds_ago=5)
        ap._record_answer(application["oa"], raw="6", timed_out=False)
        oa = application["oa"]
        assert oa["current_index"] == 1
        assert oa["answers"][0]["correct"] is True
        assert oa["answers"][0]["parsed"] == 6
        assert 4 <= oa["answers"][0]["time_taken_s"] <= 7

    def test_wrong_answer_is_recorded_not_dropped(self):
        application = make_app(section="numerical", started_seconds_ago=310,
                               question_seconds_ago=2)
        ap._record_answer(application["oa"], raw="5", timed_out=False)
        answer = application["oa"]["answers"][0]
        assert answer["correct"] is False
        assert answer["raw"] == "5"

    def test_live_question_is_left_alone(self):
        application = make_app(section="numerical", started_seconds_ago=310,
                               question_seconds_ago=10)
        assert ap.resolve(application) is False
        assert application["oa"]["current_index"] == 0

    def test_grace_buffer_protects_a_submission_at_the_buzzer(self):
        # Just past zero but inside the grace window: an /answer call already in
        # flight should still count, so nothing is timed out yet.
        application = make_app(section="numerical", started_seconds_ago=340,
                               question_seconds_ago=ap.SECONDS_PER_QUESTION + 1)
        assert ap.resolve(application) is False
        assert application["oa"]["current_index"] == 0

    def test_expired_question_times_out(self):
        application = make_app(
            section="numerical", started_seconds_ago=340,
            question_seconds_ago=ap.SECONDS_PER_QUESTION + ap.ANSWER_GRACE + 1)
        assert ap.resolve(application) is True
        oa = application["oa"]
        assert oa["current_index"] == 1
        assert oa["answers"][0]["timed_out"] is True
        assert oa["answers"][0]["correct"] is False
        assert oa["answers"][0]["time_taken_s"] == ap.SECONDS_PER_QUESTION

    def test_a_closed_tab_rolls_forward_without_handing_back_time(self):
        # Away for 70 seconds: that is two whole questions gone, and the third
        # should be part-used — not waiting with a fresh 30 seconds on it.
        application = make_app(section="numerical", started_seconds_ago=380,
                               question_seconds_ago=70)
        assert ap.resolve(application) is True
        oa = application["oa"]
        assert oa["current_index"] == 2
        assert all(a["timed_out"] for a in oa["answers"])
        left = ap._left(oa["question_started_at"], ap.SECONDS_PER_QUESTION)
        assert 19 <= left <= 21          # 30 - (70 - 60)

    def test_last_answer_finishes_the_sitting(self):
        application = make_app(section="numerical", started_seconds_ago=800,
                               question_seconds_ago=5, index=2,
                               answers=[{"question_id": "e_rolls_to_six", "raw": "6",
                                         "parsed": 6, "correct": True, "timed_out": False,
                                         "time_taken_s": 5.0},
                                        {"question_id": "n_squares", "raw": "36",
                                         "parsed": 36, "correct": True, "timed_out": False,
                                         "time_taken_s": 5.0}])
        ap._record_answer(application["oa"], raw="6", timed_out=False)
        ap._finish(application, "completed")
        assert application["status"] == ap.S_SUBMITTED
        assert application["oa"]["section"] == "done"
        assert application["oa"]["score"] == {"correct": 3, "total": 3, "pct": 100.0}


class TestSessionDeadline:
    def test_the_fifteen_minutes_is_a_hard_stop(self):
        application = make_app(section="numerical",
                               started_seconds_ago=ap.SESSION_SECONDS + 1,
                               question_seconds_ago=5, index=1,
                               answers=[{"question_id": "e_rolls_to_six", "raw": "6",
                                         "parsed": 6, "correct": True, "timed_out": False,
                                         "time_taken_s": 5.0}])
        assert ap.resolve(application) is True
        assert application["status"] == ap.S_SUBMITTED
        assert application["oa"]["finish_reason"] == "session_expired"
        # Every question is accounted for, answered or not.
        assert len(application["oa"]["answers"]) == 3
        assert application["oa"]["score"]["correct"] == 1

    def test_an_expired_sitting_left_in_the_written_section_still_closes(self):
        application = make_app(started_seconds_ago=ap.SESSION_SECONDS + 30,
                               written_text="only ever wrote this")
        assert ap.resolve(application) is True
        assert application["status"] == ap.S_SUBMITTED
        assert application["oa"]["written"]["text"] == "only ever wrote this"
        assert application["oa"]["score"]["correct"] == 0

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

    def test_quant_analyst_is_the_ceiling(self):
        assert mb.can_apply({"membership": mb.M_QUANT_ANALYST}) is False

    def test_quant_bootcamp_can_still_apply_on_to_analyst(self):
        assert mb.can_apply({"membership": mb.M_QUANT_BOOTCAMP}) is True
        assert mb.apply_programmes_for(mb.M_QUANT_BOOTCAMP) == [mb.M_QUANT_ANALYST]

    def test_fundamental_track_membership_is_a_separate_system(self):
        assert mb.can_apply({"membership": mb.M_FUND_ANALYST}) is False
        assert mb.can_apply({"membership": mb.M_FUND_BOOTCAMP}) is False

    def test_recruiters_and_hosts_are_on_the_other_side_of_the_table(self):
        assert mb.can_apply({"membership": mb.M_PUBLIC, "role": mb.ROLE_RECRUITER}) is False
        assert mb.can_apply({"membership": mb.M_PUBLIC, "role": mb.ROLE_HOST}) is False
        assert mb.can_apply({"membership": mb.M_PUBLIC, "is_admin": True}) is False

    def test_the_programmes_on_offer_are_the_quant_ones(self):
        assert mb.APPLY_PROGRAMMES == [mb.M_QUANT_BOOTCAMP, mb.M_QUANT_ANALYST]


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
            section="numerical", started_seconds_ago=ap.SESSION_SECONDS + 1,
            question_seconds_ago=5, index=1,
            answers=[{"question_id": "e_rolls_to_six", "raw": "6", "parsed": 6,
                      "correct": True, "timed_out": False, "time_taken_s": 5.0}])
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
        application = make_app(
            section="numerical", started_seconds_ago=800, question_seconds_ago=5, index=3,
            answers=[{"question_id": "e_rolls_to_six", "raw": "6", "parsed": 6,
                      "correct": True, "timed_out": False, "time_taken_s": 5.0}] * 3)
        application["oxford_email"] = "jo@merton.ox.ac.uk"

        asyncio.run(ap._finish_and_persist("u1", application, "completed"))
        assert application["status"] == ap.S_SUBMITTED
        assert len(sent) == 1

        # A second call (e.g. a retried request) must not send twice.
        asyncio.run(ap._finish_and_persist("u1", application, "completed"))
        assert len(sent) == 1

    def test_falls_back_to_the_account_email_with_no_oxford_email_on_file(self, monkeypatch):
        saved, sent = self._patch_io(monkeypatch)
        application = make_app(
            section="numerical", started_seconds_ago=800, question_seconds_ago=5, index=3,
            answers=[{"question_id": "e_rolls_to_six", "raw": "6", "parsed": 6,
                      "correct": True, "timed_out": False, "time_taken_s": 5.0}] * 3)
        application["email"] = "jo@merton.ox.ac.uk"   # no oxford_email key at all

        asyncio.run(ap._finish_and_persist("u1", application, "completed"))
        assert sent[0]["to"] == "jo@merton.ox.ac.uk"

    def test_no_address_on_file_sends_nothing_and_does_not_raise(self, monkeypatch):
        saved, sent = self._patch_io(monkeypatch)
        application = make_app(
            section="numerical", started_seconds_ago=800, question_seconds_ago=5, index=3,
            answers=[{"question_id": "e_rolls_to_six", "raw": "6", "parsed": 6,
                      "correct": True, "timed_out": False, "time_taken_s": 5.0}] * 3)

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
        assert summary == {"count": 0, "cv_avg": None, "written_avg": None, "entries": [], "mine": None}


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

    def test_a_decided_application_cannot_be_decided_again(self, monkeypatch):
        self._patch(monkeypatch, self._base(ap.S_ACCEPTED))
        admin = User(id="a1", username="root", is_admin=True)
        with pytest.raises(HTTPException):
            asyncio.run(ap.decide("u1", ap.Decision(decision="reject"), admin))


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

        async def fake_send(to, subject, title, body_html, cta_label=None, cta_url=None, ics=None):
            sent.append({"to": to, "subject": subject, "has_ics": ics is not None})
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
        assert result["interview"]["status"] == ap.INTERVIEW_PROPOSED

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

    def test_confirm_moves_to_confirmed_and_emails_both_sides_with_ics(self, monkeypatch):
        fake_db, sent = self._patch(monkeypatch, self._proposed_application())
        user = User(id="u1", username="jo")

        result = asyncio.run(ap.confirm_interview(user))

        assert result["interview"]["status"] == ap.INTERVIEW_CONFIRMED
        stored = fake_db.collections[ap.COLLECTION]["u1"]["interview"]
        assert stored["status"] == ap.INTERVIEW_CONFIRMED
        assert stored["responded_at"] is not None
        assert len(sent) == 2   # candidate + interviewer
        assert all(s["has_ics"] for s in sent)
        assert {s["to"] for s in sent} == {"jo@merton.ox.ac.uk", "priya@ox.ac.uk"}

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
        assert stored["status"] == ap.S_OA_READY   # CV already on file
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
