from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlmodel import Session, select, func
from app.db import get_session
from app.auth import current_user
from app.models import User, Order, Trade, CustomGame, MarketNews
from fastapi.templating import Jinja2Templates
from pathlib import Path
from pydantic import BaseModel
import datetime as dt
from decimal import Decimal

router = APIRouter()
BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def require_admin(user: User = Depends(current_user)):
    """Dependency to check if user is admin"""
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


def calculate_user_pnl(user_id: int, session: Session) -> float:
    """Calculate P&L for a user considering expected values for custom games"""
    from app.market_data import get_ref_price
    import logging

    log = logging.getLogger("admin")

    # Get all trades for this user
    trades = session.exec(
        select(Trade).where(
            (Trade.buyer_id == user_id) | (Trade.seller_id == user_id)
        )
    ).all()

    log.info(f"Calculating P&L for user {user_id}, found {len(trades)} trades")

    # Get all custom games with their expected values
    games = session.exec(select(CustomGame)).all()
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

        # Calculate P&L using expected values for games
        pnl = calculate_user_pnl(u.id, session)

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


class NewsCreate(BaseModel):
    content: str  # creat news


class NewsUpdate(BaseModel):
    content: str  # edit news


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

@router.post("/admin/games/{game_id}/show")
async def show_game(
        game_id: int,
        admin: User = Depends(require_admin),
        session: Session = Depends(get_session)
):
    """Mark a custom game as visible in the lobby and /symbols."""
    game = session.get(CustomGame, game_id)
    if not game:
        raise HTTPException(status_code=404, detail="Game not found")

    game.is_visible = True
    session.add(game)
    session.commit()
    session.refresh(game)
    return {"ok": True, "id": game.id, "is_visible": game.is_visible}


@router.post("/admin/games/{game_id}/hide")
async def hide_game(
        game_id: int,
        admin: User = Depends(require_admin),
        session: Session = Depends(get_session)
):
    """Hide a custom game so users cannot see it in the lobby or /symbols."""
    game = session.get(CustomGame, game_id)
    if not game:
        raise HTTPException(status_code=404, detail="Game not found")

    game.is_visible = False
    session.add(game)
    session.commit()
    session.refresh(game)
    return {"ok": True, "id": game.id, "is_visible": game.is_visible}


@router.post("/admin/games/{game_id}/pause")
async def pause_game(
        game_id: int,
        admin: User = Depends(require_admin),
        session: Session = Depends(get_session)
):
    """Pause trading for a custom game."""
    game = session.get(CustomGame, game_id)
    if not game:
        raise HTTPException(status_code=404, detail="Game not found")

    game.is_paused = True
    session.add(game)
    session.commit()
    session.refresh(game)
    return {"ok": True, "id": game.id, "is_paused": game.is_paused}


@router.post("/admin/games/{game_id}/resume")
async def resume_game(
        game_id: int,
        admin: User = Depends(require_admin),
        session: Session = Depends(get_session)
):
    """Resume trading for a custom game."""
    game = session.get(CustomGame, game_id)
    if not game:
        raise HTTPException(status_code=404, detail="Game not found")

    game.is_paused = False
    session.add(game)
    session.commit()
    session.refresh(game)
    return {"ok": True, "id": game.id, "is_paused": game.is_paused}


class ResolveGame(BaseModel):
    expected_value: float


@router.post("/admin/games/{game_id}/resolve")
async def resolve_game(
        game_id: int,
        resolve_data: ResolveGame,
        admin: User = Depends(require_admin),
        session: Session = Depends(get_session)
):
    """Update the expected value for a custom game (used for final P&L calculation)"""
    db_game = session.get(CustomGame, game_id)
    if not db_game:
        raise HTTPException(status_code=404, detail="Game not found")

    # Update the expected value
    old_value = db_game.expected_value
    db_game.expected_value = resolve_data.expected_value
    db_game.updated_at = dt.datetime.utcnow()

    session.add(db_game)
    session.commit()

    return {
        "ok": True,
        "message": f"Expected value updated from ${old_value:.2f} to ${resolve_data.expected_value:.2f}",
        "old_value": old_value,
        "new_value": resolve_data.expected_value
    }


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


@router.post("/admin/news")
async def admin_create_news(
        payload: NewsCreate,
        admin: User = Depends(require_admin),
        session: Session = Depends(get_session),
):
    """Admin add news"""
    item = MarketNews(content=payload.content)
    session.add(item)
    session.commit()
    session.refresh(item)
    return {
        "ok": True,
        "news": {
            "id": item.id,
            "content": item.content,
            "created_at": item.created_at.isoformat(),
        },
    }


@router.put("/admin/news/{news_id}")
async def admin_update_news(
        news_id: int,
        payload: NewsUpdate,
        admin: User = Depends(require_admin),
        session: Session = Depends(get_session),
):
    """Admin edit existing news"""
    item = session.get(MarketNews, news_id)
    if not item:
        raise HTTPException(status_code=404, detail="News not found")

    item.content = payload.content
    session.add(item)
    session.commit()
    session.refresh(item)

    return {
        "ok": True,
        "news": {
            "id": item.id,
            "content": item.content,
            "created_at": item.created_at.isoformat(),
        },
    }


@router.delete("/admin/news/{news_id}")
async def admin_delete_news(
        news_id: int,
        admin: User = Depends(require_admin),
        session: Session = Depends(get_session),
):
    """Admin delete news"""
    item = session.get(MarketNews, news_id)
    if not item:
        raise HTTPException(status_code=404, detail="News not found")

    session.delete(item)
    session.commit()
    return {"ok": True}