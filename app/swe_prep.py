"""
SWE Prep — HTTP routes.
===================================

Thin layer over :mod:`app.swe_prep_engine`: pages, lobby management, strategy
submission and the polled state endpoint that drives the simulation forward.

Ticks are advanced from ``GET /swe-prep/run/{id}/state`` rather than a
background task, because Cloud Run throttles the CPU between requests and
background loops stall there.  The front end polls once a second while a run is
live, and :meth:`Run.advance` replays whatever the clock says is due — so a run
stays correct even if every browser goes away for a while.
"""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from app import swe_prep_engine as engine
from app import db as db_module
from app import feedback as fb
from app import scores
from app.swe_prep_sandbox import SandboxError, StrategyTimeout, check_strategy
from app.auth import current_user
from app.models import User

log = logging.getLogger("uvicorn.error")

router = APIRouter(prefix="/swe-prep", tags=["swe-prep"])
BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

RESULTS_COLLECTION = "swe_prep_runs"

# Runs whose results have already been written to Firestore.
_persisted: set[str] = set()


# ---- Request schemas ----
class CreateRunRequest(BaseModel):
    name: str = Field(default="SWE Prep", max_length=80)


class JoinRequest(BaseModel):
    join_code: str


class CodeRequest(BaseModel):
    code: str


# ---- Helpers ----
def _require_run(run_id: str) -> engine.Run:
    run = engine.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found (it may have expired after finishing)")
    return run


def _require_member(run: engine.Run, user: User) -> engine.Participant:
    p = run.participants.get(str(user.id))
    if p is None or p.is_bot:
        raise HTTPException(status_code=403, detail="You have not joined this run")
    return p


def _can_control(run: engine.Run, user: User) -> bool:
    return user.is_admin or run.creator_id == str(user.id)


async def _persist_results(run: engine.Run) -> None:
    """Write a finished run's results to Firestore, once."""
    if run.status != "finished" or run.id in _persisted:
        return
    _persisted.add(run.id)
    try:
        await db_module.db.collection(RESULTS_COLLECTION).document(run.id).set({
            "name": run.name,
            "join_code": run.join_code,
            "created_by": run.creator_id,
            "created_at": run.created_at,
            "started_at": run.started_at,
            "finished_at": run.finished_at or dt.datetime.utcnow(),
            "run_seconds": engine.RUN_SECONDS,
            "position_limit": engine.POSITION_LIMIT,
            "items": engine.ITEM_SYMBOLS,
            "results": [r for r in run.results if not r["is_bot"]],
            "bots": [r for r in run.results if r["is_bot"]],
        })
    except Exception:
        log.warning("Failed to persist SWE Prep run %s", run.id, exc_info=True)

    for r in run.results:
        if r.get("is_bot"):
            continue
        coaching = fb.analyse("swe_prep", r)
        r["feedback"] = coaching
        # Leaderboard rows are rebuilt on every poll, so the run itself carries
        # the coaching for the state endpoint to hand back.
        if not hasattr(run, "feedback"):
            run.feedback = {}
        run.feedback[r.get("user_id", "")] = coaching
        await scores.record_result(
            "swe_prep", r.get("user_id", ""), r.get("username", ""), r.get("pnl", 0.0),
            game_id=run.id,
            detail={"fills": r.get("fills", 0), "status": r.get("status", "")},
            feedback=coaching,
        )


# ---- Pages ----
@router.get("", include_in_schema=False)
async def rules_page(request: Request):
    """Rules, strategy editor and lobby entry point."""
    return templates.TemplateResponse("swe_prep_rules.html", {
        "request": request,
        "app_name": "AlphaBook",
        "position_limit": engine.POSITION_LIMIT,
        "run_minutes": engine.RUN_SECONDS // 60,
        "tick_seconds": engine.TICK_SECONDS,
        "total_ticks": engine.TOTAL_TICKS,
        "items": [
            {"symbol": s["symbol"], "name": s["name"]} for s in engine.ITEM_SPECS
        ],
    })


@router.get("/run/{run_id}", include_in_schema=False)
async def run_page(run_id: str, request: Request):
    """Live run page: leaderboard, market, and your strategy's output."""
    _require_run(run_id)
    return templates.TemplateResponse("swe_prep_game.html", {
        "request": request,
        "app_name": "AlphaBook",
        "run_id": run_id,
        "position_limit": engine.POSITION_LIMIT,
    })


# ---- Strategy authoring ----
@router.get("/starter")
async def starter_code():
    """The template new players start from."""
    return {
        "code": engine.STARTER_CODE,
        "position_limit": engine.POSITION_LIMIT,
        "items": engine.ITEM_SYMBOLS,
        "tick_seconds": engine.TICK_SECONDS,
        "run_seconds": engine.RUN_SECONDS,
        "max_orders_per_tick": engine.MAX_ORDERS_PER_TICK,
    }


@router.post("/check")
async def check_code(req: CodeRequest, user: User = Depends(current_user)):
    """Compile a strategy against the sandbox without joining a run."""
    problems = check_strategy(req.code or "")
    return {"ok": not problems, "problems": problems}


# ---- Lobby ----
@router.post("/create")
async def create_run(req: CreateRunRequest, user: User = Depends(current_user)):
    """Open a new run. Anyone may create one, so solo practice works too."""
    run = engine.create_run(req.name.strip() or "SWE Prep", str(user.id))
    run.join(str(user.id), user.username)
    return {"ok": True, "run_id": run.id, "join_code": run.join_code}


@router.get("/open")
async def list_open_runs(user: User = Depends(current_user)):
    """Runs currently in a lobby or mid-flight, so players can find one."""
    return {
        "runs": [
            {
                "run_id": r.id,
                "name": r.name,
                "join_code": r.join_code,
                "status": r.status,
                "players": len(r.players),
                "joined": str(user.id) in r.participants,
            }
            for r in engine.open_runs()
        ]
    }


@router.post("/join")
async def join_run(req: JoinRequest, user: User = Depends(current_user)):
    run = engine.find_by_code(req.join_code)
    if run is None:
        raise HTTPException(status_code=404, detail="No open run with that code")
    try:
        run.join(str(user.id), user.username)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None
    return {"ok": True, "run_id": run.id}


@router.post("/run/{run_id}/strategy")
async def submit_strategy(run_id: str, req: CodeRequest, user: User = Depends(current_user)):
    """Attach a strategy to your seat. Validated now so errors surface early."""
    run = _require_run(run_id)
    _require_member(run, user)
    try:
        run.set_code(str(user.id), req.code or "")
    except (SandboxError, StrategyTimeout) as e:
        raise HTTPException(status_code=400, detail=str(e)) from None
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from None
    except Exception as e:  # noqa: BLE001 - author's own error, reported as text
        raise HTTPException(status_code=400, detail=f"{type(e).__name__}: {e}") from None
    return {"ok": True}


@router.get("/run/{run_id}/strategy")
async def my_strategy(run_id: str, user: User = Depends(current_user)):
    """Your currently attached code, so the editor can reload it."""
    run = _require_run(run_id)
    p = _require_member(run, user)
    return {"code": p.code, "status": p.status, "error": p.error}


@router.post("/run/{run_id}/start")
async def start_run(run_id: str, user: User = Depends(current_user)):
    run = _require_run(run_id)
    if not _can_control(run, user):
        raise HTTPException(status_code=403, detail="Only the run's creator or an admin can start it")
    try:
        run.start()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None
    return {"ok": True, "status": run.status}


@router.post("/run/{run_id}/stop")
async def stop_run(run_id: str, user: User = Depends(current_user)):
    """End a run early; results are scored exactly as if the clock had run out."""
    run = _require_run(run_id)
    if not _can_control(run, user):
        raise HTTPException(status_code=403, detail="Only the run's creator or an admin can stop it")
    if run.status != "running":
        raise HTTPException(status_code=400, detail="Run is not in progress")
    run.advance()
    run.finish()
    await _persist_results(run)
    return {"ok": True, "status": run.status}


# ---- Live state (this is what drives the simulation) ----
@router.get("/run/{run_id}/state")
async def run_state(run_id: str, user: User = Depends(current_user)):
    run = _require_run(run_id)
    uid = str(user.id)

    was_running = run.status == "running"
    ticks = run.advance()
    if was_running and run.status == "finished":
        await _persist_results(run)

    finished = run.status == "finished"
    return {
        "run_id": run.id,
        "name": run.name,
        "join_code": run.join_code,
        "status": run.status,
        "tick": run.tick,
        "total_ticks": engine.TOTAL_TICKS,
        "seconds_left": round(run.seconds_left, 1),
        "ticks_advanced": ticks,
        "can_control": _can_control(run, user),
        "position_limit": engine.POSITION_LIMIT,
        "players": [
            {
                "user_id": p.uid,
                "username": p.name,
                "status": p.status,
                "has_code": bool(p.code),
            }
            for p in run.players
        ],
        # Fair values stay hidden while the contest is live.
        "market": run.market_view(reveal_fair=finished),
        "leaderboard": run.leaderboard(),
        "tape": list(run.tape)[:25],
        "me": run.player_view(uid),
        "feedback": getattr(run, "feedback", {}).get(uid),
    }


@router.get("/history")
async def my_history(limit: int = 10, user: User = Depends(current_user)):
    """Finished runs this user took part in, newest first."""
    try:
        docs = await db_module.db.collection(RESULTS_COLLECTION) \
            .order_by("finished_at", direction="DESCENDING") \
            .limit(max(1, min(limit, 50))) \
            .get()
    except Exception:
        log.warning("Market Sim Py history query failed", exc_info=True)
        return {"runs": []}

    uid = str(user.id)
    runs = []
    for d in docs:
        data = d.to_dict() or {}
        results = data.get("results", [])
        mine: Optional[dict] = next((r for r in results if r.get("user_id") == uid), None)
        if mine is None:
            continue
        runs.append({
            "run_id": d.id,
            "name": data.get("name", ""),
            "finished_at": data.get("finished_at"),
            "rank": mine.get("rank"),
            "players": len(results),
            "pnl": mine.get("pnl"),
        })
    return {"runs": runs}
