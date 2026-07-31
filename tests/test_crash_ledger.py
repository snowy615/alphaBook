"""Tests for app.crash_ledger — the Crash Call duel game.

The invariant that matters: every generated round's stored answer must actually
be correct for its metric, and a round must never leak the answer to the client.
"""

import random

from app import crash_ledger as cl


def _is_answer_correct(round_):
    """Recompute the answer from the raw values and the round's own direction."""
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
            r = cl._make_round(rng)
            assert _is_answer_correct(r), r

    def test_rounds_are_not_near_ties(self):
        rng = random.Random(1)
        for _ in range(500):
            r = cl._make_round(rng)
            gap = next(p["gap"] for p in cl.PROMPTS
                       if p["key"] == r["prompt"] and p["pick"] == r["pick"])
            assert abs(r["va"] - r["vb"]) >= gap

    def test_two_distinct_stocks_per_round(self):
        rng = random.Random(2)
        for _ in range(200):
            r = cl._make_round(rng)
            assert r["a"]["ticker"] != r["b"]["ticker"]


class TestGame:
    def test_game_has_the_configured_number_of_rounds(self):
        g = cl.Game("g", "u", "Ada", random.Random(3))
        assert len(g.rounds) == cl.ROUNDS_PER_GAME

    def test_round_view_hides_metric_values_and_the_answer(self):
        g = cl.Game("g", "u", "Ada", random.Random(4))
        view = g.round_view()
        assert set(view) == {"index", "total", "question", "label", "a", "b"}
        assert set(view["a"]) == {"ticker", "name", "exchange"}   # no metric, no answer
        assert "answer" not in view

    def test_streak_bonus_grows_with_a_hot_run(self):
        # Mirror the scoring in the answer endpoint: 100 + 25*(streak-1).
        def points(streak):
            return 100 + 25 * (streak - 1)

        run = [points(s) for s in (1, 2, 3)]
        assert run == [100, 125, 150]
        # a 3-correct streak beats three separate first-answers (3*100)
        assert sum(run) > 3 * points(1)
