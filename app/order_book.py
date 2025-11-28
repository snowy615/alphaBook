# app/order_book.py
from dataclasses import dataclass, field
from time import time as _now
from collections import deque
from decimal import Decimal, getcontext
from typing import Deque, Dict, List, Optional

getcontext().prec = 28


@dataclass
class Order:
    id: str
    user_id: str
    side: str  # "BUY" or "SELL"
    price: Decimal
    qty: Decimal
    orig_qty: Optional[Decimal] = None
    ts: int = field(default_factory=lambda: int(_now() * 1000))

    def __post_init__(self):
        if self.orig_qty is None:
            self.orig_qty = Decimal(self.qty)


class OrderBook:
    def __init__(self):
        self.bids: Dict[Decimal, Deque[Order]] = {}
        self.asks: Dict[Decimal, Deque[Order]] = {}

    def _best_bid(self):
        return max(self.bids.keys(), default=None)

    def _best_ask(self):
        return min(self.asks.keys(), default=None)

    def add(self, order: Order) -> List[dict]:
        """
        Add a LIMIT order; returns list of trades:
        {price, qty, buyer_id, seller_id}
        """
        trades: List[dict] = []

        if order.side == "BUY":
            while order.qty > 0 and self._best_ask() is not None and self._best_ask() <= order.price:
                best_px = self._best_ask()
                head = self.asks[best_px][0]
                fill = min(order.qty, head.qty)
                head.qty -= fill
                order.qty -= fill
                trades.append({"price": best_px, "qty": fill, "buyer_id": order.user_id, "seller_id": head.user_id})
                if head.qty == 0:
                    self.asks[best_px].popleft()
                    if not self.asks[best_px]:
                        del self.asks[best_px]
            if order.qty > 0:
                self.bids.setdefault(order.price, deque()).append(order)
        else:  # SELL
            while order.qty > 0 and self._best_bid() is not None and self._best_bid() >= order.price:
                best_px = self._best_bid()
                head = self.bids[best_px][0]
                fill = min(order.qty, head.qty)
                head.qty -= fill
                order.qty -= fill
                trades.append({"price": best_px, "qty": fill, "buyer_id": head.user_id, "seller_id": order.user_id})
                if head.qty == 0:
                    self.bids[best_px].popleft()
                    if not self.bids[best_px]:
                        del self.bids[best_px]
            if order.qty > 0:
                self.asks.setdefault(order.price, deque()).append(order)
        return trades

    def cancel(self, order_id: str, user_id: str) -> bool:
        """
        Cancel an order by ID. Returns True if found and canceled.
        Verifies the user_id matches for security.
        """
        # Search bids
        for px, dq in list(self.bids.items()):
            for order in list(dq):
                if order.id == order_id and order.user_id == user_id:
                    dq.remove(order)
                    if not dq:
                        del self.bids[px]
                    return True

        # Search asks
        for px, dq in list(self.asks.items()):
            for order in list(dq):
                if order.id == order_id and order.user_id == user_id:
                    dq.remove(order)
                    if not dq:
                        del self.asks[px]
                    return True

        return False

    def list_open_for_user(self, user_id: str) -> List[dict]:
        """
        Return all open orders for a specific user.
        """
        orders = []

        # Check bids
        for px, dq in self.bids.items():
            for order in dq:
                if order.user_id == user_id:
                    filled = (order.orig_qty - order.qty) if order.orig_qty else Decimal("0")
                    orders.append({
                        "id": order.id,
                        "side": "BUY",
                        "price": order.price,
                        "qty": order.orig_qty or order.qty,
                        "filled_qty": filled,
                        "status": "OPEN",
                        "ts": order.ts,
                    })

        # Check asks
        for px, dq in self.asks.items():
            for order in dq:
                if order.user_id == user_id:
                    filled = (order.orig_qty - order.qty) if order.orig_qty else Decimal("0")
                    orders.append({
                        "id": order.id,
                        "side": "SELL",
                        "price": order.price,
                        "qty": order.orig_qty or order.qty,
                        "filled_qty": filled,
                        "status": "OPEN",
                        "ts": order.ts,
                    })

        # Sort by timestamp (newest first)
        orders.sort(key=lambda x: x["ts"], reverse=True)
        return orders

    def snapshot(self, depth: int = 10):
        bids = sorted(self.bids.items(), key=lambda kv: kv[0], reverse=True)[:depth]
        asks = sorted(self.asks.items(), key=lambda kv: kv[0])[:depth]

        def level(px, q: Deque[Order]):
            total = sum(o.qty for o in q)
            return {"px": str(px), "qty": str(total)}

        return {
            "bids": [level(px, q) for px, q in bids],
            "asks": [level(px, q) for px, q in asks],
        }

    def clear_all_orders(self):
        """Clear all orders from this book"""
        self.bids.clear()
        self.asks.clear()


def clear_all_orders():
    """Clear all orders from all books"""
    from app.state import books
    for book in books.values():
        book.clear_all_orders()