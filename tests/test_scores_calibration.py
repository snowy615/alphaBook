"""Calibrated skill: each game rated against who and what it was played
against (other players, house bots at their skill, or par for the settings),
and the leaderboard ranking on that rather than raw averages."""
import asyncio

import pytest

from app import db as db_module
from app import scores


def _merge(dst, src):
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _merge(dst[k], v)
        else:
            dst[k] = v


class _Doc:
    def __init__(self, store, doc_id):
        self._store, self.id = store, doc_id

    @property
    def exists(self):
        return self.id in self._store

    def to_dict(self):
        import copy
        return copy.deepcopy(self._store[self.id]) if self.exists else None


class _Ref:
    def __init__(self, store, doc_id):
        self._store, self.id = store, doc_id

    async def get(self):
        return _Doc(self._store, self.id)

    async def set(self, data, merge=False):
        import copy
        if merge and self.id in self._store:
            _merge(self._store[self.id], copy.deepcopy(data))
        else:
            self._store[self.id] = copy.deepcopy(data)


class _Query:
    def __init__(self, docs):
        self._docs = docs

    def select(self, _fields):
        return self

    async def get(self):
        return self._docs


class _Coll:
    def __init__(self, store):
        self._store = store

    def document(self, doc_id):
        return _Ref(self._store, doc_id)

    async def get(self):
        return [_Doc(self._store, k) for k in list(self._store)]

    def where(self, filter):
        keep = [_Doc(self._store, k) for k, v in self._store.items() if v.get(filter.field_path) == filter.value]
        return _Query(keep)


class _DB:
    def __init__(self):
        self.collections = {}

    def collection(self, name):
        return _Coll(self.collections.setdefault(name, {}))


@pytest.fixture
def db(monkeypatch):
    fake = _DB()
    monkeypatch.setattr(db_module, "db", fake)
    scores.invalidate_cache()

    async def no_xp(_uid):
        return None
    from app import rewards
    monkeypatch.setattr(rewards, "award_xp", no_xp)

    async def no_competition(_uid):
        return None
    from app import competitions
    monkeypatch.setattr(competitions, "active_for_user", no_competition)
    return fake


def run(coro):
    return asyncio.run(coro)


def _skill(db, uid, mode):
    return db.collections[scores.PLAYERS_COLLECTION][uid]["modes"][mode]["skill"]


async def _game(mode, game_id, results, bots=(), context=""):
    for uid, value in results:
        await scores.record_result(mode, uid, uid, value, game_id=game_id)
    rows = [{"user_id": uid, "username": uid, "value": value} for uid, value in results]
    rows += [{"user_id": f"bot{i}", "username": "bot", "value": v, "bot_skill": skill} for i, (skill, v) in enumerate(bots)]
    return await scores.rate_game(mode, game_id, rows, context=context)


class TestElo:
    def test_a_head_to_head(self):
        d = scores.elo_deltas([{"id": "a", "value": 10, "rating": 1000, "games": 0},
                               {"id": "b", "value": 5, "rating": 1000, "games": 0}])
        assert d["a"] == pytest.approx(scores.K_NEW * 0.5) and d["b"] == pytest.approx(-scores.K_NEW * 0.5)

    def test_beating_a_stronger_field_is_worth_more(self):
        weak = scores.elo_deltas([{"id": "me", "value": 10, "rating": 1000, "games": 0},
                                  {"id": "x", "value": 0, "rating": 850, "fixed": True, "games": 0}])
        strong = scores.elo_deltas([{"id": "me", "value": 10, "rating": 1000, "games": 0},
                                    {"id": "x", "value": 0, "rating": 1300, "fixed": True, "games": 0}])
        assert strong["me"] > weak["me"] > 0 and "x" not in strong

    def test_a_big_game_moves_you_no_more_than_a_small_one(self):
        small = scores.elo_deltas([{"id": "me", "value": 9, "rating": 1000, "games": 0},
                                   {"id": "o", "value": 0, "rating": 1000, "games": 0}])
        big = scores.elo_deltas([{"id": "me", "value": 9, "rating": 1000, "games": 0}] +
                                [{"id": f"o{i}", "value": 0, "rating": 1000, "games": 0} for i in range(30)])
        assert big["me"] == pytest.approx(small["me"])

    def test_settled_players_move_less(self):
        assert scores._k(0) > scores._k(scores.K_SETTLE_GAMES)


class TestRateGame:
    def test_the_same_pnl_against_cracked_bots_is_worth_more_than_against_noobs(self, db):
        run(_game("market_sim_py", "g1", [("amy", 500.0)], bots=[("noob", 100.0), ("noob", -50.0)]))
        run(_game("market_sim_py", "g2", [("ben", 500.0)], bots=[("cracked", 100.0), ("cracked", -50.0)]))
        assert _skill(db, "ben", "market_sim_py") > _skill(db, "amy", "market_sim_py") > scores.SKILL_START

    def test_losing_to_noob_bots_costs_more_than_losing_to_cracked_ones(self, db):
        run(_game("market_sim_py", "g1", [("amy", -500.0)], bots=[("noob", 100.0)]))
        run(_game("market_sim_py", "g2", [("ben", -500.0)], bots=[("cracked", 100.0)]))
        assert _skill(db, "amy", "market_sim_py") < _skill(db, "ben", "market_sim_py") < scores.SKILL_START

    def test_the_field_is_what_counts_not_the_raw_number(self, db):
        # A calm game: everyone makes little; a wild one: everyone makes lots.
        run(_game("headline", "calm", [("amy", 40.0), ("x1", 10.0), ("x2", 5.0)]))
        run(_game("headline", "wild", [("ben", 300.0), ("y1", 900.0), ("y2", 1200.0)]))
        assert _skill(db, "amy", "headline") > scores.SKILL_START > _skill(db, "ben", "headline")

    def test_a_game_is_rated_once(self, db):
        first = run(_game("toxic_flow", "g1", [("amy", 120.0), ("ben", 80.0)]))
        again = run(scores.rate_game("toxic_flow", "g1", [{"user_id": "amy", "value": 120.0},
                                                          {"user_id": "ben", "value": 80.0}]))
        assert first and again == {}
        assert db.collections[scores.PLAYERS_COLLECTION]["amy"]["modes"]["toxic_flow"]["skill_games"] == 1

    def test_solo_games_are_measured_against_par_for_the_settings(self, db):
        for i in range(scores.PAR_MIN_RESULTS):          # five people at hard average 50%
            run(_game("mental_math", f"h{i}", [(f"p{i}", 50.0)], context="hard"))
        for i in range(scores.PAR_MIN_RESULTS):          # five at easy average 95%
            run(_game("mental_math", f"e{i}", [(f"q{i}", 95.0)], context="easy"))
        run(_game("mental_math", "amy", [("amy", 80.0)], context="hard"))   # well above par at hard
        run(_game("mental_math", "ben", [("ben", 80.0)], context="easy"))   # below par at easy
        assert _skill(db, "amy", "mental_math") > scores.SKILL_START > _skill(db, "ben", "mental_math")

    def test_no_par_until_enough_results(self, db):
        assert run(_game("crash_ledger", "g1", [("amy", 300.0)])) == {}

    def test_live_market_and_unknown_modes_are_left_alone(self, db):
        assert run(scores.rate_game("market_sim", "x", [{"user_id": "a", "value": 1}])) == {}
        assert run(scores.rate_game("nope", "x", [{"user_id": "a", "value": 1}])) == {}


class TestBoard:
    def test_a_mode_ranks_on_skill_not_the_raw_average(self, db):
        # Amy: modest P&L but beat cracked bots; Ben: big P&L against noobs, but lost to them.
        run(_game("market_sim_py", "g1", [("amy", 200.0)], bots=[("cracked", 100.0)]))
        run(_game("market_sim_py", "g2", [("ben", 900.0)], bots=[("noob", 1000.0)]))
        board = run(scores.leaderboard(force=True))["mode_boards"]["market_sim_py"]
        assert [r["username"] for r in board] == ["amy", "ben"]
        assert board[0]["skill"] > board[1]["skill"] and board[1]["value"] > board[0]["value"]

    def test_strategy_is_summarised_across_games(self, db):
        for i, (style, edge, direction) in enumerate([("market_maker", 80.0, -10.0), ("market_maker", 60.0, 5.0),
                                                       ("directional", 10.0, 200.0)]):
            run(scores.record_result("market_sim_py", "amy", "amy", edge + direction, game_id=f"g{i}",
                                     detail={"strategy": {"style": {"key": style}, "edge": edge,
                                                          "direction": direction, "direction_hit": 0.5}}))
        row = run(scores.leaderboard(force=True))["mode_boards"]["market_sim_py"][0]
        assert row["strategy"] == {"style": "market_maker", "style_games": 2, "games": 3,
                                   "edge_per_game": 50.0, "direction_per_game": 65.0, "direction_hit": 0.5}

    def test_admins_are_left_off_without_scanning_every_account(self, db):
        db.collections["users"] = {"boss": {"is_admin": True}, "amy": {"is_admin": False}}
        run(_game("toxic_flow", "g1", [("amy", 120.0), ("boss", 80.0)]))
        players = run(scores.leaderboard(force=True))["players"]
        assert [p["username"] for p in players] == ["amy"]


class TestReplay:
    def test_history_replays_like_live_rating(self, db):
        events = [
            {"mode": "toxic_flow", "game_id": "g1", "user_id": "amy", "value": 120, "created_at": "2026-10-01"},
            {"mode": "toxic_flow", "game_id": "g1", "user_id": "ben", "value": 80, "created_at": "2026-10-01"},
            {"mode": "market_sim_py", "game_id": "r1", "user_id": "amy", "value": 300, "created_at": "2026-10-02"},
            {"mode": "market_sim", "game_id": "", "user_id": "amy", "value": 5, "created_at": "2026-10-02"},  # live: skipped
        ]
        bots = {"r1": [{"username": "Market Maker Bot", "pnl": 100}, {"username": "Momentum · cracked", "pnl": 50}]}
        out = scores.replay_skills(events, bots)
        live = {}
        run(_game("toxic_flow", "g1", [("amy", 120.0), ("ben", 80.0)]))
        run(_game("market_sim_py", "r1", [("amy", 300.0)], bots=[("normal", 100.0), ("cracked", 50.0)]))
        for (uid, mode), (skill, games) in out["skills"].items():
            live[(uid, mode)] = _skill(db, uid, mode)
            assert skill == pytest.approx(live[(uid, mode)], abs=0.01)
        assert ("amy", "market_sim") not in out["skills"]
        assert sorted(out["games"]) == ["market_sim_py__r1", "toxic_flow__g1"]

    def test_bot_skill_is_read_from_its_name_on_old_runs(self):
        assert scores._bot_skill_from_name("Momentum · cracked") == "cracked"
        assert scores._bot_skill_from_name("Market Maker Bot") == "normal"


class TestPublicStrategy:
    def test_others_see_the_style_and_words_not_the_figures(self):
        st = {"style": "market_maker", "style_games": 2, "games": 3, "edge_per_game": 50.0,
              "direction_per_game": -20.0, "direction_hit": 0.7}
        pub = scores.public_strategy_summary(st)
        assert pub == {"style": "market_maker", "style_games": 2, "games": 3, "made_on": "pricing", "call": "mostly right"}
        assert scores.public_strategy_summary(None) is None

    def test_the_leaderboard_api_keeps_figures_to_your_own_row(self, monkeypatch):
        from app import main
        st = {"style": "directional", "style_games": 1, "games": 1, "edge_per_game": 5.0,
              "direction_per_game": 90.0, "direction_hit": 0.3}

        def row(uid):
            return {"user_id": uid, "username": uid, "rank": 1, "overall": 50, "modes_played": 1, "total_games": 1,
                    "ratings": {"market_sim_py": {"rating": 50, "strategy": dict(st)}}}

        async def board(force=False):
            return {"players": [row("me"), row("them")],
                    "mode_boards": {"market_sim_py": [{**row("me"), "strategy": dict(st)}, {**row("them"), "strategy": dict(st)}]},
                    "modes": [], "min_games_full": 3}

        async def sync():
            return None

        async def viewer(_request):
            from app.models import User
            return User(id="me", username="me")
        monkeypatch.setattr(scores, "leaderboard", board)
        monkeypatch.setattr(scores, "sync_market_sim", sync)
        monkeypatch.setattr(main, "_optional_user", viewer)
        out = run(main.api_leaderboard(None))
        mine, theirs = out["mode_boards"]["market_sim_py"]
        assert mine["strategy"]["edge_per_game"] == 5.0
        assert "edge_per_game" not in theirs["strategy"] and theirs["strategy"]["made_on"] == "direction"
        assert "direction_hit" not in out["overall"][1]["ratings"]["market_sim_py"]["strategy"]
