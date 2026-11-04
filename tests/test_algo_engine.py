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
        # A call that makes no progress would otherwise spin here forever and
        # hang CI until the job timeout; fail with the reason instead.
        assert run.advance(now=run._t0 + target * engine.TICK_SECONDS), \
            f"advance() stalled at tick {run.tick} short of {target}"


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

    def test_a_whole_tick_is_never_lost_to_float_error(self, run: Run):
        """A clock started at 0.1 puts tick 4 at 3.9999999999999996 seconds."""
        run.join("u1", "Ada")
        run.start()
        run._t0 = 0.1
        assert (0.1 + 4) - 0.1 < 4
        run.advance(now=run._t0 + 4 * engine.TICK_SECONDS)
        assert run.tick == 4

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


# ── House bots ──────────────────────────────────────────────────────────────


class TestBots:
    def test_default_roster_is_a_maker_and_a_taker(self, run: Run):
        by_uid = {b.uid: b for b in run.bots}
        assert by_uid[engine.MM_BOT_ID].archetype == "market_maker"
        assert by_uid[engine.FLOW_BOT_ID].archetype == "taker"
        assert len(run.bots) == 2

    def test_add_bot_creates_a_bot_participant(self, run: Run):
        bot = run.add_bot("momentum", "good", activate_tick=0, name="Chaser")
        assert run.participants[bot.uid].is_bot
        assert run.participants[bot.uid].name == "Chaser"
        assert bot in run.bots

    @pytest.mark.parametrize("archetype, skill, match", [
        ("wizard", "normal", "unknown bot type"),
        ("taker", "godlike", "unknown skill level"),
    ])
    def test_add_bot_validates(self, run: Run, archetype, skill, match):
        with pytest.raises(ValueError, match=match):
            run.add_bot(archetype, skill)

    def test_bot_cap_is_enforced(self, run: Run):
        while len(run.bots) < engine.MAX_BOTS:
            run.add_bot("taker", "normal")
        with pytest.raises(ValueError, match="at most"):
            run.add_bot("taker", "normal")

    def test_scheduled_bot_does_not_trade_before_entry(self, run: Run):
        late = run.add_bot("market_maker", "cracked", activate_tick=30, name="Latecomer")
        started(run, "idle")
        heartbeat(run, 10)                       # before its entry
        assert run.participants[late.uid].fills == 0
        assert late.uid not in {r["user_id"] for r in run.leaderboard()}  # hidden until it enters
        heartbeat(run, 40)                       # past its entry
        assert late.uid in {r["user_id"] for r in run.leaderboard()}

    def test_bots_view_reports_entry_and_active_state(self, run: Run):
        run.add_bot("bull", "normal", activate_tick=120, name="Bull")
        started(run, "idle")
        heartbeat(run, 5)
        view = {b["name"]: b for b in run.bots_view()}
        assert view["Bull"]["enters_at"] == 120
        assert view["Bull"]["active"] is False
        assert view["Market Maker Bot"]["active"] is True

    def test_remove_pending_bot_but_not_an_active_one(self, run: Run):
        pending = run.add_bot("bear", "good", activate_tick=60, name="Bear")
        started(run, "idle")
        heartbeat(run, 5)
        run.remove_bot(pending.uid)              # not entered yet → ok
        assert pending.uid not in run.participants
        with pytest.raises(ValueError, match="already entered"):
            run.remove_bot(engine.MM_BOT_ID)     # live from the start

    @pytest.mark.parametrize("archetype", list(engine.BOT_ARCHETYPES))
    @pytest.mark.parametrize("skill", engine.SKILL_LEVELS)
    def test_every_archetype_and_skill_respects_the_limit(self, run: Run, archetype, skill):
        run.add_bot(archetype, skill, name=f"{archetype}-{skill}")
        started(run, "idle")
        heartbeat(run, 80)
        for p in run.participants.values():
            for item, qty in p.pos.items():
                assert abs(qty) <= POSITION_LIMIT, (p.name, item, qty)


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


# ── Fair value stays hidden while live ──────────────────────────────────────


class TestHiddenFair:
    def test_live_pnl_does_not_move_with_the_hidden_fair_value(self, run: Run):
        started(run, "u1")
        submit(run, "u1", {"item": "WIDGET", "side": "BUY", "qty": 20})
        before = run.player_view("u1")["pnl"]
        run.items["WIDGET"].fair *= 1.5            # a big hidden move, nothing in the book changes
        assert run.player_view("u1")["pnl"] == before
        assert run.player_view("u1")["pnl_basis"] == "market"

    def test_fair_cannot_be_backed_out_of_any_live_figure(self, run: Run):
        started(run, "u1")
        submit(run, "u1", {"item": "GADGET", "side": "BUY", "qty": 30})
        p = run.participants["u1"]
        live = {
            "view": run.player_view("u1")["pnl"],
            "board": next(r["pnl"] for r in run.leaderboard() if r["user_id"] == "u1"),
            "orders": submit(run, "u1", {"item": "WIDGET", "side": "BUY", "price": 1, "qty": 1})["pnl"],
        }
        fair = run.items["GADGET"].fair
        for name, pnl in live.items():
            implied = (pnl - p.cash) / p.pos["GADGET"]
            assert implied != pytest.approx(fair, abs=1e-6), name
        for b in run.bots_view():
            part = run.participants[b["uid"]]
            assert b["pnl"] == pytest.approx(part.pnl(run.marks_for(b["uid"])), abs=0.01)

    def test_your_own_quotes_do_not_move_your_mark(self, run: Run):
        started(run, "u1")
        submit(run, "u1", {"item": "WIDGET", "side": "BUY", "qty": 10})
        mine, theirs = run.marks_for("u1")["WIDGET"], run.marks_for("other")["WIDGET"]
        bid, ask = run._touch(run.books["WIDGET"])
        # A new best bid just under the offer: it rests and drags the book's mid up...
        submit(run, "u1", {"item": "WIDGET", "side": "BUY", "price": round(ask - 0.01, 2), "qty": 1})
        assert run.marks_for("other")["WIDGET"] > theirs
        # ...but not u1's own mark, so u1 can't talk up their own position.
        assert run.marks_for("u1")["WIDGET"] == pytest.approx(mine)

    def test_fair_is_used_once_the_run_is_over(self, run: Run):
        started(run, "u1")
        submit(run, "u1", {"item": "WIDGET", "side": "BUY", "qty": 20})
        heartbeat(run, engine.TOTAL_TICKS)
        p = run.participants["u1"]
        row = next(r for r in run.results if r["user_id"] == "u1")
        assert row["pnl_basis"] == "fair"
        assert row["pnl"] == pytest.approx(p.pnl({s: i.fair for s, i in run.items.items()}), abs=0.01)


# ── Strategy, not just P&L ──────────────────────────────────────────────────


class TestStrategy:
    def test_settled_pnl_is_exactly_edge_plus_direction(self, run: Run):
        started(run, "a", "b")
        run.add_bot("market_maker", "good", 0)
        for i in range(120):
            submit(run, "a", {"item": "WIDGET", "side": "BUY" if i % 3 else "SELL", "qty": 15})
            submit(run, "b", {"item": "DOODAD", "side": "SELL", "qty": 10})
            heartbeat(run, 1)
        heartbeat(run, engine.TOTAL_TICKS)
        fair = {s: i.fair for s, i in run.items.items()}
        for p in run.participants.values():
            assert p.pnl(fair) == pytest.approx(p.edge + p.inventory_pnl, abs=1e-6), p.name

    def test_maker_and_taker_are_told_apart(self, run: Run):
        started(run, "maker", "taker")
        bid, ask = run._touch(run.books["GIZMO"])
        inside = round(ask - 0.01, 2)                # a new best offer, inside the spread: it rests
        submit(run, "maker", {"item": "GIZMO", "side": "SELL", "price": inside, "qty": 5})
        submit(run, "taker", {"item": "GIZMO", "side": "BUY", "qty": 5})           # lifts it
        maker, taker = run.participants["maker"], run.participants["taker"]
        assert maker.maker_volume == 5 and maker.taker_volume == 0
        assert taker.taker_volume == 5 and taker.maker_volume == 0

    def test_the_fair_based_parts_wait_for_the_end(self, run: Run):
        started(run, "u1")
        submit(run, "u1", {"item": "WIDGET", "side": "BUY", "qty": 20})
        heartbeat(run, 10)
        live = run.player_view("u1")["strategy"]
        assert {"edge", "direction", "markout_per_share", "direction_hit", "style"}.isdisjoint(live)
        assert live["maker_share"] == 0.0 and live["avg_inventory"] > 0
        heartbeat(run, engine.TOTAL_TICKS)
        done = next(r for r in run.results if r["user_id"] == "u1")["strategy"]
        assert {"edge", "direction", "edge_per_share", "markout_per_share", "direction_hit", "style"} <= set(done)
        assert done["edge"] + done["direction"] == pytest.approx(
            next(r for r in run.results if r["user_id"] == "u1")["pnl"], abs=0.02)

    def test_markouts_settle_a_few_ticks_later(self, run: Run):
        started(run, "u1")
        submit(run, "u1", {"item": "DOODAD", "side": "BUY", "qty": 10})
        p = run.participants["u1"]
        assert p.markout_volume == 0
        heartbeat(run, engine.MARKOUT_TICKS + 1)
        assert p.markout_volume == 10

    def test_two_sided_quoting_is_counted(self, run: Run):
        started(run, "mm")
        submit(run, "mm", {"item": "WIDGET", "side": "BUY", "price": 50, "qty": 1},
                          {"item": "WIDGET", "side": "SELL", "price": 200, "qty": 1})
        p = run.participants["mm"]
        quoted, samples = p.quoted, p.samples
        heartbeat(run, 4)
        assert (p.quoted - quoted, p.samples - samples) == (4, 4)    # one item of four, every tick
        assert 0 < run.strategy(p, reveal=False)["quote_uptime"] <= 0.25

    @pytest.mark.parametrize("st,volume,key", [
        ({}, 0, "idle"),
        ({"maker_share": 0.9, "quote_uptime": 0.6, "edge": 50, "direction": -10}, 100, "market_maker"),
        ({"maker_share": 0.2, "quote_uptime": 0.0, "edge": 5, "direction": 400, "avg_inventory": 0.5}, 100, "directional"),
        ({"maker_share": 0.1, "quote_uptime": 0.0, "edge": 80, "direction": 5, "avg_inventory": 0.02}, 100, "taker"),
        ({"maker_share": 0.5, "quote_uptime": 0.1, "edge": 20, "direction": 5, "avg_inventory": 0.02}, 100, "mixed")])
    def test_styles(self, st, volume, key):
        assert engine.style_of(st, volume)["key"] == key


# ── Other players see a style, not figures they could copy ──────────────────


class TestPrivacy:
    def _finished(self, run):
        started(run, "a", "b")
        for i in range(30):
            submit(run, "a", {"item": "WIDGET", "side": "BUY" if i % 2 else "SELL", "qty": 10})
            submit(run, "b", {"item": "GADGET", "side": "BUY", "qty": 5})
            heartbeat(run, 1)
        heartbeat(run, engine.TOTAL_TICKS)
        return run

    def test_your_own_row_and_the_bots_are_in_full(self, run: Run):
        rows = engine.view_for(self._finished(run).results, "a")
        mine = next(r for r in rows if r["user_id"] == "a")
        assert isinstance(mine["strategy"]["edge"], float) and "positions" in mine
        bot = next(r for r in rows if r["is_bot"])
        assert isinstance(bot["strategy"]["edge"], float)

    def test_others_get_a_style_and_words_only(self, run: Run):
        rows = engine.view_for(self._finished(run).results, "a")
        other = next(r for r in rows if r["user_id"] == "b")
        assert set(other) <= {"user_id", "username", "is_bot", "pnl", "pnl_basis", "fills", "rank", "connected", "strategy"}
        st = other["strategy"]
        assert st["style"]["label"]
        assert all(isinstance(v, str) for k, v in st.items() if k != "style")      # no figures at all
        assert st["edge"] in ("positive", "negative", "flat") and st["made_on"] in ("pricing", "direction")

    def test_live_too(self, run: Run):
        started(run, "a", "b")
        submit(run, "b", {"item": "GADGET", "side": "BUY", "qty": 5})
        heartbeat(run, 3)
        other = next(r for r in engine.view_for(run.leaderboard(), "a") if r["user_id"] == "b")
        assert "positions" not in other and "quote_uptime" not in other["strategy"]
        assert other["strategy"].get("aggression") == "mostly aggressive"

    def test_the_tape_names_only_you_and_the_bots(self, run: Run):
        from app.market_sim_py import _tape_for
        started(run, "a", "b")
        submit(run, "a", {"item": "WIDGET", "side": "BUY", "qty": 5})
        submit(run, "b", {"item": "GADGET", "side": "BUY", "qty": 5})
        names = {n for t in _tape_for(run, "a") for n in (t["buyer"], t["seller"])}
        assert "a" in names and "b" not in names and "a player" in names
