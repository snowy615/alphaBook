"""Approx: the Estimathon. Scoring as in the rules sheet, hosting, joining,
submitting, and the game ending on time or when every team is done."""
import asyncio
import datetime as dt
import itertools

import pytest
from fastapi import HTTPException

from app import approx as ax
from app import db as db_module
from app.approx_questions import BANK, BY_ID
from app.models import User

HOST = User(id="h1", username="host")
AMY = User(id="a1", username="amy")
BEN = User(id="b1", username="ben")


# ── A small in-memory Firestore ─────────────────────────────────────────────

class _Doc:
    def __init__(self, store, doc_id):
        self._store, self.id = store, doc_id

    @property
    def exists(self):
        return self.id in self._store

    def to_dict(self):
        return dict(self._store[self.id]) if self.exists else None


class _Ref:
    def __init__(self, store, doc_id):
        self._store, self.id = store, doc_id

    async def get(self):
        return _Doc(self._store, self.id)

    async def set(self, data):
        self._store[self.id] = dict(data)

    async def update(self, patch):
        self._store[self.id].update(patch)


class _Coll:
    _ids = itertools.count(1)

    def __init__(self, store):
        self._store = store

    def document(self, doc_id=None):
        return _Ref(self._store, doc_id or f"g{next(self._ids)}")

    def where(self, field, _op, value):
        store = self._store

        class _Q:
            async def get(self):
                return [_Doc(store, k) for k, v in store.items() if v.get(field) == value]
        return _Q()


class _DB:
    def __init__(self):
        self.collections = {}

    def collection(self, name):
        return _Coll(self.collections.setdefault(name, {}))


@pytest.fixture
def db(monkeypatch):
    fake = _DB()
    monkeypatch.setattr(db_module, "db", fake)
    ax._locks.clear()
    return fake


def run(coro):
    return asyncio.run(coro)


def _game(db, game_id):
    return db.collections[ax.COLLECTION][game_id]


def _create(questions=None, minutes=30, attempts=None):
    qs = questions or [ax.QuestionIn(text="Keys on a piano", answer="88"),
                       ax.QuestionIn(text="Bones in the body", answer="206")]
    return run(ax.create_game(ax.CreateGame(questions=qs, minutes=minutes, attempts=attempts), HOST))


def _submit(game_id, user, q, low, high):
    return run(ax.submit_interval(game_id, ax.Submission(question=q, low=str(low), high=str(high)), user))


# ── Scoring ─────────────────────────────────────────────────────────────────

class TestScoring:
    def test_the_formula_from_the_rules(self):
        # 13 questions, none good: 10 * 2^13
        assert ax.score_of(13, []) == 81920
        # 13 good, each with ratio 2: (10 + 26) * 2^0
        assert ax.score_of(13, [2] * 13) == 36
        # 10 good of 13: (10 + sum) * 2^3
        assert ax.score_of(13, [1, 2, 3, 1, 1, 1, 1, 1, 1, 1]) == (10 + 13) * 8

    def test_ratio_points_floor_high_over_low(self):
        assert ax.ratio_points(3, 9) == 3          # exact, despite float error
        assert ax.ratio_points(100, 250) == 2
        assert ax.ratio_points(5, 5) == 1

    @pytest.mark.parametrize("raw,value", [
        ("1234", 1234), ("1,234", 1234), ("1.5e6", 1.5e6), ("3x10^6", 3e6), ("3×10^6", 3e6),
        ("3*10^6", 3e6), ("3·10^6", 3e6), ("10^7", 1e7), (" 2 x 10 ^ 3 ", 2000), (0.5, 0.5)])
    def test_numbers_as_people_write_them(self, raw, value):
        assert ax.parse_quantity(raw) == pytest.approx(value)

    @pytest.mark.parametrize("raw", ["", "0", "-5", "3^7", "abc", "1e400", "nan"])
    def test_non_positive_or_unparseable_is_refused(self, raw):
        with pytest.raises(ValueError):
            ax.parse_quantity(raw)

    def test_huge_scores_read_as_words_like_the_spreadsheet(self):
        assert ax.score_label(81920) == "81,920"
        assert ax.score_label(2e7) == "a lot"
        assert ax.score_label(5e9) == "a TON"
        assert ax.score_label(1e18) == "∞ squared"


class TestBank:
    def test_fifty_questions_each_with_a_positive_answer_and_unique_id(self):
        assert len(BANK) == 50
        assert len(BY_ID) == 50
        assert all(q["answer"] > 0 and q["text"] for q in BANK)

    def test_the_bank_endpoint_never_shows_answers(self):
        out = run(ax.question_bank(AMY))
        assert len(out["questions"]) == 50
        assert all(set(q) == {"id", "text", "category"} for q in out["questions"])
        assert out["categories"] == ["Built world", "People & places", "Body & books", "Physics", "Maths", "Probability"]
        assert all(q["category"] in out["categories"] for q in out["questions"])

    def test_every_bank_question_explains_its_answer(self):
        assert all(q.get("note") and q.get("category") for q in BANK)

    def test_the_maths_answers_are_right(self):
        import math
        assert BY_ID["lattice-paths"]["answer"] == math.comb(40, 20)
        assert BY_ID["mississippi"]["answer"] == math.factorial(11) // (24 * 24 * 2)
        assert BY_ID["chess-rectangles"]["answer"] == math.comb(9, 2) ** 2
        assert BY_ID["digits-2-1000"]["answer"] == len(str(2 ** 1000))
        assert BY_ID["digit-sum-2-1000"]["answer"] == sum(map(int, str(2 ** 1000)))
        ways = [1] + [0] * 100
        for c in (1, 2, 5, 10, 20, 50, 100):
            for v in range(c, 101):
                ways[v] += ways[v - c]
        assert BY_ID["change-pound"]["answer"] == ways[100]
        assert 2 ** (BY_ID["paper-moon"]["answer"] - 1) * 0.1e-6 < 384_400 <= 2 ** BY_ID["paper-moon"]["answer"] * 0.1e-6
        assert BY_ID["dice-all-six"]["answer"] == pytest.approx(6 * sum(1 / k for k in range(1, 7)), abs=0.01)

    def test_a_revealed_bank_question_carries_its_note(self, db):
        out = _create([ax.QuestionIn(bank_id="paper-moon")])
        run(ax.join_game(ax.JoinGame(join_code=out["join_code"]), AMY))
        st = run(ax.game_state(out["game_id"], HOST))
        assert st["questions"][0]["note"].startswith("Each fold doubles it")


# ── Hosting and joining ─────────────────────────────────────────────────────

class TestHostAndJoin:
    def test_create_with_own_and_bank_questions(self, db):
        out = _create([ax.QuestionIn(bank_id="eiffel-steps"), ax.QuestionIn(text="Height of Radcliffe Camera, m", answer="40")])
        game = _game(db, out["game_id"])
        assert game["status"] == ax.LOBBY and len(out["join_code"]) == 6
        assert game["questions"][0] == {"text": BY_ID["eiffel-steps"]["text"], "answer": 1665.0, "bank_id": "eiffel-steps",
                                        "note": BY_ID["eiffel-steps"]["note"]}
        assert game["questions"][1]["answer"] == 40.0
        assert game["attempts"] == 2 + ax.EXTRA_ATTEMPTS and game["duration_s"] == 30 * 60
        assert game["created_by"] == "h1"

    @pytest.mark.parametrize("q,words", [
        (ax.QuestionIn(text="", answer="5"), "write the question"),
        (ax.QuestionIn(text="X", answer="-3"), "positive number"),
        (ax.QuestionIn(bank_id="nope"), "not in the question bank")])
    def test_bad_questions_are_refused(self, db, q, words):
        with pytest.raises(HTTPException) as exc:
            _create([q])
        assert words in exc.value.detail

    def test_join_by_code_with_a_team_name(self, db):
        out = _create()
        j = run(ax.join_game(ax.JoinGame(join_code=out["join_code"].lower(), team_name="  The Medians "), AMY))
        assert j["game_id"] == out["game_id"]
        assert _game(db, out["game_id"])["teams"]["a1"]["name"] == "The Medians"
        assert run(ax.join_game(ax.JoinGame(join_code=out["join_code"]), AMY))["game_id"] == out["game_id"]   # idempotent
        with pytest.raises(HTTPException) as exc:
            run(ax.join_game(ax.JoinGame(join_code=out["join_code"], team_name="the medians"), BEN))
        assert exc.value.status_code == 409
        run(ax.join_game(ax.JoinGame(join_code=out["join_code"]), BEN))
        assert _game(db, out["game_id"])["teams"]["b1"]["name"] == "ben"

    def test_wrong_code_and_host_joining_their_own_game(self, db):
        out = _create()
        with pytest.raises(HTTPException) as exc:
            run(ax.join_game(ax.JoinGame(join_code="ZZZZZZ"), AMY))
        assert exc.value.status_code == 404
        assert run(ax.join_game(ax.JoinGame(join_code=out["join_code"]), HOST))["host"] is True
        assert _game(db, out["game_id"])["teams"] == {}

    def test_only_the_host_starts_and_only_with_a_team(self, db):
        gid = _create()["game_id"]
        with pytest.raises(HTTPException, match="at least one team"):
            run(ax.start_game(gid, HOST))
        run(ax.join_game(ax.JoinGame(join_code=_game(db, gid)["join_code"]), AMY))
        with pytest.raises(HTTPException) as exc:
            run(ax.start_game(gid, AMY))
        assert exc.value.status_code == 403
        run(ax.start_game(gid, HOST))
        assert _game(db, gid)["status"] == ax.PLAYING


def _playing(db, teams=(AMY,), **kw):
    out = _create(**kw)
    for u in teams:
        run(ax.join_game(ax.JoinGame(join_code=out["join_code"]), u))
    run(ax.start_game(out["game_id"], HOST))
    return out["game_id"]


# ── Playing ─────────────────────────────────────────────────────────────────

class TestPlaying:
    def test_score_updates_straight_away_and_last_submission_counts(self, db):
        gid = _playing(db)
        assert _submit(gid, AMY, 0, 80, 100)["standing"]["score"] == (10 + 1) * 2          # 1 good of 2
        st = _submit(gid, AMY, 1, 100, 250)["standing"]                                     # 206 in [100, 250]
        assert st["score"] == 10 + 1 + 2 and st["good"] == 2 and st["attempts_left"] == 7 - 2
        st = _submit(gid, AMY, 1, 300, 400)["standing"]                                     # re-submit: now a miss
        assert st["good"] == 1 and st["score"] == (10 + 1) * 2
        assert st["per_question"][1] == {"low": 300, "high": 400, "good": False, "points": None}

    @pytest.mark.parametrize("low,high,words", [("0", "5", "positive"), ("9", "3", "not be above"), ("x", "3", "positive")])
    def test_bad_intervals_are_refused(self, db, low, high, words):
        gid = _playing(db)
        with pytest.raises(HTTPException) as exc:
            _submit(gid, AMY, 0, low, high)
        assert words in exc.value.detail

    def test_submissions_run_out_and_that_finishes_the_team(self, db):
        gid = _playing(db, attempts=2)
        _submit(gid, AMY, 0, 1, 1000)
        out = _submit(gid, AMY, 1, 1, 1000)
        assert out["standing"]["attempts_left"] == 0 and out["standing"]["finished"]
        assert out["status"] == ax.FINISHED                      # the only team is done

    def test_the_game_ends_when_every_team_has_finished(self, db):
        gid = _playing(db, teams=(AMY, BEN))
        run(ax.finish_team(gid, AMY))
        assert _game(db, gid)["status"] == ax.PLAYING
        with pytest.raises(HTTPException, match="finished"):
            _submit(gid, AMY, 0, 1, 2)
        assert run(ax.finish_team(gid, BEN))["status"] == ax.FINISHED
        assert _game(db, gid)["end_reason"] == "all_finished"

    def test_the_game_ends_when_time_is_up(self, db, monkeypatch):
        gid = _playing(db)
        later = ax._now() + dt.timedelta(minutes=31)
        monkeypatch.setattr(ax, "_now", lambda: later)
        with pytest.raises(HTTPException, match="Time's up"):
            _submit(gid, AMY, 0, 1, 2)
        assert _game(db, gid)["status"] == ax.FINISHED and _game(db, gid)["end_reason"] == "time"

    def test_the_host_can_end_it_and_teams_cannot(self, db):
        gid = _playing(db)
        with pytest.raises(HTTPException):
            run(ax.end_game(gid, AMY))
        run(ax.end_game(gid, HOST))
        assert _game(db, gid)["end_reason"] == "host"

    def test_outsiders_cannot_submit(self, db):
        gid = _playing(db)
        with pytest.raises(HTTPException) as exc:
            _submit(gid, BEN, 0, 1, 2)
        assert exc.value.status_code == 403


# ── What each screen sees ───────────────────────────────────────────────────

class TestState:
    def test_teams_see_questions_without_answers_and_their_own_standing(self, db):
        gid = _playing(db, teams=(AMY, BEN))
        _submit(gid, AMY, 0, 80, 100)
        st = run(ax.game_state(gid, AMY))
        assert st["status"] == ax.PLAYING and st["is_host"] is False
        assert all("answer" not in q for q in st["questions"])
        assert st["standing"]["good"] == 1 and "leaderboard" not in st
        assert 0 < st["seconds_left"] <= 30 * 60

    def test_the_host_sees_the_leaderboard_and_answers(self, db):
        gid = _playing(db, teams=(AMY, BEN))
        _submit(gid, AMY, 0, 80, 100)                 # Amy: 1 good -> 22
        _submit(gid, BEN, 0, 80, 100)
        _submit(gid, BEN, 1, 200, 210)                # Ben: 2 good -> 12, wins
        st = run(ax.game_state(gid, HOST))
        assert st["is_host"] and [q["answer"] for q in st["questions"]] == [88, 206]
        lb = st["leaderboard"]
        assert [(r["name"], r["rank"], r["score"]) for r in lb] == [("ben", 1, 12), ("amy", 2, 22)]

    def test_lobby_hides_questions_from_teams(self, db):
        out = _create()
        run(ax.join_game(ax.JoinGame(join_code=out["join_code"]), AMY))
        st = run(ax.game_state(out["game_id"], AMY))
        assert st["status"] == ax.LOBBY and "questions" not in st and st["teams_joined"] == ["amy"]

    def test_everyone_sees_answers_and_the_board_once_finished(self, db):
        gid = _playing(db)
        run(ax.finish_team(gid, AMY))
        st = run(ax.game_state(gid, AMY))
        assert st["status"] == ax.FINISHED and st["questions"][0]["answer"] == 88 and st["leaderboard"]

    def test_strangers_are_turned_away(self, db):
        gid = _playing(db)
        with pytest.raises(HTTPException) as exc:
            run(ax.game_state(gid, BEN))
        assert exc.value.status_code == 403


# ── Fair play ───────────────────────────────────────────────────────────────

class TestFairPlay:
    def _report(self, gid, user, counts):
        return run(ax.report_flags(gid, ax.FlagReport(counts=counts), user))

    def test_reports_add_up_and_the_host_sees_them(self, db):
        gid = _playing(db, teams=(AMY, BEN))
        self._report(gid, AMY, {"fullscreen_exit": 1, "odd_keys": 3})
        self._report(gid, AMY, {"fullscreen_exit": 1, "paste": 2, "made_up": 9})
        team = _game(db, gid)["teams"]["a1"]
        assert team["flags"] == {"fullscreen_exit": 2, "odd_keys": 3, "paste": 2}
        self._report(gid, BEN, {"no_fullscreen": 1})
        ben = next(r for r in run(ax.game_state(gid, HOST))["leaderboard"] if r["name"] == "ben")
        assert ben["flags_text"] == ["no full screen: 1"]
        self._report(gid, BEN, {"no_fullscreen": -1})
        lb = {r["name"]: r for r in run(ax.game_state(gid, HOST))["leaderboard"]}
        assert lb["amy"]["flags"] == {"fullscreen_exit": 2, "odd_keys": 3, "paste": 2}
        assert "left full screen: 2" in lb["amy"]["flags_text"] and "irregular keys: 3" in lb["amy"]["flags_text"]
        assert lb["ben"]["flags"] == {"no_fullscreen": 1}

    def test_a_report_is_capped_and_only_counts_while_playing(self, db):
        out = _create()
        run(ax.join_game(ax.JoinGame(join_code=out["join_code"]), AMY))
        assert self._report(out["game_id"], AMY, {"odd_keys": 5})["ignored"] is True      # lobby
        run(ax.start_game(out["game_id"], HOST))
        self._report(out["game_id"], AMY, {"odd_keys": 10_000, "copy": -4})
        assert _game(db, out["game_id"])["teams"]["a1"]["flags"] == {"odd_keys": ax.FLAG_MAX_PER_REPORT}

    def test_outsiders_cannot_report(self, db):
        gid = _playing(db)
        with pytest.raises(HTTPException) as exc:
            self._report(gid, BEN, {"odd_keys": 1})
        assert exc.value.status_code == 403

    def test_the_page_guards_play_and_previews_scores(self):
        from pathlib import Path
        js = (Path(ax.__file__).parent / "static" / "approx.js").read_text()
        for needle in ('document.addEventListener("paste", block("paste"))', 'document.addEventListener("copy", block("copy"))',
                       '["c", "v", "x"].includes', 'requestFullscreen', 'note("fullscreen_exit")', 'note("left_page")',
                       'note("odd_keys")', '/flags`, { counts }', "If it's in:", "if not:"):
            assert needle in js, needle
        assert "confirm(\"Lock in" not in js           # no browser pop-up while in full screen
        assert 'document.addEventListener("beforeinput"' in js and '"Unidentified"' in js   # phone keyboards
        assert "if (guarding && !away)" in js                                               # one trip away, one count


# ── The modified mode ───────────────────────────────────────────────────────

CY = User(id="c1", username="cy")


@pytest.fixture
def clock(monkeypatch):
    """A clock that moves a second each time it's read, so "who answered
    first" never comes down to microseconds."""
    t = {"now": dt.datetime(2026, 10, 25, 12, 0, tzinfo=dt.timezone.utc)}

    def now():
        t["now"] += dt.timedelta(seconds=1)
        return t["now"]
    monkeypatch.setattr(ax, "_now", now)
    return t


def _mod(questions, users=(AMY, BEN, CY)):
    out = run(ax.create_game(ax.CreateGame(questions=questions, mode="modified"), HOST))
    for u in users:
        run(ax.join_game(ax.JoinGame(join_code=out["join_code"]), u))
    run(ax.start_game(out["game_id"], HOST))
    return out["game_id"]


def _answer(game_id, user, q, value):
    return run(ax.submit_interval(game_id, ax.Submission(question=q, value=str(value)), user))


FIXED_50 = ax.QuestionIn(text="Pick a number", kind="fixed", answer="50")


class TestModified:
    def test_closest_gets_as_many_points_as_teams_and_ties_go_to_whoever_was_first(self, db, clock):
        gid = _mod([FIXED_50])
        _answer(gid, BEN, 0, 48)        # first in
        _answer(gid, AMY, 0, 52)        # just as close, later
        _answer(gid, CY, 0, 70)
        st = run(ax.game_state(gid, HOST))
        assert st["status"] == ax.FINISHED          # everyone has answered everything
        board = {r["name"]: r for r in st["leaderboard"]}
        assert (board["ben"]["points"], board["amy"]["points"], board["cy"]["points"]) == (3, 2, 1)
        assert [r["name"] for r in st["leaderboard"]] == ["ben", "amy", "cy"]
        assert board["ben"]["firsts"] == 1

    def test_a_team_that_does_not_answer_gets_nothing_and_points_count_every_team(self, db, clock):
        gid = _mod([FIXED_50, ax.QuestionIn(text="Another", kind="fixed", answer="10")])
        _answer(gid, AMY, 0, 50)
        _answer(gid, BEN, 0, 40)
        _answer(gid, AMY, 1, 10)
        run(ax.end_game(gid, HOST))
        board = {r["name"]: r for r in run(ax.game_state(gid, AMY))["leaderboard"]}
        assert board["amy"]["points"] == 3 + 3       # three teams in the game, so first is worth 3
        assert board["ben"]["points"] == 2 and board["cy"]["points"] == 0

    def test_one_answer_per_question_and_the_range_is_kept(self, db, clock):
        gid = _mod([ax.QuestionIn(text="1 to 10", kind="fixed", answer="5", min="1", max="10")])
        with pytest.raises(HTTPException) as exc:
            _answer(gid, AMY, 0, 11)
        assert "from 1 to 10" in exc.value.detail
        with pytest.raises(HTTPException) as exc:
            _answer(gid, AMY, 0, "seven")
        assert "Enter a number" in exc.value.detail
        _answer(gid, AMY, 0, 7)
        with pytest.raises(HTTPException) as exc:
            _answer(gid, AMY, 0, 6)
        assert "already answered" in exc.value.detail

    def test_zero_and_negative_answers_are_fine(self, db, clock):
        gid = _mod([ax.QuestionIn(text="Temperature", kind="fixed", answer="-3")], users=(AMY,))
        assert _answer(gid, AMY, 0, "-2")["standing"]["per_question"][0]["shown"] == "-2"
        assert ax.parse_number("0") == 0 and ax.parse_number("-1.5e3") == -1500
        with pytest.raises(ValueError):
            ax.parse_number("--5")

    def test_teams_see_no_answers_or_points_until_it_is_over_and_the_host_sees_it_all(self, db, clock):
        gid = _mod([ax.QuestionIn(text="Guess my number", kind="random", min="1", max="100"), FIXED_50])
        _answer(gid, AMY, 0, 50)
        mine = run(ax.game_state(gid, AMY))
        assert "leaderboard" not in mine
        assert all("answer" not in q for q in mine["questions"])
        assert "points" not in mine["standing"] and "rank" not in mine["standing"]["per_question"][0]
        assert mine["questions"][0]["hint"] == "The system drew a whole number from 1 to 100. Answer from 1 to 100."
        host = run(ax.game_state(gid, HOST))
        drawn = host["questions"][0]["answer"]
        assert 1 <= drawn <= 100 and drawn == int(drawn) and host["questions"][0]["provisional"] is False
        assert host["questions"][0]["closest"] == [{"name": "amy", "shown": "50"}]
        assert host["leaderboard"][0]["name"] == "amy"
        run(ax.end_game(gid, HOST))
        done = run(ax.game_state(gid, AMY))
        assert done["questions"][0]["answer"] == drawn and done["standing"]["points"] == 3
        assert done["standing"]["per_question"][0]["rank"] == 1

    def test_answers_worked_out_from_the_teams(self, db, clock):
        qs = [ax.QuestionIn(bank_id="m-two-thirds"), ax.QuestionIn(bank_id="m-median"),
              ax.QuestionIn(bank_id="m-sum"), ax.QuestionIn(bank_id="m-max-identical"),
              ax.QuestionIn(bank_id="m-distinct"), ax.QuestionIn(bank_id="m-digit-1")]
        gid = _mod(qs)
        for user, values in ((AMY, [30, 10, 5, 2, 3, 11]), (BEN, [30, 20, 10, 2, 3, 0]), (CY, [60, 300, 20, 1, 1, 1])):
            for i, v in enumerate(values):
                _answer(gid, user, i, v)
        answers = [q["answer"] for q in run(ax.game_state(gid, AMY))["questions"]]
        assert answers[0] == pytest.approx(2 / 3 * 40)
        assert answers[1] == 20 and answers[2] == 35
        assert answers[3] == 2                     # 2 given twice
        assert answers[4] == 2                     # 3 and 1
        # every digit 1 in every answer to every question
        assert answers[5] == sum(str(v).count("1") for v in (30, 10, 5, 2, 3, 11, 30, 20, 10, 2, 3, 0,
                                                             60, 300, 20, 1, 1, 1))

    def test_a_computed_tie_is_a_tie_whatever_the_float_noise(self, db, clock):
        gid = _mod([ax.QuestionIn(text="Average", kind="mean", factor="1")], users=(AMY, BEN))
        _answer(gid, BEN, 0, 0.1)                  # first in
        _answer(gid, AMY, 0, 0.3)                  # average 0.2: in floats 0.3 is a hair closer, but it's a tie
        board = {r["name"]: r["points"] for r in run(ax.game_state(gid, HOST))["leaderboard"]}
        assert board == {"ben": 2, "amy": 1}

    def test_score_questions_use_the_other_questions(self, db, clock):
        gid = _mod([FIXED_50, ax.QuestionIn(bank_id="m-lowest-score"), ax.QuestionIn(bank_id="m-highest-score")])
        _answer(gid, AMY, 0, 50); _answer(gid, BEN, 0, 49); _answer(gid, CY, 0, 10)      # 3, 2, 1 on question 1
        _answer(gid, AMY, 1, 1); _answer(gid, BEN, 1, 3); _answer(gid, CY, 1, 9)
        _answer(gid, AMY, 2, 3); _answer(gid, BEN, 2, 2); _answer(gid, CY, 2, 9)
        st = run(ax.game_state(gid, AMY))
        assert [q["answer"] for q in st["questions"]] == [50, 1, 3]
        board = {r["name"]: r["points"] for r in st["leaderboard"]}
        assert board == {"amy": 3 + 3 + 3, "ben": 2 + 2 + 2, "cy": 1 + 1 + 1}

    def test_the_host_sets_a_fixed_answer_later_and_points_follow(self, db, clock):
        gid = _mod([ax.QuestionIn(bank_id="m-oaf-applications")], users=(AMY, BEN))
        _answer(gid, AMY, 0, 100)
        _answer(gid, BEN, 0, 300)
        st = run(ax.game_state(gid, AMY))
        assert st["status"] == ax.FINISHED and st["questions"][0]["answer"] is None
        assert all(r["points"] == 0 for r in st["leaderboard"])
        with pytest.raises(HTTPException) as exc:
            run(ax.set_answer(gid, ax.SetAnswer(question=0, answer="250"), AMY))
        assert exc.value.status_code == 403
        run(ax.set_answer(gid, ax.SetAnswer(question=0, answer="250"), HOST))
        board = {r["name"]: r["points"] for r in run(ax.game_state(gid, AMY))["leaderboard"]}
        assert board == {"ben": 2, "amy": 1}

    def test_only_fixed_answers_can_be_set(self, db, clock):
        gid = _mod([ax.QuestionIn(bank_id="m-random-100")], users=(AMY,))
        with pytest.raises(HTTPException) as exc:
            run(ax.set_answer(gid, ax.SetAnswer(question=0, answer="5"), HOST))
        assert "from the game itself" in exc.value.detail

    def test_ordinary_bank_questions_play_as_normal_intervals(self, db, clock):
        gid = _mod([ax.QuestionIn(bank_id="eiffel-steps"), ax.QuestionIn(bank_id="m-median")], users=(AMY,))
        q = _game(db, gid)["questions"][0]
        assert q["kind"] == ax.INTERVAL and q["answer"] == 1665
        assert _game(db, gid)["attempts"] == 1 + ax.EXTRA_ATTEMPTS
        # Bank questions say how they're answered in their own words: no extra line.
        assert [q["hint"] for q in run(ax.game_state(gid, AMY))["questions"]] == ["", ""]

    @pytest.mark.parametrize("q,words", [
        (ax.QuestionIn(text="R", kind="random", min="1"), "whole-number range"),
        (ax.QuestionIn(text="R", kind="random", min="5", max="2"), "lowest allowed answer is above"),
        (ax.QuestionIn(text="R", kind="random", min="1.5", max="9"), "whole-number range"),
        (ax.QuestionIn(text="D", kind="digit_count", digit="12"), "one digit"),
        (ax.QuestionIn(text="M", kind="mean", factor="0"), "factor"),
        (ax.QuestionIn(text="K", kind="nonsense"), "where its answer comes from"),
        (ax.QuestionIn(bank_id="m-nope"), "not in the question bank")])
    def test_bad_modified_questions_are_refused(self, db, q, words):
        with pytest.raises(HTTPException) as exc:
            run(ax.create_game(ax.CreateGame(questions=[q], mode="modified"), HOST))
        assert words in exc.value.detail

    def test_every_modified_bank_question_makes_a_game(self, db):
        from app.approx_questions import MODIFIED_BANK
        out = run(ax.create_game(ax.CreateGame(questions=[ax.QuestionIn(bank_id=q["id"]) for q in MODIFIED_BANK],
                                               mode="modified"), HOST))
        game = _game(db, out["game_id"])
        assert game["mode"] == "modified" and game["attempts"] == 0      # no interval questions
        assert all(q.get("note") for q in MODIFIED_BANK)
        bank = run(ax.question_bank(AMY))
        assert {q["id"] for q in bank["modified"]} == {q["id"] for q in MODIFIED_BANK}
        assert all("answer" not in q for q in bank["modified"])

    def test_numbers_written_out_and_factors(self):
        assert ax._written_out(1e3) == "1000" and ax._written_out(0.25) == "0.25" and ax._written_out(1e-7) == "0.0000001"
        assert ax.parse_factor("2/3") == pytest.approx(2 / 3) and ax.parse_factor("") == 1
        assert ax.factor_label(2 / 3) == "2/3"

    def test_normal_games_still_score_intervals(self, db):
        out = _create()
        assert _game(db, out["game_id"])["mode"] == "normal"


def _interval(game_id, user, q, low, high):
    return run(ax.submit_interval(game_id, ax.Submission(question=q, low=str(low), high=str(high)), user))


MIXED = [ax.QuestionIn(text="Keys on a piano", kind="interval", answer="88"),
         ax.QuestionIn(text="Bones in the body", kind="interval", answer="206"),
         FIXED_50, ax.QuestionIn(bank_id="m-max-identical")]


class TestMixed:
    """A modified game with normal (interval) questions and closest-number ones."""

    def test_the_interval_score_shows_as_you_go_and_the_rest_waits(self, db, clock):
        gid = _mod(MIXED)
        out = _interval(gid, AMY, 0, 80, 100)
        part = out["standing"]["interval"]
        assert part["good"] == 1 and part["score"] == ax.score_of(2, [1]) and part["attempts_left"] == 2 + 5 - 1
        assert out["standing"]["per_question"][0] == {"low": 80, "high": 100, "good": True, "points": 1, "interval": True}
        _answer(gid, AMY, 2, 49)
        mine = run(ax.game_state(gid, AMY))
        assert mine["has_interval"] and mine["has_crowd"]
        assert [q["interval"] for q in mine["questions"]] == [True, True, False, False]
        assert "points" not in mine["standing"] and "leaderboard" not in mine
        assert mine["standing"]["interval"]["score_label"] == ax.score_label(ax.score_of(2, [1]))

    def test_each_part_is_ranked_and_the_places_add_up(self, db, clock):
        gid = _mod(MIXED)
        # Intervals: amy both good and tight, ben one good, cy none.
        _interval(gid, AMY, 0, 80, 100); _interval(gid, AMY, 1, 200, 210)
        _interval(gid, BEN, 0, 50, 100)
        # Closest number: cy closest twice, then ben, then amy.
        _answer(gid, CY, 2, 50); _answer(gid, BEN, 2, 45); _answer(gid, AMY, 2, 10)
        _answer(gid, CY, 3, 1); _answer(gid, BEN, 3, 3); _answer(gid, AMY, 3, 9)
        run(ax.end_game(gid, HOST))
        st = run(ax.game_state(gid, AMY))
        board = {r["name"]: r for r in st["leaderboard"]}
        assert {n: (r["interval"], r["crowd"], r["total"]) for n, r in board.items()} == {
            "amy": (3, 1, 4), "ben": (2, 2, 4), "cy": (1, 3, 4)}
        # All level on places: then closest-number points decide.
        assert [r["name"] for r in st["leaderboard"]] == ["cy", "ben", "amy"]
        assert st["standing"]["parts"] == {"interval": 3, "crowd": 1, "total": 4}

    def test_level_teams_share_a_place(self):
        assert ax._part_points({"a": 40, "b": 40, "c": 80}, lower_is_better=True) == {"a": 3, "b": 3, "c": 1}
        assert ax._part_points({"a": 5, "b": 9, "c": 9}, lower_is_better=False) == {"a": 1, "b": 3, "c": 3}

    def test_a_team_is_done_only_when_both_parts_are(self, db, clock):
        gid = _mod([MIXED[0], FIXED_50], users=(AMY, BEN))
        _answer(gid, AMY, 1, 50)
        assert not _game(db, gid)["teams"]["a1"]["finished"]          # intervals still to use
        for _ in range(1 + ax.EXTRA_ATTEMPTS):
            _interval(gid, AMY, 0, 80, 90)
        assert _game(db, gid)["teams"]["a1"]["finished"]
        with pytest.raises(HTTPException):
            _interval(gid, AMY, 0, 80, 90)

    def test_bad_intervals_and_interval_answers(self, db, clock):
        gid = _mod(MIXED, users=(AMY,))
        with pytest.raises(HTTPException) as exc:
            _interval(gid, AMY, 0, 100, 80)
        assert "lower end" in exc.value.detail
        with pytest.raises(HTTPException) as exc:
            run(ax.create_game(ax.CreateGame(questions=[ax.QuestionIn(text="X", kind="interval")], mode="modified"), HOST))
        assert "needs its answer" in exc.value.detail
        with pytest.raises(HTTPException):
            run(ax.set_answer(gid, ax.SetAnswer(question=0, answer="-4"), HOST))
        run(ax.set_answer(gid, ax.SetAnswer(question=0, answer="90"), HOST))
        assert _game(db, gid)["questions"][0]["answer"] == 90

    def test_digits_count_both_ends_of_an_interval(self, db, clock):
        gid = _mod([MIXED[0], ax.QuestionIn(bank_id="m-digit-1")], users=(AMY,))
        _interval(gid, AMY, 0, 81, 110)                # 1 + 2
        _answer(gid, AMY, 1, 1)                        # + 1
        assert run(ax.game_state(gid, HOST))["questions"][1]["answer"] == 4

    def test_the_host_sees_how_many_intervals_are_good(self, db, clock):
        gid = _mod(MIXED, users=(AMY, BEN))
        _interval(gid, AMY, 0, 80, 100); _interval(gid, BEN, 0, 10, 20)
        q = run(ax.game_state(gid, HOST))["questions"][0]
        assert (q["answered"], q["good"], q["kind_label"]) == (2, 1, ax.KINDS["interval"])
