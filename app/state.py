# app/state.py
from collections import defaultdict
from decimal import Decimal
from typing import Dict, List
import asyncio, datetime as dt

from app.order_book import OrderBook

# single source of truth for books & locks
books: Dict[str, OrderBook] = defaultdict(OrderBook)
locks: Dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

def list_user_orders(user_id: int) -> List[dict]:
    """Return all OPEN (remaining qty>0) orders for this user from all books."""
    uid = str(user_id)
    out: List[dict] = []
    for sym, book in books.items():
        for side, ladder in (("BUY", book.bids), ("SELL", book.asks)):
            for px, dq in ladder.items():
                for o in list(dq):
                    if o.user_id == uid and o.qty > 0:
                        filled = (o.orig_qty - o.qty) if hasattr(o, "orig_qty") else Decimal("0")
                        out.append({
                            "id": o.id,
                            "symbol": sym,
                            "side": side,
                            "qty": float(o.orig_qty) if hasattr(o, "orig_qty") else float(o.qty),
                            "filled_qty": float(filled),
                            "price": float(o.price),
                            "status": "OPEN",
                            "created_at": dt.datetime.fromtimestamp(getattr(o, "created_at", 0)).isoformat(),
                        })
    # newest first is usually friendlier
    out.sort(key=lambda r: r["created_at"], reverse=True)
    return out

def cancel_order_by_id(user_id: int, order_id: str) -> bool:
    """Remove a single order from any book if it belongs to the user."""
    uid = str(user_id)
    for sym, book in books.items():
        for ladder in (book.bids, book.asks):
            for px, dq in list(ladder.items()):
                for o in list(dq):
                    if o.id == order_id and o.user_id == uid:
                        try:
                            dq.remove(o)
                        except ValueError:
                            return False
                        if not dq:
                            # clean empty price level
                            del ladder[px]
                        return True
    return False
