# app/state.py
from collections import defaultdict
from decimal import Decimal
from typing import Dict, List
import asyncio, datetime as dt

from app.order_book import OrderBook

# single source of truth for books & locks
books: Dict[str, OrderBook] = defaultdict(OrderBook)
locks: Dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


def list_user_orders(user_id: str) -> List[dict]:
    """Return all OPEN (remaining qty>0) orders for this user from all books."""
    out: List[dict] = []
    for sym, book in books.items():
        user_orders = book.list_open_for_user(user_id)
        for o in user_orders:
            o["symbol"] = sym
            out.append(o)

    # Sort by timestamp (newest first)
    out.sort(key=lambda r: r.get("ts", 0), reverse=True)
    return out


def cancel_order_by_id(order_id: str, user_id: str) -> bool:
    """Remove a single order from any book if it belongs to the user."""
    for book in books.values():
        if book.cancel(order_id, user_id):
            return True
    return False