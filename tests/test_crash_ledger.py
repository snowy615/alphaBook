"""Tests for app.crash_ledger — Crash Call, the crash-statistic market-making
drill, plus the gamification layer.

The invariants that matter: a round never leaks its true value to the client;
difficulty controls how much of an outlier the name is; scoring rewards tight
correct markets, pays nothing for untradeably wide ones, and punishes being
picked off in proportion to the edge given away — all in units of each
statistic's own spread, so no statistic is harder to score well on than
another. Head-to-head rooms must serve every player the identical set.
"""

import random

from app import crash_ledger as cl


def _quote_around(rnd, half_widths):
    """A market centred on the truth, `half_widths` spreads wide either side."""
    half = half_widths * rnd["spread"]
    return rnd["truth"] - half, rnd["truth"] + half


class TestDataset:
    def test_stocks_have_the_metrics_the_game_uses(self):
        assert len(cl._STOCKS) >= 10
        keys = {"ticker", "name", "exchange", "worst_drawdown", "volatility",
                "avg_return", "total_return", "worst_period"}
        for s in cl._STOCKS:
            assert keys <= set(s), s.get("ticker")

    def test_quotable_statistics_have_a_usable_spread(self):
        # A market can only be fair on a statistic with a sane linear range;
        # total_return runs to +66,000% and is deliberately excluded.
        quotable = {p["key"] for p in cl.MARKET_PROMPTS}
        assert "total_return" not in quotable
        for key in quotable:
            mean, sd = cl._STAT_SPREAD[key]
            assert sd > 0
            lo, hi = next((p["lo"], p["hi"]) for p in cl.MARKET_PROMPTS if p["key"] == key)
            assert lo <= mean <= hi


class TestRounds:
    def test_round_carries_a_truth_on_its_own_scale(self):
        rng = random.Random(0)
        for _ in range(300):
            r = cl._make_round(rng, rng.random())
            assert r["lo"] <= r["truth"] <= r["hi"]
            assert r["spread"] > 0
            assert r["stock"]["name"] in r["q"]

    def test_higher_difficulty_serves_bigger_outliers(self):
        rng = random.Random(7)

        def avg_abs_z(diff):
            zs = []
            for _ in range(300):
                r = cl._make_round(rng, diff)
                mean, sd = cl._STAT_SPREAD[r["prompt"]]
                zs.append(abs((r["truth"] - mean) / sd))
            return sum(zs) / len(zs)

        # Easy rounds sit near the cohort average; hard ones are the surprises.
        assert avg_abs_z(0.05) < avg_abs_z(0.95)

    def test_round_view_never_leaks_the_answer(self):
        def numbers(obj):
            """Every number anywhere in the payload."""
            if isinstance(obj, bool):
                return
            if isinstance(obj, (int, float)):
                yield float(obj)
            elif isinstance(obj, dict):
                for v in obj.values():
                    yield from numbers(v)
            elif isinstance(obj, (list, tuple)):
                for v in obj:
                    yield from numbers(v)

        rng = random.Random(2)
        for _ in range(200):
            r = cl._make_round(rng, rng.random())
            view = cl._round_view(r, 0, 10)
            assert "truth" not in view
            # The scale bounds may coincidentally equal the truth; nothing else may.
            bounds = {float(r["lo"]), float(r["hi"])}
            leaked = [n for n in numbers(view)
                      if n == float(r["truth"]) and n not in bounds]
            assert not leaked, (r["truth"], view)


class TestScoring:
    def _round(self, seed=5, diff=0.5):
        return cl._make_round(random.Random(seed), diff)

    def test_tighter_correct_markets_pay_more(self):
        r = self._round()
        tight = cl.score_quote(r, *_quote_around(r, 0.15), 0)
        loose = cl.score_quote(r, *_quote_around(r, 1.0), 0)
        assert tight["held"] and loose["held"]
        assert tight["points"] > loose["points"] > 0

    def test_untradeably_wide_markets_earn_nothing(self):
        r = self._round()
        res = cl.score_quote(r, *_quote_around(r, cl.MAX_TRADEABLE_WIDTH), 0)
        assert res["held"] is True          # the truth is inside…
        assert res["tradeable"] is False    # …but nobody would trade it
        assert res["points"] == 0

    def test_being_picked_off_costs_points_and_names_the_side(self):
        r = self._round()
        sd = r["spread"]
        lifted = cl.score_quote(r, r["truth"] - 2 * sd, r["truth"] - sd, 0)
        hit = cl.score_quote(r, r["truth"] + sd, r["truth"] + 2 * sd, 0)
        assert lifted["side"] == "lifted" and lifted["points"] < 0
        assert hit["side"] == "hit" and hit["points"] < 0

    def test_bigger_misses_cost_more_but_stay_capped(self):
        r = self._round()
        sd = r["spread"]
        near = cl.score_quote(r, r["truth"] - 1.2 * sd, r["truth"] - 0.2 * sd, 0)
        far = cl.score_quote(r, r["truth"] - 12 * sd, r["truth"] - 10 * sd, 0)
        assert far["points"] < near["points"] < 0
        assert far["points"] >= -cl.MAX_LOSS

    def test_scoring_is_scale_free_across_statistics(self):
        # The same bravery on drawdown (sd ≈ 26) and volatility (sd ≈ 2.3)
        # must score the same, or one statistic becomes the way to farm points.
        rng = random.Random(3)
        seen = {}
        for _ in range(400):
            r = cl._make_round(rng, 0.5)
            res = cl.score_quote(r, *_quote_around(r, 0.5), 0)
            seen.setdefault(r["prompt"], set()).add(res["points"])
        assert len(seen) > 1
        for key, points in seen.items():
            assert points == next(iter(seen.values())), (key, seen)

    def test_a_streak_only_pays_while_markets_hold(self):
        r = self._round()
        plain = cl.score_quote(r, *_quote_around(r, 0.5), 0)
        streaked = cl.score_quote(r, *_quote_around(r, 0.5), 4)
        assert streaked["points"] > plain["points"]
        sd = r["spread"]
        missed = cl.score_quote(r, r["truth"] + sd, r["truth"] + 2 * sd, 4)
        assert missed["points"] < 0        # no streak bonus on a pick-off


class TestGame:
    def test_game_starts_with_a_round_ready(self):
        g = cl.Game("g", "u", "Ada", random.Random(3))
        assert g.current is not None
        assert g.idx == 0 and not g.done

    def test_round_view_hides_the_truth(self):
        g = cl.Game("g", "u", "Ada", random.Random(4))
        view = g.round_view()
        assert "truth" not in view
        assert set(view["stock"]) == {"ticker", "name", "exchange", "category"}
        assert {"lo", "hi", "step", "unit", "cohort_avg", "spread"} <= set(view)

    def test_playing_through_reaches_done_after_all_rounds(self):
        g = cl.Game("g", "u", "Ada", random.Random(9))
        outs = []
        for _ in range(cl.ROUNDS_PER_GAME):
            outs.append(g.quote(*_quote_around(g.current, 0.3)))
        assert outs[-1]["done"] is True
        assert g.correct == cl.ROUNDS_PER_GAME
        assert g.score > 0

    def test_a_bad_session_can_finish_negative(self):
        g = cl.Game("g", "u", "Ada", random.Random(10))
        for _ in range(cl.ROUNDS_PER_GAME):
            r = g.current
            sd = r["spread"]
            g.quote(r["truth"] + 2 * sd, r["truth"] + 3 * sd)   # always wrong side
        assert g.score < 0
        assert g.correct == 0

    def test_difficulty_rises_when_markets_hold_and_falls_when_picked_off(self):
        g = cl.Game("g", "u", "Ada", random.Random(11))
        r = g.current
        g.quote(r["truth"] + r["spread"], r["truth"] + 2 * r["spread"])
        assert g.difficulty < cl.DIFF_START            # picked off → easier

        g2 = cl.Game("g2", "u", "Ada", random.Random(11))
        g2.quote(*_quote_around(g2.current, 0.3))
        assert g2.difficulty > cl.DIFF_START           # held → harder

    def test_streak_milestones_fire(self):
        g = cl.Game("g", "u", "Ada", random.Random(13))
        milestones = []
        for _ in range(cl.ROUNDS_PER_GAME):
            out = g.quote(*_quote_around(g.current, 0.3))
            if out["milestone"]:
                milestones.append(out["milestone"])
        assert 3 in milestones and 5 in milestones


class TestRooms:
    def test_every_player_gets_the_identical_set_of_rounds(self):
        room = cl.Room("r", "CODE1", "u1", "Ada")
        room.join("u1", "Ada")
        room.join("u2", "Grace")
        room.status = "active"
        a, b = room.round_view("u1"), room.round_view("u2")
        assert a == b
        assert len(room.rounds) == cl.ROUNDS_PER_GAME

    def test_identical_quotes_score_identically(self):
        room = cl.Room("r", "CODE2", "u1", "Ada")
        room.join("u1", "Ada")
        room.join("u2", "Grace")
        room.status = "active"
        for _ in range(cl.ROUNDS_PER_GAME):
            rnd = room.rounds[room.players["u1"]["idx"]]
            quote = _quote_around(rnd, 0.4)
            room.quote("u1", *quote)
            room.quote("u2", *quote)
        assert room.players["u1"]["score"] == room.players["u2"]["score"]
        assert room.status == "finished"

    def test_difficulty_ramps_across_the_room_rounds(self):
        room = cl.Room("r", "CODE3", "u1", "Ada")
        diffs = [r["difficulty"] for r in room.rounds]
        assert diffs == sorted(diffs)
        assert diffs[0] < diffs[-1]

    def test_standings_rank_by_score(self):
        room = cl.Room("r", "CODE4", "u1", "Ada")
        room.join("u1", "Ada")
        room.join("u2", "Grace")
        room.status = "active"
        rnd = room.rounds[0]
        room.quote("u1", *_quote_around(rnd, 0.2))            # tight and right
        room.quote("u2", rnd["truth"] + 2 * rnd["spread"],
                   rnd["truth"] + 3 * rnd["spread"])          # picked off
        rows = room.standings()
        assert rows[0]["username"] == "Ada" and rows[0]["rank"] == 1
        assert rows[1]["username"] == "Grace"


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
