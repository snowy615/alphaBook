"""Shared fixtures for OrderBook tests."""

import pytest
from decimal import Decimal
from app.order_book import Order, OrderBook


@pytest.fixture
def book() -> OrderBook:
    """Return a fresh, empty OrderBook."""
    return OrderBook()


def make_order(
    *,
    order_id: str = "o1",
    user_id: str = "user_A",
    side: str = "BUY",
    price: str = "100.00",
    qty: str = "10",
    ts: int | None = None,
) -> Order:
    """Helper to build an Order with sensible defaults.

    All numeric values are accepted as strings and converted to Decimal
    to match production usage.
    """
    kw = dict(
        id=order_id,
        user_id=user_id,
        side=side,
        price=Decimal(price),
        qty=Decimal(qty),
    )
    if ts is not None:
        kw["ts"] = ts
    return Order(**kw)


def make_buy(**kw) -> Order:
    """Shortcut for ``make_order(side="BUY", ...)``."""
    kw.setdefault("side", "BUY")
    return make_order(**kw)


def make_sell(**kw) -> Order:
    """Shortcut for ``make_order(side="SELL", ...)``."""
    kw.setdefault("side", "SELL")
    return make_order(**kw)
