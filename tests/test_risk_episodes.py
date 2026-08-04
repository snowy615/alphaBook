"""Tests for the Risks game's episode library and scoring maths.

Everything under test is pure, so no Firestore and no clock are involved.
"""
import pytest

from app import risk_episodes as ep_lib


@pytest.fixture
def episode():
    """A small hand-built episode: one name halves, one doubles, one is flat."""
    return {
        "episode_id": "test_ep",
        "universe": "test",
        "universe_label": "Test",
        "days": 3,
        "dates": ["2025-01-01", "2025-01-02", "2025-01-03"],
        "index": [100.0, 100.0, 100.0],
        "names": [
            {"ticker": "DOWN", "closes": [100.0, 75.0, 50.0],
             "published_beta": 1.5, "realised_beta": 1.9, "shock_group": "victim"},
            {"ticker": "UP", "closes": [100.0, 150.0, 200.0],
             "published_beta": 0.8, "realised_beta": 0.4, "shock_group": "defensive"},
            {"ticker": "FLAT", "closes": [100.0, 100.0, 100.0],
             "published_beta": 1.0, "realised_beta": 1.0, "shock_group": "normal"},
        ],
        "panic_days": [1],
        "rebound_days": [2],
        "index_return_pct": 0.0,
        "index_drawdown_pct": 0.0,
    }


class TestLibrary:
    def test_shipped_episodes_load(self):
        eps = ep_lib.all_episodes()
        assert eps, "no episodes are installed in app/data/risk_episodes"
        for ep in eps:
            assert ep["days"] >= 2
            assert len(ep["names"]) >= 4
            for name in ep["names"]:
                assert len(name["closes"]) == ep["days"]
                assert all(c > 0 for c in name["closes"])
            assert len(ep["index"]) == ep["days"]

    def test_every_shipped_episode_has_real_dispersion(self):
        """Names must pull apart, or a market-neutral book has nothing to pick.

        Index depth is deliberately not asserted: the net-exposure cap means
        index direction is not the game, and the v7 episodes include rounds
        where the index barely moves while single names range 40pp apart.
        """
        for ep in ep_lib.all_episodes():
            rets = [(n["closes"][-1] / n["closes"][0] - 1) * 100 for n in ep["names"]]
            assert max(rets) - min(rets) > 15, ep["episode_id"]

    def test_v7_extras_are_well_formed_where_present(self):
        for ep in ep_lib.all_episodes():
            if ep.get("messages"):
                assert len(ep["messages"]) == ep["days"], ep["episode_id"]
                assert all(m.get("text") for m in ep["messages"]), ep["episode_id"]
            for start, end in (ep.get("phases") or {}).values():
                assert 0 <= start <= end <= ep["days"], ep["episode_id"]
            if ep.get("blend"):
                assert abs(sum(ep["blend"].values()) - 1.0) < 0.02, ep["episode_id"]
            for event in ep.get("events") or []:
                assert 0 <= event["day"] < ep["days"], ep["episode_id"]

    def test_universes_are_summarised(self):
        for u in ep_lib.universes():
            assert u["episodes"] >= 1
            assert u["min_days"] <= u["max_days"]

    def test_get_missing_episode_raises(self):
        with pytest.raises(ep_lib.EpisodeNotFound):
            ep_lib.get_episode("nope")

    def test_pick_episode_honours_universe(self):
        universe = ep_lib.universes()[0]["universe"]
        assert ep_lib.pick_episode(universe)["universe"] == universe

    def test_pick_unknown_universe_raises(self):
        with pytest.raises(ep_lib.EpisodeNotFound):
            ep_lib.pick_episode("not-a-universe")


class TestDisclosure:
    def test_live_view_hides_the_answers(self, episode):
        for row in ep_lib.public_names(episode, 1):
            assert "published_beta" in row
            assert "realised_beta" not in row
            assert "shock_group" not in row

    def test_live_view_shows_day_and_total_moves(self, episode):
        rows = {r["ticker"]: r for r in ep_lib.public_names(episode, 1)}
        assert rows["DOWN"]["price"] == 75.0
        assert rows["DOWN"]["day_pct"] == -25.0
        assert rows["DOWN"]["total_pct"] == -25.0

    def test_day_zero_reports_no_daily_move(self, episode):
        for row in ep_lib.public_names(episode, 0):
            assert row["day_pct"] == 0.0

    def test_day_index_is_clamped(self, episode):
        assert ep_lib.public_names(episode, 99)[0]["price"] == \
            ep_lib.public_names(episode, episode["days"] - 1)[0]["price"]

    def test_cohort_is_public_but_the_answers_are_not(self, episode):
        episode["names"][0]["sector"] = "Technology"
        row = ep_lib.public_names(episode, 1)[0]
        assert row["sector"] == "Technology"
        assert "shock_group" not in row

    def test_wire_is_absent_without_messages(self, episode):
        assert ep_lib.message_on(episode, 0) is None

    def test_wire_returns_the_day_and_clamps(self, episode):
        episode["messages"] = [
            {"text": "calm", "confidence": "Low"},
            {"text": "panic", "confidence": "High"},
            {"text": "bounce", "confidence": "Moderate"},
        ]
        assert ep_lib.message_on(episode, 1)["text"] == "panic"
        assert ep_lib.message_on(episode, 99)["text"] == "bounce"
        assert ep_lib.message_on(episode, -5)["text"] == "calm"

    def test_aftermath_is_empty_without_v7_extras(self, episode):
        assert ep_lib.aftermath(episode) == {}

    def test_aftermath_sorts_the_blend_heaviest_first(self, episode):
        episode["blend"] = {"Dot-com Bust": 0.25, "GFC": 0.75}
        episode["phases"] = {"crash": [1, 2]}
        after = ep_lib.aftermath(episode)
        assert [b["period"] for b in after["blend"]] == ["GFC", "Dot-com Bust"]
        assert after["phases"]["crash"] == [1, 2]

    def test_reveal_exposes_everything_sorted_worst_first(self, episode):
        reveal = ep_lib.reveal_names(episode)
        assert [r["ticker"] for r in reveal] == ["DOWN", "FLAT", "UP"]
        assert reveal[0]["shock_group"] == "victim"
        assert reveal[0]["realised_beta"] == 1.9
        assert reveal[0]["drawdown_pct"] == -50.0


class TestPositionMaths:
    def test_positions_accumulate_and_net_off(self):
        trades = [
            {"ticker": "A", "delta": 100, "price": 10.0, "day": 0},
            {"ticker": "A", "delta": -40, "price": 11.0, "day": 1},
            {"ticker": "B", "delta": -50, "price": 20.0, "day": 1},
        ]
        assert ep_lib.positions_after(trades) == {"A": 60, "B": -50}

    def test_positions_respect_the_day_cutoff(self):
        trades = [
            {"ticker": "A", "delta": 100, "price": 10.0, "day": 0},
            {"ticker": "A", "delta": 100, "price": 10.0, "day": 2},
        ]
        assert ep_lib.positions_after(trades, 0) == {"A": 100}
        assert ep_lib.positions_after(trades, 2) == {"A": 200}

    def test_fully_closed_position_disappears(self):
        trades = [
            {"ticker": "A", "delta": 100, "price": 10.0, "day": 0},
            {"ticker": "A", "delta": -100, "price": 12.0, "day": 1},
        ]
        assert ep_lib.positions_after(trades) == {}

    def test_cash_is_reduced_by_purchases_and_raised_by_shorts(self):
        buy = [{"ticker": "A", "delta": 100, "price": 10.0, "day": 0}]
        assert ep_lib.cash_after(buy) == ep_lib.START_EQUITY - 1000
        sell = [{"ticker": "A", "delta": -100, "price": 10.0, "day": 0}]
        assert ep_lib.cash_after(sell) == ep_lib.START_EQUITY + 1000

    def test_exposure_splits_gross_from_net(self):
        gross, net = ep_lib.exposure({"A": 100, "B": -100}, {"A": 10.0, "B": 10.0})
        assert gross == 2000
        assert net == 0


class TestScoring:
    def test_doing_nothing_scores_flat(self, episode):
        card = ep_lib.score_player(episode, [], 2)
        assert card["pnl"] == 0
        assert card["max_drawdown"] == 0
        assert card["score"] == 0

    def test_long_the_faller_loses_money(self, episode):
        trades = [{"ticker": "DOWN", "delta": 100, "price": 100.0, "day": 0}]
        assert ep_lib.score_player(episode, trades, 2)["pnl"] == pytest.approx(-5000)

    def test_short_the_faller_makes_money(self, episode):
        trades = [{"ticker": "DOWN", "delta": -100, "price": 100.0, "day": 0}]
        assert ep_lib.score_player(episode, trades, 2)["pnl"] == pytest.approx(5000)

    def test_the_market_neutral_pair_trade_pays(self, episode):
        # Short the victim, long the defensive name: the trade the exposure
        # limits are designed to push players towards.
        trades = [
            {"ticker": "DOWN", "delta": -100, "price": 100.0, "day": 0},
            {"ticker": "UP", "delta": 100, "price": 100.0, "day": 0},
        ]
        card = ep_lib.score_player(episode, trades, 2)
        assert card["pnl"] == pytest.approx(15000)
        # Marked on day 2: short 100 DOWN at 50, long 100 UP at 200.
        assert card["net"] == pytest.approx(-100 * 50 + 100 * 200)
        assert card["gross"] == pytest.approx(100 * 50 + 100 * 200)

    def test_trades_only_count_from_the_day_they_are_placed(self, episode):
        late = [{"ticker": "DOWN", "delta": -100, "price": 75.0, "day": 1}]
        # Entered at 75 on day 1, marked at 50 on day 2.
        assert ep_lib.score_player(episode, late, 2)["pnl"] == pytest.approx(2500)

    def test_drawdown_penalty_separates_two_equal_pnls(self, episode):
        """Two players finish +5,000. The one who was under water scores less."""
        # Straight line up: short the faller on day 0 and hold.
        smooth = [{"ticker": "DOWN", "delta": -100, "price": 100.0, "day": 0}]
        # Loses 5,000 shorting the riser, then makes 10,000 back on the faller.
        bumpy = [{"ticker": "UP", "delta": -100, "price": 100.0, "day": 0},
                 {"ticker": "UP", "delta": 100, "price": 150.0, "day": 1},
                 {"ticker": "DOWN", "delta": -400, "price": 75.0, "day": 1}]
        a = ep_lib.score_player(episode, smooth, 2)
        b = ep_lib.score_player(episode, bumpy, 2)

        assert a["pnl"] == pytest.approx(5000)
        assert b["pnl"] == pytest.approx(5000)
        assert a["max_drawdown"] == pytest.approx(0)
        assert b["max_drawdown"] == pytest.approx(5000)
        assert a["score"] == pytest.approx(5000)
        assert b["score"] == pytest.approx(2500)

    def test_max_drawdown_is_peak_to_trough(self):
        assert ep_lib.max_drawdown([100, 120, 90, 110]) == 30
        assert ep_lib.max_drawdown([100, 110, 120]) == 0
        assert ep_lib.max_drawdown([]) == 0

    def test_equity_curve_covers_every_day_to_date(self, episode):
        curve = ep_lib.equity_curve(episode, [], 2)
        assert len(curve) == 3
        assert all(v == ep_lib.START_EQUITY for v in curve)


class TestTradeLimits:
    def prices(self):
        return {"A": 100.0, "B": 100.0, "C": 100.0}

    def test_unknown_ticker_is_rejected(self):
        ok, why = ep_lib.check_trade({}, self.prices(), "ZZZ", 100)
        assert not ok and "not in this basket" in why

    def test_a_naked_long_breaches_the_net_band(self):
        # 5000 shares at 100 is 500k net, twice the 250k band.
        ok, why = ep_lib.check_trade({}, self.prices(), "A", 5000)
        assert not ok and "Net exposure" in why

    def test_a_hedged_pair_passes(self):
        ok, why = ep_lib.check_trade({"B": -5000}, self.prices(), "A", 5000)
        assert ok, why

    def test_gross_cap_binds_even_when_perfectly_hedged(self):
        # 11k long against 11k short is net flat but 2.2m gross, over the 2m cap.
        ok, why = ep_lib.check_trade({"B": -11000}, self.prices(), "A", 11000)
        assert not ok and "Gross exposure" in why

    def test_a_small_naked_position_inside_the_band_passes(self):
        ok, why = ep_lib.check_trade({}, self.prices(), "A", 2000)
        assert ok, why

    def test_closing_a_position_is_always_allowed(self):
        ok, why = ep_lib.check_trade({"A": 2000}, self.prices(), "A", 0)
        assert ok, why
