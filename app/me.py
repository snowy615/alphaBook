from __future__ import annotations
import time, datetime as dt
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from app.db import get_session
from app.auth import current_user

# Try to locate a fills/trades-like model; fall back to Order, else None.
# This keeps the app running even if your schema doesn't include 'Trade'.
try:
    from app.models import Trade as _FillModel  # type: ignore
except Exception:
    try:
        from app.models import Fill as _FillModel  # type: ignore
    except Exception:
        try:
            from app.models import Execution as _FillModel  # type: ignore
        except Exception:
            _FillModel = None  # type: ignore

try:
    from app.models import Order as _OrderModel  # type: ignore
except Exception:
    _OrderModel = None  # type: ignore

router = APIRouter(prefix="", tags=["me"])


def _now_ms() -> int:
    return int(time.time() * 1000)


@router.get("/me")
def me(user=Depends(current_user)):
    """Simple identity endpoint used by the header/login UI."""
    # Works with either 'username' or 'name' present on your User model
    name = getattr(user, "username", None) or getattr(user, "name", None) or getattr(user, "email", None)
    return {"id": user.id, "username": name or f"user-{user.id}"}


@router.get("/me/summary")
def me_summary(
    user=Depends(current_user),
    session: Session = Depends(get_session),
):
    """
    Aggregate light-weight position + P&L summary.

    - If a fills/trades table exists: builds positions from those.
    - Else if only 'Order' exists and has status/filled fields, uses filled qty.
    - Else returns zeros so the UI still renders.
    """
    positions: Dict[str, Dict[str, float]] = {}
    cash = 0.0  # keep zero unless you track deposits/withdrawals separately

    def add_fill(sym: str, side: str, qty: float, px: float):
        if not sym or qty <= 0 or not (px == px):  # NaN guard
            return
        side_u = str(side).upper()
        sign = 1.0 if side_u == "BUY" else -1.0
        p = positions.setdefault(sym, {"qty": 0.0, "notional": 0.0})
        p["qty"] += sign * qty
        p["notional"] += sign * qty * px

    # Path A: use fills/trades if present
    if _FillModel is not None:
        q = select(_FillModel).where(getattr(_FillModel, "user_id") == user.id)
        rows = session.exec(q).all()
        for f in rows:
            sym = getattr(f, "symbol", None) or getattr(f, "sym", None) or ""
            side = getattr(f, "side", "BUY")
            qty = float(getattr(f, "qty", 0) or getattr(f, "quantity", 0) or 0)
            px = float(getattr(f, "price", 0) or getattr(f, "px", 0) or 0)
            add_fill(sym, side, qty, px)

    # Path B: fall back to 'filled' portion of Orders if available
    elif _OrderModel is not None:
        q = select(_OrderModel).where(getattr(_OrderModel, "user_id") == user.id)
        rows = session.exec(q).all()
        for o in rows:
            sym = getattr(o, "symbol", None) or getattr(o, "sym", None) or ""
            side = getattr(o, "side", "BUY")
            # prefer filled_qty if available, else qty for older schemas
            qty = float(getattr(o, "filled_qty", None) or getattr(o, "qty", 0) or 0)
            px = float(getattr(o, "avg_price", None) or getattr(o, "price", 0) or getattr(o, "px", 0) or 0)
            if qty > 0 and px > 0:
                add_fill(sym, side, qty, px)

    # Totals across symbols
    total_qty = sum(p["qty"] for p in positions.values()) if positions else 0.0
    total_notional = sum(p["notional"] for p in positions.values()) if positions else 0.0
    avg_cost = (abs(total_notional) / abs(total_qty)) if (total_qty and abs(total_qty) > 1e-12) else None

    # Without a pricing source on the backend we report mark-like fields as 0
    pnl_open = 0.0
    pnl_day = 0.0
    delta = total_qty  # treat 1 share as 1 delta in a stock-only demo

    equity = (cash + pnl_open)

    return {
        "positions": [{"symbol": s, **v} for s, v in positions.items()],
        "totals": {
            "qty": total_qty,
            "notional": total_notional,
            "avg_cost": avg_cost,
            "delta": delta,
            "pnl_open": pnl_open,
            "pnl_day": pnl_day,
            "cash": cash,
            "equity": equity,
        },
    }


@router.get("/me/pnl")
def me_pnl(
    user=Depends(current_user),
    session: Session = Depends(get_session),
):
    """
    Return a time series for the P&L chart.

    If a fills/trades table exists, we return cumulative cashflow as a proxy P&L:
      PnL_t = -sum(BUY qty*px) + sum(SELL qty*px)
    Otherwise, return an empty series (UI still renders).
    """
    pts: List[Dict[str, float]] = []

    if _FillModel is None:
        return {"points": pts}

    q = select(_FillModel).where(getattr(_FillModel, "user_id") == user.id)
    # Try ordering by a time column if present
    try:
        q = q.order_by(getattr(_FillModel, "created_at"))
    except Exception:
        pass

    pnl = 0.0
    for f in session.exec(q).all():
        side = str(getattr(f, "side", "BUY")).upper()
        qty = float(getattr(f, "qty", 0) or getattr(f, "quantity", 0) or 0)
        px = float(getattr(f, "price", 0) or getattr(f, "px", 0) or 0)
        # Cashflow convention: BUY is negative cash, SELL is positive
        pnl += (-qty * px) if side == "BUY" else (qty * px)

        ts = getattr(f, "created_at", None)
        if ts and hasattr(ts, "timestamp"):
            tms = int(ts.timestamp() * 1000)
        else:
            tms = _now_ms()
        pts.append({"t": tms, "y": pnl})

    return {"points": pts}


# ----------------------- OPEN ORDERS + CANCEL -----------------------

@router.get("/me/orders", include_in_schema=False)
def my_open_orders(
    user=Depends(current_user),
    session: Session = Depends(get_session),
):
    """
    List this user's OPEN orders. Works against different schemas by probing
    common field names and filtering out filled/canceled states.
    """
    if _OrderModel is None:
        return []

    q = select(_OrderModel).where(getattr(_OrderModel, "user_id") == user.id)
    rows = session.exec(q).all()

    def status_of(o) -> str:
        s = getattr(o, "status", None) or getattr(o, "state", None) or "NEW"
        return str(s).upper()

    open_rows = []
    for o in rows:
        st = status_of(o)
        if st in {"CANCELED", "CANCELLED", "FILLED", "DONE", "REJECTED"}:
            continue
        open_rows.append({
            "id": getattr(o, "id", None),
            "symbol": getattr(o, "symbol", "") or getattr(o, "sym", ""),
            "side": getattr(o, "side", ""),
            "qty": float(getattr(o, "qty", 0) or getattr(o, "quantity", 0) or 0),
            "price": float(getattr(o, "price", 0) or getattr(o, "px", 0) or 0),
            "status": st,
            "created_at": str(
                getattr(o, "created_at", "")
                or getattr(o, "ts", "")
                or getattr(o, "created", "")
                or ""
            ),
        })

    return open_rows


@router.delete("/orders/{order_id}", include_in_schema=False)
def cancel_my_order(
    order_id: int,
    user=Depends(current_user),
    session: Session = Depends(get_session),
):
    """
    Cancel a single order owned by the current user. If the order is already
    terminal (FILLED/CANCELED/etc), returns OK with that status.
    """
    if _OrderModel is None:
        raise HTTPException(status_code=404, detail="Orders not supported")

    o = session.get(_OrderModel, order_id)
    if not o or getattr(o, "user_id", None) != user.id:
        raise HTTPException(status_code=404, detail="Order not found")

    st = str(getattr(o, "status", "") or getattr(o, "state", "") or "").upper()
    if st in {"CANCELED", "CANCELLED", "FILLED", "DONE", "REJECTED"}:
        return {"ok": True, "status": st}

    # Flip common fields to CANCELED
    if hasattr(o, "status"):
        setattr(o, "status", "CANCELED")
    if hasattr(o, "state"):
        setattr(o, "state", "CANCELED")
    if hasattr(o, "is_active"):
        try:
            setattr(o, "is_active", False)
        except Exception:
            pass

    # (optional) try to notify a matching engine if you have one
    try:
        from app.matching import cancel_order as engine_cancel  # adjust if present
        try:
            engine_cancel(order_id)
        except Exception:
            pass
    except Exception:
        pass

    session.add(o)
    session.commit()
    return {"ok": True, "status": "CANCELED"}
