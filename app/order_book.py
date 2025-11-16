# app/order_book.py  (REPLACE the file)
from dataclasses import dataclass
from collections import deque
from decimal import Decimal, getcontext
from typing import Deque, Dict, List

getcontext().prec = 28

@dataclass
class Order:
    id: str
    user_id: str
    side: str     # "BUY" or "SELL"
    price: Decimal
    qty: Decimal

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

    def snapshot(self, depth: int = 10):
        bids = sorted(self.bids.items(), key=lambda kv: kv[0], reverse=True)[:depth]
        asks = sorted(self.asks.items(), key=lambda kv: kv[0])[:depth]
        def level(px, q: Deque[Order]):
            total = sum(o.qty for o in q)
            return {"px": str(px), "qty": str(total)}
        return {"bids": [level(px, q) for px, q in bids],
                "asks": [level(px, q) for px, q in asks]}
