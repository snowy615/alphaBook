# app/me.py
from __future__ import annotations
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Tuple

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlmodel import Session, select

from app.db import get_session
from app.models import Trade, User  # adjust if your names differ
from app.auth import current_user
from app.market_data import get_ref_price

router = APIRouter()

# In-memory PnL series per user_id: list[(ts_epoch_ms, pnl_value)]
_PNL_SERIES: Dict[int, List[Tuple[int, float]]] = {}

def _now_ms() -> int:
    return int(time.time() * 1000)

def _calc_from_trades(session: Session, user_id: int):
    """
    Positions, cash, MTM, exposures, pnl from the user's trades.
    Assumes equities (delta ≈ position). If your Trade schema differs, tweak the fields.
    """
    trades: List[Trade] = session.exec(
        select(Trade).where(Trade.user_id == user_id)
    ).all()

    positions: Dict[str, float] = {}
    cash = 0.0

    for t in trades:
        sym = t.symbol.upper()
        qty = float(t.qty)
        px  = float(t.price)
        # BUY increases position (+qty) and uses cash (-px*qty)
        # SELL decreases position (-qty) and receives cash (+px*qty)
        if str(t.side).upper() == "BUY":
            positions[sym] = positions.get(sym, 0.0) + qty
            cash -= px * qty
        else:
            positions[sym] = positions.get(sym, 0.0) - qty
            cash += px * qty

    mtm_value = 0.0
    gross_exposure = 0.0
    net_exposure = 0.0
    delta_total = 0.0

    pos_rows = []
    for sym, q in positions.items():
        ref = get_ref_price(sym) or 0.0
        value = q * ref
        mtm_value += value
        gross_exposure += abs(q) * ref
        net_exposure += value
        delta_total += q  # equity delta ≈ shares
        pos_rows.append({
            "symbol": sym,
            "qty": q,
            "ref_price": ref,
            "value": value,
            "delta": q,
        })

    pnl = cash + mtm_value  # relative to zero initial cash

    # sort positions by largest absolute value first for a nice UI
    pos_rows.sort(key=lambda r: abs(r["value"]), reverse=True)

    return {
        "positions": pos_rows,
        "totals": {
            "cash": cash,
            "mtm_value": mtm_value,
            "pnl": pnl,
            "gross_exposure": gross_exposure,
            "net_exposure": net_exposure,
            "delta": delta_total,
        },
    }

def _append_pnl_point(user_id: int, pnl_value: float, keep_seconds: int = 6 * 3600):
    series = _PNL_SERIES.setdefault(user_id, [])
    now = _now_ms()
    # De-duplicate if last point is within ~1s and identical
    if series and now - series[-1][0] < 1000 and abs(series[-1][1] - pnl_value) < 1e-9:
        return
    series.append((now, pnl_value))
    cutoff = now - keep_seconds * 1000
    # prune old points
    i = 0
    for i in range(len(series)):
        if series[i][0] >= cutoff:
            break
    if i > 0:
        del series[:i]
    # soft cap
    if len(series) > 4000:
        del series[: len(series) - 4000]

@router.get("/me/metrics")
def me_metrics(user: User = Depends(current_user), session: Session = Depends(get_session)):
    snap = _calc_from_trades(session, user.id)
    _append_pnl_point(user.id, snap["totals"]["pnl"])
    payload = {
        "user": {"id": user.id, "username": user.username},
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **snap,
    }
    return JSONResponse(payload)

@router.get("/me/pnl_series")
def me_pnl_series(user: User = Depends(current_user)):
    series = _PNL_SERIES.get(user.id, [])
    return {"points": series}
