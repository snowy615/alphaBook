from __future__ import annotations
import time, datetime as dt
from typing import Any, Dict, List, Optional
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from app.db import get_session
from app.auth import current_user

# In-memory state helpers
from app.state import list_user_orders, cancel_order_by_id

# DB models
try:
    from app.models import Trade as _FillModel
except Exception:
    try:
        from app.models import Fill as _FillModel
    except Exception:
        try:
            from app.models import Execution as _FillModel
        except Exception:
            _FillModel = None

try:
    from app.models import Order as _OrderModel
except Exception:
    _OrderModel = None

router = APIRouter(prefix="", tags=["me"])


def _now_ms() -> int:
    return int(time.time() * 1000)


@router.get("/me")
def me(user=Depends(current_user)):
    """Simple identity endpoint used by the header/login UI."""
    name = getattr(user, "username", None) or getattr(user, "name", None) or getattr(user, "email", None)
    return {"id": user.id, "username": name or f"user-{user.id}"}


@router.get("/me/summary")
def me_summary(
        user=Depends(current_user),
        session: Session = Depends(get_session),
):
    """Calculate positions and P&L from Trade table."""
    from app.market_data import get_ref_price

    if _FillModel is None:
        return {
            "positions": [],
            "totals": {
                "qty": 0.0, "notional": 0.0, "avg_cost": None, "delta": 0.0,
                "pnl_open": 0.0, "pnl_day": 0.0, "cash": 10000.0, "equity": 10000.0,
            },
        }

    # Fetch all trades for this user
    stmt = select(_FillModel).where(
        (_FillModel.buyer_id == user.id) | (_FillModel.seller_id == user.id)
    ).order_by(_FillModel.created_at)

    trades = session.exec(stmt).all()

    # Build positions from trades
    positions = {}

    for trade in trades:
        symbol = trade.symbol
        price = Decimal(trade.price)
        qty = Decimal(trade.qty)

        if symbol not in positions:
            positions[symbol] = {
                "qty": Decimal("0"),
                "total_cost": Decimal("0"),
                "realized_pnl": Decimal("0"),
            }

        pos = positions[symbol]

        # User is the buyer - going long
        if trade.buyer_id == user.id:
            if pos["qty"] >= 0:
                # Opening or adding to long
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

        # User is the seller - going short
        if trade.seller_id == user.id:
            if pos["qty"] <= 0:
                # Opening or adding to short
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

    # Calculate totals
    total_qty = Decimal("0")
    total_notional = Decimal("0")
    total_unrealized = Decimal("0")
    total_realized = Decimal("0")

    for symbol, pos in positions.items():
        qty = pos["qty"]
        total_cost = pos["total_cost"]
        realized = pos["realized_pnl"]

        ref_price = get_ref_price(symbol) or 0.0
        ref_decimal = Decimal(str(ref_price))

        # Calculate average cost
        avg_cost = total_cost / abs(qty) if qty != 0 else Decimal("0")

        # Calculate unrealized P&L
        if qty > 0:
            unrealized = (ref_decimal - avg_cost) * qty
        elif qty < 0:
            unrealized = (avg_cost - ref_decimal) * abs(qty)
        else:
            unrealized = Decimal("0")

        notional = ref_decimal * abs(qty)

        total_qty += qty
        total_notional += notional
        total_unrealized += unrealized
        total_realized += realized

    avg_cost = (total_notional / abs(total_qty)) if total_qty != 0 else None
    starting_cash = 10000.0
    cash = starting_cash + float(total_realized) - float(total_notional)
    equity = cash + float(total_unrealized)

    return {
        "positions": [],
        "totals": {
            "qty": float(total_qty),
            "notional": float(total_notional),
            "avg_cost": float(avg_cost) if avg_cost else None,
            "delta": float(total_qty),
            "pnl_open": float(total_unrealized),
            "pnl_day": float(total_unrealized),
            "cash": cash,
            "equity": equity,
        },
    }


@router.get("/me/pnl")
def me_pnl(
        user=Depends(current_user),
        session: Session = Depends(get_session),
):
    """Return a time series for the P&L chart."""
    pts: List[Dict[str, float]] = []

    if _FillModel is None:
        return {"points": pts}

    stmt = select(_FillModel).where(
        (_FillModel.buyer_id == user.id) | (_FillModel.seller_id == user.id)
    ).order_by(_FillModel.created_at)

    trades = session.exec(stmt).all()

    # Track positions to calculate realized P&L over time
    positions: Dict[str, Dict[str, Decimal]] = {}
    cumulative_pnl = Decimal("0")

    for trade in trades:
        symbol = trade.symbol
        price = Decimal(trade.price)
        qty = Decimal(trade.qty)

        if symbol not in positions:
            positions[symbol] = {"qty": Decimal("0"), "total_cost": Decimal("0")}

        pos = positions[symbol]
        realized_this_trade = Decimal("0")

        if trade.buyer_id == user.id:
            # User is buying
            if pos["qty"] < 0:
                # Covering short
                close_qty = min(qty, abs(pos["qty"]))
                if pos["qty"] != 0:
                    avg_short = abs(pos["total_cost"] / pos["qty"])
                    realized_this_trade = (avg_short - price) * close_qty

                remaining = qty - close_qty
                pos["qty"] += qty

                if pos["qty"] > 0:
                    pos["total_cost"] = price * remaining
                elif pos["qty"] == 0:
                    pos["total_cost"] = Decimal("0")
            else:
                # Adding to long
                pos["total_cost"] += price * qty
                pos["qty"] += qty

        if trade.seller_id == user.id:
            # User is selling
            if pos["qty"] > 0:
                # Closing long
                close_qty = min(qty, pos["qty"])
                if pos["qty"] != 0:
                    avg_long = pos["total_cost"] / pos["qty"]
                    realized_this_trade = (price - avg_long) * close_qty

                remaining = qty - close_qty
                pos["qty"] -= qty

                if pos["qty"] < 0:
                    pos["total_cost"] = price * remaining
                elif pos["qty"] == 0:
                    pos["total_cost"] = Decimal("0")
            else:
                # Adding to short
                pos["total_cost"] += price * qty
                pos["qty"] -= qty

        cumulative_pnl += realized_this_trade

        ts = trade.created_at
        if ts and hasattr(ts, "timestamp"):
            tms = int(ts.timestamp() * 1000)
        else:
            tms = _now_ms()

        pts.append({"t": tms, "y": float(cumulative_pnl)})

    return {"points": pts}


# ----------------------- OPEN ORDERS + CANCEL -----------------------

def _normalize_open_orders(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Make sure the shape is friendly to the frontend."""
    out: List[Dict[str, Any]] = []
    for r in rows or []:
        qty = float(r.get("qty") or r.get("quantity") or 0.0)
        filled = float(r.get("filled_qty") or r.get("filled") or r.get("executed_qty") or 0.0)
        out.append({
            "id": str(r.get("id") or r.get("order_id") or ""),
            "symbol": r.get("symbol") or r.get("sym") or "",
            "side": str(r.get("side") or "").upper(),
            "price": float(r.get("price") or r.get("px") or 0.0),
            "qty": qty,
            "filled_qty": filled,
            "remaining": max(qty - filled, 0.0),
            "status": r.get("status") or "OPEN",
            "created_at": r.get("created_at") or r.get("ts") or "",
        })
    out.sort(key=lambda v: v.get("created_at") or "", reverse=True)
    return out


@router.get("/me/orders", include_in_schema=False)
def my_open_orders(
        user=Depends(current_user),
        session: Session = Depends(get_session)
) -> List[Dict[str, Any]]:
    """List open orders from database."""
    if _OrderModel is None:
        return []

    stmt = select(_OrderModel).where(
        _OrderModel.user_id == user.id,
        _OrderModel.status == "OPEN"
    ).order_by(_OrderModel.created_at.desc())

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

    return _normalize_open_orders(rows)


def _cancel_any(order_id: str, user, session: Session) -> Dict[str, Any]:
    """Cancel order from both memory and database."""

    # First, check if order exists in database and belongs to user
    if _OrderModel:
        stmt = select(_OrderModel).where(
            _OrderModel.order_id == order_id,
            _OrderModel.user_id == user.id,
            _OrderModel.status == "OPEN"
        )
        db_order = session.exec(stmt).first()

        if not db_order:
            raise HTTPException(status_code=404, detail="Order not found or already closed")

        # Try to cancel from in-memory book (may not exist if partially filled)
        try:
            cancel_order_by_id(order_id, str(user.id))
        except Exception:
            # Order might not be in memory anymore, that's ok
            pass

        # Always update database status
        db_order.status = "CANCELED"
        db_order.updated_at = dt.datetime.utcnow()
        session.add(db_order)
        session.commit()

        return {"ok": True, "status": "CANCELED"}

    # Fallback: try memory only
    try:
        ok = cancel_order_by_id(order_id, str(user.id))
        if ok:
            return {"ok": True, "status": "CANCELED"}
    except Exception:
        pass

    raise HTTPException(status_code=404, detail="Order not found")

@router.post("/me/orders/{order_id}/cancel", include_in_schema=False)
def cancel_my_order_post(
        order_id: str,
        user=Depends(current_user),
        session: Session = Depends(get_session),
):
    """Cancel order (POST)."""
    return _cancel_any(order_id, user, session)


@router.delete("/orders/{order_id}", include_in_schema=False)
def cancel_my_order_delete(
        order_id: str,
        user=Depends(current_user),
        session: Session = Depends(get_session),
):
    """Cancel order (DELETE)."""
    return _cancel_any(order_id, user, session)