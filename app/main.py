from pathlib import Path
import asyncio, uuid, json, time
from collections import defaultdict
from decimal import Decimal
from typing import Dict, Set, List
import datetime as dt

# Load env vars explicitly
from dotenv import load_dotenv
load_dotenv()

from pydantic import BaseModel


from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Depends, HTTPException
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

from app.order_book import OrderBook, Order as BookOrder
from app.schemas import OrderIn, Ack, PriceOut
from app.market_data import start_ref_engine, get_ref_price, set_hint_mid
from app.db import init_firestore
from app import db as db_module
from google.cloud import firestore as firestore_module
from google.cloud.firestore import FieldFilter
from app.auth import router as auth_router, current_user
from app.models import User, Order as DBOrder, Trade as DBTrade, CustomGame, MarketNews # explicit import
from app.me import router as me_router
from app.state import books, locks
from app import admin

app = FastAPI(title="AlphaBook")

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

DEFAULT_SYMBOLS: List[str] = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA"]
TOP_DEPTH: int = 10

subscribers: Dict[str, Set[WebSocket]] = defaultdict(set)

positions: Dict[int, Dict[str, Dict[str, Decimal]]] = defaultdict(
    lambda: defaultdict(lambda: {"qty": Decimal("0"), "avg": Decimal("0"), "realized": Decimal("0")})
)

class NewsOut(BaseModel):
    id: str # ObjectId string
    content: str
    created_at: dt.datetime

    class Config:
        from_attributes = True

@app.get("/news", response_model=List[NewsOut])
async def get_news(limit: int = 20):
    # Firestore fetch
    news_ref = db_module.db.collection("market_news").order_by("created_at", direction=firestore_module.Query.DESCENDING).limit(limit)
    docs = await news_ref.get()
    items = []
    for d in docs:
        data = d.to_dict()
        # id is not in data usually if we dont put it there
        items.append(NewsOut(id=d.id, **data))
    return items

@app.on_event("startup")
async def _startup():
    import time as time_module
    import traceback

    print("=" * 60)
    print("🚀 AlphaBook Starting Up...")
    print("=" * 60)

    # Initialize database
    try:
        print("🔄 Initializing Firestore...")
        init_firestore()
        print("✅ Firestore initialized")
        print("✅ Firestore initialized")
        # time_module.sleep(0.5) # Removed blocking sleep
    except Exception as e:
        print(f"❌ FATAL: Database initialization error: {e}")
        traceback.print_exc()
        raise

    # Create admin user if doesn't exist
    try:
        print(f"🔄 Setting up admin user...")
        # Check if admin exists
        # In this new flow, we might want to ensure 'admin' user exists in Firestore.
        # But we relied on 'username' query before.
        users_ref = db_module.db.collection("users")
        
        q = users_ref.where(filter=FieldFilter("username", "==", "admin")).limit(1)
        docs = await q.get()
        
        if not docs:
            # We need a UID for the admin. Let's make one up or use a constant.
            admin_uid = "admin_user_id"
            admin_user = User(
                id=admin_uid,
                username="admin",
                password_hash="firebase_managed", 
                balance=10000.0,
                is_admin=True,
                is_blacklisted=False,
                firebase_uid=admin_uid
            )
            await users_ref.document(admin_uid).set(admin_user.model_dump(exclude={"id"}))
            print("✅ Admin user created: username='admin'")
        else:
            # Verify admin status
            d = docs[0]
            if not d.get("is_admin"):
                await d.reference.update({"is_admin": True})
            print(f"✅ Admin user verified: admin")

    except Exception as e:
        print(f"⚠️ Admin user setup error: {e}")


    # Start market data engine
    print("🔄 Starting market data engine...")
    asyncio.create_task(start_ref_engine(DEFAULT_SYMBOLS, fast_tick=1.5, official_period=180))
    print("✅ Market data engine started")

    # Reload open orders from Firestore into in-memory order book
    try:
        print("🔄 Reloading open orders from Firestore...")
        open_orders_q = db_module.db.collection("orders").where("status", "==", "OPEN")
        open_order_docs = await open_orders_q.get()
        loaded_count = 0
        for doc in open_order_docs:
            o = doc.to_dict()
            symbol = o.get("symbol", "")
            if not symbol:
                continue
            remaining_qty = Decimal(o.get("qty", "0")) - Decimal(o.get("filled_qty", "0"))
            if remaining_qty <= 0:
                continue
            book = books[symbol]
            order = BookOrder(
                id=o.get("order_id", doc.id),
                user_id=o.get("user_id", ""),
                side=o.get("side", "BUY"),
                price=Decimal(o.get("price", "0")),
                qty=remaining_qty,
                orig_qty=Decimal(o.get("qty", "0")),
            )
            # Insert directly into the book (don't match — just restore resting orders)
            if order.side == "BUY":
                from collections import deque
                book.bids.setdefault(order.price, deque()).append(order)
            else:
                from collections import deque
                book.asks.setdefault(order.price, deque()).append(order)
            loaded_count += 1
        print(f"✅ Reloaded {loaded_count} open orders into memory")
    except Exception as e:
        print(f"⚠️ Failed to reload open orders: {e}")

    print("=" * 60)
    print("✅ AlphaBook Started Successfully!")
    print("=" * 60)


# ---- PnL helpers ----
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


def _metrics_for(user_id: str):
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
async def home(request: Request):
    from app.models import CustomGame

    # Firestore: CustomGame
    games_ref = db_module.db.collection("custom_games")
    # Compound query might need index. For now just filtering in python if small, or simple queries.
    # Firestore allows .where().where()
    q = games_ref.where(filter=FieldFilter("is_active", "==", True)).where(filter=FieldFilter("is_visible", "==", True))
    docs = await q.get()
    
    games = [CustomGame(id=d.id, **d.to_dict()) for d in docs]

    symbols: List[str] = []
    game_data = {}

    for game in games:
        symbols.append(game.symbol)
        game_data[game.symbol] = {
            "name": game.name,
            "instructions": game.instructions,
        }

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "app_name": "AlphaBook",
            "symbols": symbols,
            "symbols_json": json.dumps(symbols),
            "game_data_json": json.dumps(game_data),
            "depth": TOP_DEPTH,
        },
    )



@app.get("/trade/{symbol}", include_in_schema=False)
async def trade_page(symbol: str, request: Request):
    """Individual trading page for a specific custom game symbol"""
    from app.models import CustomGame

    symbol = symbol.upper()
    
    
    # Firestore
    games_ref = db_module.db.collection("custom_games")
    q = games_ref.where(filter=FieldFilter("symbol", "==", symbol)).limit(1)
    docs = await q.get()
    
    game = None
    if docs:
        d = docs[0]
        # Check active/visible manually or add to query if we have composite index
        g_data = d.to_dict()
        if g_data.get("is_active") and g_data.get("is_visible"):
             game = CustomGame(id=d.id, **g_data)

    if not game:
        raise HTTPException(status_code=404, detail=f"Symbol {symbol} not found")

    is_custom_game = True
    game_info = {
        "name": game.name,
        "instructions": game.instructions
    }

    return templates.TemplateResponse(
        "trading.html",
        {
            "request": request,
            "app_name": "AlphaBook",
            "symbol": symbol,
            "depth": TOP_DEPTH,
            "is_custom_game": is_custom_game,
            "game_info": game_info,
        },
    )



# ---- Utils ----
@app.get("/symbols")
async def get_symbols():
    """Get all available symbols (only visible custom games)."""
    from app.models import CustomGame

    # Firestore
    games_ref = db_module.db.collection("custom_games")
    q = games_ref.where("is_active", "==", True).where("is_visible", "==", True)
    docs = await q.get()
    
    symbols = [d.get("symbol") for d in docs]
    return {"symbols": symbols}



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


# ---------- OPEN ORDERS ----------
@app.get("/me/orders", include_in_schema=False)
async def me_orders(user: User = Depends(current_user)):
    """Return OPEN orders for this user from the database."""
    # Note: user.id is ObjectId, so we cast to str if needed or check how it's stored
    # In User model id is standard _id. In Order model user_id is indexed str (from `app/models.py`)
    
    orders_ref = db_module.db.collection("orders")
    # Filter by user_id and status
    q = orders_ref.where("user_id", "==", str(user.id)).where("status", "==", "OPEN").order_by("created_at", direction=firestore_module.Query.DESCENDING)
    docs = await q.get()

    rows = []
    for d in docs:
        o_data = d.to_dict()
        # Ensure we handle fields correctly
        rows.append({
            "id": o_data.get("order_id"),
            "symbol": o_data.get("symbol"),
            "side": o_data.get("side"),
            "price": float(o_data.get("price")),
            "qty": float(o_data.get("qty")),
            "filled_qty": float(o_data.get("filled_qty", 0)),
            "status": o_data.get("status"),
            "created_at": o_data.get("created_at").isoformat() if o_data.get("created_at") else "",
        })

    return rows

@app.delete("/orders/{order_id}", include_in_schema=False)
async def cancel_order(
        order_id: str,
        user: User = Depends(current_user)
):
    """
    Cancel one of the user's orders from both memory and database.

    When the related CustomGame is paused, cancellation is NOT allowed.
    """
    # Find in database
    # Find in database
    orders_ref = db_module.db.collection("orders")
    # Need to find the document with order_id field
    q = orders_ref.where("order_id", "==", order_id).where("user_id", "==", str(user.id)).where("status", "==", "OPEN").limit(1)
    docs = await q.get()

    if not docs:
        raise HTTPException(status_code=404, detail="Order not found")
    
    db_order_doc = docs[0]
    db_order_data = db_order_doc.to_dict()

    symbol = db_order_data.get("symbol").upper()

    # Check game paused
    games_ref = db_module.db.collection("custom_games")
    q_game = games_ref.where("symbol", "==", symbol).limit(1)
    g_docs = await q_game.get()
    
    if g_docs:
        g_data = g_docs[0].to_dict()
        if g_data.get("is_paused"):
            raise HTTPException(
                status_code=403,
                detail="Trading for this game is currently paused; orders cannot be cancelled."
            )

    # Cancel in memory book
    book = books[symbol]
    book.cancel(order_id, user_id=str(user.id))

    # Update database
    await db_order_doc.reference.update({
        "status": "CANCELED",
        "updated_at": dt.datetime.utcnow()
    })

    return {"ok": True, "status": "CANCELED"}

@app.post("/orders", response_model=Ack)
async def submit_order(
        order_in: OrderIn,
        user: User = Depends(current_user)
):
    print(f"DEBUG: submit_order start. Sym={order_in.symbol} Side={order_in.side} Ux={user.username}")
    symbol = order_in.symbol.upper()

    # Only allow trading on defined custom games
    games_ref = db_module.db.collection("custom_games")
    q = games_ref.where("symbol", "==", symbol).limit(1)
    print("DEBUG: Checking custom games...")
    docs = await q.get()
    print(f"DEBUG: Found {len(docs)} games")

    if not docs:
         raise HTTPException(status_code=404, detail="Symbol is not tradable.")
    
    cg_data = docs[0].to_dict()
    # cg = CustomGame(id=docs[0].id, **cg_data) # Optional wrapper

    if not cg_data.get("is_active"):
        raise HTTPException(status_code=403, detail="This game is not active.")
    if not cg_data.get("is_visible"):
        raise HTTPException(status_code=403, detail="This game is hidden by the administrator.")
    if cg_data.get("is_paused"):
        raise HTTPException(status_code=403, detail="Trading for this game is currently paused.")

    book = books[symbol]
    lock = locks[symbol]

    order_id = str(uuid.uuid4())

    # Create database record FIRST
    db_order = DBOrder(
        order_id=order_id,
        user_id=str(user.id),
        symbol=symbol,
        side=order_in.side,
        price=str(order_in.price), # Cast to str
        qty=str(order_in.qty), # Cast to str
        filled_qty="0",
        status="OPEN",
        created_at=dt.datetime.utcnow()
    )
    # Use order_id as Document ID for easy lookup? Or auto-id?
    # Using auto-id is safer for collisions if uuid fails (unlikely), but using order_id as key is faster lookup.
    # Let's use order_id as doc id.
    await db_module.db.collection("orders").document(order_id).set(db_order.model_dump(exclude={"id"}))

    # Create in-memory order
    order = BookOrder(
        id=order_id,
        user_id=str(user.id),
        side=order_in.side,
        price=Decimal(order_in.price),
        qty=Decimal(order_in.qty),
        orig_qty=Decimal(order_in.qty),
        ts=int(time.time() * 1000),
    )

    # Match in order book
    print("DEBUG: Acquiring lock...")
    async with lock:
        print("DEBUG: Lock acquired. Adding order...")
        fills = book.add(order)
        print("DEBUG: Order added. Snapshotting...")
        snap = book.snapshot(depth=TOP_DEPTH)
    print("DEBUG: Lock released. Fills:", len(fills))

    # Record trades in database
    total_filled = Decimal("0")
    
    # Batch write for trades?
    print("DEBUG: Starting batch...")
    batch = db_module.db.batch()
    
    for tr in fills:
        px, q = tr["price"], tr["qty"]
        buyer_id, seller_id = str(tr["buyer_id"]), str(tr["seller_id"])

        # Save trade to database
        trade_id = str(uuid.uuid4())
        trade = DBTrade(
            symbol=symbol,
            buyer_id=buyer_id,
            seller_id=seller_id,
            price=str(px),
            qty=str(q),
            buy_order_id=order_id if order_in.side == "BUY" else "",
            sell_order_id=order_id if order_in.side == "SELL" else "",
            created_at=dt.datetime.utcnow()
        )
        trade_ref = db_module.db.collection("trades").document(trade_id)
        batch.set(trade_ref, trade.model_dump(exclude={"id"}))

        # Update positions
        # Ensure positions exist
        if buyer_id not in positions: positions[buyer_id] = defaultdict(lambda: {"qty": Decimal("0"), "avg": Decimal("0"), "realized": Decimal("0")})
        if seller_id not in positions: positions[seller_id] = defaultdict(lambda: {"qty": Decimal("0"), "avg": Decimal("0"), "realized": Decimal("0")})
        
        _apply_buy(positions[buyer_id][symbol], px, q)
        _apply_sell(positions[seller_id][symbol], px, q)

        total_filled += q

    print("DEBUG: Committing batch...")
    await batch.commit()
    print("DEBUG: Batch committed.")

    # Update order status in database
    update_data = {
        "filled_qty": str(total_filled),
        "updated_at": dt.datetime.utcnow()
    }
    if total_filled >= Decimal(order_in.qty):
        update_data["status"] = "FILLED"
    
    await db_module.db.collection("orders").document(order_id).update(update_data)

    # Update mid hint for ref price
    try:
        bids = snap.get("bids", [])
        asks = snap.get("asks", [])
        if bids and asks:
            bb = float(bids[0]["px"])
            aa = float(asks[0]["px"])
            set_hint_mid(symbol, (bb + aa) / 2.0)
    except Exception:
        pass

    # Broadcast update
    await _broadcast(symbol, {
        "type": "snapshot",
        "symbol": symbol,
        "book": snap,
        "ref_price": get_ref_price(symbol),
    })
    
    print("DEBUG: submitting ACK")
    return Ack(
        order_id=order.id,
        trades=[{
            "px": str(t["price"]),
            "qty": str(t["qty"]),
            "buyer": t["buyer_id"],
            "seller": t["seller_id"]
        } for t in fills],
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
# ---- Auth routes ----
app.include_router(auth_router)
app.include_router(me_router)
app.include_router(admin.router)

from app import files
app.include_router(files.router)
