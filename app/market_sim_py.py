"""
Market Simulation Py — HTTP order gateway.
==========================================

The public face of the client-side algo game. Players run their strategy on
their own machine (see ``client/algo_client.py``) and reach the market only
through these endpoints:

* ``POST /run/{id}/orders``  — submit up to a batch of orders, matched on
  arrival; rate-limited to ``ORDER_RATE_PER_SEC`` per user.
* ``POST /run/{id}/cancel``  — pull resting orders.
* ``GET  /run/{id}/market``  — the market snapshot a bot polls each loop.
* ``GET  /run/{id}/token``   — mint the bearer token a client authenticates with.

No player code runs on the server, so there is no sandbox — the gateway only
ever validates and matches orders. The market's heartbeat (fair-value walk +
house bots) is advanced request-driven from the poll endpoints, the same way
the rest of AlphaBook copes with Cloud Run's between-request CPU throttling.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from app import algo_engine as engine
from app import db as db_module
from app import feedback as fb
from app import scores
from app import world as world_mod
from app.algo_engine import OrderRejected
from app.algo_ratelimit import RateLimiter
from app.auth import create_token, current_user
from app.models import User

log = logging.getLogger("uvicorn.error")

router = APIRouter(prefix="/market-sim-py", tags=["market-sim-py"])
BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

RESULTS_COLLECTION = "algo_sim_runs"

# One shared limiter: orders cost tokens, reads do not.
_order_limiter = RateLimiter(rate=engine.ORDER_RATE_PER_SEC, burst=engine.ORDER_BURST)

# Runs whose results have already been written to Firestore.
_persisted: set[str] = set()


# ---- Request schemas ----
class CreateRunRequest(BaseModel):
    name: str = Field(default="Market Simulation Py", max_length=80)


class JoinRequest(BaseModel):
    join_code: str


class OrderIn(BaseModel):
    item: Optional[str] = None
    side: Optional[str] = None
    qty: Optional[float] = None
    price: Optional[float] = None
    action: Optional[str] = None


class OrdersRequest(BaseModel):
    orders: List[OrderIn] = Field(default_factory=list)


class CancelRequest(BaseModel):
    item: Optional[str] = None


class AddBotRequest(BaseModel):
    archetype: str
    skill: str
    activate_seconds: int = 0
    name: Optional[str] = Field(default=None, max_length=40)


class WorldActionIn(BaseModel):
    """One empire action. Loosely typed on purpose — every field is optional
    here and the world engine owns the real validation, so a player gets one
    consistent, explanatory error style rather than a Pydantic dump."""
    type: str
    building: Optional[str] = None
    unit: Optional[str] = None
    unit_id: Optional[str] = None
    x: Optional[int] = None
    y: Optional[int] = None
    count: Optional[int] = None
    side: Optional[str] = None
    resource: Optional[str] = None
    qty: Optional[float] = None


class WorldActionsRequest(BaseModel):
    actions: List[WorldActionIn] = Field(default_factory=list)


# ---- Helpers ----
def _require_run(run_id: str) -> engine.Run:
    run = engine.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found (it may have expired after finishing)")
    return run


def _require_member(run: engine.Run, user: User) -> engine.Participant:
    p = run.member(str(user.id))
    if p is None:
        raise HTTPException(status_code=403, detail="You have not joined this run")
    return p


def _can_control(run: engine.Run, user: User) -> bool:
    return user.is_admin or run.creator_id == str(user.id)


async def _advance_and_maybe_persist(run: engine.Run) -> None:
    was_running = run.status == "running"
    run.advance()
    if was_running and run.status == "finished":
        await _persist_results(run)


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
        log.warning("Failed to persist Market Sim Py run %s", run.id, exc_info=True)

    for r in run.results:
        if r.get("is_bot"):
            continue
        # Attached to the result row itself so the run page can show it without
        # another round trip.
        coaching = fb.analyse("market_sim_py", r)
        r["feedback"] = coaching
        # Leaderboard rows are rebuilt on every poll, so the run itself
        # carries the coaching for the state endpoint to hand back.
        if not hasattr(run, "feedback"):
            run.feedback = {}
        run.feedback[r.get("user_id", "")] = coaching
        await scores.record_result(
            "market_sim_py", r.get("user_id", ""), r.get("username", ""), r.get("pnl", 0.0),
            game_id=run.id,
            detail={"fills": r.get("fills", 0), "volume": r.get("volume", 0)},
            feedback=coaching,
        )


# ---- Pages ----
@router.get("", include_in_schema=False)
async def rules_page(request: Request):
    """Rules, lobby entry point, and how to connect a bot."""
    return templates.TemplateResponse("market_sim_py_rules.html", {
        "request": request,
        "app_name": "AlphaBook",
        "position_limit": engine.POSITION_LIMIT,
        "run_minutes": engine.RUN_SECONDS // 60,
        "order_rate": engine.ORDER_RATE_PER_SEC,
        "items": [{"symbol": s["symbol"], "name": s["name"]} for s in engine.ITEM_SPECS],
    })


def _client_source() -> str:
    path = BASE_DIR.parent / "client" / "algo_client.py"
    try:
        return path.read_text()
    except OSError:
        raise HTTPException(status_code=404, detail="Client not found") from None


def _request_origin(request: Request) -> str:
    """The public origin this request arrived on (handles the Cloud Run proxy)."""
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}"


def _as_download(source: str) -> Response:
    return Response(
        content=source,
        media_type="text/x-python",
        headers={"Content-Disposition": 'attachment; filename="algo_client.py"'},
    )


@router.get("/client.py", include_in_schema=False)
async def download_client():
    """The blank client (placeholders) — for anyone grabbing it outside a run."""
    return _as_download(_client_source())


@router.get("/run/{run_id}/client.py", include_in_schema=False)
async def download_run_client(run_id: str, request: Request, user: User = Depends(current_user)):
    """The client with this run's id, the server origin, and a fresh token for
    the signed-in user already filled in — download and run, no copy-paste."""
    _require_run(run_id)
    source = _client_source()
    source = source.replace('"https://alphabook.uk")', f'"{_request_origin(request)}")', 1)
    source = source.replace('"PASTE_YOUR_RUN_ID_HERE"', f'"{run_id}"')
    source = source.replace('"PASTE_YOUR_TOKEN_HERE"', f'"{create_token(str(user.id))}"')
    return _as_download(source)


@router.get("/run/{run_id}", include_in_schema=False)
async def run_page(run_id: str, request: Request):
    """Live run page: leaderboard, market, and connect-your-bot panel."""
    _require_run(run_id)
    return templates.TemplateResponse("market_sim_py_game.html", {
        "request": request,
        "app_name": "AlphaBook",
        "run_id": run_id,
        "position_limit": engine.POSITION_LIMIT,
        "order_rate": engine.ORDER_RATE_PER_SEC,
    })


# ---- Lobby ----
@router.post("/create")
async def create_run(req: CreateRunRequest, user: User = Depends(current_user)):
    """Open a new run. Anyone may create one, so solo practice works too."""
    run = engine.create_run(req.name.strip() or "Market Simulation Py", str(user.id))
    run.join(str(user.id), user.username)
    return {"ok": True, "run_id": run.id, "join_code": run.join_code}


@router.get("/open")
async def list_open_runs(user: User = Depends(current_user)):
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


# ---- Bots (admin) ----
@router.get("/bots/catalog")
async def bots_catalog():
    """The archetypes and skill levels an admin can add to a run."""
    return {
        "archetypes": [
            {"key": k, "label": v["label"], "desc": v["desc"]}
            for k, v in engine.BOT_ARCHETYPES.items()
        ],
        "skills": engine.SKILL_LEVELS,
        "max_bots": engine.MAX_BOTS,
        "run_seconds": engine.RUN_SECONDS,
    }


@router.post("/run/{run_id}/bots")
async def add_bot(run_id: str, req: AddBotRequest, user: User = Depends(current_user)):
    """Add a house bot that enters at a chosen time (seconds from run start)."""
    run = _require_run(run_id)
    if not _can_control(run, user):
        raise HTTPException(status_code=403, detail="Only the run's creator or an admin can manage bots")
    activate_tick = max(0, int(req.activate_seconds / engine.TICK_SECONDS))
    try:
        bot = run.add_bot(req.archetype, req.skill, activate_tick, name=(req.name or None))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None
    return {"ok": True, "uid": bot.uid, "bots": run.bots_view()}


@router.delete("/run/{run_id}/bots/{bot_uid}")
async def remove_bot(run_id: str, bot_uid: str, user: User = Depends(current_user)):
    """Remove a bot that hasn't entered yet."""
    run = _require_run(run_id)
    if not _can_control(run, user):
        raise HTTPException(status_code=403, detail="Only the run's creator or an admin can manage bots")
    try:
        run.remove_bot(bot_uid)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None
    return {"ok": True, "bots": run.bots_view()}


# ---- The order gateway (what a player's client talks to) ----
@router.get("/run/{run_id}/token")
async def issue_token(run_id: str, user: User = Depends(current_user)):
    """Mint a bearer token the player's client authenticates with.

    Equivalent capability to the site session, but explicit: the player copies
    this into their bot so it can trade on their behalf.
    """
    _require_run(run_id)
    return {
        "token": create_token(str(user.id)),
        "run_id": run_id,
        "username": user.username,
        "order_rate": engine.ORDER_RATE_PER_SEC,
        "position_limit": engine.POSITION_LIMIT,
        "items": engine.ITEM_SYMBOLS,
    }


@router.get("/run/{run_id}/market")
async def market(run_id: str, user: User = Depends(current_user)):
    """The market snapshot a bot polls each loop. Advances the heartbeat.

    Reads aren't rate-limited (they're cheap), but they do drive the clock, so
    even a spectator's polling keeps the market alive.
    """
    run = _require_run(run_id)
    await _advance_and_maybe_persist(run)
    finished = run.status == "finished"
    p = run.member(str(user.id))
    if p is not None:
        p.last_seen = time.monotonic()
    return {
        "status": run.status,
        "tick": run.tick,
        "total_ticks": engine.TOTAL_TICKS,
        "seconds_left": round(run.seconds_left, 1),
        "position_limit": engine.POSITION_LIMIT,
        "items": engine.ITEM_SYMBOLS,
        "market": run.market_snapshot(reveal_fair=finished),
        "me": run.player_view(str(user.id)),
        "feedback": getattr(run, "feedback", {}).get(str(user.id)),
    }


@router.post("/run/{run_id}/orders")
async def submit_orders(run_id: str, req: OrdersRequest, user: User = Depends(current_user)):
    """Submit a batch of orders. Each costs one token; the batch is matched on
    arrival. Orders beyond the current token allowance come back marked
    ``rate_limited`` rather than applied."""
    run = _require_run(run_id)
    _require_member(run, user)
    await _advance_and_maybe_persist(run)

    orders = [o.model_dump(exclude_none=True) for o in req.orders]
    if not orders:
        raise HTTPException(status_code=400, detail="No orders supplied")

    allowance = _order_limiter.take(str(user.id), min(len(orders), engine.MAX_ORDERS_PER_REQUEST))
    try:
        result = run.submit_orders(str(user.id), orders, allowance)
    except OrderRejected as e:
        raise HTTPException(status_code=400, detail=str(e)) from None

    result["status"] = run.status
    result["seconds_left"] = round(run.seconds_left, 1)
    if allowance < min(len(orders), engine.MAX_ORDERS_PER_REQUEST):
        result["retry_after"] = round(_order_limiter.retry_after(str(user.id)), 3)
    return result


@router.post("/run/{run_id}/cancel")
async def cancel_orders(run_id: str, req: CancelRequest, user: User = Depends(current_user)):
    """Cancel resting orders (one item, or all). Costs one token."""
    run = _require_run(run_id)
    _require_member(run, user)
    await _advance_and_maybe_persist(run)
    if not _order_limiter.allow(str(user.id)):
        raise HTTPException(status_code=429, detail=f"Rate limited — max {engine.ORDER_RATE_PER_SEC}/s")
    try:
        removed = run.cancel(str(user.id), req.item)
    except OrderRejected as e:
        raise HTTPException(status_code=400, detail=str(e)) from None
    return {"ok": True, "cancelled": removed}


# ---- Spectator / leaderboard state (the web run page polls this) ----
@router.get("/run/{run_id}/state")
async def run_state(run_id: str, user: User = Depends(current_user)):
    run = _require_run(run_id)
    await _advance_and_maybe_persist(run)
    finished = run.status == "finished"
    return {
        "run_id": run.id,
        "name": run.name,
        "join_code": run.join_code,
        "status": run.status,
        "tick": run.tick,
        "total_ticks": engine.TOTAL_TICKS,
        "seconds_left": round(run.seconds_left, 1),
        "can_control": _can_control(run, user),
        "my_uid": str(user.id),
        "position_limit": engine.POSITION_LIMIT,
        "players": [
            {"user_id": p.uid, "username": p.name,
             "connected": (time.monotonic() - p.last_seen) < 10.0}
            for p in run.players
        ],
        "market": run.market_snapshot(reveal_fair=finished),
        "leaderboard": run.leaderboard(),
        "bots": run.bots_view(),
        "tape": list(run.tape)[:25],
        "me": run.player_view(str(user.id)),
    }


# ─────────────────────────────────────────────────────────────────────────────
# The world layer
#
# The same authenticated, rate-limited shape as the order gateway: the client
# posts intents, the server owns the rules. Nothing here executes player code
# either — a world action is validated JSON, exactly like an order.
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/world/catalogue")
async def world_catalogue():
    """Costs, yields and stats. Static, so a client can fetch it once."""
    return world_mod.catalogue()


@router.get("/run/{run_id}/world")
async def world_state(run_id: str, user: User = Depends(current_user)):
    """The whole board plus your own empire. Polled by the dashboard and bots."""
    run = _require_run(run_id)
    await _advance_and_maybe_persist(run)
    p = run.member(str(user.id))
    if p is not None:
        p.last_seen = time.monotonic()
    return {
        "status": run.status,
        "tick": run.tick,
        "world_tick": run.world.tick_no,
        "world_tick_seconds": world_mod.WORLD_TICK_SECONDS,
        "map": run.world.map_view(),
        "me": run.world.player_view(str(user.id)),
        "standings": run.world.standings(),
    }


@router.post("/run/{run_id}/world/actions")
async def world_actions(run_id: str, req: WorldActionsRequest,
                        user: User = Depends(current_user)):
    """Apply a batch of world actions in order.

    Each action is reported on individually rather than the batch failing as a
    whole: a strategy that queues six builds and can afford four should get the
    four, plus a readable reason for the two it missed.
    """
    run = _require_run(run_id)
    _require_member(run, user)
    await _advance_and_maybe_persist(run)

    if run.status != "running":
        raise HTTPException(status_code=400,
                            detail="The world is only open while the run is live")
    if not req.actions:
        raise HTTPException(status_code=400, detail="No actions supplied")

    allowance = _order_limiter.take(str(user.id),
                                    min(len(req.actions), engine.MAX_ORDERS_PER_REQUEST))
    results = []
    applied = 0
    for i, action in enumerate(req.actions[:engine.MAX_ORDERS_PER_REQUEST]):
        if i >= allowance:
            results.append({"ok": False, "rate_limited": True,
                            "error": "Slow down — you are over the action rate limit"})
            continue
        try:
            results.append(run.world.act(str(user.id), action.model_dump(exclude_none=True)))
            applied += 1
        except world_mod.WorldRejected as exc:
            results.append({"ok": False, "error": str(exc)})

    return {
        "applied": applied,
        "results": results,
        "me": run.world.player_view(str(user.id)),
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
