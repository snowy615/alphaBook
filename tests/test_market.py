"""The Market Simulation's prices and its market-maker bot: prices follow
the real quote, and the bot never leaves the book crossed."""
import asyncio
import datetime as dt
from decimal import Decimal

import pytest

from app import market_data as md
from app import market_maker as mm
from app.order_book import Order, OrderBook


def _crossed(book):
    bb, ba = book._best_bid(), book._best_ask()
    return bb is not None and ba is not None and bb >= ba


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    for d in (mm._bot_bids, mm._bot_asks, mm._quote_center, mm._ladder_center,
              md._official, md._official_info, md._synth, md._mid_hint):
        d.clear()
    yield


class TestNoCrossedBook:
    def test_a_big_jump_requotes_the_whole_ladder_without_crossing(self):
        book = OrderBook()
        mm._update_quotes("MSFT", book, 422.0)
        assert not _crossed(book)
        for mid in (513.0, 430.0, 431.0, 600.0, 300.0):
            mm._update_quotes("MSFT", book, mid)
            assert not _crossed(book), mid
            bb, ba = book._best_bid(), book._best_ask()
            assert float(bb) < mid < float(ba)

    def test_small_moves_never_cross_either(self):
        book = OrderBook()
        mid = 300.0
        for i in range(400):
            mid *= 1 + ((i * 37) % 11 - 5) / 10_000      # wandering a few bps a tick
            mm._update_quotes("AAPL", book, mid)
            mm._uncross(book)
            assert not _crossed(book)

    def test_the_bot_never_quotes_through_someones_resting_order(self):
        book = OrderBook()
        # Someone's offer sits below where the bot would bid.
        book.add(Order(id="u1", user_id="alice", side="SELL", price=Decimal("299.90"), qty=Decimal("5")))
        mm._update_quotes("AAPL", book, 300.0)
        assert not _crossed(book)
        assert book._best_bid() < Decimal("299.90")

    def test_uncross_pulls_stale_bot_orders(self):
        book = OrderBook()
        for oid, side, px in (("b", "BUY", "509.21"), ("a", "SELL", "501.33")):
            q = book.bids if side == "BUY" else book.asks
            q.setdefault(Decimal(px), __import__("collections").deque()).append(
                Order(id=oid, user_id=mm.BOT_USER_ID, side=side, price=Decimal(px), qty=Decimal("10")))
        assert _crossed(book)
        assert mm._uncross(book) >= 1 and not _crossed(book)

    def test_the_center_snaps_to_a_far_real_price(self, monkeypatch):
        monkeypatch.setattr(mm, "get_official_price", lambda s: 513.0)
        monkeypatch.setattr(mm, "get_ref_price", lambda s: 513.0)
        mm._quote_center["MSFT"] = 422.0
        assert mm._update_center("MSFT") == 513.0


class TestPrices:
    def test_a_far_real_price_is_jumped_to(self):
        md._synth["AAPL"] = 300.0
        md._accept("AAPL", 327.98, 1_790_000_000, "finnhub")
        assert md.get_ref_price("AAPL") == 327.98
        assert md.quote_status("AAPL")["source"] == "finnhub"

    def test_between_quotes_it_stays_close_to_the_real_price(self, monkeypatch):
        monkeypatch.setattr(md, "_fast_tick_sec", 0)
        monkeypatch.setattr(md, "us_market_open", lambda now=None: True)
        md._accept("AAPL", 300.0, 1_790_000_000, "finnhub")
        md.set_hint_mid("AAPL", 330.0)          # the room pushing hard
        for _ in range(500):
            md._last_synth_step["AAPL"] = 0
            md._synth_step("AAPL")
        lean = abs(md.get_ref_price("AAPL") - 300.0) / 300.0 * 10_000
        assert lean <= md.MID_NUDGE_MAX_BP + 3       # can lean a little, never wander

    def test_no_drift_while_the_market_is_closed(self, monkeypatch):
        monkeypatch.setattr(md, "_fast_tick_sec", 0)
        monkeypatch.setattr(md, "us_market_open", lambda now=None: False)
        md._accept("TSLA", 355.77, 1_790_000_000, "finnhub")
        for _ in range(50):
            md._last_synth_step["TSLA"] = 0
            md._synth_step("TSLA")
        assert md.get_ref_price("TSLA") == pytest.approx(355.77)

    def test_market_hours(self):
        ny = lambda *a: dt.datetime(*a, tzinfo=md._NEW_YORK)
        assert md.us_market_open(ny(2026, 10, 1, 10, 0))        # Thursday morning
        assert not md.us_market_open(ny(2026, 10, 1, 9, 29))
        assert not md.us_market_open(ny(2026, 10, 1, 16, 0))
        assert not md.us_market_open(ny(2026, 10, 3, 12, 0))    # Saturday

    def test_finnhub_quote_is_read(self, monkeypatch):
        monkeypatch.setattr(md, "FINNHUB_API_KEY", "test-key")
        seen = {}

        class Resp:
            status_code = 200
            def json(self):
                return {"c": 327.98, "t": 1_790_000_000, "pc": 325.0}

        class Client:
            async def get(self, url, params=None, headers=None):
                seen.update(url=url, params=params, headers=headers)
                return Resp()

        got = asyncio.run(md._fetch_finnhub(Client(), "AAPL"))
        assert got == (327.98, 1_790_000_000)
        assert seen["headers"]["X-Finnhub-Token"] == "test-key"     # in a header, not the URL
        assert "test-key" not in seen["url"]

    def test_no_key_means_no_finnhub_call(self, monkeypatch):
        monkeypatch.setattr(md, "FINNHUB_API_KEY", "")
        assert asyncio.run(md._fetch_finnhub(object(), "AAPL")) is None

    def test_a_newer_quote_from_another_instance_is_picked_up(self, monkeypatch):
        from tests.test_applications import _FakeDB
        fake = _FakeDB()
        fake.collections[md.QUOTE_CACHE] = {"META": {
            "price": 728.28, "source": "finnhub", "quote_time": "2026-10-01T17:02:00+00:00"}}
        from app import db as db_module
        monkeypatch.setattr(db_module, "db", fake)
        md._last_cache_read.clear()
        md._synth["META"] = 614.0
        asyncio.run(md._read_cache("META"))
        assert md.get_official_price("META") == 728.28 and md.get_ref_price("META") == 728.28
