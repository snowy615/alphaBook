"""Tests for app.feedback — the per-game coaching.

Feedback that confidently says the wrong thing is worse than none, so the
directional claims are pinned here: which side of a market got picked off,
which question type is weak, whether a run traded at all. Every analyser must
also survive junk input, because it runs inside a game's finish path.
"""

import pytest

from app import feedback as fb


def texts(out):
    return " ".join(n["text"] for n in out["notes"])


class TestShape:
    @pytest.mark.parametrize("mode", sorted(fb.ANALYSERS))
    def test_every_analyser_returns_the_shared_shape(self, mode):
        out = fb.analyse(mode, {})
        assert set(out) == {"headline", "grade", "stats", "notes"}
        assert out["grade"] in {"great", "good", "mixed", "poor"}
        assert isinstance(out["stats"], list) and isinstance(out["notes"], list)
        assert len(out["notes"]) <= 4

    @pytest.mark.parametrize("mode", sorted(fb.ANALYSERS))
    def test_analysers_survive_junk(self, mode):
        for junk in ({"rounds": None}, {"pnl": "nonsense"}, {"questions": 5}):
            out = fb.analyse(mode, junk)
            assert out["headline"]

    def test_unknown_mode_is_harmless(self):
        assert fb.analyse("no_such_mode", {"anything": 1})["headline"]


class TestCrashLedger:
    def _rounds(self, side, n=10, width_units=0.5, points=-50):
        return [{"ticker": "AAA", "label": "worst drawdown", "prompt": "worst_drawdown",
                 "truth": -50.0, "bid": -60.0, "ask": -40.0, "points": points,
                 "side": side, "tradeable": True, "width": 20.0,
                 "width_units": width_units} for _ in range(n)]

    def test_being_lifted_says_the_market_was_too_low(self):
        out = fb.analyse("crash_ledger", {"rounds": self._rounds("lifted"), "score": -500})
        body = texts(out)
        assert "too low" in body
        assert "too high" not in body

    def test_being_hit_says_the_market_was_too_high(self):
        out = fb.analyse("crash_ledger", {"rounds": self._rounds("hit"), "score": -500})
        body = texts(out)
        assert "too high" in body
        assert "too low" not in body

    def test_untradeable_markets_are_called_out(self):
        rounds = self._rounds("held", width_units=5.0, points=0)
        for r in rounds:
            r["tradeable"] = False
        out = fb.analyse("crash_ledger", {"rounds": rounds, "score": 0})
        assert "nobody traded them" in texts(out)

    def test_a_clean_sweep_is_told_to_tighten(self):
        rounds = self._rounds("held", width_units=0.5, points=150)
        out = fb.analyse("crash_ledger", {"rounds": rounds, "score": 1500})
        assert out["grade"] == "great"
        assert "tightening" in texts(out)


class TestMentalMath:
    def test_the_weakest_question_type_is_named(self):
        questions = [{"type": "addition"}] * 4 + [{"type": "division"}] * 4
        answers = [{"index": i, "correct": i < 4} for i in range(8)]
        out = fb.analyse("mental_math", {"questions": questions, "answers": answers})
        body = texts(out)
        assert "Division" in body and "weak spot" in body
        assert "Addition is solid" in body

    def test_unanswered_questions_are_flagged(self):
        questions = [{"type": "addition"}] * 10
        answers = [{"index": i, "correct": True} for i in range(6)]
        out = fb.analyse("mental_math", {"questions": questions, "answers": answers})
        assert "ran out of time on 4" in texts(out)


class TestBotRun:
    def test_no_fills_is_the_headline_problem(self):
        out = fb.analyse("market_sim_py", {"pnl": 0, "fills": 0, "orders_accepted": 5})
        assert "never got a fill" in out["headline"]
        assert "No fills" in texts(out)

    def test_heavy_rejection_rate_is_called_out(self):
        out = fb.analyse("swe_prep", {"pnl": 10, "fills": 3, "volume": 30,
                                      "orders_accepted": 10, "orders_rejected": 10})
        assert "rejected" in texts(out)

    def test_a_crashed_strategy_reports_its_error(self):
        out = fb.analyse("market_sim_py", {"pnl": 0, "fills": 0, "error": "ZeroDivisionError: x"})
        assert out["grade"] == "poor"
        assert "ZeroDivisionError" in texts(out)


class TestRisks:
    def test_drawdown_eating_the_pnl_is_explained(self):
        out = fb.analyse("risks", {"pnl": 4000, "max_drawdown": 5000, "score": -1000,
                                   "gross": 19500, "trade_count": 1, "gross_limit": 20000})
        body = texts(out)
        assert "drawdown" in body
        assert "gross limit" in body

    def test_a_clean_run_reads_as_great(self):
        out = fb.analyse("risks", {"pnl": 5000, "max_drawdown": 500, "score": 4750,
                                   "gross": 8000, "trade_count": 9, "gross_limit": 20000})
        assert out["grade"] == "great"


class TestPokerAuction:
    def test_overpaying_at_auction_is_named(self):
        out = fb.analyse("poker_auction", {"money": 600, "start_money": 1000,
                                           "hand": "One Pair", "award": 100,
                                           "spent": 500, "cards": 9})
        body = texts(out)
        assert "winner's curse" in body
        assert "second-price" in body
