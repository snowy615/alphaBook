from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlmodel import Session, select, func
from app.db import get_session
from app.auth import current_user
from app.models import User, Order, Trade, CustomGame
from fastapi.templating import Jinja2Templates
from pathlib import Path
from pydantic import BaseModel
import datetime as dt

router = APIRouter()
BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def require_admin(user: User = Depends(current_user)):
    """Dependency to check if user is admin"""
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


@router.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(
        request: Request,
        user: User = Depends(require_admin),
        session: Session = Depends(get_session)
):
    """Admin dashboard page"""
    # Get all users
    users = session.exec(select(User)).all()

    # Calculate stats for each user
    user_stats = []
    for u in users:
        # Count orders
        orders_count = session.exec(
            select(func.count(Order.id)).where(Order.user_id == u.id)
        ).one()

        # Count trades
        trades_count = session.exec(
            select(func.count(Trade.id)).where(
                (Trade.buyer_id == u.id) | (Trade.seller_id == u.id)
            )
        ).one()

        # Calculate P&L
        pnl = u.balance - 10000.0

        user_stats.append({
            "id": u.id,
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
    games = session.exec(select(CustomGame).where(CustomGame.is_active == True)).all()

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


@router.post("/admin/games")
async def create_game(
        game: GameCreate,
        admin: User = Depends(require_admin),
        session: Session = Depends(get_session)
):
    """Create a new custom game"""
    # Validate symbol format
    symbol = game.symbol.upper().strip()
    if not symbol.startswith("GAME"):
        raise HTTPException(status_code=400, detail="Symbol must start with 'GAME'")

    # Check for invalid characters (only alphanumeric allowed)
    if not symbol.replace('GAME', '').replace('_', '').isalnum():
        raise HTTPException(status_code=400,
                            detail="Symbol can only contain letters, numbers, and underscores (no spaces or special characters)")

    # Check if symbol already exists
    existing = session.exec(select(CustomGame).where(CustomGame.symbol == symbol)).first()
    if existing:
        raise HTTPException(status_code=400, detail=f"Game with symbol {symbol} already exists")

    new_game = CustomGame(
        symbol=symbol,
        name=game.name,
        instructions=game.instructions,
        expected_value=game.expected_value,
        is_active=True,
        created_by=admin.id
    )

    session.add(new_game)
    session.commit()
    session.refresh(new_game)

    # Add to order books
    from app.state import books
    from app.order_book import OrderBook
    books[symbol] = OrderBook()

    return {"ok": True, "game": {
        "id": new_game.id,
        "symbol": new_game.symbol,
        "name": new_game.name,
        "instructions": new_game.instructions,
        "expected_value": new_game.expected_value
    }}


@router.put("/admin/games/{game_id}")
async def update_game(
        game_id: int,
        game: GameCreate,
        admin: User = Depends(require_admin),
        session: Session = Depends(get_session)
):
    """Update a custom game"""
    db_game = session.get(CustomGame, game_id)
    if not db_game:
        raise HTTPException(status_code=404, detail="Game not found")

    db_game.name = game.name
    db_game.instructions = game.instructions
    db_game.expected_value = game.expected_value
    db_game.updated_at = dt.datetime.utcnow()

    session.add(db_game)
    session.commit()

    return {"ok": True, "message": "Game updated"}


@router.delete("/admin/games/{game_id}")
async def delete_game(
        game_id: int,
        admin: User = Depends(require_admin),
        session: Session = Depends(get_session)
):
    """Deactivate a custom game"""
    db_game = session.get(CustomGame, game_id)
    if not db_game:
        raise HTTPException(status_code=404, detail="Game not found")

    db_game.is_active = False
    db_game.updated_at = dt.datetime.utcnow()

    session.add(db_game)
    session.commit()

    return {"ok": True, "message": "Game deactivated"}


@router.post("/admin/users/{user_id}/blacklist")
async def blacklist_user(
        user_id: int,
        admin: User = Depends(require_admin),
        session: Session = Depends(get_session)
):
    """Blacklist a user"""
    user = session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if user.is_admin:
        raise HTTPException(status_code=400, detail="Cannot blacklist admin users")

    user.is_blacklisted = True
    session.add(user)
    session.commit()

    return {"ok": True, "message": f"User {user.username} blacklisted"}


@router.post("/admin/users/{user_id}/unblacklist")
async def unblacklist_user(
        user_id: int,
        admin: User = Depends(require_admin),
        session: Session = Depends(get_session)
):
    """Remove blacklist from a user"""
    user = session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user.is_blacklisted = False
    session.add(user)
    session.commit()

    return {"ok": True, "message": f"User {user.username} unblacklisted"}


@router.delete("/admin/users/{user_id}")
async def delete_user(
        user_id: int,
        admin: User = Depends(require_admin),
        session: Session = Depends(get_session)
):
    """Delete a user and all their data"""
    user = session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if user.is_admin:
        raise HTTPException(status_code=400, detail="Cannot delete admin users")

    # Delete user's orders
    orders = session.exec(select(Order).where(Order.user_id == user_id)).all()
    for order in orders:
        session.delete(order)

    # Delete user's trades
    trades = session.exec(
        select(Trade).where((Trade.buyer_id == user_id) | (Trade.seller_id == user_id))
    ).all()
    for trade in trades:
        session.delete(trade)

    # Delete user
    session.delete(user)
    session.commit()

    return {"ok": True, "message": f"User {user.username} deleted"}


@router.post("/admin/reset-all")
async def reset_all_users(
        admin: User = Depends(require_admin),
        session: Session = Depends(get_session)
):
    """Reset all users to initial state"""
    # Reset all user balances except admins
    users = session.exec(select(User).where(User.is_admin == False)).all()

    for user in users:
        user.balance = 10000.0
        session.add(user)

    # Delete all trades
    trades = session.exec(select(Trade)).all()
    for trade in trades:
        session.delete(trade)

    # Delete all orders
    orders = session.exec(select(Order)).all()
    for order in orders:
        session.delete(order)

    session.commit()

    # Clear in-memory order book
    from app.order_book import clear_all_orders
    clear_all_orders()

    return {"ok": True, "message": "All users reset to initial state"}