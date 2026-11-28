"""Tests for app.lone_bid — lowest unique number wins.

Pinned: who wins a round (duplicates cancel, lowest single wins), what a
round costs (played card gone, the other locked for exactly one round),
checkpoints (top scorer survives, ties on tokens, points reset, tokens kept),
hand resets, the full 18-round structure ending 1 prize / 1 runner-up /
1 eliminated, the clock, and that nobody sees another player's pick before
the phase closes.
"""

import datetime as dt
import random

import pytest

from app import feedback
from app import lone_bid as lb


class FakeUser:
    def __init__(self, uid, name="p", admin=False):
        self.id = uid
        self.username = name
        self.is_admin = admin


def make_game(humans=7):
    """Seven humans (u0..u6) dealt in and in the reveal phase of round 1."""
    game = {"players": [lb.new_player(f"u{i}", f"p{i}") for i in range(humans)], "status": "lobby",
            "created_by": "u0", "join_code": "AAA111"}
    lb.start(game, "normal", random.Random(0))
    return game


def reveal_all(game, pairs):
    for uid, pair in pairs.items():
        lb.submit_reveal(game, uid, pair)


def play_all(game, cards):
    for uid, card in cards.items():
        lb.submit_play(game, uid, card)


def run_round(game, pairs, cards, rng=None):
    """One full round with everyone's choices given, ending in the result phase."""
    rng = rng or random.Random(1)
    reveal_all(game, pairs)
    assert lb.advance(game, rng=rng) and game["phase"] == "play"
    play_all(game, cards)
    assert lb.advance(game, rng=rng) and game["phase"] == "result"


def next_round(game):
    lb.advance(game, now=lb._now() + dt.timedelta(seconds=60), rng=random.Random(2))


def ids(game):
    return [p["user_id"] for p in lb.active(game)]


class TestWinner:
    def test_duplicates_cancel_and_the_lowest_single_wins(self):
        assert lb.winning_card([1, 1, 2, 3, 3]) == 2
        assert lb.winning_card([1, 1, 2, 2, 5, 7]) == 5
        assert lb.winning_card([4, 4, 6, 6]) is None
        assert lb.winning_card([8]) == 8

    def test_win_chances_are_exact(self):
        # One rival who plays 1 or 2 evenly: my 1 wins when they play 2; my 2
        # never wins (their 1 stands alone, or their 2 cancels mine).
        chance = lb.win_chances([[(1, 0.5), (2, 0.5)]])
        assert chance[1] == pytest.approx(0.5) and chance[2] == pytest.approx(0.0) and chance[3] == 0
        # Two rivals who both play 1: they cancel, so my 2 always wins.
        both = lb.win_chances([[(1, 1.0)], [(1, 1.0)]])
        assert both[2] == pytest.approx(1.0) and both[1] == 0


class TestRound:
    def test_winner_scores_face_value_and_a_token_and_cards_move(self):
        g = make_game()
        pairs = {"u0": [1, 2], "u1": [1, 3], "u2": [2, 4], "u3": [3, 5], "u4": [4, 6], "u5": [5, 7], "u6": [6, 8]}
        cards = {"u0": 1, "u1": 1, "u2": 2, "u3": 3, "u4": 4, "u5": 5, "u6": 6}
        run_round(g, pairs, cards)
        u2 = lb.player(g, "u2")
        assert g["last"]["winner"] == "u2" and g["last"]["value"] == 2 and g["last"]["cancelled"] == [1]
        assert (u2["score"], u2["tokens"]) == (2, 1)
        assert 2 not in u2["hand"] and u2["locked"] == 4              # played gone, the other locked
        assert lb.player(g, "u0")["cancelled"] == 1

    def test_a_locked_card_is_out_for_exactly_one_round(self):
        g = make_game()
        others = lambda game: {uid: lb.available(lb.player(game, uid))[:2] for uid in ids(game) if uid != "u0"}
        pairs = {"u0": [3, 7], **others(g)}
        run_round(g, pairs, {uid: pr[0] for uid, pr in pairs.items()})
        next_round(g)
        assert lb.player(g, "u0")["locked"] == 7 and 7 not in lb.available(lb.player(g, "u0"))
        with pytest.raises(ValueError):
            lb.submit_reveal(g, "u0", [7, 1])
        pairs = {"u0": [1, 2], **others(g)}
        run_round(g, pairs, {uid: pr[0] for uid, pr in pairs.items()})
        next_round(g)
        u0 = lb.player(g, "u0")
        assert u0["locked"] == 2 and 7 in lb.available(u0)            # free again a round later
        assert 3 not in u0["hand"] and 1 not in u0["hand"]

    def test_reveal_and_play_are_checked(self):
        g = make_game()
        with pytest.raises(ValueError):
            lb.submit_reveal(g, "u0", [3, 3])                          # two different cards
        with pytest.raises(ValueError):
            lb.submit_reveal(g, "u0", [3, 9])                          # not a card
        lb.submit_reveal(g, "u0", [3, 4])
        with pytest.raises(ValueError):
            lb.submit_reveal(g, "u0", [5, 6])                          # once a round
        with pytest.raises(ValueError):
            lb.submit_play(g, "u0", 3)                                 # not time yet
        reveal_all(g, {f"u{i}": [1, 2] for i in range(1, 7)})
        lb.advance(g, rng=random.Random(0))
        with pytest.raises(ValueError):
            lb.submit_play(g, "u0", 5)                                 # only one you revealed
        lb.submit_play(g, "u0", 4)

    def test_nobody_sees_another_pick_before_the_phase_closes(self):
        g = make_game()
        lb.submit_reveal(g, "u0", [1, 8])
        seen = lb.state_view(g, "g", FakeUser("u1"))
        theirs = next(p for p in seen["players"] if p["user_id"] == "u0")
        assert "revealed" not in theirs and theirs["ready"] is True
        mine = lb.state_view(g, "g", FakeUser("u0"))
        assert next(p for p in mine["players"] if p["is_me"])["revealed"] == [1, 8]
        reveal_all(g, {f"u{i}": [2, 3] for i in range(1, 7)})
        lb.advance(g, rng=random.Random(0))
        lb.submit_play(g, "u0", 8)
        seen = lb.state_view(g, "g", FakeUser("u1"))
        theirs = next(p for p in seen["players"] if p["user_id"] == "u0")
        assert theirs["revealed"] == [1, 8] and "played" not in theirs   # pairs public, the play isn't

    def test_the_clock_picks_for_anyone_who_didnt(self):
        g = make_game()
        lb.submit_reveal(g, "u0", [1, 2])
        assert not lb.advance(g, rng=random.Random(0))                 # others still choosing
        later = lb._now() + dt.timedelta(seconds=lb.REVEAL_SECONDS + 1)
        lb.advance(g, now=later, rng=random.Random(0))
        assert g["phase"] == "play" and len(g["reveals"]) == 7
        lb.advance(g, now=later + dt.timedelta(seconds=lb.PLAY_SECONDS + 1), rng=random.Random(0))
        assert g["phase"] == "result" and g["history"][0]["round"] == 1


class TestCheckpoints:
    def test_top_scorer_survives_points_reset_tokens_stay(self):
        g = make_game()
        g["round"] = 3
        lb.player(g, "u1")["score"], lb.player(g, "u1")["tokens"] = 9, 2
        lb.player(g, "u4")["score"], lb.player(g, "u4")["tokens"] = 5, 3
        everyone_matches = {uid: [4, 5] for uid in ids(g)}
        run_round(g, everyone_matches, {uid: 4 for uid in ids(g)})       # all cancel: nobody wins
        assert g["last"]["winner"] is None and g["last"]["checkpoint"]["user_id"] == "u1"
        assert lb.player(g, "u1")["status"] == "survived" and lb.player(g, "u1")["out_round"] == 3
        assert len(lb.active(g)) == 6 and all(p["score"] == 0 for p in lb.active(g))
        assert lb.player(g, "u4")["tokens"] == 3                          # tokens kept
        next_round(g)
        assert g["round"] == 4 and "u1" not in g["reveals"] and len(lb.active(g)) == 6

    def test_a_tie_on_points_goes_to_tokens(self):
        g = make_game()
        a, b = lb.player(g, "u1"), lb.player(g, "u2")
        a["score"], a["tokens"] = 6, 1
        b["score"], b["tokens"] = 6, 2
        order, drawn = lb._standings([a, b], random.Random(0))
        assert order[0] is b and not drawn
        b["tokens"] = 1
        _, drawn = lb._standings([a, b], random.Random(0))
        assert drawn                                                     # level on both: a draw

    def test_hands_reset_after_rounds_6_and_12(self):
        g = make_game()
        g["round"] = 6
        for p in lb.active(g):
            p["hand"], p["locked"] = [5, 6, 7], 6
        run_round(g, {uid: [5, 7] for uid in ids(g)}, {uid: 5 for uid in ids(g)})
        assert all(p["hand"] == list(lb.CARDS) and p["locked"] is None for p in lb.active(g))


class TestFullGame:
    def test_eighteen_rounds_four_survivors_then_prize_runner_up_eliminated(self):
        rng = random.Random(5)
        g = {"players": [lb.new_player("h", "Hana")], "status": "lobby"}
        lb.start(g, "good", rng)
        assert len(lb.players(g)) == lb.SEATS and sum(p["is_bot"] for p in lb.players(g)) == 6
        now = lb._now()
        while g["status"] == "playing":
            now += dt.timedelta(seconds=60)
            lb.advance(g, now, rng)
        statuses = [p["status"] for p in lb.players(g)]
        assert statuses.count("survived") == 4 and statuses.count("winner") == 1
        assert statuses.count("runner_up") == 1 and statuses.count("eliminated") == 1
        assert sorted(p["out_round"] for p in lb.players(g) if p["status"] == "survived") == [3, 6, 9, 12]
        assert len(g["history"]) == lb.ROUNDS
        table = lb.final_table(g)
        assert table[0]["status"] == "winner" and table[-1]["status"] == "eliminated"
        view = lb.state_view(g, "g", FakeUser("h"))
        assert view["status"] == "finished" and len(view["results"]) == 7

    def test_every_bot_level_plays_legally(self):
        for level in lb.BOT_LEVELS:
            rng = random.Random(3)
            g = {"players": [], "status": "lobby"}
            lb.start(g, level, rng)
            now = lb._now()
            while g["status"] == "playing":
                now += dt.timedelta(seconds=60)
                lb.advance(g, now, rng)
            assert g["status"] == "finished"


class TestFixes:
    def test_once_every_human_has_survived_the_bots_finish_at_once(self):
        rng = random.Random(9)
        g = {"players": [lb.new_player("h", "Hana")], "status": "lobby"}
        lb.start(g, "normal", rng)
        g["round"] = 3
        lb.player(g, "h")["score"] = 99                                   # Hana tops the round-3 checkpoint
        now = lb._now()
        for _ in range(10):
            if lb.player(g, "h")["status"] != "active":
                break
            if g["phase"] == "reveal" and "h" not in g["reveals"]:
                lb.submit_reveal(g, "h", lb.available(lb.player(g, "h"))[:2])
            if g["phase"] == "play" and "h" not in g["plays"]:
                lb.submit_play(g, "h", g["reveals"]["h"][0])
            lb.advance(g, now, rng)
        assert lb.player(g, "h")["status"] == "survived"
        assert g["status"] == "finished" and len(g["history"]) == lb.ROUNDS - 2   # rounds 3..18, no waiting

    def test_how_a_place_was_settled(self):
        a = {"score": 5, "tokens": 1}
        assert lb.decided_by(a, {"score": 3, "tokens": 4}) == "points"
        assert lb.decided_by(a, {"score": 5, "tokens": 0}) == "tokens"
        assert lb.decided_by(a, {"score": 5, "tokens": 1}) == "draw"

    def test_the_final_says_how_the_prize_and_the_elimination_were_settled(self):
        rng = random.Random(4)
        g = {"players": [], "status": "lobby"}
        lb.start(g, "noob", rng)
        lb.advance(g, lb._now(), rng)                                      # all bots: plays straight through
        assert g["status"] == "finished"
        assert set(g["last"]["final_decided"]) == {"prize", "elimination"}
        assert [h.get("survivor") is not None for h in g["history"]].count(True) == 4


class TestFeedback:
    def test_coaching_for_each_outcome(self):
        for outcome in ("winner", "survived", "runner_up", "eliminated"):
            out = feedback.analyse("lone_bid", {"outcome": outcome, "out_round": 6, "tokens": 2, "rounds_won": 2,
                                                "rounds_played": 9, "points_total": 7, "cancelled": 4,
                                                "best_win": 5})
            assert out["headline"] and out["stats"] and out["notes"]
