"""Tests for app.competitions — event setup, format and timetable.

The parts worth pinning are the ones a host can't easily check by eye: that a
scheduled event opens itself and a timed one closes itself, that settings are
validated against each mode's real options rather than trusted from the form,
and that a locked format only applies to the modes the event actually scores.
"""

import datetime as dt

import pytest

from app import competitions as comp


def now():
    return dt.datetime.now(dt.timezone.utc)


class TestSchedule:
    def test_a_scheduled_event_opens_itself_when_due(self):
        data = {"status": comp.STATUS_SCHEDULED,
                "starts_at": now() - dt.timedelta(minutes=1)}
        assert comp.sync_schedule(data) is True
        assert data["status"] == comp.STATUS_RUNNING
        assert data["started_at"] is not None

    def test_a_future_start_stays_scheduled(self):
        data = {"status": comp.STATUS_SCHEDULED,
                "starts_at": now() + dt.timedelta(hours=2)}
        assert comp.sync_schedule(data) is False
        assert data["status"] == comp.STATUS_SCHEDULED

    def test_a_running_event_closes_itself_at_its_end_time(self):
        data = {"status": comp.STATUS_RUNNING,
                "ends_at": now() - dt.timedelta(seconds=5)}
        assert comp.sync_schedule(data) is True
        assert data["status"] == comp.STATUS_FINISHED

    def test_it_can_open_and_close_in_one_pass(self):
        # Nobody looked for the whole window; one read has to catch both edges.
        data = {"status": comp.STATUS_SCHEDULED,
                "starts_at": now() - dt.timedelta(hours=2),
                "ends_at": now() - dt.timedelta(hours=1)}
        assert comp.sync_schedule(data) is True
        assert data["status"] == comp.STATUS_FINISHED

    def test_a_finished_event_is_left_alone(self):
        data = {"status": comp.STATUS_FINISHED,
                "starts_at": now() - dt.timedelta(days=1)}
        assert comp.sync_schedule(data) is False

    def test_naive_timestamps_are_read_as_utc(self):
        # Firestore returns tz-aware values but local writes may not be.
        data = {"status": comp.STATUS_SCHEDULED,
                "starts_at": dt.datetime.utcnow() - dt.timedelta(minutes=1)}
        assert comp.sync_schedule(data) is True


class TestWhenParsing:
    def test_iso_with_and_without_zone(self):
        assert comp._parse_when("2026-09-01T18:00:00Z").tzinfo is not None
        assert comp._parse_when("2026-09-01T18:00:00").tzinfo is not None

    def test_blank_is_no_schedule(self):
        assert comp._parse_when(None) is None
        assert comp._parse_when("") is None

    def test_nonsense_is_rejected(self):
        with pytest.raises(Exception):
            comp._parse_when("next tuesday-ish")


class TestSettings:
    def test_only_configurable_modes_carry_settings(self):
        out = comp._clean_settings(["mental_math", "fiveos"], {})
        assert "mental_math" in out
        assert "fiveos" not in out          # nothing to configure, so nothing stored

    def test_defaults_fill_in_when_the_form_says_nothing(self):
        out = comp._clean_settings(["mental_math"], {})
        mm = out["mental_math"]
        assert mm["difficulty"] == "medium"
        assert mm["num_questions"] == 10
        assert mm["question_types"]

    def test_numbers_are_clamped_to_their_range(self):
        out = comp._clean_settings(
            ["mental_math"], {"mental_math": {"num_questions": 5000}})
        assert out["mental_math"]["num_questions"] == 50
        out = comp._clean_settings(
            ["mental_math"], {"mental_math": {"num_questions": -3}})
        assert out["mental_math"]["num_questions"] == 5

    def test_junk_numbers_fall_back_to_the_default(self):
        out = comp._clean_settings(
            ["mental_math"], {"mental_math": {"time_per_question": "soon"}})
        assert out["mental_math"]["time_per_question"] == 15

    def test_an_unknown_choice_is_refused(self):
        out = comp._clean_settings(
            ["mental_math"], {"mental_math": {"difficulty": "impossible"}})
        assert out["mental_math"]["difficulty"] == "medium"

    def test_unknown_question_types_are_dropped(self):
        out = comp._clean_settings(["mental_math"], {"mental_math": {
            "question_types": ["addition", "telepathy"]}})
        assert out["mental_math"]["question_types"] == ["addition"]

    def test_emptying_the_types_falls_back_rather_than_breaking_the_game(self):
        out = comp._clean_settings(
            ["mental_math"], {"mental_math": {"question_types": ["telepathy"]}})
        assert out["mental_math"]["question_types"]      # never an unplayable set

    def test_settings_for_a_mode_not_in_the_event_are_ignored(self):
        out = comp._clean_settings(["fiveos"], {"mental_math": {"difficulty": "hard"}})
        assert out == {}


class TestSpec:
    def test_the_form_spec_covers_the_configurable_modes(self):
        spec = comp.mode_settings_spec()
        assert {"mental_math", "headline", "risks"} <= set(spec)

    def test_every_field_declares_what_the_form_needs(self):
        for mode, fields in comp.mode_settings_spec().items():
            for f in fields:
                assert {"key", "label", "type"} <= set(f), (mode, f)
                assert f["type"] in {"select", "number", "multi"}
                if f["type"] in {"select", "multi"}:
                    assert f.get("options"), (mode, f["key"])

    def test_scenario_options_come_from_the_game_itself(self):
        from app import headline as hl
        opts = {o["value"] for o in comp.mode_settings_spec()["headline"][0]["options"]}
        assert opts == set(hl.TEMPLATES)
