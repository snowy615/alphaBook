"""Tests for app.crash_ledger — the Crash Call duel game + gamification layer.

Invariants that matter: a round's stored answer is always correct for its
metric and never leaks to the client; difficulty scales pair-closeness; and the
level/tier math is monotonic.
"""

import random

from app import crash_ledger as cl


def _is_answer_correct(round_):
    va, vb = round_["va"], round_["vb"]
    expected = ("a" if va > vb else "b") if round_["pick"] == "max" else ("a" if va < vb else "b")
    return round_["answer"] == expected


class TestDataset:
    def test_stocks_have_the_metrics_the_game_uses(self):
        assert len(cl._STOCKS) >= 10
        keys = {"ticker", "name", "exchange", "worst_drawdown", "volatility",
                "avg_return", "total_return", "worst_period"}
        for s in cl._STOCKS:
            assert keys <= set(s), s.get("ticker")


class TestRounds:
    def test_round_answers_are_always_correct(self):
        rng = random.Random(0)
        for _ in range(500):
            r = cl._make_round(rng, rng.random())
            assert _is_answer_correct(r), r

    def test_rounds_respect_the_prompt_gap_floor(self):
        rng = random.Random(1)
        for _ in range(500):
            r = cl._make_round(rng, rng.random())
            gap = next(p["gap"] for p in cl.PROMPTS
                       if p["key"] == r["prompt"] and p["pick"] == r["pick"])
            assert abs(r["va"] - r["vb"]) >= gap

    def test_higher_difficulty_serves_closer_pairs_on_average(self):
        rng = random.Random(7)

        def avg_gap(diff):
            gaps = []
            for _ in range(400):
                r = cl._make_round(rng, diff)
                gaps.append(abs(r["va"] - r["vb"]))
            return sum(gaps) / len(gaps)

        # Easy rounds should be more obvious (wider gaps) than hard ones.
        assert avg_gap(0.05) > avg_gap(0.95)

    def test_two_distinct_stocks_per_round(self):
        rng = random.Random(2)
        for _ in range(200):
            r = cl._make_round(rng, rng.random())
            assert r["a"]["ticker"] != r["b"]["ticker"]


class TestGame:
    def test_game_starts_with_a_round_ready(self):
        g = cl.Game("g", "u", "Ada", random.Random(3))
        assert g.current is not None
        assert g.idx == 0 and not g.done

    def test_round_view_hides_values_and_answer(self):
        g = cl.Game("g", "u", "Ada", random.Random(4))
        view = g.round_view()
        assert set(view) == {"index", "total", "question", "label", "a", "b"}
        assert set(view["a"]) == {"ticker", "name", "exchange"}

    def test_playing_through_reaches_done_after_all_rounds(self):
        g = cl.Game("g", "u", "Ada", random.Random(9))
        outs = []
        for _ in range(cl.ROUNDS_PER_GAME):
            outs.append(g.answer(g.current["answer"]))   # always correct
        assert outs[-1]["done"] is True
        assert g.correct == cl.ROUNDS_PER_GAME
        assert g.score > 0

    def test_difficulty_rises_on_correct_and_falls_on_wrong(self):
        g = cl.Game("g", "u", "Ada", random.Random(11))
        wrong = "b" if g.current["answer"] == "a" else "a"
        g.answer(wrong)
        assert g.difficulty < cl.DIFF_START           # a miss made it easier
        g2 = cl.Game("g2", "u", "Ada", random.Random(11))
        g2.answer(g2.current["answer"])
        assert g2.difficulty > cl.DIFF_START           # a hit made it harder

    def test_streak_milestones_fire(self):
        g = cl.Game("g", "u", "Ada", random.Random(13))
        milestones = []
        for _ in range(cl.ROUNDS_PER_GAME):
            out = g.answer(g.current["answer"])
            if out["milestone"]:
                milestones.append(out["milestone"])
        assert 3 in milestones and 5 in milestones


class TestProgression:
    def test_levels_are_monotonic_in_xp(self):
        levels = [cl._level_for_xp(xp) for xp in range(0, 5000, 25)]
        assert levels == sorted(levels)
        assert cl._level_for_xp(0) == 1
        assert cl._level_for_xp(50) == 2

    def test_level_progress_is_a_fraction(self):
        for xp in (0, 30, 199, 200, 1234):
            lvl, into, span, pct = cl._level_progress(xp)
            assert 0.0 <= pct < 1.0
            assert into < span

    def test_tier_rises_with_level(self):
        keys = [cl._tier_for_level(lv)["key"] for lv in (1, 4, 8, 15, 25)]
        assert keys == ["bronze", "silver", "gold", "platinum", "diamond"]

    def test_new_player_has_an_endowed_head_start(self):
        # Seeded XP puts a fresh player partway up level 1, not at a cold zero.
        assert cl.NEW_PLAYER_XP > 0
        lvl, into, span, pct = cl._level_progress(cl.NEW_PLAYER_XP)
        assert pct > 0.0
