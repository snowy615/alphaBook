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
        assert all(set(q) == {"id", "text"} for q in out["questions"])


# ── Hosting and joining ─────────────────────────────────────────────────────

class TestHostAndJoin:
    def test_create_with_own_and_bank_questions(self, db):
        out = _create([ax.QuestionIn(bank_id="piano-keys"), ax.QuestionIn(text="Height of Radcliffe Camera, m", answer="40")])
        game = _game(db, out["game_id"])
        assert game["status"] == ax.LOBBY and len(out["join_code"]) == 6
        assert game["questions"][0] == {"text": BY_ID["piano-keys"]["text"], "answer": 88.0, "bank_id": "piano-keys"}
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
        assert ben["flags_text"] == ["played without full screen (browser wouldn't allow it): 1"]
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
