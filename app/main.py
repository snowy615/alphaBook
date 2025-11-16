
import asyncio
import uuid
from collections import defaultdict
from decimal import Decimal
from typing import Dict, Set

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import RedirectResponse
from app.order_book import OrderBook, Order
from app.schemas import OrderIn, Ack, PriceOut
from app.market_data import poll_prices, get_last

app = FastAPI(title="Mini Exchange (Python)")

books: Dict[str, OrderBook] = defaultdict(OrderBook)
locks: Dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
subscribers: Dict[str, Set[WebSocket]] = defaultdict(set)

DEFAULT_SYMBOLS = ["AAPL", "MSFT"]

@app.on_event("startup")
async def _startup():
    asyncio.create_task(poll_prices(DEFAULT_SYMBOLS, interval_sec=60))
@app.get("/")
def root():
    return RedirectResponse(url="/docs")

@app.get("/health")
def health():
    return {"ok": True}

@app.get("/reference/{symbol}", response_model=PriceOut)
def get_reference(symbol: str):
    return PriceOut(symbol=symbol, price=get_last(symbol))

@app.get("/book/{symbol}")
def get_book(symbol: str):
    return books[symbol].snapshot()

@app.post("/orders", response_model=Ack)
async def submit_order(order_in: OrderIn):
    book = books[order_in.symbol]
    lock = locks[order_in.symbol]
    order = Order(
        id=str(uuid.uuid4()),
        user_id=order_in.user_id,
        side=order_in.side,
        price=Decimal(order_in.price),
        qty=Decimal(order_in.qty),
    )
    async with lock:
        trades = book.add(order)
        snap = book.snapshot()
    await _broadcast(order_in.symbol, {"type": "snapshot", "symbol": order_in.symbol, "book": snap})
    return Ack(order_id=order.id,
               trades=[{"px": str(px), "qty": str(qty), "aggressor": side} for px, qty, side in trades],
               snapshot=snap)

@app.websocket("/ws/book/{symbol}")
async def ws_book(ws: WebSocket, symbol: str):
    await ws.accept()
    subscribers[symbol].add(ws)
    await ws.send_json({"type": "snapshot", "symbol": symbol, "book": books[symbol].snapshot(),
                        "ref_price": get_last(symbol)})
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        subscribers[symbol].discard(ws)

async def _broadcast(symbol: str, payload: dict):
    dead = []
    for ws in list(subscribers[symbol]):
        try:
            await ws.send_json(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        subscribers[symbol].discard(ws)
