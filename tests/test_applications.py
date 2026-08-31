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

    def test_mix_adds_up_to_the_paper(self):
        assert sum(ap.PAPER_MIX.values()) == ap.NUMERICAL_QUESTIONS

    def test_bank_can_fill_every_slot(self):
        by_kind = Counter(q["kind"] for q in ap.QUESTION_BANK)
        for kind, needed in ap.PAPER_MIX.items():
            assert by_kind[kind] >= needed

    def test_paper_has_the_fixed_mix_and_no_repeats(self):
        paper = ap.build_paper(random.Random(7))
        assert len(paper) == ap.NUMERICAL_QUESTIONS
        assert len(set(paper)) == ap.NUMERICAL_QUESTIONS
        assert Counter(ap.QUESTION_BY_ID[q]["kind"] for q in paper) == Counter(ap.PAPER_MIX)

    def test_the_two_sections_fill_the_sitting_exactly(self):
        assert (ap.WRITTEN_SECONDS
                + ap.NUMERICAL_QUESTIONS * ap.SECONDS_PER_QUESTION) == ap.SESSION_SECONDS


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

    def test_someone_already_on_a_programme_may_not(self):
        assert mb.can_apply({"membership": mb.M_QUANT_ANALYST}) is False
        assert mb.can_apply({"membership": mb.M_QUANT_BOOTCAMP}) is False
        assert mb.can_apply({"membership": mb.M_FUND_ANALYST}) is False

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
