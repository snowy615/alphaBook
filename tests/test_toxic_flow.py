"""Tests for app.toxic_flow — the bluffing-with-margin game.

Money is the whole game here, so the payouts are pinned: margin pricing, the
four resolution branches, the fine, bankruptcy, and the liquidity bonuses
including ties. Also pinned: a player's hand and a face-down claim never reach
another player's state payload, because the entire bluff depends on that.
"""

import datetime as dt

import pytest

from app import toxic_flow as tf


class FakeUser:
    def __init__(self, uid, name="p", admin=False):
        self.id = uid
        self.username = name
        self.is_admin = admin


def make_game(n=3, chips=100):
    players = [{"user_id": f"u{i}", "username": f"p{i}", "chips": chips,
                "hand": [], "bankrupt": False} for i in range(n)]
    return {
        "join_code": "AAA111", "status": "playing", "players": players,
        "pile": [], "declared_rank": None, "turn_id": "u0",
        "claim": None, "pending_choice": None, "log": [], "reveal": None,
        "created_by": "u0", "scored": False,
    }


def card(rank, suit="hearts"):
    return {"rank": rank, "suit": suit}


def open_claim(game, uid, rank, cards, seconds_ago=0.0):
    """Put a claim on the table as if `uid` had just played it."""
    margin = len(cards) * tf.rank_value(rank)
    p = tf._player(game, uid)
    p["chips"] -= margin
    opened = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=seconds_ago)
    game["claim"] = {
        "player_id": uid, "rank": rank, "count": len(cards), "margin": margin,
        "cards": cards, "opened_at": opened.isoformat(), "audits": {},
    }
    game["declared_rank"] = rank
    return margin


class TestPricing:
    @pytest.mark.parametrize("rank,value", [
        (1, 11), (2, 2), (9, 9), (10, 10), (11, 10), (12, 10), (13, 10),
    ])
    def test_rank_values(self, rank, value):
        assert tf.rank_value(rank) == value

    def test_margin_examples_from_the_rulebook(self):
        assert 2 * tf.rank_value(13) == 20      # two kings
        assert 4 * tf.rank_value(3) == 12       # four 3s

    def test_the_sequence_wraps(self):
        assert tf.next_rank(13) == 1            # King → Ace
        assert tf.next_rank(1) == 2
        assert tf.next_rank(7) == 8


class TestUnaudited:
    def test_truth_returns_the_margin_and_leaves_the_cards(self):
        g = make_game()
        margin = open_claim(g, "u0", 5, [card(5), card(5)], seconds_ago=21)
        assert tf._player(g, "u0")["chips"] == 100 - margin

        assert tf.resolve_due(g) is True
        assert tf._player(g, "u0")["chips"] == 100      # margin back, no profit
        assert len(g["pile"]) == 2
        assert g["claim"] is None

    def test_a_lie_that_survives_asks_for_a_choice(self):
        g = make_game()
        open_claim(g, "u0", 5, [card(9), card(9)], seconds_ago=21)
        tf.resolve_due(g)
        assert g["claim"] is None
        assert g["pending_choice"]["player_id"] == "u0"

    def test_conceal_pays_the_skim(self):
        g = make_game()
        margin = open_claim(g, "u0", 5, [card(9)], seconds_ago=21)
        tf.resolve_due(g)
        g["pending_choice"]["margin"] = margin
        # conceal: margin back plus the bank's bonus
        tf._credit(g, "u0", margin + tf.CONCEAL_BONUS)
        assert tf._player(g, "u0")["chips"] == 100 + tf.CONCEAL_BONUS

    def test_window_has_to_expire_first(self):
        g = make_game()
        open_claim(g, "u0", 5, [card(5)], seconds_ago=1)
        assert tf.resolve_due(g) is False        # still inside the 20 seconds
        assert g["claim"] is not None


class TestAudited:
    def test_catching_a_lie_pays_margin_and_the_fine(self):
        g = make_game()
        g["pile"] = [card(2), card(3), card(4)]          # 3 already down
        margin = open_claim(g, "u0", 5, [card(9), card(9)])   # lie, margin 10

        g["claim"]["audits"] = {"u1": margin}
        tf._player(g, "u1")["chips"] -= margin           # stake escrowed
        tf._resolve_audited(g)

        # 5 cards in the middle at resolution -> $5 fine on top of the margin.
        assert tf._player(g, "u1")["chips"] == 100 + margin + 5
        assert tf._player(g, "u0")["chips"] == 100 - margin - 5
        # and the liar eats the pile
        assert len(tf._player(g, "u0")["hand"]) == 5
        assert g["pile"] == []

    def test_the_squeeze_pays_the_truthful_player(self):
        g = make_game()
        margin = open_claim(g, "u0", 5, [card(5), card(5)])    # true
        g["claim"]["audits"] = {"u1": margin}
        tf._player(g, "u1")["chips"] -= margin
        tf._resolve_audited(g)

        # margin back plus the auditor's stake
        assert tf._player(g, "u0")["chips"] == 100 + margin
        assert tf._player(g, "u1")["chips"] == 100 - margin
        assert len(tf._player(g, "u1")["hand"]) == 2      # auditor takes the pile

    def test_a_pooled_audit_splits_pro_rata(self):
        g = make_game(n=3)
        open_claim(g, "u0", 10, [card(9), card(9)])            # lie, margin 20
        g["claim"]["audits"] = {"u1": 15, "u2": 5}
        tf._player(g, "u1")["chips"] -= 15
        tf._player(g, "u2")["chips"] -= 5
        tf._resolve_audited(g)

        # spoils = margin 20 + fine 2 = 22, split 75/25
        assert tf._player(g, "u1")["chips"] == 100 + round(22 * 0.75)
        assert tf._player(g, "u2")["chips"] == 100 + round(22 * 0.25)
        # largest contributor is the auditor of record
        assert g["reveal"]["auditor"] == "u1"

    def test_reaching_the_margin_is_what_triggers_the_audit(self):
        g = make_game()
        margin = open_claim(g, "u0", 5, [card(5), card(5)])
        g["claim"]["audits"] = {"u1": margin - 1}
        assert tf.resolve_due(g) is False        # short of the margin, no audit
        g["claim"]["audits"]["u1"] = margin
        assert tf.resolve_due(g) is True


class TestInsolvency:
    def test_a_stack_at_zero_is_out_and_its_cards_return(self):
        g = make_game()
        tf._player(g, "u0")["hand"] = [card(4), card(6)]
        tf._pay(g, "u0", 100)
        p = tf._player(g, "u0")
        assert p["bankrupt"] and p["chips"] == 0
        assert p["hand"] == []
        assert len(g["pile"]) == 2               # shuffled back into the pile

    def test_a_fine_cannot_take_more_than_the_stack(self):
        g = make_game(chips=3)
        taken = tf._pay(g, "u1", 50)
        assert taken == 3
        assert tf._player(g, "u1")["chips"] == 0

    def test_turn_order_skips_the_bankrupt(self):
        g = make_game(n=3)
        tf._bankrupt(g, tf._player(g, "u1"))
        g["turn_id"] = "u0"
        tf._advance_turn(g, "u0")
        assert g["turn_id"] == "u2"


class TestEndgame:
    def test_bonuses_follow_fewest_cards(self):
        g = make_game(n=3)
        tf._player(g, "u0")["hand"] = []                    # 1st
        tf._player(g, "u1")["hand"] = [card(2)]             # 2nd
        tf._player(g, "u2")["hand"] = [card(3), card(4)]    # 3rd
        tf._pay_liquidity_bonuses(g)
        assert tf._player(g, "u0")["chips"] == 180
        assert tf._player(g, "u1")["chips"] == 140
        assert tf._player(g, "u2")["chips"] == 120

    def test_a_tie_splits_the_tier(self):
        g = make_game(n=3)
        tf._player(g, "u0")["hand"] = []
        tf._player(g, "u1")["hand"] = []
        tf._player(g, "u2")["hand"] = [card(3)]
        tf._pay_liquidity_bonuses(g)
        assert tf._player(g, "u0")["chips"] == 140          # 80 split two ways
        assert tf._player(g, "u1")["chips"] == 140
        assert tf._player(g, "u2")["chips"] == 140          # still takes 2nd, $40

    def test_an_empty_hand_ends_it_only_once_nothing_is_pending(self):
        g = make_game()
        tf._player(g, "u0")["hand"] = []
        tf._player(g, "u1")["hand"] = [card(2)]
        tf._player(g, "u2")["hand"] = [card(3)]
        g["claim"] = {"player_id": "u0", "rank": 5, "count": 1, "margin": 5,
                      "cards": [card(5)], "opened_at":
                      dt.datetime.now(dt.timezone.utc).isoformat(), "audits": {}}
        tf._check_endgame(g)
        assert g["status"] == "playing"       # a live claim keeps it open
        g["claim"] = None
        tf._check_endgame(g)
        assert g["status"] == "finished"

    def test_highest_capital_wins_not_the_empty_hand(self):
        g = make_game(n=3)
        tf._player(g, "u0")["hand"] = []
        tf._player(g, "u0")["chips"] = 40      # went out but bled money
        tf._player(g, "u1")["hand"] = [card(2)]
        tf._player(g, "u1")["chips"] = 150
        tf._player(g, "u2")["hand"] = [card(3), card(4)]
        tf._pay_liquidity_bonuses(g)
        view = tf._state_view({**g, "status": "finished"}, "g1", FakeUser("u0"))
        assert view["results"][0]["username"] == "p1"     # 190 beats 120


class TestSecrecy:
    def test_your_hand_never_reaches_another_player(self):
        g = make_game()
        tf._player(g, "u0")["hand"] = [card(7), card(8)]
        view = tf._state_view(g, "g1", FakeUser("u1"))
        assert "hand" in view                      # u1 sees their own (empty)
        blob = repr(view)
        assert "'rank': 7" not in blob and "'rank': 8" not in blob

    def test_a_face_down_claim_stays_face_down(self):
        g = make_game()
        open_claim(g, "u0", 5, [card(9), card(9)])          # a lie
        view = tf._state_view(g, "g1", FakeUser("u1"))
        assert view["claim"]["count"] == 2
        assert view["claim"]["rank"] == "5"                 # what was claimed
        assert "cards" not in view["claim"]                 # not what was played

    def test_the_claimant_cannot_see_the_audit_breakdown(self):
        g = make_game()
        open_claim(g, "u0", 5, [card(5)])
        g["claim"]["audits"] = {"u1": 5}
        view = tf._state_view(g, "g1", FakeUser("u0"))
        assert view["claim"]["pool"] == 5                   # size is public
        assert "audits" not in view["claim"]                # who staked is not
