"""Tests for app.interview_oa — the timed, one-attempt probability/EV screen.

Pinned here: answer parsing (decimals, fractions, percentages), grading
against tolerance, the server-side clock (deadline/seconds-left/expiry), and
the advance-and-score bookkeeping that runs a session from question to
question and finally to a scored finish. All pure functions over plain
dicts, same approach as test_toxic_flow.py — no Firestore involved.
"""

import datetime as dt

from app import interview_oa as oa


def make_session(question_ids=None, index=0, started_seconds_ago=0.0):
    qids = question_ids or ["die_ev", "coin3_2h", "heart_card"]
    started = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=started_seconds_ago)
    return {
        "user_id": "u1", "username": "cand", "status": "active",
        "question_ids": qids, "current_index": index, "answers": [],
        "question_started_at": started,
    }


class TestParseAnswer:
    def test_plain_decimal(self):
        assert oa._parse_answer("0.375") == 0.375

    def test_integer(self):
        assert oa._parse_answer("7") == 7.0

    def test_fraction(self):
        assert oa._parse_answer("3/8") == 0.375

    def test_percentage(self):
        assert abs(oa._parse_answer("37.5%") - 0.375) < 1e-9

    def test_whitespace_and_commas(self):
        assert oa._parse_answer(" 1,234.5 ") == 1234.5

    def test_blank_is_none(self):
        assert oa._parse_answer("") is None
        assert oa._parse_answer(None) is None

    def test_garbage_is_none(self):
        assert oa._parse_answer("about three eighths") is None

    def test_division_by_zero_is_none(self):
        assert oa._parse_answer("3/0") is None


class TestGrading:
    def test_exact_match_within_tolerance(self):
        q = oa.QUESTION_BY_ID["coin3_2h"]     # answer 0.375, tol 0.01
        assert oa._grade(q, 0.375) is True
        assert oa._grade(q, 0.38) is True

    def test_outside_tolerance_fails(self):
        q = oa.QUESTION_BY_ID["coin3_2h"]
        assert oa._grade(q, 0.5) is False

    def test_unparsed_answer_fails(self):
        q = oa.QUESTION_BY_ID["die_ev"]
        assert oa._grade(q, None) is False

    def test_every_bank_question_has_a_consistent_key(self):
        # Sanity check on the hand-authored bank itself, not just the grader.
        for q in oa.QUESTION_BANK:
            assert 0 <= q["tol"] < 1
            assert oa._grade(q, q["answer"]) is True


class TestClock:
    def test_seconds_left_counts_down(self):
        s = make_session(started_seconds_ago=5)
        left = oa._seconds_left(s)
        assert 14.0 < left <= 15.0

    def test_seconds_left_floors_at_zero(self):
        s = make_session(started_seconds_ago=999)
        assert oa._seconds_left(s) == 0.0

    def test_deadline_handles_iso_string_timestamps(self):
        started = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=3)
        s = make_session()
        s["question_started_at"] = started.isoformat()
        assert 16.0 < oa._seconds_left(s) < 18.0


class TestResolveExpired:
    def test_does_nothing_before_the_deadline(self):
        s = make_session(started_seconds_ago=5)
        assert oa.resolve_expired(s) is False
        assert s["current_index"] == 0
        assert s["answers"] == []

    def test_does_nothing_within_the_grace_window(self):
        s = make_session(started_seconds_ago=oa.TIME_PER_QUESTION + 1)
        assert oa.resolve_expired(s) is False   # 1s past zero, grace is 3s

    def test_times_out_past_the_grace_window(self):
        s = make_session(started_seconds_ago=oa.TIME_PER_QUESTION + oa.ANSWER_GRACE + 1)
        assert oa.resolve_expired(s) is True
        assert s["current_index"] == 1
        assert s["answers"][0]["timed_out"] is True
        assert s["answers"][0]["correct"] is False

    def test_only_acts_on_active_sessions(self):
        s = make_session(started_seconds_ago=999)
        s["status"] = "finished"
        assert oa.resolve_expired(s) is False


class TestAdvanceAndScore:
    def test_recording_an_answer_moves_to_the_next_question(self):
        s = make_session(question_ids=["die_ev", "coin3_2h"])
        oa._record_answer(s, 0, "3.5", timed_out=False)
        assert s["current_index"] == 1
        assert s["status"] == "active"
        assert s["answers"][0]["correct"] is True

    def test_the_last_answer_finishes_and_scores_the_session(self):
        s = make_session(question_ids=["die_ev", "coin3_2h"])
        oa._record_answer(s, 0, "3.5", timed_out=False)          # correct
        oa._record_answer(s, 1, "not a number", timed_out=False)  # wrong
        assert s["status"] == "finished"
        assert s["score"] == {"correct": 1, "total": 2, "pct": 50.0}

    def test_a_timeout_still_counts_toward_the_total(self):
        s = make_session(question_ids=["die_ev"])
        oa._record_answer(s, 0, None, timed_out=True)
        assert s["status"] == "finished"
        assert s["score"] == {"correct": 0, "total": 1, "pct": 0.0}


class TestQuestionView:
    def test_hides_the_answer_key(self):
        s = make_session(started_seconds_ago=2)
        view = oa._question_view(s)
        assert "answer" not in view
        assert view["prompt"] == oa.QUESTION_BY_ID[s["question_ids"][0]]["prompt"]
        assert view["index"] == 0
        assert view["total"] == 3

    def test_none_once_past_the_last_question(self):
        s = make_session(question_ids=["die_ev"], index=1)
        assert oa._question_view(s) is None
