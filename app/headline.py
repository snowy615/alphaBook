"""
Headline Trading Game Router
=============================
Real-time futures trading simulation with news-driven price movements.
"""
import random
import string
import math
import datetime as dt
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app import db as db_module
from app.auth import current_user
from app.models import User

router = APIRouter(prefix="/headline", tags=["headline"])
BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# ---- Templates ----
TEMPLATES = {
    "tech_ipo": {
        "name": "Tech Stock IPO",
        "description": "A hot new tech company just went public. Trade the hype!",
        "start_price": 100,
        "duration": 300,  # 5 minutes
        "news": [
            {"caption": "Q1 Revenue Beats Expectations",
             "detail": "The company reports 40% year-over-year revenue growth, beating analyst estimates by 15%. Gross margins expand to 72%.",
             "impact": 0.06},
            {"caption": "CEO Under SEC Investigation",
             "detail": "Reports emerge that the CEO is being investigated for potential insider trading before the IPO. The board convenes an emergency meeting.",
             "impact": -0.08},
            {"caption": "Major Partnership Announced",
             "detail": "The company signs a 5-year strategic partnership with a Fortune 100 company, expected to generate $2B in recurring revenue.",
             "impact": 0.07},
            {"caption": "Competitor Launches Rival Product",
             "detail": "Industry giant unveils a competing product at 60% lower cost. Analysts question the company's pricing power and moat.",
             "impact": -0.05},
            {"caption": "Insider Lock-Up Period Ends",
             "detail": "Early employees and VCs can now sell shares. An estimated 200M shares become eligible for sale, representing 40% of outstanding shares.",
             "impact": -0.04},
            {"caption": "FDA Approval for Key Product",
             "detail": "The company's flagship medical AI product receives full FDA approval, opening a $50B addressable market.",
             "impact": 0.09},
        ],
    },
    "oil": {
        "name": "Crude Oil Futures",
        "description": "Trade WTI Crude Oil as geopolitical events unfold.",
        "start_price": 100,
        "duration": 300,
        "news": [
            {"caption": "OPEC Announces Production Cuts",
             "detail": "OPEC+ agrees to cut output by 2 million barrels per day starting next month. Saudi Arabia pledges additional voluntary cuts.",
             "impact": 0.07},
            {"caption": "US Strategic Reserve Release",
             "detail": "The White House announces release of 50 million barrels from the Strategic Petroleum Reserve to combat rising energy costs.",
             "impact": -0.05},
            {"caption": "Middle East Tensions Escalate",
             "detail": "Military conflict disrupts shipping through the Strait of Hormuz. 20% of global oil supply passes through this chokepoint.",
             "impact": 0.08},
            {"caption": "Renewable Energy Breakthrough",
             "detail": "Scientists announce a major breakthrough in solid-state batteries, potentially accelerating the transition away from fossil fuels.",
             "impact": -0.06},
            {"caption": "China Reopening Boosts Demand",
             "detail": "China lifts remaining COVID restrictions. Analysts project a 1.5M barrel/day increase in demand over the next quarter.",
             "impact": 0.05},
            {"caption": "New Pipeline Approved",
             "detail": "A major transnational pipeline project receives final regulatory approval, expected to add 800K barrels/day of supply capacity.",
             "impact": -0.04},
        ],
    },
    "crypto": {
        "name": "Crypto Token",
        "description": "Trade a volatile cryptocurrency as regulation and adoption news breaks.",
        "start_price": 100,
        "duration": 300,
        "news": [
            {"caption": "SEC Approves Spot ETF",
             "detail": "The Securities and Exchange Commission approves the first spot cryptocurrency ETF, opening the floodgates for institutional investment.",
             "impact": 0.10},
            {"caption": "Major Exchange Hacked",
             "detail": "One of the top 3 exchanges reports a $400M security breach. Withdrawals are suspended indefinitely. Contagion fears spread.",
             "impact": -0.09},
            {"caption": "Payment Giant Enables Crypto",
             "detail": "A major global payments company announces all merchants can now accept this token natively, reaching 30M+ merchant locations.",
             "impact": 0.07},
            {"caption": "Country Bans All Crypto Trading",
             "detail": "A major economy announces a complete ban on cryptocurrency trading and mining, affecting 15% of global hash rate.",
             "impact": -0.07},
            {"caption": "Network Upgrade Successful",
             "detail": "The highly anticipated upgrade completes without issues, reducing transaction fees by 90% and increasing throughput 10x.",
             "impact": 0.06},
            {"caption": "Whale Dump Detected",
             "detail": "On-chain analysis shows a wallet holding 2% of total supply has moved all tokens to exchange wallets in the last hour.",
             "impact": -0.05},
        ],
    },
}


def generate_join_code():
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=6))


def generate_price_path(template_key: str, duration: int = 300):
    """Generate the full price path and news schedule for a game.

    News creates a directional bias in the random walk rather than an
    instant jump.  E.g. impact = +0.08 → prob_up ≈ 0.74 for the next
    min(45s, next_news).  Between active effects the walk is 50/50.
    """
    tmpl = TEMPLATES[template_key]
    start_price = tmpl["start_price"]
    news_items = list(tmpl["news"])
    random.shuffle(news_items)

    # Schedule news at random times (spread across the duration)
    num_news = len(news_items)
    earliest = int(duration * 0.10)
    latest = int(duration * 0.90)
    news_times = sorted(random.sample(range(earliest, latest), min(num_news, latest - earliest)))

    news_schedule = []
    for i, t in enumerate(news_times[:num_news]):
        news_schedule.append({
            "time": t,
            "caption": news_items[i]["caption"],
            "detail": news_items[i]["detail"],
            "impact": news_items[i]["impact"],
        })

    # Pre-compute bias windows: each news creates a bias lasting
    # min(45 ticks, ticks_until_next_news)
    BIAS_DURATION = 45  # seconds
    bias_at_tick = {}  # tick → prob_up (0.5 = neutral)

    for idx, ns in enumerate(news_schedule):
        start_t = ns["time"]
        # End = min(start + 45, next_news_time)
        if idx + 1 < len(news_schedule):
            end_t = min(start_t + BIAS_DURATION, news_schedule[idx + 1]["time"])
        else:
            end_t = min(start_t + BIAS_DURATION, duration)

        # Map impact → probability of going up
        # impact * 3 gives nice range: ±0.04→±0.12 shift, ±0.10→±0.30 shift
        prob_up = max(0.10, min(0.90, 0.5 + ns["impact"] * 3))

        for t in range(start_t, end_t):
            bias_at_tick[t] = prob_up

    # Build price path tick by tick
    prices = [start_price]
    noise_std = 0.003  # base volatility per tick

    price = start_price
    for t in range(1, duration + 1):
        abs_move = abs(random.gauss(0, noise_std))
        prob_up = bias_at_tick.get(t, 0.5)  # default 50/50

        if random.random() < prob_up:
            price = price * (1 + abs_move)
        else:
            price = price * (1 - abs_move)

        price = max(price, 1)  # floor at 1
        prices.append(round(price, 2))

    return {
        "prices": prices,  # index = tick (0..duration)
        "news_schedule": news_schedule,
    }


# ---- Request schemas ----
class JoinRequest(BaseModel):
    join_code: str


class TradeRequest(BaseModel):
    position: int  # Target position: -1000 to +1000


class CreateRequest(BaseModel):
    template: str  # template key


# ---- Pages ----
@router.get("", include_in_schema=False)
async def rules_page(request: Request):
    return templates.TemplateResponse("headline_rules.html", {
        "request": request,
        "app_name": "AlphaBook",
        "templates": {k: {"name": v["name"], "description": v["description"]}
                      for k, v in TEMPLATES.items()},
    })


@router.get("/game/{game_id}", include_in_schema=False)
async def game_page(game_id: str, request: Request):
    doc = await db_module.db.collection("headline_games").document(game_id).get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Game not found")
    return templates.TemplateResponse("headline_game.html", {
        "request": request,
        "app_name": "AlphaBook",
        "game_id": game_id,
    })


# ---- API: Templates list ----
@router.get("/templates")
async def list_templates():
    return {k: {"name": v["name"], "description": v["description"]}
            for k, v in TEMPLATES.items()}


# ---- Admin: Create game ----
@router.post("/create")
async def create_game(req: CreateRequest, user: User = Depends(current_user)):
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin only")

    if req.template not in TEMPLATES:
        raise HTTPException(status_code=400, detail=f"Unknown template: {req.template}")

    tmpl = TEMPLATES[req.template]
    join_code = generate_join_code()

    game_data = {
        "join_code": join_code,
        "template": req.template,
        "template_name": tmpl["name"],
        "status": "lobby",  # lobby → active → finished
        "duration": tmpl["duration"],
        "start_price": tmpl["start_price"],
        "prices": [],  # generated on start
        "news_schedule": [],  # generated on start
        "players": [],
        "trades": {},  # {user_id: [{delta, price, tick}]}
        "created_by": str(user.id),
        "created_at": dt.datetime.utcnow(),
        "started_at": None,
    }

    doc_ref = db_module.db.collection("headline_games").document()
    await doc_ref.set(game_data)

    return {"ok": True, "game_id": doc_ref.id, "join_code": join_code}


# ---- Player: Join game ----
@router.post("/join")
async def join_game(req: JoinRequest, user: User = Depends(current_user)):
    code = req.join_code.strip().upper()
    q = db_module.db.collection("headline_games") \
        .where("join_code", "==", code) \
        .where("status", "==", "lobby").limit(1)
    docs = await q.get()
    if not docs:
        raise HTTPException(status_code=404, detail="Game not found or already started")

    doc = docs[0]
    game_data = doc.to_dict()

    for p in game_data.get("players", []):
        if p["user_id"] == str(user.id):
            return {"ok": True, "game_id": doc.id, "message": "Already joined"}

    players = game_data.get("players", [])
    players.append({
        "user_id": str(user.id),
        "username": user.username,
    })
    await doc.reference.update({"players": players})

    return {"ok": True, "game_id": doc.id}


# ---- Admin: Start game ----
@router.post("/game/{game_id}/start")
async def start_game(game_id: str, user: User = Depends(current_user)):
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin only")

    doc = await db_module.db.collection("headline_games").document(game_id).get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Game not found")

    game_data = doc.to_dict()
    if game_data["status"] != "lobby":
        raise HTTPException(status_code=400, detail="Game already started")

    # Generate price path
    path = generate_price_path(game_data["template"], game_data["duration"])

    # Initialize trades for each player
    trades = {}
    for p in game_data.get("players", []):
        trades[p["user_id"]] = []

    await doc.reference.update({
        "status": "active",
        "prices": path["prices"],
        "news_schedule": path["news_schedule"],
        "trades": trades,
        "started_at": dt.datetime.utcnow(),
    })

    return {"ok": True, "status": "active"}


# ---- Player: Trade ----
@router.post("/game/{game_id}/trade")
async def trade(game_id: str, req: TradeRequest, user: User = Depends(current_user)):
    if req.position < -1000 or req.position > 1000:
        raise HTTPException(status_code=400, detail="Position must be between -1000 and +1000")

    doc = await db_module.db.collection("headline_games").document(game_id).get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Game not found")

    game_data = doc.to_dict()
    if game_data["status"] != "active":
        raise HTTPException(status_code=400, detail="Game is not active")

    uid = str(user.id)
    player_ids = [p["user_id"] for p in game_data.get("players", [])]
    if uid not in player_ids:
        raise HTTPException(status_code=403, detail="Not in this game")

    # Calculate current tick
    started_at = game_data["started_at"]
    if hasattr(started_at, 'timestamp'):
        elapsed = (dt.datetime.utcnow() - started_at).total_seconds()
    else:
        elapsed = 0
    tick = min(int(elapsed), game_data["duration"])

    if tick >= game_data["duration"]:
        raise HTTPException(status_code=400, detail="Game has ended")

    # Get current position
    trades = game_data.get("trades", {})
    user_trades = trades.get(uid, [])
    current_pos = sum(t["delta"] for t in user_trades)
    delta = req.position - current_pos

    if delta == 0:
        return {"ok": True, "message": "No change"}

    # Record trade
    prices = game_data.get("prices", [])
    current_price = prices[tick] if tick < len(prices) else prices[-1]

    user_trades.append({
        "delta": delta,
        "price": current_price,
        "tick": tick,
    })
    trades[uid] = user_trades

    await doc.reference.update({"trades": trades})

    return {"ok": True, "position": req.position, "price": current_price}


# ---- Game state ----
@router.get("/game/{game_id}/state")
async def game_state(game_id: str, user: User = Depends(current_user)):
    doc = await db_module.db.collection("headline_games").document(game_id).get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Game not found")

    game_data = doc.to_dict()
    uid = str(user.id)
    is_admin = user.is_admin
    status = game_data["status"]
    players = game_data.get("players", [])

    player_ids = [p["user_id"] for p in players]
    if uid not in player_ids and not is_admin:
        raise HTTPException(status_code=403, detail="Not in this game")

    result = {
        "game_id": game_id,
        "status": status,
        "players": players,
        "join_code": game_data.get("join_code", ""),
        "template_name": game_data.get("template_name", ""),
        "is_admin": is_admin,
        "duration": game_data.get("duration", 300),
        "start_price": game_data.get("start_price", 100),
    }

    if status == "lobby":
        return result

    # Active or finished
    started_at = game_data.get("started_at")
    prices = game_data.get("prices", [])
    news_schedule = game_data.get("news_schedule", [])
    trades = game_data.get("trades", {})
    duration = game_data.get("duration", 300)

    # Calculate current tick
    if started_at and hasattr(started_at, 'timestamp'):
        elapsed = (dt.datetime.utcnow() - started_at).total_seconds()
    else:
        elapsed = 0
    tick = min(int(elapsed), duration)

    # Auto-finish if time is up
    if tick >= duration and status == "active":
        await doc.reference.update({"status": "finished"})
        status = "finished"
        result["status"] = "finished"

    current_price = prices[tick] if tick < len(prices) else prices[-1] if prices else 100

    # Price history up to current tick
    result["tick"] = tick
    result["current_price"] = current_price
    result["price_history"] = prices[:tick + 1]

    # Released news (only show news that has happened)
    result["news"] = [n for n in news_schedule if n["time"] <= tick]

    # Calculate positions and PnL for all players
    leaderboard = []
    my_position = 0
    my_pnl = 0.0

    for p in players:
        pid = p["user_id"]
        user_trades = trades.get(pid, [])
        position = sum(t["delta"] for t in user_trades)
        pnl = sum(t["delta"] * (current_price - t["price"]) for t in user_trades)

        entry = {
            "user_id": pid,
            "username": p["username"],
            "position": position,
            "pnl": round(pnl, 2),
        }
        leaderboard.append(entry)

        if pid == uid:
            my_position = position
            my_pnl = round(pnl, 2)

    # Sort leaderboard by PnL descending
    leaderboard.sort(key=lambda x: x["pnl"], reverse=True)

    result["leaderboard"] = leaderboard
    result["my_position"] = my_position
    result["my_pnl"] = my_pnl
    result["my_trades"] = trades.get(uid, [])

    return result
