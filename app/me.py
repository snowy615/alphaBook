from __future__ import annotations
import time, datetime as dt
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from app.db import get_session
from app.auth import current_user

# In-memory state helpers (your central source of truth for the live book)
from app.state import list_user_orders, cancel_order_by_id

# --- Optional DB fallbacks (kept for compatibility if you also persist orders/fills)
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
            qty = float(getattr(o, "filled_qty", None) or getattr(o, "qty", 0) or 0)
            px = float(getattr(o, "avg_price", None) or getattr(o, "price", 0) or getattr(o, "px", 0) or 0)
            if qty > 0 and px > 0:
                add_fill(sym, side, qty, px)

    total_qty = sum(p["qty"] for p in positions.values()) if positions else 0.0
    total_notional = sum(p["notional"] for p in positions.values()) if positions else 0.0
    avg_cost = (abs(total_notional) / abs(total_qty)) if (total_qty and abs(total_qty) > 1e-12) else None

    pnl_open = 0.0
    pnl_day = 0.0
    delta = total_qty  # 1 share ≈ 1 delta in stock-only demo
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
    try:
        q = q.order_by(getattr(_FillModel, "created_at"))
    except Exception:
        pass

    pnl = 0.0
    for f in session.exec(q).all():
        side = str(getattr(f, "side", "BUY")).upper()
        qty = float(getattr(f, "qty", 0) or getattr(f, "quantity", 0) or 0)
        px = float(getattr(f, "price", 0) or getattr(f, "px", 0) or 0)
        pnl += (-qty * px) if side == "BUY" else (qty * px)

        ts = getattr(f, "created_at", None)
        if ts and hasattr(ts, "timestamp"):
            tms = int(ts.timestamp() * 1000)
        else:
            tms = _now_ms()
        pts.append({"t": tms, "y": pnl})

    return {"points": pts}


# ----------------------- OPEN ORDERS (OFFERS) + CANCEL -----------------------

def _normalize_open_orders(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Make sure the shape is friendly to the frontend (ids, numbers, created_at)."""
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
    # newest first if time present
    out.sort(key=lambda v: v.get("created_at") or "", reverse=True)
    return out


@router.get("/me/orders", include_in_schema=False)
def my_open_orders(user=Depends(current_user)) -> List[Dict[str, Any]]:
    """
    List this user's OPEN/WORKING offers from the in-memory books.
    Returns a PLAIN ARRAY (what app.js expects).
    """
    # Prefer the live in-memory book
    try:
        raw = list_user_orders(str(user.id))  # -> list[dict]
        return _normalize_open_orders(raw)
    except Exception:
        pass

    # Fallback to DB if available (kept for compatibility)
    if _OrderModel is None:
        return []
    # Note: we don't need a DB session for list_user_orders path; only for fallback.
    # Use a short-lived session for safety.
    return []  # if you want, you can paste your previous DB fallback here


def _cancel_db_order(order_id: str, user, session: Session) -> Dict[str, Any]:
    """DB fallback cancel (optional)."""
    if _OrderModel is None:
        raise HTTPException(status_code=404, detail="Orders not supported")

    # Many DB schemas use int ids, but our API path accepts str (UUID for memory book).
    # Best effort: try int conversion; if it fails, 404 for DB path.
    try:
        db_id = int(order_id)
    except Exception:
        raise HTTPException(status_code=404, detail="Order not found")

    o = session.get(_OrderModel, db_id)
    if not o or getattr(o, "user_id", None) != user.id:
        raise HTTPException(status_code=404, detail="Order not found")

    st = str(getattr(o, "status", "") or getattr(o, "state", "") or "").upper()
    if st in {"CANCELED", "CANCELLED", "FILLED", "DONE", "REJECTED"}:
        return {"ok": True, "status": st}

    if hasattr(o, "status"):
        setattr(o, "status", "CANCELLED")
    if hasattr(o, "state"):
        setattr(o, "state", "CANCELLED")
    if hasattr(o, "is_active"):
        try:
            setattr(o, "is_active", False)
        except Exception:
            pass
    session.add(o)
    session.commit()
    return {"ok": True, "status": "CANCELLED"}


def _cancel_any(order_id: str, user, session: Session) -> Dict[str, Any]:
    # 1) Try live in-memory cancel
    try:
        ok = cancel_order_by_id(order_id, str(user.id))
        if ok:
            return {"ok": True, "status": "CANCELLED"}
    except Exception:
        pass
    # 2) Optional DB fallback
    if _OrderModel is not None:
        return _cancel_db_order(order_id, user, session)
    raise HTTPException(status_code=404, detail="Order not found")


@router.post("/me/orders/{order_id}/cancel", include_in_schema=False)
def cancel_my_order_post(
    order_id: str,
    user=Depends(current_user),
    session: Session = Depends(get_session),
):
    """Primary route used by the frontend (POST)."""
    return _cancel_any(order_id, user, session)


@router.delete("/orders/{order_id}", include_in_schema=False)
def cancel_my_order_delete(
    order_id: str,
    user=Depends(current_user),
    session: Session = Depends(get_session),
):
    """Alias route (DELETE) used by your app.js."""
    return _cancel_any(order_id, user, session)
