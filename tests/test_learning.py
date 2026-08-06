"""Tests for placement scoring and the learning path.

Everything here is pure — no Firestore, no clock.
"""
import pytest

from app import learning


class TestQuizShape:
    def test_every_question_is_answerable(self):
        assert learning.QUESTIONS, "no placement questions defined"
        for q in learning.QUESTIONS:
            assert q["key"] and q["prompt"]
            assert len(q["options"]) >= 2
            values = [o["value"] for o in q["options"]]
            assert len(values) == len(set(values)), q["key"]
            assert all(isinstance(o["points"], int) for o in q["options"])

    def test_question_keys_are_unique(self):
        keys = [q["key"] for q in learning.QUESTIONS]
        assert len(keys) == len(set(keys))

    def test_every_question_has_a_zero_option(self):
        # A total beginner must be able to score 0 on every question.
        for q in learning.QUESTIONS:
            assert min(o["points"] for o in q["options"]) == 0, q["key"]

    def test_cutoffs_sit_inside_the_scale(self):
        assert 0 < learning.INTERMEDIATE_AT < learning.ADVANCED_AT < learning.MAX_POINTS


class TestPlacement:
    def test_all_lowest_answers_place_beginner(self):
        answers = {q["key"]: min(q["options"], key=lambda o: o["points"])["value"]
                   for q in learning.QUESTIONS}
        result = learning.score_answers(answers)
        assert result["level"] == "beginner"
        assert result["points"] == 0

    def test_all_highest_answers_place_advanced(self):
        answers = {q["key"]: max(q["options"], key=lambda o: o["points"])["value"]
                   for q in learning.QUESTIONS}
        result = learning.score_answers(answers)
        assert result["level"] == "advanced"
        assert result["points"] == learning.MAX_POINTS

    def test_a_middling_answer_sheet_places_intermediate(self):
        answers = {"traded": "dabbled", "orderbook": "basics",
                   "quant": "some", "code": "learning"}
        assert learning.score_answers(answers)["level"] == "intermediate"

    def test_no_answers_is_beginner_not_a_crash(self):
        result = learning.score_answers({})
        assert result["level"] == "beginner"
        assert result["points"] == 0
        assert result["reasons"] == []

    def test_unknown_option_values_are_ignored(self):
        result = learning.score_answers({"traded": "not-an-option", "quant": "strong"})
        assert result["points"] == 6

    def test_placement_explains_itself(self):
        answers = {"traded": "professional", "orderbook": "new",
                   "quant": "some", "code": "none"}
        result = learning.score_answers(answers)
        assert result["reasons"], "placement should say why"
        assert any("professional" in r.lower() for r in result["reasons"])

    def test_coding_flag_gates_the_coding_modes(self):
        assert learning.score_answers({"code": "fluent"})["codes"] is True
        assert learning.score_answers({"code": "none"})["codes"] is False

    @pytest.mark.parametrize("points,expected", [
        (0, "beginner"),
        (learning.INTERMEDIATE_AT - 1, "beginner"),
        (learning.INTERMEDIATE_AT, "intermediate"),
        (learning.ADVANCED_AT - 1, "intermediate"),
        (learning.ADVANCED_AT, "advanced"),
        (learning.MAX_POINTS, "advanced"),
    ])
    def test_cutoffs(self, points, expected):
        assert learning.level_for_points(points) == expected


class TestPaths:
    def test_every_level_has_a_path(self):
        for level in learning.LEVELS:
            steps = learning.path_for(level)
            assert steps, level
            for s in steps:
                assert s["mode"] and s["title"] and s["why"] and s["teaches"]

    def test_paths_point_at_real_modes(self):
        from app import scores
        for level in learning.LEVELS:
            for s in learning.path_for(level):
                assert s["mode"] in scores.MODES, f"{level}: {s['mode']}"

    def test_beginner_path_has_no_coding_mode(self):
        coding = {"market_sim_py", "swe_prep"}
        assert not coding & {s["mode"] for s in learning.path_for("beginner")}

    def test_advanced_path_includes_the_coding_modes(self):
        assert {"market_sim_py", "swe_prep"} <= {
            s["mode"] for s in learning.path_for("advanced")}

    def test_unknown_level_falls_back_to_beginner(self):
        assert learning.path_for("wizard") == learning.path_for("beginner")


class TestProgress:
    def test_nothing_played_points_at_step_one(self):
        p = learning.build_progress("beginner", set())
        assert p["done"] == 0
        assert p["pct"] == 0
        assert p["next"]["index"] == 1
        assert p["next"]["is_next"] is True
        assert p["complete"] is False

    def test_finishing_a_mode_ticks_its_step(self):
        first = learning.path_for("beginner")[0]["mode"]
        p = learning.build_progress("beginner", {first})
        assert p["steps"][0]["done"] is True
        assert p["done"] == 1
        assert p["next"]["index"] == 2

    def test_next_skips_over_completed_steps(self):
        path = learning.path_for("beginner")
        p = learning.build_progress("beginner", {path[0]["mode"], path[1]["mode"]})
        assert p["next"]["index"] == 3

    def test_playing_everything_completes_the_path(self):
        modes = {s["mode"] for s in learning.path_for("intermediate")}
        p = learning.build_progress("intermediate", modes)
        assert p["complete"] is True
        assert p["next"] is None
        assert p["pct"] == 100

    def test_exactly_one_step_is_flagged_next(self):
        p = learning.build_progress("advanced", set())
        assert sum(1 for s in p["steps"] if s["is_next"]) == 1

    def test_mode_meta_supplies_labels_and_links(self):
        from app import scores
        p = learning.build_progress("beginner", set(), scores.MODES)
        for s in p["steps"]:
            assert s["label"] != s["mode"], "label should come from mode meta"
            assert s["href"].startswith("/")

    def test_progress_survives_missing_mode_meta(self):
        p = learning.build_progress("beginner", set(), None)
        assert p["steps"][0]["href"] == "/"

    def test_unrelated_played_modes_do_not_tick_steps(self):
        p = learning.build_progress("beginner", {"not_a_mode"})
        assert p["done"] == 0


class TestLevelLadder:
    def test_levels_step_upwards(self):
        assert learning.next_level("beginner") == "intermediate"
        assert learning.next_level("intermediate") == "advanced"

    def test_top_level_has_nowhere_to_go(self):
        assert learning.next_level("advanced") is None

    def test_unknown_level_has_no_successor(self):
        assert learning.next_level("wizard") is None

    def test_every_level_has_display_copy(self):
        for level in learning.LEVELS:
            assert learning.LEVEL_META[level]["label"]
            assert learning.LEVEL_META[level]["blurb"]
