"""
Risks — crash-survival portfolio game
=====================================
A synthetic crash episode is replayed a day at a time. Players run a roughly
market-neutral book across a basket of names and are scored on P&L net of a
drawdown penalty, so surviving the panic window matters as much as calling it.

Episode data and all the scoring maths live in app/risk_episodes.py.
"""
import datetime as dt
import logging
import random
import string
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app import db as db_module
from app import feedback as fb
from app import risk_episodes as ep_lib
from app import scores
from app.auth import current_user
from app.models import User

log = logging.getLogger("uvicorn.error")

router = APIRouter(prefix="/risks", tags=["risks"])
BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

COLLECTION = "risk_games"


def generate_join_code() -> str:
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=6))


# ---- Request schemas ----
class CreateRequest(BaseModel):
    universe: str | None = None
    seconds_per_day: int = ep_lib.DEFAULT_SECONDS_PER_DAY
    name: str | None = None


class JoinRequest(BaseModel):
    join_code: str


class TradeRequest(BaseModel):
    ticker: str
    target: int


def _current_day(game: dict) -> int:
    """Which day of the episode the clock is on."""
    started_at = game.get("started_at")
    if not started_at:
        return 0
    now = dt.datetime.now(dt.timezone.utc)
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=dt.timezone.utc)
    elapsed = (now - started_at).total_seconds()
    spd = max(1, int(game.get("seconds_per_day", ep_lib.DEFAULT_SECONDS_PER_DAY)))
    return int(elapsed // spd)


async def _load(game_id: str):
    doc = await db_module.db.collection(COLLECTION).document(game_id).get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Game not found")
    return doc, doc.to_dict()


def _episode_of(game: dict) -> dict:
    try:
        return ep_lib.get_episode(game["episode_id"])
    except ep_lib.EpisodeNotFound:
        raise HTTPException(
            status_code=500,
            detail="This round's episode data is no longer on the server",
        ) from None


# ---- Pages ----
@router.get("", include_in_schema=False)
async def rules_page(request: Request):
    return templates.TemplateResponse("risks_rules.html", {
        "request": request,
        "app_name": "AlphaBook",
        "universes": ep_lib.universes(),
        "start_equity": ep_lib.START_EQUITY,
        "gross_limit": ep_lib.GROSS_LIMIT,
        "net_limit": ep_lib.NET_LIMIT,
        "dd_penalty": ep_lib.DRAWDOWN_PENALTY,
    })


@router.get("/game/{game_id}", include_in_schema=False)
async def game_page(game_id: str, request: Request):
    await _load(game_id)
    return templates.TemplateResponse("risks_game.html", {
        "request": request,
        "app_name": "AlphaBook",
        "game_id": game_id,
        "start_equity": ep_lib.START_EQUITY,
        "gross_limit": ep_lib.GROSS_LIMIT,
        "net_limit": ep_lib.NET_LIMIT,
    })


# ---- API ----
@router.get("/universes")
async def list_universes():
    return {"universes": ep_lib.universes()}


@router.get("/open")
async def open_games(user: User = Depends(current_user)):
    """Rounds still in their lobby or running, newest first."""
    q = db_module.db.collection(COLLECTION).where("status", "in", ["lobby", "active"])
    docs = await q.get()
    uid = str(user.id)
    rows = []
    for d in docs:
        g = d.to_dict()
        rows.append({
            "game_id": d.id,
            "name": g.get("name") or "Risks",
            "join_code": g.get("join_code", ""),
            "universe_label": g.get("universe_label", ""),
            "days": g.get("days", 0),
            "status": g.get("status"),
            "players": len(g.get("players", [])),
            "joined": any(p["user_id"] == uid for p in g.get("players", [])),
            "created_at": (g.get("created_at").isoformat()
                           if g.get("created_at") else ""),
        })
    rows.sort(key=lambda r: r["created_at"], reverse=True)
    return {"runs": rows}


@router.post("/create")
async def create_game(req: CreateRequest, user: User = Depends(current_user)):
    try:
        episode = ep_lib.pick_episode(req.universe or None)
    except ep_lib.EpisodeNotFound:
        raise HTTPException(
            status_code=400,
            detail="No crash episodes are installed for that universe",
        ) from None

    spd = max(ep_lib.MIN_SECONDS_PER_DAY,
              min(ep_lib.MAX_SECONDS_PER_DAY, int(req.seconds_per_day)))
    join_code = generate_join_code()

    game = {
        "join_code": join_code,
        "name": (req.name or "").strip()[:60] or f"Risks · {episode['universe_label']}",
        # The episode id is stored, not the prices: the path stays server-side
        # so a player cannot read the future out of the game document.
        "episode_id": episode["episode_id"],
        "universe": episode["universe"],
        "universe_label": episode["universe_label"],
        "days": episode["days"],
        "seconds_per_day": spd,
        "status": "lobby",
        "players": [],
        "trades": {},
        "created_by": str(user.id),
        "created_at": dt.datetime.now(dt.timezone.utc),
        "started_at": None,
    }

    doc_ref = db_module.db.collection(COLLECTION).document()
    await doc_ref.set(game)
    return {"ok": True, "game_id": doc_ref.id, "join_code": join_code}


@router.post("/join")
async def join_game(req: JoinRequest, user: User = Depends(current_user)):
    code = req.join_code.strip().upper()
    if len(code) != 6:
        raise HTTPException(status_code=400, detail="Join codes are six characters")

    q = db_module.db.collection(COLLECTION) \
        .where("join_code", "==", code) \
        .where("status", "in", ["lobby", "active"]).limit(1)
    docs = await q.get()
    if not docs:
        raise HTTPException(status_code=404, detail="No open round with that code")

    doc = docs[0]
    game = doc.to_dict()
    uid = str(user.id)
    players = game.get("players", [])

    if any(p["user_id"] == uid for p in players):
        return {"ok": True, "game_id": doc.id, "message": "Already joined"}

    players.append({"user_id": uid, "username": user.username})
    trades = game.get("trades", {})
    trades.setdefault(uid, [])
    await doc.reference.update({"players": players, "trades": trades})
    return {"ok": True, "game_id": doc.id}


@router.post("/game/{game_id}/start")
async def start_game(game_id: str, user: User = Depends(current_user)):
    doc, game = await _load(game_id)
    if str(user.id) != game.get("created_by") and not user.is_admin:
        raise HTTPException(status_code=403, detail="Only the host can start this round")
    if game["status"] != "lobby":
        return {"ok": True, "status": game["status"]}

    players = game.get("players", [])
    uid = str(user.id)
    if not any(p["user_id"] == uid for p in players):
        players.append({"user_id": uid, "username": user.username})

    trades = game.get("trades", {})
    for p in players:
        trades.setdefault(p["user_id"], [])

    await doc.reference.update({
        "status": "active",
        "players": players,
        "trades": trades,
        "started_at": dt.datetime.now(dt.timezone.utc),
    })
    return {"ok": True, "status": "active"}


@router.post("/game/{game_id}/stop")
async def stop_game(game_id: str, user: User = Depends(current_user)):
    doc, game = await _load(game_id)
    if str(user.id) != game.get("created_by") and not user.is_admin:
        raise HTTPException(status_code=403, detail="Only the host can end this round")
    if game["status"] == "active":
        day = min(_current_day(game), game["days"] - 1)
        await doc.reference.update({
            "status": "finished",
            "finished_day": day,
            "scored": True,
        })
        if not game.get("scored"):
            await _record_scores(game_id, game, day)
    return {"ok": True, "status": "finished"}


async def _record_scores(game_id: str, game: dict, day: int) -> None:
    """Rate each player and write them a note on how they carried the risk."""
    try:
        episode = _episode_of(game)
        trades = game.get("trades", {})
        feedback_by_user = {}
        for p in game.get("players", []):
            card = ep_lib.score_player(episode, trades.get(p["user_id"], []), day)
            coaching = fb.analyse("risks", {**card, "gross_limit": ep_lib.GROSS_LIMIT})
            feedback_by_user[p["user_id"]] = coaching
            await scores.record_result(
                "risks", p["user_id"], p.get("username", ""), card["score"],
                game_id=game_id,
                detail={"pnl": card["pnl"], "max_drawdown": card["max_drawdown"]},
                feedback=coaching,
            )
        await db_module.db.collection(COLLECTION).document(game_id).update(
            {"feedback": feedback_by_user})
    except Exception:
        log.warning("Risks score recording failed for game %s", game_id, exc_info=True)


@router.post("/game/{game_id}/trade")
async def trade(game_id: str, req: TradeRequest, user: User = Depends(current_user)):
    doc, game = await _load(game_id)
    if game["status"] != "active":
        raise HTTPException(status_code=400, detail="This round is not running")

    uid = str(user.id)
    if not any(p["user_id"] == uid for p in game.get("players", [])):
        raise HTTPException(status_code=403, detail="You are not in this round")

    episode = _episode_of(game)
    day = _current_day(game)
    if day >= episode["days"]:
        raise HTTPException(status_code=400, detail="The round has closed")

    trades = game.get("trades", {})
    my_trades = trades.get(uid, [])
    if len(my_trades) >= ep_lib.MAX_TRADES_PER_PLAYER:
        raise HTTPException(status_code=429, detail="Trade limit reached for this round")

    ticker = req.ticker.strip().upper()
    prices = ep_lib.prices_on(episode, day)
    positions = ep_lib.positions_after(my_trades)

    target = int(req.target)
    ok, why = ep_lib.check_trade(positions, prices, ticker, target)
    if not ok:
        raise HTTPException(status_code=400, detail=why)

    delta = target - positions.get(ticker, 0)
    if delta == 0:
        return {"ok": True, "message": "No change", "position": target}

    my_trades.append({
        "ticker": ticker,
        "delta": delta,
        "price": prices[ticker],
        "day": day,
    })
    trades[uid] = my_trades
    await doc.reference.update({"trades": trades})

    return {"ok": True, "ticker": ticker, "position": target, "price": prices[ticker]}


@router.get("/game/{game_id}/state")
async def game_state(game_id: str, user: User = Depends(current_user)):
    doc, game = await _load(game_id)
    uid = str(user.id)
    players = game.get("players", [])
    is_host = uid == game.get("created_by") or user.is_admin

    if not any(p["user_id"] == uid for p in players) and not is_host:
        raise HTTPException(status_code=403, detail="You are not in this round")

    episode = _episode_of(game)
    status = game["status"]
    total_days = episode["days"]

    out = {
        "game_id": game_id,
        "name": game.get("name", "Risks"),
        "join_code": game.get("join_code", ""),
        "universe_label": game.get("universe_label", ""),
        "status": status,
        "total_days": total_days,
        "seconds_per_day": game.get("seconds_per_day", ep_lib.DEFAULT_SECONDS_PER_DAY),
        "can_control": is_host,
        "my_uid": uid,
        "players": [{"username": p["username"]} for p in players],
        "start_equity": ep_lib.START_EQUITY,
        "gross_limit": ep_lib.GROSS_LIMIT,
        "net_limit": ep_lib.NET_LIMIT,
        "dd_penalty": ep_lib.DRAWDOWN_PENALTY,
    }

    if status == "lobby":
        out["day"] = 0
        return out

    raw_day = _current_day(game)
    finished = status == "finished" or raw_day >= total_days
    day = min(raw_day, total_days - 1)
    if status == "finished" and game.get("finished_day") is not None:
        day = min(int(game["finished_day"]), total_days - 1)

    # Auto-close once the clock runs past the last day. `scored` goes in the
    # same write so concurrent pollers don't each record the round.
    just_finished = False
    if finished and status == "active":
        await doc.reference.update({"status": "finished", "finished_day": day, "scored": True})
        just_finished = not game.get("scored")
        status = "finished"

    out["status"] = status
    out["day"] = day
    out["days_left"] = max(0, total_days - 1 - day)
    out["names"] = ep_lib.public_names(episode, day)
    out["index"] = [round(v, 2) for v in episode["index"][:day + 1]]
    # The generator's commentary for today only. It is written from the move
    # that has already happened, so it gives nothing away about tomorrow.
    out["wire"] = ep_lib.message_on(episode, day)

    trades = game.get("trades", {})

    leaderboard = []
    for p in players:
        pid = p["user_id"]
        card = ep_lib.score_player(episode, trades.get(pid, []), day)
        leaderboard.append({
            "user_id": pid,
            "username": p["username"],
            "pnl": card["pnl"],
            "max_drawdown": card["max_drawdown"],
            "score": card["score"],
            "gross": card["gross"],
        })
    leaderboard.sort(key=lambda r: r["score"], reverse=True)
    for i, row in enumerate(leaderboard, 1):
        row["rank"] = i
    out["leaderboard"] = leaderboard

    if just_finished:
        # Same path as a host-ended round, so the coaching is identical either way.
        await _record_scores(game_id, game, day)
        game["feedback"] = (await doc.reference.get()).to_dict().get("feedback", {})

    if status == "finished":
        out["feedback"] = (game.get("feedback") or {}).get(uid)

    if any(p["user_id"] == uid for p in players):
        mine = ep_lib.score_player(episode, trades.get(uid, []), day)
        out["me"] = mine
        out["my_equity_curve"] = [
            round(v, 2) for v in ep_lib.equity_curve(episode, trades.get(uid, []), day)
        ]
    else:
        out["me"] = None
        out["my_equity_curve"] = []

    if status == "finished":
        # Only now: the realised betas, the shock groups and where the panic was.
        out["reveal"] = {
            "names": ep_lib.reveal_names(episode),
            "panic_days": episode["panic_days"],
            "rebound_days": episode["rebound_days"],
            "index_return_pct": episode["index_return_pct"],
            "index_drawdown_pct": episode["index_drawdown_pct"],
            "full_index": [round(v, 2) for v in episode["index"]],
            # Which historical crashes were blended, the phase boundaries and
            # the dated macro events, when the episode carries them.
            **ep_lib.aftermath(episode),
        }

    return out
