from pathlib import Path
import asyncio, uuid, json, time
from collections import defaultdict
from decimal import Decimal
from typing import Dict, Set, List
import datetime as dt

from pydantic import BaseModel

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Depends, HTTPException
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from sqlmodel import Session, select

from app.order_book import OrderBook, Order as BookOrder
from app.schemas import OrderIn, Ack, PriceOut
from app.market_data import start_ref_engine, get_ref_price, set_hint_mid
from app.db import init_db, get_session
from app.auth import router as auth_router, current_user
from app.models import User, Order as DBOrder, Trade as DBTrade, CustomGame
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
    id: int
    content: str
    created_at: dt.datetime

    class Config:
        from_attributes = True

@app.get("/news", response_model=List[NewsOut])
def get_news(limit: int = 20, session: Session = Depends(get_session)):
    from app.models import MarketNews
    items = session.exec(
        select(MarketNews).order_by(MarketNews.created_at.desc()).limit(limit)
    ).all()
    return items

@app.on_event("startup")
async def _startup():
    from app.auth import hash_pw
    from sqlmodel import Session
    import time as time_module
    import traceback

    print("=" * 60)
    print("🚀 AlphaBook Starting Up...")
    print("=" * 60)

    # Initialize database and create tables
    try:
        print("🔄 Initializing database...")
        init_db()
        print("✅ Database tables created/verified")

        # Give the database a moment to be ready
        time_module.sleep(0.5)

    except Exception as e:
        print(f"❌ FATAL: Database initialization error: {e}")
        traceback.print_exc()
        raise  # Stop startup if database fails

    # Create admin user if doesn't exist
    max_retries = 3
    for attempt in range(1, max_retries + 1):
        try:
            from app.db import engine
            print(f"🔄 Setting up admin user (attempt {attempt}/{max_retries})...")

            with Session(engine) as session:
                admin = session.exec(select(User).where(User.username == "admin")).first()
                if not admin:
                    admin = User(
                        username="admin",
                        password_hash=hash_pw("alphabook"),
                        balance=10000.0,
                        is_admin=True,
                        is_blacklisted=False
                    )
                    session.add(admin)
                    session.commit()
                    print("✅ Admin user created: username='admin', password='alphabook'")
                else:
                    if not admin.is_admin:
                        admin.is_admin = True
                        session.add(admin)
                        session.commit()
                    print(f"✅ Admin user verified: {admin.username}")
            break  # Success, exit retry loop

        except Exception as e:
            print(f"⚠️ Admin user setup error (attempt {attempt}/{max_retries}): {e}")
            if attempt == max_retries:
                print("❌ FATAL: Could not set up admin user after retries")
                traceback.print_exc()
                raise
            time_module.sleep(1)

    # Start market data engine
    print("🔄 Starting market data engine...")
    asyncio.create_task(start_ref_engine(DEFAULT_SYMBOLS, fast_tick=1.5, official_period=180))
    print("✅ Market data engine started")

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
def home(request: Request, session: Session = Depends(get_session)):
    from app.models import CustomGame

    # Only show visible & active custom games on the landing page
    games = session.exec(
        select(CustomGame).where(
            CustomGame.is_active == True,
            CustomGame.is_visible == True,
        )
    ).all()

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
def trade_page(symbol: str, request: Request, session: Session = Depends(get_session)):
    """Individual trading page for a specific custom game symbol"""
    from app.models import CustomGame

    symbol = symbol.upper()

    game = session.exec(
        select(CustomGame).where(
            CustomGame.symbol == symbol,
            CustomGame.is_active == True,
            CustomGame.is_visible == True,
        )
    ).first()

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
def get_symbols(session: Session = Depends(get_session)):
    """Get all available symbols (only visible custom games)."""
    from app.models import CustomGame

    games = session.exec(
        select(CustomGame).where(
            CustomGame.is_active == True,
            CustomGame.is_visible == True,
        )
    ).all()

    symbols = [g.symbol for g in games]
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
def me_orders(user: User = Depends(current_user), session: Session = Depends(get_session)):
    """Return OPEN orders for this user from the database."""
    stmt = select(DBOrder).where(
        DBOrder.user_id == user.id,
        DBOrder.status == "OPEN"
    ).order_by(DBOrder.created_at.desc())

    orders = session.exec(stmt).all()

    rows = []
    for o in orders:
        qty = Decimal(o.qty)
        filled = Decimal(o.filled_qty)
        rows.append({
            "id": o.order_id,
            "symbol": o.symbol,
            "side": o.side,
            "price": float(o.price),
            "qty": float(qty),
            "filled_qty": float(filled),
            "status": o.status,
            "created_at": o.created_at.isoformat(),
        })

    return rows

@app.delete("/orders/{order_id}", include_in_schema=False)
def cancel_order(
        order_id: str,
        user: User = Depends(current_user),
        session: Session = Depends(get_session)
):
    """
    Cancel one of the user's orders from both memory and database.

    When the related CustomGame is paused, cancellation is NOT allowed.
    """
    # Find in database
    stmt = select(DBOrder).where(
        DBOrder.order_id == order_id,
        DBOrder.user_id == user.id,
        DBOrder.status == "OPEN"
    )
    db_order = session.exec(stmt).first()

    if not db_order:
        raise HTTPException(status_code=404, detail="Order not found")

    symbol = db_order.symbol.upper()

    # Check if this symbol is a custom game and whether it is paused
    game = session.exec(
        select(CustomGame).where(CustomGame.symbol == symbol)
    ).first()

    # If there is a related custom game and it is paused, block cancellation
    if game and game.is_paused:
        raise HTTPException(
            status_code=403,
            detail="Trading for this game is currently paused; orders cannot be cancelled."
        )

    # Cancel in memory book
    book = books[symbol]
    book.cancel(order_id, user_id=str(user.id))

    # Update database
    db_order.status = "CANCELED"
    db_order.updated_at = dt.datetime.utcnow()
    session.add(db_order)
    session.commit()

    return {"ok": True, "status": "CANCELED"}

@app.post("/orders", response_model=Ack)
async def submit_order(
        order_in: OrderIn,
        user: User = Depends(current_user),
        session: Session = Depends(get_session)
):
    symbol = order_in.symbol.upper()

    # Only allow trading on defined custom games, and enforce visibility/pause flags
    from app.models import CustomGame
    cg = session.exec(
        select(CustomGame).where(CustomGame.symbol == symbol)
    ).first()

    if not cg:
        raise HTTPException(status_code=404, detail="Symbol is not tradable.")

    if not cg.is_active:
        raise HTTPException(status_code=403, detail="This game is not active.")
    if not cg.is_visible:
        raise HTTPException(status_code=403, detail="This game is hidden by the administrator.")
    if cg.is_paused:
        raise HTTPException(status_code=403, detail="Trading for this game is currently paused.")

    book = books[symbol]
    lock = locks[symbol]

    order_id = str(uuid.uuid4())

    # Create database record FIRST
    db_order = DBOrder(
        order_id=order_id,
        user_id=user.id,
        symbol=symbol,
        side=order_in.side,
        price=order_in.price,
        qty=order_in.qty,
        filled_qty="0",
        status="OPEN",
        created_at=dt.datetime.utcnow()
    )
    session.add(db_order)
    session.commit()
    session.refresh(db_order)

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
    async with lock:
        fills = book.add(order)
        snap = book.snapshot(depth=TOP_DEPTH)

    # Record trades in database
    total_filled = Decimal("0")
    for tr in fills:
        px, q = tr["price"], tr["qty"]
        buyer_id, seller_id = int(tr["buyer_id"]), int(tr["seller_id"])

        # Save trade to database
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
        session.add(trade)

        # Update positions
        _apply_buy(positions[buyer_id][symbol], px, q)
        _apply_sell(positions[seller_id][symbol], px, q)

        total_filled += q

    # Update order status in database
    db_order.filled_qty = str(total_filled)
    if total_filled >= Decimal(order_in.qty):
        db_order.status = "FILLED"
    db_order.updated_at = dt.datetime.utcnow()
    session.add(db_order)
    session.commit()

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
app.include_router(auth_router)
app.include_router(me_router)
app.include_router(admin.router)
