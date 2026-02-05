from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from app.db import db
from google.cloud import firestore
from google.cloud.firestore import FieldFilter, Or
from app.auth import current_user
from app.models import User, Order, Trade, CustomGame, MarketNews
from fastapi.templating import Jinja2Templates
from pathlib import Path
from pydantic import BaseModel
import datetime as dt
from decimal import Decimal
from typing import List

router = APIRouter()
BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def require_admin(user: User = Depends(current_user)):
    """Dependency to check if user is admin"""
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


async def calculate_user_pnl(user_id: str) -> float:
    """Calculate P&L for a user considering expected values for custom games"""
    from app.market_data import get_ref_price
    import logging

    log = logging.getLogger("admin")

    # Get all trades for this user
    trades_ref = db.collection("trades")
    filter_buy = FieldFilter("buyer_id", "==", user_id)
    filter_sell = FieldFilter("seller_id", "==", user_id)
    q = trades_ref.where(filter=Or(filters=[filter_buy, filter_sell]))
    docs = await q.get()
    
    trades = [Trade(id=d.id, **d.to_dict()) for d in docs]

    log.info(f"Calculating P&L for user {user_id}, found {len(trades)} trades")

    # Get all custom games with their expected values
    games_ref = db.collection("custom_games")
    # Assuming small number of games, fetching all
    g_docs = await games_ref.get()
    games = [CustomGame(id=d.id, **d.to_dict()) for d in g_docs]
    
    game_expected_values = {game.symbol: float(game.expected_value) for game in games}

    log.info(f"Game expected values: {game_expected_values}")

    # Track positions
    positions = {}

    for trade in trades:
        symbol = trade.symbol
        price = Decimal(trade.price)
        qty = Decimal(trade.qty)

        if symbol not in positions:
            positions[symbol] = {
                "qty": Decimal("0"),
                "total_cost": Decimal("0"),
                "realized_pnl": Decimal("0")
            }

        pos = positions[symbol]

        # User is buyer
        if trade.buyer_id == user_id:
            if pos["qty"] >= 0:
                # Opening/adding to long
                pos["total_cost"] += price * qty
                pos["qty"] += qty
            else:
                # Covering short
                close_qty = min(qty, abs(pos["qty"]))
                if pos["qty"] != 0:
                    avg_short = abs(pos["total_cost"] / pos["qty"])
                    pos["realized_pnl"] += (avg_short - price) * close_qty

                pos["qty"] += qty
                remaining = qty - close_qty

                if pos["qty"] > 0:
                    pos["total_cost"] = price * remaining
                elif pos["qty"] == 0:
                    pos["total_cost"] = Decimal("0")

        # User is seller
        if trade.seller_id == user_id:
            if pos["qty"] <= 0:
                # Opening/adding to short
                pos["total_cost"] += price * qty
                pos["qty"] -= qty
            else:
                # Closing long
                close_qty = min(qty, pos["qty"])
                if pos["qty"] != 0:
                    avg_long = pos["total_cost"] / pos["qty"]
                    pos["realized_pnl"] += (price - avg_long) * close_qty

                pos["qty"] -= qty
                remaining = qty - close_qty

                if pos["qty"] < 0:
                    pos["total_cost"] = price * remaining
                elif pos["qty"] == 0:
                    pos["total_cost"] = Decimal("0")

    log.info(f"User {user_id} positions: {positions}")

    # Calculate total P&L
    total_pnl = Decimal("0")

    for symbol, pos in positions.items():
        qty = pos["qty"]
        total_cost = pos["total_cost"]
        realized = pos["realized_pnl"]

        # Determine reference price: use expected value for games, market price for equities
        if symbol in game_expected_values:
            ref_price = Decimal(str(game_expected_values[symbol]))
        else:
            market_ref = get_ref_price(symbol)
            ref_price = Decimal(str(market_ref)) if market_ref else Decimal("0")

        log.info(f"Symbol {symbol}: qty={qty}, total_cost={total_cost}, ref_price={ref_price}")

        # Calculate unrealized P&L
        unrealized = Decimal("0")
        if qty != 0 and ref_price > 0:
            avg_cost = total_cost / abs(qty)

            if qty > 0:
                # Long position: profit if ref_price > avg_cost
                unrealized = (ref_price - avg_cost) * qty
            else:
                # Short position: profit if avg_cost > ref_price
                unrealized = (avg_cost - ref_price) * abs(qty)

        log.info(f"Symbol {symbol}: realized={realized}, unrealized={unrealized}")

        total_pnl += realized + unrealized

    log.info(f"User {user_id} total P&L: {total_pnl}")

    return float(total_pnl)


@router.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(
        request: Request,
        user: User = Depends(require_admin)
):
    """Admin dashboard page"""
    # Get all users
    users_ref = db.collection("users")
    u_docs = await users_ref.get()
    users = [User(id=d.id, **d.to_dict()) for d in u_docs]

    # Calculate stats for each user
    user_stats = []
    
    # Pre-fetch all trades and orders might be better if strict, but expensive.
    # For now, looping (slow for many users but ok for toy app).
    for u in users:
        uid = str(u.id)
        # Count orders
        orders_ref = db.collection("orders")
        o_q = orders_ref.where("user_id", "==", uid).count()
        o_agg = await o_q.get()
        orders_count = o_agg[0][0].value

        # Count trades
        trades_ref = db.collection("trades")
        filter_buy = FieldFilter("buyer_id", "==", uid)
        filter_sell = FieldFilter("seller_id", "==", uid)
        t_q = trades_ref.where(filter=Or(filters=[filter_buy, filter_sell])).count()
        t_agg = await t_q.get()
        trades_count = t_agg[0][0].value

        # Calculate P&L using expected values for games
        pnl = await calculate_user_pnl(uid)

        user_stats.append({
            "id": uid,
            "username": u.username,
            "balance": u.balance,
            "pnl": pnl,
            "orders": orders_count,
            "trades": trades_count,
            "is_admin": u.is_admin,
            "is_blacklisted": u.is_blacklisted,
            "created_at": u.created_at
        })

    # Sort by P&L for leaderboard
    leaderboard = sorted(user_stats, key=lambda x: x["pnl"], reverse=True)

    # Get custom games
    games_ref = db.collection("custom_games")
    g_q = games_ref.where("is_active", "==", True)
    g_docs = await g_q.get()
    games = [CustomGame(id=d.id, **d.to_dict()) for d in g_docs]

    return templates.TemplateResponse("admin.html", {
        "request": request,
        "user": user,
        "users": user_stats,
        "leaderboard": leaderboard,
        "total_users": len(users),
        "games": games
    })


# Custom Game Management
class GameCreate(BaseModel):
    symbol: str
    name: str
    instructions: str
    expected_value: float


class NewsCreate(BaseModel):
    content: str  # creat news


class NewsUpdate(BaseModel):
    content: str  # edit news


@router.post("/admin/games")
async def create_game(
        game: GameCreate,
        admin: User = Depends(require_admin)
):
    """Create a new custom game"""
    # Validate symbol format
    symbol = game.symbol.upper().strip()
    if not symbol.startswith("GAME"):
        raise HTTPException(status_code=400, detail="Symbol must start with 'GAME'")

    # Check if symbol already exists
    games_ref = db.collection("custom_games")
    q = games_ref.where("symbol", "==", symbol).limit(1)
    docs = await q.get()
    
    if docs:
        raise HTTPException(status_code=400, detail=f"Game with symbol {symbol} already exists")

    game_id = f"game_{symbol.lower()}"
    new_game = CustomGame(
        id=game_id,
        symbol=symbol,
        name=game.name,
        instructions=game.instructions,
        expected_value=game.expected_value,
        is_active=True,
        created_by=str(admin.id),
        created_at=dt.datetime.utcnow(),
        updated_at=dt.datetime.utcnow()
    )

    await db.collection("custom_games").document(game_id).set(new_game.model_dump(exclude={"id"}))

    # Add to order books
    from app.state import books
    from app.order_book import OrderBook
    books[symbol] = OrderBook()

    return {"ok": True, "game": {
        "id": str(new_game.id),
        "symbol": new_game.symbol,
        "name": new_game.name,
        "instructions": new_game.instructions,
        "expected_value": new_game.expected_value
    }}


@router.put("/admin/games/{game_id}")
async def update_game(
        game_id: str,
        game: GameCreate,
        admin: User = Depends(require_admin)
):
    """Update a custom game"""
    # game_id is the document ID
    doc_ref = db.collection("custom_games").document(game_id)
    doc = await doc_ref.get()
    
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Game not found")

    update_data = {
        "name": game.name,
        "instructions": game.instructions,
        "expected_value": game.expected_value,
        "updated_at": dt.datetime.utcnow()
    }

    await doc_ref.update(update_data)

    return {"ok": True, "message": "Game updated"}

@router.delete("/admin/games/{game_id}")
async def delete_game(
        game_id: str,
        admin: User = Depends(require_admin)
):
    """Deactivate a custom game"""
    doc_ref = db.collection("custom_games").document(game_id)
    doc = await doc_ref.get()
    
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Game not found")

    await doc_ref.update({
        "is_active": False,
        "updated_at": dt.datetime.utcnow()
    })

    return {"ok": True, "message": "Game deactivated"}

@router.post("/admin/games/{game_id}/show")
async def show_game(
        game_id: str,
        admin: User = Depends(require_admin)
):
    """Mark a custom game as visible in the lobby and /symbols."""
    doc_ref = db.collection("custom_games").document(game_id)
    doc = await doc_ref.get()

    if not doc.exists:
        raise HTTPException(status_code=404, detail="Game not found")

    await doc_ref.update({"is_visible": True})
    return {"ok": True, "id": game_id, "is_visible": True}


@router.post("/admin/games/{game_id}/hide")
async def hide_game(
        game_id: str,
        admin: User = Depends(require_admin)
):
    """Hide a custom game so users cannot see it in the lobby or /symbols."""
    doc_ref = db.collection("custom_games").document(game_id)
    doc = await doc_ref.get()

    if not doc.exists:
        raise HTTPException(status_code=404, detail="Game not found")

    await doc_ref.update({"is_visible": False})
    return {"ok": True, "id": game_id, "is_visible": False}


@router.post("/admin/games/{game_id}/pause")
async def pause_game(
        game_id: str,
        admin: User = Depends(require_admin)
):
    """Pause trading for a custom game."""
    doc_ref = db.collection("custom_games").document(game_id)
    doc = await doc_ref.get()

    if not doc.exists:
        raise HTTPException(status_code=404, detail="Game not found")

    await doc_ref.update({"is_paused": True})
    return {"ok": True, "id": game_id, "is_paused": True}


@router.post("/admin/games/{game_id}/resume")
async def resume_game(
        game_id: str,
        admin: User = Depends(require_admin)
):
    """Resume trading for a custom game."""
    doc_ref = db.collection("custom_games").document(game_id)
    doc = await doc_ref.get()

    if not doc.exists:
        raise HTTPException(status_code=404, detail="Game not found")

    await doc_ref.update({"is_paused": False})
    return {"ok": True, "id": game_id, "is_paused": False}


class ResolveGame(BaseModel):
    expected_value: float


@router.post("/admin/games/{game_id}/resolve")
async def resolve_game(
        game_id: str,
        resolve_data: ResolveGame,
        admin: User = Depends(require_admin)
):
    """Update the expected value for a custom game (used for final P&L calculation)"""
    doc_ref = db.collection("custom_games").document(game_id)
    doc = await doc_ref.get()

    if not doc.exists:
        raise HTTPException(status_code=404, detail="Game not found")
        
    db_game_data = doc.to_dict()

    # Update the expected value
    old_value = db_game_data.get("expected_value", 0.0)
    
    await doc_ref.update({
        "expected_value": resolve_data.expected_value,
        "updated_at": dt.datetime.utcnow()
    })

    return {
        "ok": True,
        "message": f"Expected value updated from ${old_value:.2f} to ${resolve_data.expected_value:.2f}",
        "old_value": old_value,
        "new_value": resolve_data.expected_value
    }


@router.post("/admin/users/{user_id}/blacklist")
async def blacklist_user(
        user_id: str,
        admin: User = Depends(require_admin)
):
    """Blacklist a user"""
    doc_ref = db.collection("users").document(user_id)
    doc = await doc_ref.get()
    
    if not doc.exists:
        raise HTTPException(status_code=404, detail="User not found")
    
    u_data = doc.to_dict()
    if u_data.get("is_admin"):
         raise HTTPException(status_code=400, detail="Cannot blacklist admin users")

    await doc_ref.update({"is_blacklisted": True})

    return {"ok": True, "message": f"User {u_data.get('username')} blacklisted"}


@router.post("/admin/users/{user_id}/unblacklist")
async def unblacklist_user(
        user_id: str,
        admin: User = Depends(require_admin)
):
    """Remove blacklist from a user"""
    doc_ref = db.collection("users").document(user_id)
    doc = await doc_ref.get()
    
    if not doc.exists:
        raise HTTPException(status_code=404, detail="User not found")

    await doc_ref.update({"is_blacklisted": False})
    
    u_data = doc.to_dict()
    return {"ok": True, "message": f"User {u_data.get('username')} unblacklisted"}


@router.delete("/admin/users/{user_id}")
async def delete_user(
        user_id: str,
        admin: User = Depends(require_admin)
):
    """Delete a user and all their data"""
    doc_ref = db.collection("users").document(user_id)
    doc = await doc_ref.get()
    
    if not doc.exists:
        raise HTTPException(status_code=404, detail="User not found")
        
    u_data = doc.to_dict()
    if u_data.get("is_admin"):
        raise HTTPException(status_code=400, detail="Cannot delete admin users")

    # Delete user's orders
    orders_ref = db.collection("orders")
    o_docs = await orders_ref.where("user_id", "==", user_id).get()
    for d in o_docs:
        await d.reference.delete()

    # Delete user's trades (simpler to query separately for buyer/seller or just iterate if not too many)
    # A cleaner way is complex OR query or two queries.
    trades_ref = db.collection("trades")
    t_docs_1 = await trades_ref.where("buyer_id", "==", user_id).get()
    t_docs_2 = await trades_ref.where("seller_id", "==", user_id).get()
    
    # Use a set of IDs to avoid double delete if user bought from themselves (unlikely but possible logic)
    t_ids = set()
    for d in t_docs_1 + t_docs_2:
        if d.id not in t_ids:
            await d.reference.delete()
            t_ids.add(d.id)

    # Delete user
    await doc_ref.delete()

    return {"ok": True, "message": f"User {u_data.get('username')} deleted"}


@router.post("/admin/reset-all")
async def reset_all_users(
        admin: User = Depends(require_admin)
):
    """Reset all users to initial state"""
    # Reset all user balances except admins
    users_ref = db.collection("users")
    # Firestore doesn't support "not equal" easily combined with updates without iterating
    # We'll select all and filter in app, or use != query if index exists
    
    # Simple iteration for safety
    docs = await users_ref.get()
    batch = db.batch()
    
    for d in docs:
        u = d.to_dict()
        if not u.get("is_admin"):
             batch.update(d.reference, {"balance": 10000.0})
    
    await batch.commit()

    # Delete all trades
    # To delete all validly we must list and delete chunks
    async def delete_all_in_collection(coll_name, batch_size=50):
        coll_ref = db.collection(coll_name)
        while True:
            docs = await coll_ref.limit(batch_size).get()
            if not docs:
                break
            for d in docs:
                await d.reference.delete()

    await delete_all_in_collection("trades")
    await delete_all_in_collection("orders")

    # Clear in-memory order book
    from app.order_book import clear_all_orders
    clear_all_orders()

    return {"ok": True, "message": "All users reset to initial state"}


@router.post("/admin/news")
async def admin_create_news(
        payload: NewsCreate,
        admin: User = Depends(require_admin)
):
    """Admin add news"""
    news_id = str(uuid.uuid4())
    item = MarketNews(
        id=news_id,
        content=payload.content,
        created_at=dt.datetime.utcnow()
    )
    
    await db.collection("market_news").document(news_id).set(item.model_dump(exclude={"id"}))
    
    return {
        "ok": True,
        "news": {
            "id": news_id,
            "content": item.content,
            "created_at": item.created_at.isoformat(),
        },
    }


@router.put("/admin/news/{news_id}")
async def admin_update_news(
        news_id: str,
        payload: NewsUpdate,
        admin: User = Depends(require_admin)
):
    """Admin edit existing news"""
    doc_ref = db.collection("market_news").document(news_id)
    doc = await doc_ref.get()
    
    if not doc.exists:
        raise HTTPException(status_code=404, detail="News not found")

    await doc_ref.update({
        "content": payload.content,
        # "updated_at": dt.datetime.utcnow() 
    })
    
    # Fetch updated if needed, or just return payload
    item_data = doc.to_dict()

    return {
        "ok": True,
        "news": {
            "id": news_id,
            "content": payload.content,
            "created_at": item_data.get("created_at").isoformat() if item_data.get("created_at") else "",
        },
    }


@router.delete("/admin/news/{news_id}")
async def admin_delete_news(
        news_id: str,
        admin: User = Depends(require_admin)
):
    """Admin delete news"""
    doc_ref = db.collection("market_news").document(news_id)
    doc = await doc_ref.get()
    
    if not doc.exists:
        raise HTTPException(status_code=404, detail="News not found")

    await doc_ref.delete()
    return {"ok": True}