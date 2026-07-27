"""Tests for app.swe_prep_engine — the SWE Prep contest engine.

The position limit is the rule players will attack hardest, so most of these
tests are about it holding under resting orders, market orders and short sides.
"""

import time

import pytest

from app import swe_prep_engine as engine
from app.swe_prep_engine import POSITION_LIMIT, Run


@pytest.fixture(autouse=True)
def clean_registry():
    engine.reset()
    yield
    engine.reset()


@pytest.fixture
def run() -> Run:
    return engine.create_run("test run", "creator", seed=1234)


def started(run: Run, **players: str) -> Run:
    """Join each ``name=code`` player and start the run."""
    for name, code in players.items():
        run.join(name, name)
        run.set_code(name, code)
    run.start()
    return run


def fast_forward(run: Run, ticks: int) -> None:
    """Replay exactly ``ticks`` more ticks, regardless of real elapsed time."""
    target = min(run.tick + ticks, engine.TOTAL_TICKS)
    while run.tick < target and run.status == "running":
        run.advance(now=run._t0 + target * engine.TICK_SECONDS)


BUY_HARD = """
def on_tick(ctx):
    out = []
    for item in ctx["items"]:
        out.append({"item": item, "side": "BUY", "price": 100000.0, "qty": 500})
    return out
"""

SELL_HARD = """
def on_tick(ctx):
    out = []
    for item in ctx["items"]:
        out.append({"item": item, "side": "SELL", "price": 0.01, "qty": 500})
    return out
"""

REST_ORDERS = """
def on_tick(ctx):
    out = []
    for item in ctx["items"]:
        quote = ctx["market"][item]
        if quote["bid"] is not None:
            out.append({"item": item, "side": "BUY", "price": quote["bid"] - 5, "qty": 400})
    return out
"""


# ── Lobby ──────────────────────────────────────────────────────────────────


class TestLobby:
    def test_join_is_idempotent(self, run: Run):
        first = run.join("u1", "Ada")
        again = run.join("u1", "Ada")
        assert first is again
        assert len(run.players) == 1

    def test_bots_are_not_players(self, run: Run):
        assert len(run.players) == 0
        assert engine.MM_BOT_ID in run.participants
        assert engine.FLOW_BOT_ID in run.participants

    def test_set_code_rejects_a_sandbox_violation(self, run: Run):
        from app.swe_prep_sandbox import SandboxError

        run.join("u1", "Ada")
        with pytest.raises(SandboxError):
            run.set_code("u1", "import os\ndef on_tick(ctx):\n    return []\n")

    def test_set_code_requires_membership(self, run: Run):
        with pytest.raises(ValueError, match="not joined"):
            run.set_code("stranger", "def on_tick(ctx):\n    return []\n")

    def test_start_needs_at_least_one_strategy(self, run: Run):
        run.join("u1", "Ada")
        with pytest.raises(ValueError, match="no strategies"):
            run.start()

    def test_code_locks_once_running(self, run: Run):
        started(run, u1="def on_tick(ctx):\n    return []\n")
        with pytest.raises(ValueError, match="locked"):
            run.set_code("u1", "def on_tick(ctx):\n    return []\n")

    def test_join_closes_once_running(self, run: Run):
        started(run, u1="def on_tick(ctx):\n    return []\n")
        with pytest.raises(ValueError, match="already started"):
            run.join("late", "Late")


# ── The position limit ─────────────────────────────────────────────────────


class TestPositionLimit:
    def test_long_side_is_capped(self, run: Run):
        started(run, greedy=BUY_HARD)
        fast_forward(run, 40)
        for item, qty in run.participants["greedy"].pos.items():
            assert qty <= POSITION_LIMIT, item

    def test_short_side_is_capped(self, run: Run):
        started(run, greedy=SELL_HARD)
        fast_forward(run, 40)
        for item, qty in run.participants["greedy"].pos.items():
            assert qty >= -POSITION_LIMIT, item

    def test_resting_orders_count_toward_the_limit(self, run: Run):
        """Two 400-lot resting bids fit; the third would breach and is refused."""
        started(run, quoter=REST_ORDERS)
        fast_forward(run, 5)
        p = run.participants["quoter"]
        for symbol in engine.ITEM_SYMBOLS:
            resting_buy, _ = run._resting(run.books[symbol], "quoter")
            assert p.pos[symbol] + resting_buy <= POSITION_LIMIT

    def test_bots_respect_the_limit_too(self, run: Run):
        started(run, idle="def on_tick(ctx):\n    return []\n")
        fast_forward(run, 120)
        for bot_id in (engine.MM_BOT_ID, engine.FLOW_BOT_ID):
            for item, qty in run.participants[bot_id].pos.items():
                assert abs(qty) <= POSITION_LIMIT, (bot_id, item)

    def test_oversized_single_order_is_refused(self, run: Run):
        started(run, big=f"""
def on_tick(ctx):
    return [{{"item": "WIDGET", "side": "BUY", "price": 100000.0, "qty": {POSITION_LIMIT + 1}}}]
""")
        fast_forward(run, 2)
        p = run.participants["big"]
        assert p.orders_accepted == 0
        assert p.orders_rejected > 0
        assert "position limit" in p.last_reject


# ── Order validation ───────────────────────────────────────────────────────


class TestOrderValidation:
    @pytest.mark.parametrize("order, expected", [
        ('{"item": "NOPE", "side": "BUY", "price": 1.0, "qty": 1}', "unknown item"),
        ('{"item": "WIDGET", "side": "HOLD", "price": 1.0, "qty": 1}', "side must be"),
        ('{"item": "WIDGET", "side": "BUY", "price": 1.0, "qty": 0}', "positive"),
        ('{"item": "WIDGET", "side": "BUY", "price": -1.0, "qty": 1}', "between 0"),
        ('{"item": "WIDGET", "side": "BUY", "price": "abc", "qty": 1}', "must be a number"),
        ('{"item": "WIDGET", "side": "BUY", "price": 1.0, "qty": "lots"}', "whole number"),
        ('{"action": "self_destruct"}', "unknown action"),
    ])
    def test_bad_orders_are_rejected_with_a_reason(self, run: Run, order: str, expected: str):
        started(run, bad=f"def on_tick(ctx):\n    return [{order}]\n")
        fast_forward(run, 2)
        p = run.participants["bad"]
        assert p.orders_rejected > 0
        assert expected in p.last_reject

    def test_non_list_return_is_rejected(self, run: Run):
        started(run, bad='def on_tick(ctx):\n    return "buy everything"\n')
        fast_forward(run, 2)
        assert "list of order dicts" in run.participants["bad"].last_reject

    def test_returning_none_is_fine(self, run: Run):
        started(run, quiet="def on_tick(ctx):\n    return None\n")
        fast_forward(run, 5)
        assert run.participants["quiet"].orders_rejected == 0

    def test_cancel_all_pulls_resting_orders(self, run: Run):
        started(run, quoter="""
def on_tick(ctx):
    if ctx["tick"] == 0:
        return [{"item": "WIDGET", "side": "BUY", "price": 1.0, "qty": 10}]
    return [{"action": "cancel_all"}]
""")
        fast_forward(run, 1)
        assert run._resting(run.books["WIDGET"], "quoter")[0] == 10
        fast_forward(run, 1)
        assert run._resting(run.books["WIDGET"], "quoter")[0] == 0

    def test_market_order_leaves_no_resting_remainder(self, run: Run):
        started(run, taker="""
def on_tick(ctx):
    if ctx["tick"] == 0:
        return [{"item": "WIDGET", "side": "BUY", "qty": 900}]
    return []
""")
        fast_forward(run, 1)
        resting_buy, _ = run._resting(run.books["WIDGET"], "taker")
        assert resting_buy == 0

    def test_only_the_first_orders_of_a_tick_are_taken(self, run: Run):
        started(run, spammer="""
def on_tick(ctx):
    return [{"item": "WIDGET", "side": "BUY", "price": 1.0, "qty": 1} for i in range(50)]
""")
        fast_forward(run, 1)
        p = run.participants["spammer"]
        assert p.orders_accepted == engine.MAX_ORDERS_PER_TICK
        assert "only the first" in p.last_reject

    def test_a_giant_returned_list_is_refused_without_being_copied(self, run: Run):
        """``[x] * N`` dodges the line budget; the engine must not iterate it."""
        started(run, bomb="""
def on_tick(ctx):
    return [{"item": "WIDGET", "side": "BUY", "price": 1.0, "qty": 1}] * 5000000
""")
        fast_forward(run, 1)
        p = run.participants["bomb"]
        assert p.orders_accepted == 0
        assert "limit is" in p.last_reject


# ── Misbehaving strategies ─────────────────────────────────────────────────


class TestMisbehaviour:
    def test_a_raising_strategy_is_disqualified_not_fatal(self, run: Run):
        started(run, broken="def on_tick(ctx):\n    return 1 / 0\n", fine="def on_tick(ctx):\n    return []\n")
        fast_forward(run, engine.MAX_ERRORS + 5)
        assert run.participants["broken"].status == "disqualified"
        assert run.participants["fine"].status == "ready"
        assert run.status == "running"

    def test_a_looping_strategy_is_disqualified(self, run: Run):
        started(run, hog="def on_tick(ctx):\n    while True:\n        pass\n")
        fast_forward(run, engine.MAX_TIMEOUTS + 2)
        assert run.participants["hog"].status == "disqualified"

    def test_disqualification_pulls_resting_orders(self, run: Run):
        started(run, flaky="""
def on_tick(ctx):
    if ctx["tick"] == 0:
        return [{"item": "WIDGET", "side": "BUY", "price": 1.0, "qty": 10}]
    return 1 / 0
""")
        fast_forward(run, engine.MAX_ERRORS + 5)
        assert run.participants["flaky"].status == "disqualified"
        assert run._resting(run.books["WIDGET"], "flaky") == (0.0, 0.0)

    def test_a_strategy_that_fails_to_compile_at_start_is_marked(self, run: Run):
        run.join("u1", "Ada")
        run.set_code("u1", "def on_tick(ctx):\n    return []\n")
        # Smuggle past set_code to simulate code that only breaks at load time.
        run.participants["u1"].code = "x = 1 / 0\ndef on_tick(ctx):\n    return []\n"
        run.start()
        assert run.participants["u1"].status == "error"
        assert "ZeroDivisionError" in run.participants["u1"].error


# ── P&L and lifecycle ──────────────────────────────────────────────────────


class TestScoring:
    def test_pnl_is_cash_plus_position_at_fair_value(self, run: Run):
        started(run, u1="def on_tick(ctx):\n    return []\n")
        p = run.participants["u1"]
        p.cash = -1000.0
        p.pos["WIDGET"] = 10.0
        expected = -1000.0 + 10.0 * run.items["WIDGET"].fair
        assert p.pnl({s: i.fair for s, i in run.items.items()}) == pytest.approx(expected)

    def test_a_flat_strategy_ends_at_zero(self, run: Run):
        started(run, idle="def on_tick(ctx):\n    return []\n")
        fast_forward(run, engine.TOTAL_TICKS)
        row = next(r for r in run.leaderboard() if r["username"] == "idle")
        assert row["pnl"] == 0.0

    def test_a_buy_costs_the_taker_cash_and_gives_them_the_position(self, run: Run):
        started(run, taker="""
def on_tick(ctx):
    if ctx["tick"] == 0:
        return [{"item": "WIDGET", "side": "BUY", "qty": 20}]
    return []
""")
        fast_forward(run, 1)
        taker = run.participants["taker"]
        assert taker.pos["WIDGET"] == 20
        assert taker.cash < 0

    def test_cash_and_positions_are_conserved_across_the_market(self, run: Run):
        """Every fill has two sides, so the whole market always nets to zero."""
        started(run, a=engine.STARTER_CODE, b=BUY_HARD, c=SELL_HARD)
        fast_forward(run, 120)
        assert sum(p.cash for p in run.participants.values()) == pytest.approx(0, abs=1e-6)
        for symbol in engine.ITEM_SYMBOLS:
            assert sum(p.pos[symbol] for p in run.participants.values()) == pytest.approx(0)

    def test_run_finishes_on_the_clock(self, run: Run):
        started(run, idle="def on_tick(ctx):\n    return []\n")
        run.advance(now=run._t0 + engine.RUN_SECONDS + 1)
        while run.status == "running":
            run.advance(now=run._t0 + engine.RUN_SECONDS + 1)
        assert run.status == "finished"
        assert run.tick == engine.TOTAL_TICKS
        assert run.results

    def test_catch_up_is_bounded_per_call(self, run: Run):
        started(run, idle="def on_tick(ctx):\n    return []\n")
        executed = run.advance(now=run._t0 + engine.RUN_SECONDS)
        assert executed == engine.MAX_CATCHUP_TICKS

    def test_advance_does_nothing_before_start(self, run: Run):
        run.join("u1", "Ada")
        assert run.advance() == 0
        assert run.tick == 0

    def test_finish_is_idempotent(self, run: Run):
        started(run, idle="def on_tick(ctx):\n    return []\n")
        run.finish()
        first = run.finished_at
        run.finish()
        assert run.finished_at is first

    def test_leaderboard_is_ranked_by_pnl(self, run: Run):
        started(run, idle="def on_tick(ctx):\n    return []\n")
        fast_forward(run, 60)
        rows = run.leaderboard()
        assert [r["rank"] for r in rows] == list(range(1, len(rows) + 1))
        assert all(rows[i]["pnl"] >= rows[i + 1]["pnl"] for i in range(len(rows) - 1))


# ── The strategy-facing context ────────────────────────────────────────────


class TestContext:
    def test_context_exposes_the_documented_keys(self, run: Run):
        started(run, u1="def on_tick(ctx):\n    return []\n")
        ctx = run._context(run.participants["u1"])
        assert set(ctx) == {
            "tick", "seconds_left", "limit", "items", "market", "position",
            "open_orders", "cash", "pnl", "memory",
        }
        assert ctx["limit"] == POSITION_LIMIT
        assert ctx["items"] == engine.ITEM_SYMBOLS
        assert set(ctx["market"]["WIDGET"]) == {
            "bid", "ask", "bid_qty", "ask_qty", "mid", "spread", "last",
        }

    def test_on_start_orders_are_placed_before_the_first_tick(self, run: Run):
        started(run, opener="""
def on_start(ctx):
    return [{"item": "WIDGET", "side": "BUY", "price": 90.0, "qty": 50}]
def on_tick(ctx):
    return []
""")
        # start() has run but no tick has; the resting bid should already be there.
        assert run._resting(run.books["WIDGET"], "opener")[0] == 50

    def test_memory_survives_between_ticks(self, run: Run):
        started(run, counter="""
def on_tick(ctx):
    ctx["memory"]["n"] = ctx["memory"].get("n", 0) + 1
    return []
""")
        fast_forward(run, 5)
        assert run.participants["counter"].memory["n"] == 5

    def test_bots_quote_a_two_sided_market(self, run: Run):
        started(run, idle="def on_tick(ctx):\n    return []\n")
        fast_forward(run, 3)
        for row in run.market_view():
            assert row["bid"] is not None and row["ask"] is not None, row["item"]
            assert row["bid"] < row["ask"], row["item"]

    def test_fair_value_is_hidden_until_the_run_ends(self, run: Run):
        started(run, idle="def on_tick(ctx):\n    return []\n")
        fast_forward(run, 3)
        assert all("fair" not in row for row in run.market_view(reveal_fair=False))
        assert all("fair" in row for row in run.market_view(reveal_fair=True))

    def test_players_act_in_a_varied_order_across_ticks(self, run: Run):
        """No player should get a systematic first-mover edge from join order.

        Each strategy stamps the shared tape with its uid as it acts, so the
        first entry added each tick is whoever the engine called first.
        """
        code = """
def on_tick(ctx):
    return [{"item": "WIDGET", "side": "BUY", "qty": 1}]
"""
        started(run, aaa=code, bbb=code, ccc=code)
        first_actors = set()
        for _ in range(40):
            before = run.tick
            len_before = len(run.tape)
            run.advance(now=run._t0 + (before + 1) * engine.TICK_SECONDS)
            # The oldest tape entry added this tick names the earliest taker.
            added = list(run.tape)[: len(run.tape) - len_before]
            takers = [t["buyer"] for t in reversed(added) if t["buyer"] in ("aaa", "bbb", "ccc")]
            if takers:
                first_actors.add(takers[0])
        assert len(first_actors) > 1, "player call order never varied"

    def test_player_view_hides_other_players(self, run: Run):
        started(run, u1="def on_tick(ctx):\n    return []\n")
        assert run.player_view(engine.MM_BOT_ID) is None
        assert run.player_view("nobody") is None
        assert run.player_view("u1")["status"] == "ready"


# ── The starter template must work out of the box ──────────────────────────


class TestStarterCode:
    def test_starter_strategy_runs_a_full_contest_cleanly(self, run: Run):
        started(run, student=engine.STARTER_CODE)
        while run.status == "running":
            run.advance(now=run._t0 + engine.RUN_SECONDS + 1)
        p = run.participants["student"]
        assert p.status == "ready"
        assert p.orders_rejected == 0
        assert p.fills > 0

    def test_a_full_run_stays_fast_enough_for_the_event_loop(self, run: Run):
        """600 ticks with two strategies must not cost seconds of wall clock."""
        started(run, a=engine.STARTER_CODE, b=engine.STARTER_CODE)
        start = time.monotonic()
        while run.status == "running":
            run.advance(now=run._t0 + engine.RUN_SECONDS + 1)
        assert time.monotonic() - start < 5.0


# ── Registry ───────────────────────────────────────────────────────────────


class TestRegistry:
    def test_join_codes_are_unique(self):
        codes = {engine.create_run(f"r{i}", "c").join_code for i in range(20)}
        assert len(codes) == 20

    def test_find_by_code_is_case_insensitive(self, run: Run):
        assert engine.find_by_code(run.join_code.lower()) is run

    def test_finished_runs_are_not_joinable_by_code(self, run: Run):
        started(run, idle="def on_tick(ctx):\n    return []\n")
        run.finish()
        assert engine.find_by_code(run.join_code) is None

    def test_open_runs_lists_lobbies_and_live_runs(self, run: Run):
        assert run in engine.open_runs()
        started(run, idle="def on_tick(ctx):\n    return []\n")
        assert run in engine.open_runs()
        run.finish()
        assert run not in engine.open_runs()
