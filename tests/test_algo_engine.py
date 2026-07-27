"""Tests for app.algo_engine — the client-side Market Simulation Py engine.

No player code runs here anymore: strategies live on players' machines and
reach the market through submit_orders(). The rules that matter most are the
position limit and order validation, since those are the server's only defence
now that anyone can POST orders directly.
"""


import pytest

from app import algo_engine as engine
from app.algo_engine import POSITION_LIMIT, OrderRejected, Run


@pytest.fixture(autouse=True)
def clean_registry():
    engine.reset()
    yield
    engine.reset()


@pytest.fixture
def run() -> Run:
    return engine.create_run("test run", "creator", seed=1234)


def started(run: Run, *uids: str) -> Run:
    """Join each uid, start the run, and warm the book with one heartbeat."""
    for uid in uids:
        run.join(uid, uid)
    run.start()
    run.advance(now=run._t0 + engine.TICK_SECONDS)   # one tick so bots quote
    return run


def heartbeat(run: Run, ticks: int) -> None:
    target = min(run.tick + ticks, engine.TOTAL_TICKS)
    while run.tick < target and run.status == "running":
        run.advance(now=run._t0 + target * engine.TICK_SECONDS)


def submit(run: Run, uid: str, *orders, allowance=None):
    """Submit orders with a full allowance unless one is given."""
    if allowance is None:
        allowance = len(orders)
    return run.submit_orders(uid, list(orders), allowance)


# ── Lobby & lifecycle ──────────────────────────────────────────────────────


class TestLobby:
    def test_join_is_idempotent(self, run: Run):
        assert run.join("u1", "Ada") is run.join("u1", "Ada")
        assert len(run.players) == 1

    def test_bots_are_not_players(self, run: Run):
        assert len(run.players) == 0
        assert engine.MM_BOT_ID in run.participants
        assert engine.FLOW_BOT_ID in run.participants

    def test_member_ignores_bots_and_strangers(self, run: Run):
        run.join("u1", "Ada")
        assert run.member("u1").name == "Ada"
        assert run.member(engine.MM_BOT_ID) is None
        assert run.member("nobody") is None

    def test_join_closes_once_finished(self, run: Run):
        started(run, "u1")
        run.finish()
        with pytest.raises(ValueError, match="already finished"):
            run.join("late", "Late")

    def test_join_allowed_mid_run(self, run: Run):
        """Unlike the old model, a public run lets players join after it starts."""
        started(run, "u1")
        run.join("u2", "Latecomer")
        assert run.member("u2") is not None

    def test_orders_rejected_before_start(self, run: Run):
        run.join("u1", "Ada")
        with pytest.raises(OrderRejected, match="not currently live"):
            submit(run, "u1", {"item": "WIDGET", "side": "BUY", "qty": 10})

    def test_orders_rejected_for_non_member(self, run: Run):
        started(run, "u1")
        with pytest.raises(OrderRejected, match="not joined"):
            submit(run, "stranger", {"item": "WIDGET", "side": "BUY", "qty": 10})


# ── The position limit ─────────────────────────────────────────────────────


class TestPositionLimit:
    def test_market_buys_cannot_exceed_the_cap(self, run: Run):
        started(run, "greedy")
        for _ in range(30):
            submit(run, "greedy", {"item": "WIDGET", "side": "BUY", "qty": 900})
            heartbeat(run, 1)
        assert run.participants["greedy"].pos["WIDGET"] <= POSITION_LIMIT

    def test_market_sells_cannot_exceed_the_cap(self, run: Run):
        started(run, "greedy")
        for _ in range(30):
            submit(run, "greedy", {"item": "WIDGET", "side": "SELL", "qty": 900})
            heartbeat(run, 1)
        assert run.participants["greedy"].pos["WIDGET"] >= -POSITION_LIMIT

    def test_resting_orders_count_toward_the_limit(self, run: Run):
        """Two 400-lot resting bids fit; the third would breach and is refused."""
        started(run, "quoter")
        # price far below the market so they rest instead of filling
        for _ in range(2):
            r = submit(run, "quoter", {"item": "WIDGET", "side": "BUY", "price": 1.0, "qty": 400})
            assert r["results"][0]["ok"]
        r = submit(run, "quoter", {"item": "WIDGET", "side": "BUY", "price": 1.0, "qty": 400})
        assert not r["results"][0]["ok"]
        assert "position limit" in r["results"][0]["error"]

    def test_oversized_single_order_is_refused(self, run: Run):
        started(run, "big")
        r = submit(run, "big", {"item": "WIDGET", "side": "BUY", "price": 50000.0,
                                "qty": POSITION_LIMIT + 1})
        assert not r["results"][0]["ok"]
        assert "position limit" in r["results"][0]["error"]

    def test_bots_respect_the_limit_too(self, run: Run):
        started(run, "idle")
        heartbeat(run, 120)
        for bot_id in (engine.MM_BOT_ID, engine.FLOW_BOT_ID):
            for item, qty in run.participants[bot_id].pos.items():
                assert abs(qty) <= POSITION_LIMIT, (bot_id, item)


# ── Order validation ───────────────────────────────────────────────────────


class TestOrderValidation:
    @pytest.mark.parametrize("order, expected", [
        ({"item": "NOPE", "side": "BUY", "price": 1.0, "qty": 1}, "unknown item"),
        ({"item": "WIDGET", "side": "HOLD", "price": 1.0, "qty": 1}, "side must be"),
        ({"item": "WIDGET", "side": "BUY", "price": 1.0, "qty": 0}, "positive"),
        ({"item": "WIDGET", "side": "BUY", "price": -1.0, "qty": 1}, "between 0"),
        ({"item": "WIDGET", "side": "BUY", "price": "abc", "qty": 1}, "must be a number"),
        ({"item": "WIDGET", "side": "BUY", "price": 1.0, "qty": "lots"}, "whole number"),
        ({"action": "self_destruct"}, "unknown action"),
    ])
    def test_bad_orders_are_rejected_with_a_reason(self, run: Run, order, expected):
        started(run, "bad")
        r = submit(run, "bad", order)
        assert not r["results"][0]["ok"]
        assert expected in r["results"][0]["error"]

    def test_cancel_all_pulls_resting_orders(self, run: Run):
        started(run, "quoter")
        submit(run, "quoter", {"item": "WIDGET", "side": "BUY", "price": 1.0, "qty": 10})
        assert run._resting(run.books["WIDGET"], "quoter")[0] == 10
        r = submit(run, "quoter", {"action": "cancel_all"})
        assert r["results"][0]["ok"]
        assert run._resting(run.books["WIDGET"], "quoter")[0] == 0

    def test_market_order_leaves_no_resting_remainder(self, run: Run):
        started(run, "taker")
        r = submit(run, "taker", {"item": "WIDGET", "side": "BUY", "qty": 900})
        assert run._resting(run.books["WIDGET"], "taker")[0] == 0
        assert r["results"][0]["resting"] == 0

    def test_batch_over_the_cap_is_trimmed(self, run: Run):
        started(run, "spammer")
        orders = [{"item": "WIDGET", "side": "BUY", "price": 1.0, "qty": 1} for _ in range(50)]
        r = run.submit_orders("spammer", orders, allowance=50)
        # only MAX_ORDERS_PER_REQUEST processed, plus one trailing notice
        accepted = [x for x in r["results"] if x.get("ok")]
        assert len(accepted) == engine.MAX_ORDERS_PER_REQUEST
        assert any("per request" in x.get("error", "") for x in r["results"])


# ── Rate-limit allowance handling ──────────────────────────────────────────


class TestAllowance:
    def test_orders_beyond_allowance_are_marked_rate_limited(self, run: Run):
        started(run, "fast")
        orders = [{"item": "WIDGET", "side": "BUY", "price": 1.0, "qty": 1} for _ in range(5)]
        r = run.submit_orders("fast", orders, allowance=3)
        oks = [x for x in r["results"] if x.get("ok")]
        limited = [x for x in r["results"] if x.get("rate_limited")]
        assert len(oks) == 3
        assert len(limited) == 2

    def test_zero_allowance_applies_nothing(self, run: Run):
        started(run, "fast")
        r = run.submit_orders("fast", [{"item": "WIDGET", "side": "BUY", "price": 1.0, "qty": 1}],
                              allowance=0)
        assert all(not x.get("ok") for x in r["results"])
        assert run._resting(run.books["WIDGET"], "fast")[0] == 0


# ── Scoring ────────────────────────────────────────────────────────────────


class TestScoring:
    def test_a_buy_costs_cash_and_gives_position(self, run: Run):
        started(run, "taker")
        submit(run, "taker", {"item": "WIDGET", "side": "BUY", "qty": 20})
        p = run.participants["taker"]
        assert p.pos["WIDGET"] == 20
        assert p.cash < 0

    def test_cash_and_positions_are_conserved(self, run: Run):
        started(run, "a", "b")
        for _ in range(40):
            submit(run, "a", {"item": "WIDGET", "side": "BUY", "qty": 20})
            submit(run, "b", {"item": "GADGET", "side": "SELL", "qty": 20})
            heartbeat(run, 1)
        assert sum(p.cash for p in run.participants.values()) == pytest.approx(0, abs=1e-6)
        for symbol in engine.ITEM_SYMBOLS:
            assert sum(p.pos[symbol] for p in run.participants.values()) == pytest.approx(0)

    def test_pnl_is_cash_plus_position_at_fair_value(self, run: Run):
        started(run, "u1")
        p = run.participants["u1"]
        p.cash = -1000.0
        p.pos["WIDGET"] = 10.0
        expected = -1000.0 + 10.0 * run.items["WIDGET"].fair
        assert p.pnl({s: i.fair for s, i in run.items.items()}) == pytest.approx(expected)

    def test_a_flat_player_ends_at_zero(self, run: Run):
        started(run, "idle")
        heartbeat(run, engine.TOTAL_TICKS)
        row = next(r for r in run.leaderboard() if r["username"] == "idle")
        assert row["pnl"] == 0.0

    def test_leaderboard_is_ranked_by_pnl(self, run: Run):
        started(run, "idle")
        heartbeat(run, 60)
        rows = run.leaderboard()
        assert [r["rank"] for r in rows] == list(range(1, len(rows) + 1))
        assert all(rows[i]["pnl"] >= rows[i + 1]["pnl"] for i in range(len(rows) - 1))


# ── The market heartbeat & clock ───────────────────────────────────────────


class TestHeartbeat:
    def test_run_finishes_on_the_clock(self, run: Run):
        started(run, "idle")
        run.advance(now=run._t0 + engine.RUN_SECONDS + 1)
        while run.status == "running":
            run.advance(now=run._t0 + engine.RUN_SECONDS + 1)
        assert run.status == "finished"
        assert run.tick == engine.TOTAL_TICKS
        assert run.results

    def test_catch_up_is_bounded_per_call(self, run: Run):
        run.join("u1", "Ada")
        run.start()
        executed = run.advance(now=run._t0 + engine.RUN_SECONDS)
        assert executed == engine.MAX_CATCHUP_TICKS

    def test_advance_does_nothing_before_start(self, run: Run):
        run.join("u1", "Ada")
        assert run.advance() == 0
        assert run.tick == 0

    def test_finish_is_idempotent(self, run: Run):
        started(run, "idle")
        run.finish()
        first = run.finished_at
        run.finish()
        assert run.finished_at is first

    def test_bots_quote_a_two_sided_market(self, run: Run):
        started(run, "idle")
        heartbeat(run, 3)
        for row in run.market_snapshot():
            assert row["bid"] is not None and row["ask"] is not None, row["item"]
            assert row["bid"] < row["ask"], row["item"]

    def test_orders_matched_between_ticks_are_continuous(self, run: Run):
        """A player can trade many times within a single heartbeat tick."""
        started(run, "taker")
        before = run.tick
        for _ in range(5):
            submit(run, "taker", {"item": "WIDGET", "side": "BUY", "qty": 10})
        assert run.tick == before          # no tick advanced
        assert run.participants["taker"].pos["WIDGET"] == 50


# ── Views ──────────────────────────────────────────────────────────────────


class TestViews:
    def test_fair_value_is_hidden_until_the_run_ends(self, run: Run):
        started(run, "idle")
        heartbeat(run, 3)
        assert all("fair" not in row for row in run.market_snapshot(reveal_fair=False))
        assert all("fair" in row for row in run.market_snapshot(reveal_fair=True))

    def test_market_snapshot_exposes_documented_fields(self, run: Run):
        started(run, "idle")
        heartbeat(run, 2)
        row = run.market_snapshot()[0]
        assert {"item", "name", "bid", "ask", "bid_qty", "ask_qty", "mid", "spread", "last"} <= set(row)

    def test_player_view_hides_bots_and_strangers(self, run: Run):
        started(run, "u1")
        assert run.player_view(engine.MM_BOT_ID) is None
        assert run.player_view("nobody") is None
        view = run.player_view("u1")
        assert set(view["positions"]) == set(engine.ITEM_SYMBOLS)
        assert set(view["open_orders"]) == set(engine.ITEM_SYMBOLS)


# ── Registry ───────────────────────────────────────────────────────────────


class TestRegistry:
    def test_join_codes_are_unique(self):
        codes = {engine.create_run(f"r{i}", "c").join_code for i in range(20)}
        assert len(codes) == 20

    def test_find_by_code_is_case_insensitive(self, run: Run):
        assert engine.find_by_code(run.join_code.lower()) is run

    def test_finished_runs_are_not_joinable_by_code(self, run: Run):
        started(run, "idle")
        run.finish()
        assert engine.find_by_code(run.join_code) is None

    def test_open_runs_lists_lobbies_and_live_runs(self, run: Run):
        assert run in engine.open_runs()
        started(run, "idle")
        assert run in engine.open_runs()
        run.finish()
        assert run not in engine.open_runs()
