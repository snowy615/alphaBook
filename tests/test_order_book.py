"""Comprehensive tests for app.order_book.OrderBook."""

from decimal import Decimal

import pytest

from app.order_book import Order, OrderBook
from tests.conftest import make_buy, make_sell, make_order


# ── Adding orders that rest (no match) ─────────────────────────────────────


class TestAddBuyRests:
    """A buy order placed below the best ask (or into an empty book) should rest."""

    def test_buy_rests_in_empty_book(self, book: OrderBook):
        buy = make_buy(order_id="b1", price="100.00", qty="5")
        trades = book.add(buy)

        assert trades == []
        assert book.has_active_order("b1")

    def test_buy_rests_below_best_ask(self, book: OrderBook):
        book.add(make_sell(order_id="s1", price="105.00", qty="5"))
        buy = make_buy(order_id="b1", price="100.00", qty="5")
        trades = book.add(buy)

        assert trades == []
        assert book.has_active_order("b1")
        assert book.has_active_order("s1")


class TestAddSellRests:
    """A sell order placed above the best bid (or into an empty book) should rest."""

    def test_sell_rests_in_empty_book(self, book: OrderBook):
        sell = make_sell(order_id="s1", price="100.00", qty="5")
        trades = book.add(sell)

        assert trades == []
        assert book.has_active_order("s1")

    def test_sell_rests_above_best_bid(self, book: OrderBook):
        book.add(make_buy(order_id="b1", price="95.00", qty="5"))
        sell = make_sell(order_id="s1", price="100.00", qty="5")
        trades = book.add(sell)

        assert trades == []
        assert book.has_active_order("s1")
        assert book.has_active_order("b1")


# ── Matching / crossing the spread ──────────────────────────────────────────


class TestBuyCrossesSpread:
    """An aggressive buy that meets or exceeds the best ask should trade."""

    def test_full_fill_exact_price(self, book: OrderBook):
        book.add(make_sell(order_id="s1", user_id="seller", price="100.00", qty="5"))
        buy = make_buy(order_id="b1", user_id="buyer", price="100.00", qty="5")
        trades = book.add(buy)

        assert len(trades) == 1
        t = trades[0]
        assert t["price"] == Decimal("100.00")
        assert t["qty"] == Decimal("5")
        assert t["buyer_id"] == "buyer"
        assert t["seller_id"] == "seller"
        # Both orders fully filled → removed from book
        assert not book.has_active_order("s1")
        assert not book.has_active_order("b1")

    def test_buy_price_above_ask_trades_at_ask_price(self, book: OrderBook):
        """Trade should happen at the resting (ask) price, not the aggressive price."""
        book.add(make_sell(order_id="s1", user_id="seller", price="100.00", qty="5"))
        buy = make_buy(order_id="b1", user_id="buyer", price="105.00", qty="5")
        trades = book.add(buy)

        assert trades[0]["price"] == Decimal("100.00")


class TestSellCrossesSpread:
    """An aggressive sell that meets or goes below the best bid should trade."""

    def test_full_fill_exact_price(self, book: OrderBook):
        book.add(make_buy(order_id="b1", user_id="buyer", price="100.00", qty="5"))
        sell = make_sell(order_id="s1", user_id="seller", price="100.00", qty="5")
        trades = book.add(sell)

        assert len(trades) == 1
        t = trades[0]
        assert t["price"] == Decimal("100.00")
        assert t["qty"] == Decimal("5")
        assert t["buyer_id"] == "buyer"
        assert t["seller_id"] == "seller"
        assert not book.has_active_order("b1")
        assert not book.has_active_order("s1")

    def test_sell_price_below_bid_trades_at_bid_price(self, book: OrderBook):
        """Trade should happen at the resting (bid) price, not the aggressive price."""
        book.add(make_buy(order_id="b1", user_id="buyer", price="105.00", qty="5"))
        sell = make_sell(order_id="s1", user_id="seller", price="100.00", qty="5")
        trades = book.add(sell)

        assert trades[0]["price"] == Decimal("105.00")


# ── Partial fills ───────────────────────────────────────────────────────────


class TestPartialFills:
    def test_aggressive_buy_partially_fills_then_rests(self, book: OrderBook):
        book.add(make_sell(order_id="s1", user_id="seller", price="100.00", qty="3"))
        buy = make_buy(order_id="b1", user_id="buyer", price="100.00", qty="5")
        trades = book.add(buy)

        assert len(trades) == 1
        assert trades[0]["qty"] == Decimal("3")
        # remaining 2 rests on the bid side
        assert book.has_active_order("b1")
        assert not book.has_active_order("s1")

        snap = book.snapshot()
        assert len(snap["bids"]) == 1
        assert snap["bids"][0]["qty"] == "2"

    def test_aggressive_sell_partially_fills_then_rests(self, book: OrderBook):
        book.add(make_buy(order_id="b1", user_id="buyer", price="100.00", qty="3"))
        sell = make_sell(order_id="s1", user_id="seller", price="100.00", qty="5")
        trades = book.add(sell)

        assert len(trades) == 1
        assert trades[0]["qty"] == Decimal("3")
        assert book.has_active_order("s1")
        assert not book.has_active_order("b1")

        snap = book.snapshot()
        assert len(snap["asks"]) == 1
        assert snap["asks"][0]["qty"] == "2"

    def test_resting_order_partially_filled_keeps_remainder(self, book: OrderBook):
        book.add(make_sell(order_id="s1", user_id="seller", price="100.00", qty="10"))
        buy = make_buy(order_id="b1", user_id="buyer", price="100.00", qty="4")
        trades = book.add(buy)

        assert trades[0]["qty"] == Decimal("4")
        assert book.has_active_order("s1")
        snap = book.snapshot()
        assert snap["asks"][0]["qty"] == "6"


# ── Price-time priority ────────────────────────────────────────────────────


class TestPriceTimePriority:
    def test_earlier_order_at_same_price_fills_first(self, book: OrderBook):
        book.add(make_sell(order_id="s1", user_id="seller1", price="100.00", qty="5", ts=1000))
        book.add(make_sell(order_id="s2", user_id="seller2", price="100.00", qty="5", ts=2000))

        buy = make_buy(order_id="b1", user_id="buyer", price="100.00", qty="5")
        trades = book.add(buy)

        assert len(trades) == 1
        assert trades[0]["seller_id"] == "seller1"  # earlier order matched first
        assert book.has_active_order("s2")
        assert not book.has_active_order("s1")

    def test_better_price_fills_before_earlier_time(self, book: OrderBook):
        """Price priority trumps time priority."""
        book.add(make_sell(order_id="s1", user_id="seller1", price="102.00", qty="5", ts=1000))
        book.add(make_sell(order_id="s2", user_id="seller2", price="100.00", qty="5", ts=2000))

        buy = make_buy(order_id="b1", user_id="buyer", price="102.00", qty="5")
        trades = book.add(buy)

        assert len(trades) == 1
        assert trades[0]["price"] == Decimal("100.00")
        assert trades[0]["seller_id"] == "seller2"


# ── Multiple fills from one aggressive order ────────────────────────────────


class TestMultipleFills:
    def test_buy_sweeps_multiple_ask_levels(self, book: OrderBook):
        book.add(make_sell(order_id="s1", user_id="seller1", price="100.00", qty="3"))
        book.add(make_sell(order_id="s2", user_id="seller2", price="101.00", qty="3"))
        book.add(make_sell(order_id="s3", user_id="seller3", price="102.00", qty="3"))

        buy = make_buy(order_id="b1", user_id="buyer", price="102.00", qty="7")
        trades = book.add(buy)

        assert len(trades) == 3
        assert trades[0]["price"] == Decimal("100.00")
        assert trades[0]["qty"] == Decimal("3")
        assert trades[1]["price"] == Decimal("101.00")
        assert trades[1]["qty"] == Decimal("3")
        assert trades[2]["price"] == Decimal("102.00")
        assert trades[2]["qty"] == Decimal("1")

        # s3 should still have qty 2 resting
        assert book.has_active_order("s3")
        snap = book.snapshot()
        assert snap["asks"][0]["qty"] == "2"

    def test_sell_sweeps_multiple_bid_levels(self, book: OrderBook):
        book.add(make_buy(order_id="b1", user_id="buyer1", price="102.00", qty="3"))
        book.add(make_buy(order_id="b2", user_id="buyer2", price="101.00", qty="3"))
        book.add(make_buy(order_id="b3", user_id="buyer3", price="100.00", qty="3"))

        sell = make_sell(order_id="s1", user_id="seller", price="100.00", qty="7")
        trades = book.add(sell)

        assert len(trades) == 3
        # Best bid first (102), then 101, then 100
        assert trades[0]["price"] == Decimal("102.00")
        assert trades[0]["qty"] == Decimal("3")
        assert trades[1]["price"] == Decimal("101.00")
        assert trades[1]["qty"] == Decimal("3")
        assert trades[2]["price"] == Decimal("100.00")
        assert trades[2]["qty"] == Decimal("1")

        assert book.has_active_order("b3")
        snap = book.snapshot()
        assert snap["bids"][0]["qty"] == "2"


# ── Cancel orders ───────────────────────────────────────────────────────────


class TestCancelOrder:
    def test_cancel_buy_by_id(self, book: OrderBook):
        book.add(make_buy(order_id="b1", user_id="u1", price="100.00", qty="5"))
        assert book.cancel("b1", "u1") is True
        assert not book.has_active_order("b1")

    def test_cancel_sell_by_id(self, book: OrderBook):
        book.add(make_sell(order_id="s1", user_id="u1", price="100.00", qty="5"))
        assert book.cancel("s1", "u1") is True
        assert not book.has_active_order("s1")

    def test_cancel_fails_for_wrong_user_id(self, book: OrderBook):
        book.add(make_buy(order_id="b1", user_id="u1", price="100.00", qty="5"))
        assert book.cancel("b1", "u_wrong") is False
        assert book.has_active_order("b1")

    def test_cancel_returns_false_for_nonexistent_order(self, book: OrderBook):
        assert book.cancel("no_such_id", "u1") is False

    def test_cancel_cleans_up_empty_price_level(self, book: OrderBook):
        book.add(make_buy(order_id="b1", user_id="u1", price="100.00", qty="5"))
        book.cancel("b1", "u1")
        snap = book.snapshot()
        assert snap["bids"] == []


# ── cancel_all_for_user ─────────────────────────────────────────────────────


class TestCancelAllForUser:
    def test_removes_all_orders_for_user(self, book: OrderBook):
        book.add(make_buy(order_id="b1", user_id="target", price="99.00", qty="5"))
        book.add(make_sell(order_id="s1", user_id="target", price="105.00", qty="3"))
        book.add(make_buy(order_id="b2", user_id="other", price="98.00", qty="2"))

        removed = book.cancel_all_for_user("target")

        assert removed == 2
        assert not book.has_active_order("b1")
        assert not book.has_active_order("s1")
        assert book.has_active_order("b2")

    def test_returns_zero_when_no_orders(self, book: OrderBook):
        assert book.cancel_all_for_user("ghost") == 0


# ── clear_all_orders ────────────────────────────────────────────────────────


class TestClearAllOrders:
    def test_clears_entire_book(self, book: OrderBook):
        book.add(make_buy(order_id="b1", price="100.00", qty="5"))
        book.add(make_sell(order_id="s1", price="105.00", qty="5"))

        book.clear_all_orders()

        snap = book.snapshot()
        assert snap == {"bids": [], "asks": []}
        assert not book.has_active_order("b1")
        assert not book.has_active_order("s1")


# ── has_active_order ────────────────────────────────────────────────────────


class TestHasActiveOrder:
    def test_returns_true_for_resting_buy(self, book: OrderBook):
        book.add(make_buy(order_id="b1", price="100.00", qty="5"))
        assert book.has_active_order("b1") is True

    def test_returns_true_for_resting_sell(self, book: OrderBook):
        book.add(make_sell(order_id="s1", price="100.00", qty="5"))
        assert book.has_active_order("s1") is True

    def test_returns_false_for_unknown_id(self, book: OrderBook):
        assert book.has_active_order("xyz") is False

    def test_returns_false_after_full_fill(self, book: OrderBook):
        book.add(make_sell(order_id="s1", user_id="seller", price="100.00", qty="5"))
        book.add(make_buy(order_id="b1", user_id="buyer", price="100.00", qty="5"))
        assert book.has_active_order("s1") is False


# ── list_open_for_user ──────────────────────────────────────────────────────


class TestListOpenForUser:
    def test_returns_correct_orders(self, book: OrderBook):
        book.add(make_buy(order_id="b1", user_id="alice", price="99.00", qty="5", ts=1000))
        book.add(make_sell(order_id="s1", user_id="alice", price="105.00", qty="3", ts=2000))
        book.add(make_buy(order_id="b2", user_id="bob", price="98.00", qty="2"))

        orders = book.list_open_for_user("alice")

        assert len(orders) == 2
        ids = {o["id"] for o in orders}
        assert ids == {"b1", "s1"}

    def test_returns_empty_list_for_unknown_user(self, book: OrderBook):
        assert book.list_open_for_user("nobody") == []

    def test_orders_sorted_newest_first(self, book: OrderBook):
        book.add(make_buy(order_id="b1", user_id="alice", price="99.00", qty="5", ts=1000))
        book.add(make_sell(order_id="s1", user_id="alice", price="105.00", qty="3", ts=2000))

        orders = book.list_open_for_user("alice")
        assert orders[0]["id"] == "s1"  # ts=2000 first
        assert orders[1]["id"] == "b1"  # ts=1000 second

    def test_filled_qty_reflects_partial_fill(self, book: OrderBook):
        book.add(make_sell(order_id="s1", user_id="seller", price="100.00", qty="10"))
        book.add(make_buy(order_id="b1", user_id="buyer", price="100.00", qty="4"))

        orders = book.list_open_for_user("seller")
        assert len(orders) == 1
        o = orders[0]
        assert o["qty"] == Decimal("10")
        assert o["filled_qty"] == Decimal("4")
        assert o["status"] == "OPEN"


# ── Snapshot ────────────────────────────────────────────────────────────────


class TestSnapshot:
    def test_empty_book_snapshot(self, book: OrderBook):
        snap = book.snapshot()
        assert snap == {"bids": [], "asks": []}

    def test_snapshot_returns_correct_levels(self, book: OrderBook):
        book.add(make_buy(order_id="b1", price="100.00", qty="5"))
        book.add(make_buy(order_id="b2", price="99.00", qty="3"))
        book.add(make_sell(order_id="s1", price="105.00", qty="4"))
        book.add(make_sell(order_id="s2", price="106.00", qty="2"))

        snap = book.snapshot()

        assert len(snap["bids"]) == 2
        assert len(snap["asks"]) == 2
        # bids sorted high→low
        assert snap["bids"][0]["px"] == "100.00"
        assert snap["bids"][1]["px"] == "99.00"
        # asks sorted low→high
        assert snap["asks"][0]["px"] == "105.00"
        assert snap["asks"][1]["px"] == "106.00"

    def test_snapshot_aggregates_qty_at_same_price(self, book: OrderBook):
        book.add(make_buy(order_id="b1", user_id="u1", price="100.00", qty="5"))
        book.add(make_buy(order_id="b2", user_id="u2", price="100.00", qty="3"))

        snap = book.snapshot()
        assert len(snap["bids"]) == 1
        assert snap["bids"][0]["qty"] == "8"

    def test_snapshot_respects_depth_limit(self, book: OrderBook):
        for i in range(15):
            book.add(make_buy(order_id=f"b{i}", price=str(80 + i), qty="1"))

        snap = book.snapshot(depth=5)
        assert len(snap["bids"]) == 5
        # Should be the 5 highest prices
        assert snap["bids"][0]["px"] == "94"

    def test_snapshot_values_are_strings(self, book: OrderBook):
        book.add(make_buy(order_id="b1", price="100.50", qty="3.5"))
        snap = book.snapshot()
        assert isinstance(snap["bids"][0]["px"], str)
        assert isinstance(snap["bids"][0]["qty"], str)


# ── Self-trade scenario ────────────────────────────────────────────────────


class TestSelfTrade:
    def test_same_user_both_sides_still_matches(self, book: OrderBook):
        """Current logic does NOT prevent self-trades."""
        book.add(make_sell(order_id="s1", user_id="alice", price="100.00", qty="5"))
        buy = make_buy(order_id="b1", user_id="alice", price="100.00", qty="5")
        trades = book.add(buy)

        assert len(trades) == 1
        assert trades[0]["buyer_id"] == "alice"
        assert trades[0]["seller_id"] == "alice"


# ── Decimal precision ──────────────────────────────────────────────────────


class TestDecimalPrecision:
    def test_prices_maintain_decimal_precision(self, book: OrderBook):
        """Decimal("0.1") + Decimal("0.2") == Decimal("0.3") – no float errors."""
        price = Decimal("0.1") + Decimal("0.2")
        book.add(make_sell(order_id="s1", user_id="seller", price=str(price), qty="1"))
        buy = make_buy(order_id="b1", user_id="buyer", price="0.3", qty="1")
        trades = book.add(buy)

        assert len(trades) == 1
        assert trades[0]["price"] == Decimal("0.3")

    def test_small_qty_precision(self, book: OrderBook):
        book.add(make_sell(order_id="s1", user_id="seller", price="100.00", qty="0.00000001"))
        buy = make_buy(order_id="b1", user_id="buyer", price="100.00", qty="0.00000001")
        trades = book.add(buy)

        assert len(trades) == 1
        assert trades[0]["qty"] == Decimal("0.00000001")

    def test_orig_qty_preserved_on_order(self, book: OrderBook):
        order = make_buy(order_id="b1", price="100.00", qty="7.5")
        assert order.orig_qty == Decimal("7.5")
        assert order.qty == Decimal("7.5")
