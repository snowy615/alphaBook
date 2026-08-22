"""Tests for app.dark_pool — the hidden-info trading game with calibration checkpoints.

Pinned here: hand-tier ranking (the renamed poker evaluator), the equity
calculator's boundary cases (certain win/loss/split at showdown), the betting
engine's turn/settlement bookkeeping and desk-limit cap, the checkpoint
scoring formula, and the follow-up-prompt generator that closes out a
session. These are all pure functions over plain dicts, same approach as
test_toxic_flow.py, so no Firestore is involved.
"""

from app import dark_pool as dp


def card(strength, sector="rates"):
    return {"sector": sector, "strength": strength}


def make_game(n=3, chips=500):
    desks = [{"user_id": f"u{i}", "username": f"p{i}", "chips": chips, "bankrupt": False}
             for i in range(n)]
    return {
        "join_code": "AAA111", "status": "playing", "desks": desks,
        "print_no": 0, "hand": None, "log": [], "session_log": [], "print_history": [],
        "created_by": "u0", "scored": False,
    }


class TestHandTiers:
    def test_high_card(self):
        tiles = [card(2), card(5, "fx"), card(9, "commods"), card(11, "equities"), card(4, "fx")]
        assert dp.evaluate_5(tiles)[0] == 0

    def test_pair(self):
        tiles = [card(5), card(5, "fx"), card(2), card(9, "commods"), card(11, "equities")]
        assert dp.evaluate_5(tiles)[0] == 1

    def test_two_pair(self):
        tiles = [card(5), card(5, "fx"), card(9), card(9, "commods"), card(2)]
        assert dp.evaluate_5(tiles)[0] == 2

    def test_trips(self):
        tiles = [card(5), card(5, "fx"), card(5, "commods"), card(9), card(2)]
        assert dp.evaluate_5(tiles)[0] == 3

    def test_trend_run(self):
        tiles = [card(4), card(5, "fx"), card(6, "commods"), card(7), card(8, "equities")]
        assert dp.evaluate_5(tiles)[0] == 4

    def test_the_wheel_counts_as_a_run(self):
        tiles = [card(1), card(2, "fx"), card(3, "commods"), card(4), card(5, "equities")]
        score = dp.evaluate_5(tiles)
        assert score == (4, (5,))

    def test_sector_lock(self):
        tiles = [card(2), card(5), card(9), card(11), card(13)]   # all "rates"
        assert dp.evaluate_5(tiles)[0] == 5

    def test_full_house_is_cross_consensus(self):
        tiles = [card(5), card(5, "fx"), card(5, "commods"), card(9), card(9, "fx")]
        assert dp.evaluate_5(tiles)[0] == 6

    def test_quad_signal(self):
        tiles = [card(5), card(5, "fx"), card(5, "commods"), card(5, "equities"), card(9)]
        assert dp.evaluate_5(tiles)[0] == 7

    def test_sector_run_beats_plain_run(self):
        tiles = [card(4), card(5), card(6), card(7), card(8)]   # same sector, consecutive
        assert dp.evaluate_5(tiles)[0] == 8

    def test_peak_convergence_is_the_top_tier(self):
        tiles = [card(1), card(10), card(11), card(12), card(13)]   # same sector, ace-high run
        assert dp.evaluate_5(tiles)[0] == 9

    def test_tier_ordering(self):
        pair = dp.evaluate_5([card(5), card(5, "fx"), card(2), card(9, "commods"), card(11, "equities")])
        two_pair = dp.evaluate_5([card(5), card(5, "fx"), card(9), card(9, "commods"), card(2)])
        assert two_pair > pair


class TestBestRead:
    def test_picks_the_best_five_of_seven(self):
        tiles = [card(5), card(5, "fx"), card(2), card(9, "commods"), card(11, "equities"),
                 card(13, "fx"), card(3)]
        result = dp.best_read(tiles)
        assert result["tier"] == 1
        assert result["tier_name"] == "Consensus Pair"

    def test_hole_tiles_can_upgrade_the_board(self):
        board = [card(9), card(10, "fx"), card(2, "commods"), card(3, "equities"), card(4, "fx")]
        hole = [card(9, "fx"), card(9, "commods")]
        result = dp.best_read(hole + board)
        assert result["tier"] == 3    # trips nines, using the board's lone nine


class TestEquity:
    def test_certain_win_at_showdown(self):
        my = [card(5), card(5, "fx")]
        opp = [card(2), card(3, "fx")]
        board = [card(9), card(10, "fx"), card(11, "commods"), card(12, "equities"), card(1, "fx")]
        assert dp.compute_equity(my, [opp], board, unseen=[], remaining=0) == 1.0

    def test_certain_loss_at_showdown(self):
        my = [card(2), card(3, "fx")]
        opp = [card(5), card(5, "fx")]
        board = [card(9), card(10, "fx"), card(11, "commods"), card(12, "equities"), card(1, "fx")]
        assert dp.compute_equity(my, [opp], board, unseen=[], remaining=0) == 0.0

    def test_a_shared_board_read_splits_the_pot(self):
        # Both hole pairs are lower than every board value, so neither improves
        # on it — both players just play the board, and it's a dead-even split.
        my = [card(2), card(3, "fx")]
        opp = [card(2, "commods"), card(4, "equities")]
        board = [card(9), card(13, "fx"), card(5, "commods"), card(7, "equities"), card(1, "fx")]
        assert dp.compute_equity(my, [opp], board, unseen=[], remaining=0) == 0.5

    def test_exact_enumeration_stays_within_bounds(self):
        my = [card(1), card(1, "fx")]     # a strong pair
        opp = [card(13), card(12, "fx")]  # high-card only
        board = [card(2), card(3, "fx"), card(4, "commods"), card(6, "equities")]   # 1 tile to come
        seen = {(t["sector"], t["strength"]) for t in my + opp + board}
        unseen = [t for t in dp.FULL_DECK if (t["sector"], t["strength"]) not in seen]
        eq = dp.compute_equity(my, [opp], board, unseen, remaining=1)
        assert 0.0 <= eq <= 1.0
        assert eq > 0.6   # a made pair of aces should be well ahead of king-high here

    def test_monte_carlo_path_stays_within_bounds(self):
        my = [card(1), card(1, "fx")]
        opp = [card(2), card(3, "fx")]
        board = []   # pre-open: 5 tiles still to come, too big to enumerate exactly
        seen = {(t["sector"], t["strength"]) for t in my + opp}
        unseen = [t for t in dp.FULL_DECK if (t["sector"], t["strength"]) not in seen]
        eq = dp.compute_equity(my, [opp], board, unseen, remaining=5)
        assert 0.0 <= eq <= 1.0


class TestBettingMechanics:
    def test_desk_limit_is_the_shortest_active_stack(self):
        g = make_game(n=3, chips=500)
        dp._desk(g, "u1")["chips"] = 40
        hand = {"order": ["u0", "u1", "u2"], "folded": [], "all_in": [],
                "committed": {"u0": 0, "u1": 0, "u2": 0}}
        assert dp._desk_limit(g, hand) == 40

    def test_a_folded_desk_does_not_constrain_the_limit(self):
        g = make_game(n=3, chips=500)
        dp._desk(g, "u1")["chips"] = 5
        hand = {"order": ["u0", "u1", "u2"], "folded": ["u1"], "all_in": [],
                "committed": {"u0": 0, "u1": 0, "u2": 0}}
        assert dp._desk_limit(g, hand) == 500

    def test_stage_settled_requires_matched_bets_and_action(self):
        hand = {"order": ["u0", "u1"], "folded": [], "all_in": [],
                "committed": {"u0": 20, "u1": 0}, "current_bet": 20, "acted": ["u0"]}
        assert dp._stage_settled(hand) is False
        hand["committed"]["u1"] = 20
        hand["acted"].append("u1")
        assert dp._stage_settled(hand) is True

    def test_single_active_desk_is_always_settled(self):
        hand = {"order": ["u0", "u1"], "folded": ["u1"], "all_in": [],
                "committed": {"u0": 0, "u1": 0}, "current_bet": 0, "acted": []}
        assert dp._stage_settled(hand) is True


class TestPrintFlow:
    def test_start_print_deals_and_posts_the_ante(self):
        g = make_game(n=3, chips=500)
        dp._start_print(g)
        hand = g["hand"]
        assert hand["pot"] == 3 * dp.ANTE
        assert all(len(hand["hole"][uid]) == 2 for uid in hand["order"])
        assert all(d["chips"] == 500 - dp.ANTE for d in dp._desks(g))
        assert hand["stage"] == "preopen"
        assert hand["turn_id"] == hand["order"][0]
        assert hand["pending_checkpoint"] is None    # nobody owes anything yet

    def test_seat_one_is_not_skipped_at_print_start(self):
        # Regression: turn assignment used to always advance past order[0].
        for _ in range(20):
            g = make_game(n=4, chips=500)
            dp._start_print(g)
            assert g["hand"]["turn_id"] == g["hand"]["order"][0]

    def test_everyone_folding_to_one_ends_the_print(self):
        g = make_game(n=3, chips=500)
        dp._start_print(g)
        hand = g["hand"]
        first, second, third = hand["order"]
        pot_before = hand["pot"]
        hand["folded"] = [first, second]
        dp._advance_to_actionable(g)
        assert hand["stage"] == "showdown"
        assert hand["winners"] == [third]
        assert dp._desk(g, third)["chips"] == 500 - dp.ANTE + pot_before
        assert g["print_history"][-1]["awarded"] == {third: pot_before}

    def test_checking_around_progresses_every_street_to_showdown(self):
        g = make_game(n=2, chips=500)
        dp._start_print(g)
        hand = g["hand"]
        a, b = hand["order"]

        hand["acted"] = [a, b]
        dp._advance_to_actionable(g)
        assert hand["stage"] == "open" and len(hand["public"]) == 3

        hand["acted"] = [a, b]
        dp._advance_to_actionable(g)
        assert hand["stage"] == "revision" and len(hand["public"]) == 4

        hand["acted"] = [a, b]
        dp._advance_to_actionable(g)
        assert hand["stage"] == "close" and len(hand["public"]) == 5

        hand["acted"] = [a, b]
        dp._advance_to_actionable(g)
        assert hand["stage"] == "showdown"
        assert hand["winners"]


class TestCheckpoint:
    def test_no_checkpoint_when_nobody_faces_a_bet(self):
        g = make_game(n=2, chips=500)
        dp._start_print(g)
        assert g["hand"]["pending_checkpoint"] is None

    def test_a_checkpoint_opens_for_whoever_faces_the_raise(self):
        g = make_game(n=2, chips=500)
        dp._start_print(g)
        hand = g["hand"]
        a, b = hand["order"]

        dp._desk(g, a)["chips"] -= 50
        hand["committed"][a] = 50
        hand["current_bet"] = 50
        hand["acted"] = [a]
        hand["turn_id"] = b

        dp._maybe_open_checkpoint(g)
        cp = hand["pending_checkpoint"]
        assert cp is not None
        assert cp["user_id"] == b
        assert cp["to_call"] == 50
        assert 0.0 <= cp["true_prob"] <= 1.0
        assert cp["submitted"] is False

    def test_scoring_rewards_a_narrow_correct_interval(self):
        cp = {"true_prob": 0.5, "est_prob": 50, "ci_low": 45, "ci_high": 55,
              "est_ev": 10, "true_ev_call": 10}
        result = dp._score_checkpoint(cp)
        assert result["ci_hit"] is True
        assert result["prob_error"] == 0.0
        assert result["cal_points"] > 90

    def test_scoring_penalises_a_confident_miss(self):
        cp = {"true_prob": 0.2, "est_prob": 80, "ci_low": 75, "ci_high": 85,
              "est_ev": 10, "true_ev_call": -30}
        result = dp._score_checkpoint(cp)
        assert result["ci_hit"] is False
        assert result["prob_error"] == 60.0
        assert result["cal_points"] == 0.0
        assert result["implied_action"] == "call"

    def test_implied_action_follows_the_players_own_ev_number(self):
        cp = {"true_prob": 0.3, "est_prob": 30, "ci_low": 20, "ci_high": 40,
              "est_ev": -15, "true_ev_call": -10}
        assert dp._score_checkpoint(cp)["implied_action"] == "fold"


class TestFollowups:
    def test_surfaces_the_worst_narrow_miss(self):
        entries = [
            {"print_no": 2, "stage": "open", "ci_hit": False, "ci_width": 10, "prob_error": 40,
             "ci_low": 60, "ci_high": 70, "true_prob": 0.2, "rationality_break": False,
             "action": "call", "est_ev": 5},
            {"print_no": 4, "stage": "revision", "ci_hit": True, "ci_width": 20, "prob_error": 2,
             "ci_low": 40, "ci_high": 60, "true_prob": 0.45, "rationality_break": False,
             "action": "call", "est_ev": 5},
        ]
        prompts = dp._followups(entries)
        assert len(prompts) == 1
        assert "print #2" in prompts[0]

    def test_flags_a_rationality_break(self):
        entries = [{"print_no": 5, "stage": "close", "ci_hit": True, "ci_width": 10, "prob_error": 1,
                    "ci_low": 10, "ci_high": 20, "true_prob": 0.15, "rationality_break": True,
                    "action": "call", "est_ev": -25}]
        prompts = dp._followups(entries)
        assert any("your own EV" in p for p in prompts)

    def test_no_misses_means_no_prompts(self):
        entries = [{"print_no": 1, "stage": "open", "ci_hit": True, "ci_width": 20, "prob_error": 3,
                    "ci_low": 40, "ci_high": 60, "true_prob": 0.5, "rationality_break": False,
                    "action": "call", "est_ev": 5}]
        assert dp._followups(entries) == []
