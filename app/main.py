from pathlib import Path
import asyncio, uuid, json, time            # <-- added time
from collections import defaultdict
from decimal import Decimal
from typing import Dict, Set, List

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Depends, HTTPException  # +HTTPException
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

from app.order_book import OrderBook, Order
from app.schemas import OrderIn, Ack, PriceOut
from app.market_data import start_ref_engine, get_ref_price, set_hint_mid
from app.db import init_db
from app.auth import router as auth_router, current_user
from app.models import User
from app.me import router as me_router
from app.state import books, locks

app = FastAPI(title="AlphaBook")

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

DEFAULT_SYMBOLS: List[str] = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA"]
TOP_DEPTH: int = 10


subscribers: Dict[str, Set[WebSocket]] = defaultdict(set)

from decimal import Decimal
positions: Dict[int, Dict[str, Dict[str, Decimal]]] = defaultdict(
    lambda: defaultdict(lambda: {"qty": Decimal("0"), "avg": Decimal("0"), "realized": Decimal("0")})
)

@app.on_event("startup")
async def _startup():
    init_db()
    asyncio.create_task(start_ref_engine(DEFAULT_SYMBOLS, fast_tick=1.5, official_period=180))

# ---- PnL helpers (unchanged) ----
def _apply_buy(pos: Dict[str, Decimal], price: Decimal, qty: Decimal):
    q, avg, realized = pos["qty"], pos["avg"], pos["realized"]
    if q >= 0:
        new_q = q + qty
        pos["avg"] = (avg * q + price * qty) / new_q if new_q != 0 else Decimal("0")
        pos["qty"] = new_q
    else:
        close = min(qty, -q)
        pos["realized"] = realized + (avg - price) * close
        q += qty
        pos["qty"] = q
        pos["avg"] = Decimal("0") if q == 0 else price

def _apply_sell(pos: Dict[str, Decimal], price: Decimal, qty: Decimal):
    q, avg, realized = pos["qty"], pos["avg"], pos["realized"]
    if q <= 0:
        new_q = q - qty
        pos["avg"] = (avg * (-q) + price * qty) / (-new_q) if new_q != 0 else Decimal("0")
        pos["qty"] = new_q
    else:
        close = min(qty, q)
        pos["realized"] = realized + (price - avg) * close
        q -= qty
        pos["qty"] = q
        pos["avg"] = Decimal("0") if q == 0 else price

def _metrics_for(user_id: int):
    from app.market_data import get_last
    out = {}
    for sym, p in positions[user_id].items():
        ref = get_last(sym)
        qty, avg, realized = p["qty"], p["avg"], p["realized"]
        unreal = Decimal("0") if ref is None or qty == 0 else (Decimal(str(ref)) - avg) * qty
        total = realized + unreal
        out[sym] = {
            "position": str(qty),
            "avg_price": str(avg),
            "delta": str(qty),
            "realized": str(realized),
            "unrealized": str(unreal),
            "total_pnl": str(total),
            "ref": ref,
        }
    return out

# ---- Pages ----
@app.get("/", include_in_schema=False)
def home(request: Request):
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "app_name": "AlphaBook",
            "symbols": DEFAULT_SYMBOLS,
            "symbols_json": json.dumps(DEFAULT_SYMBOLS),
            "depth": TOP_DEPTH,
        },
    )

# ---- Utils ----
@app.get("/symbols")
def get_symbols():
    return {"symbols": DEFAULT_SYMBOLS}

@app.get("/health")
def health():
    return {"ok": True}

@app.get("/reference/{symbol}", response_model=PriceOut)
def get_reference(symbol: str):
    return PriceOut(symbol=symbol, price=get_ref_price(symbol))

@app.get("/book/{symbol}")
def get_book(symbol: str):
    return books[symbol].snapshot(depth=TOP_DEPTH)

# ---- Auth protected ----
@app.get("/me/metrics")
def me_metrics(user: User = Depends(current_user)):
    return {"user": user.username, "metrics": _metrics_for(user.id)}

# ---------- OPEN ORDERS (in-memory) ----------
@app.get("/me/orders", include_in_schema=False)
def me_orders(user: User = Depends(current_user)):
    """Return OPEN orders for this user from the in-memory books."""
    rows = []
    uid = str(user.id)
    for sym, book in books.items():
        for r in book.list_open_for_user(uid):
            r = dict(r)               # copy so we can add symbol
            r["symbol"] = sym
            rows.append(r)
    rows.sort(key=lambda r: r.get("ts", 0), reverse=True)
    return rows

@app.delete("/orders/{order_id}", include_in_schema=False)
def cancel_order(order_id: str, user: User = Depends(current_user)):
    """Cancel one of the user's orders from the in-memory books."""
    uid = str(user.id)
    for book in books.values():
        if book.cancel(order_id, user_id=uid):
            return {"ok": True, "status": "CANCELED"}
    raise HTTPException(status_code=404, detail="Order not found")

@app.post("/orders", response_model=Ack)
async def submit_order(order_in: OrderIn, user: User = Depends(current_user)):
    symbol = order_in.symbol
    book = books[symbol]
    lock = locks[symbol]
    order = Order(
        id=str(uuid.uuid4()),
        user_id=str(user.id),
        side=order_in.side,
        price=Decimal(order_in.price),
        qty=Decimal(order_in.qty),
        orig_qty=Decimal(order_in.qty),   # NEW: keep original
        ts=time.time(),                   # NEW: timestamp
    )
    async with lock:
        fills = book.add(order)
        snap = book.snapshot(depth=TOP_DEPTH)

    for tr in fills:
        px, q = tr["price"], tr["qty"]
        buyer_id, seller_id = int(tr["buyer_id"]), int(tr["seller_id"])
        _apply_buy(positions[buyer_id][symbol], px, q)
        _apply_sell(positions[seller_id][symbol], px, q)

    try:
        bids = snap.get("bids", [])
        asks = snap.get("asks", [])
        if bids and asks:
            bb = float(bids[0]["px"])
            aa = float(asks[0]["px"])
            set_hint_mid(symbol, (bb + aa) / 2.0)
    except Exception:
        pass

    await _broadcast(symbol, {
        "type": "snapshot",
        "symbol": symbol,
        "book": snap,
        "ref_price": get_ref_price(symbol),
    })

    return Ack(
        order_id=order.id,
        trades=[{"px": str(t["price"]), "qty": str(t["qty"]), "buyer": t["buyer_id"], "seller": t["seller_id"]} for t in fills],
        snapshot=snap,
    )

# ---- WebSocket ----
@app.websocket("/ws/book/{symbol}")
async def ws_book(ws: WebSocket, symbol: str):
    await ws.accept()
    subscribers[symbol].add(ws)
    await ws.send_json({
        "type": "snapshot",
        "symbol": symbol,
        "book": books[symbol].snapshot(depth=TOP_DEPTH),
        "ref_price": get_ref_price(symbol),
    })
    try:
        while True:
            await asyncio.sleep(3600)
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

# ---- Auth routes ----
app.include_router(auth_router)
app.include_router(me_router)
