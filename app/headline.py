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
# strength: "strong" → prob ~0.88, "moderate" → ~0.72, "weak" → ~0.58
STRENGTH_TO_PROB = {"strong": 0.38, "moderate": 0.22, "weak": 0.08}

TEMPLATES = {
    "tech_ipo": {
        "name": "Tech Stock IPO",
        "description": "A hot new tech company just went public. Trade the hype!",
        "start_price": 100,
        "duration": 300,
        "news": [
            {"caption": "Q1 Revenue Beats Expectations", "detail": "The company reports 40% YoY revenue growth, beating analyst estimates by 15%. Gross margins expand to 72%.",
             "impact": 1, "strength": "strong", "analysis": "Strong earnings beat signals robust demand and pricing power — a clear bullish catalyst for growth stocks."},
            {"caption": "CEO Under SEC Investigation", "detail": "Reports emerge that the CEO is being investigated for potential insider trading before the IPO.",
             "impact": -1, "strength": "strong", "analysis": "Executive legal risk creates massive uncertainty. Investors flee governance concerns, especially for a newly public company."},
            {"caption": "Major Partnership Announced", "detail": "The company signs a 5-year strategic partnership with a Fortune 100 company for $2B in recurring revenue.",
             "impact": 1, "strength": "moderate", "analysis": "Large enterprise deals validate the product but take time to materialize. Moderately bullish as revenue is spread over years."},
            {"caption": "Competitor Launches Rival Product", "detail": "Industry giant unveils a competing product at 60% lower cost. Analysts question pricing power.",
             "impact": -1, "strength": "moderate", "analysis": "Competition from a well-funded rival threatens market share, but the company may retain customers through switching costs."},
            {"caption": "Insider Lock-Up Expiration", "detail": "200M shares from early employees and VCs become eligible for sale — 40% of outstanding shares.",
             "impact": -1, "strength": "moderate", "analysis": "Lock-up expiry creates sell pressure. While not all insiders sell, the overhang weighs on sentiment."},
            {"caption": "Analyst Initiates with Buy Rating", "detail": "Goldman Sachs initiates coverage with a Buy rating and $150 price target, citing strong TAM.",
             "impact": 1, "strength": "weak", "analysis": "Analyst coverage is positive but expected for hot IPOs. Weak signal — the market already anticipated coverage."},
            {"caption": "User Growth Slowing", "detail": "Monthly active users grew only 5% QoQ, down from 25% the prior quarter. Churn rate ticks up to 8%.",
             "impact": -1, "strength": "moderate", "analysis": "Decelerating growth is concerning for a company priced on hypergrowth. Market re-rates growth expectations downward."},
            {"caption": "Patent Granted for Core Technology", "detail": "The company receives a broad patent covering its core AI technology, strengthening its competitive moat.",
             "impact": 1, "strength": "weak", "analysis": "IP protection is positive long-term but doesn't change near-term fundamentals. Weak bullish signal."},
            {"caption": "Short Seller Report Published", "detail": "A prominent short seller publishes a 60-page report alleging inflated metrics and accounting irregularities.",
             "impact": -1, "strength": "strong", "analysis": "Short reports from credible firms cause significant selling as investors seek to de-risk until allegations are addressed."},
            {"caption": "Government Contract Win", "detail": "The company wins a $500M federal contract for AI infrastructure, beating out 5 competitors.",
             "impact": 1, "strength": "strong", "analysis": "Government contracts provide stable, high-margin revenue and validate the technology. Strongly bullish."},
            {"caption": "CFO Resignation", "detail": "The CFO announces immediate resignation citing 'personal reasons'. No successor has been named.",
             "impact": -1, "strength": "weak", "analysis": "C-suite departures are concerning but CFO changes are common post-IPO. Mildly bearish without further context."},
            {"caption": "Product Goes Viral on Social Media", "detail": "A demo video reaches 50M views. App downloads surge 300% overnight.",
             "impact": 1, "strength": "moderate", "analysis": "Viral moments drive short-term user acquisition but sustainability is uncertain. Moderately bullish on momentum."},
        ],
    },
    "oil": {
        "name": "Crude Oil Futures",
        "description": "Trade WTI Crude Oil as geopolitical events unfold.",
        "start_price": 100,
        "duration": 300,
        "news": [
            {"caption": "OPEC Announces Deep Production Cuts", "detail": "OPEC+ agrees to cut output by 2M barrels/day. Saudi Arabia pledges additional voluntary cuts.",
             "impact": 1, "strength": "strong", "analysis": "Supply cuts directly reduce available barrels. OPEC's willingness to cut aggressively signals price floor defense."},
            {"caption": "US Strategic Reserve Release", "detail": "White House announces 50M barrel release from the SPR to combat rising energy costs.",
             "impact": -1, "strength": "moderate", "analysis": "SPR releases add temporary supply but don't change long-term fundamentals. Moderate bearish pressure."},
            {"caption": "Strait of Hormuz Disrupted", "detail": "Military conflict disrupts shipping through the Strait of Hormuz. 20% of global oil transits this chokepoint.",
             "impact": 1, "strength": "strong", "analysis": "Chokepoint disruptions create immediate supply fear. Historical precedent shows sharp price spikes when Hormuz is threatened."},
            {"caption": "Battery Breakthrough Announced", "detail": "Solid-state battery breakthrough promises 1000-mile EV range at half the cost. Mass production by 2026.",
             "impact": -1, "strength": "weak", "analysis": "Long-term demand destruction for oil, but years away from impacting consumption. Weak bearish on sentiment only."},
            {"caption": "China Manufacturing Surges", "detail": "China PMI hits 58.7, highest in 3 years. Industrial diesel demand jumps 12% MoM.",
             "impact": 1, "strength": "moderate", "analysis": "China is the world's largest oil importer. Strong manufacturing activity directly translates to higher crude demand."},
            {"caption": "US Shale Production Record", "detail": "Permian Basin output reaches all-time high of 6.2M bbl/day. New drilling permits up 30%.",
             "impact": -1, "strength": "moderate", "analysis": "Record US production offsets OPEC cuts. The supply response from shale is faster than ever, capping upside."},
            {"caption": "Pipeline Explosion in Russia", "detail": "Major explosion damages the Druzhba pipeline, cutting 500K bbl/day of exports to Europe.",
             "impact": 1, "strength": "moderate", "analysis": "Pipeline disruptions remove physical supply from the market. Repairs typically take weeks, extending the impact."},
            {"caption": "Global EV Sales Hit 30% Market Share", "detail": "Electric vehicles reach 30% of global new car sales for the first time.",
             "impact": -1, "strength": "weak", "analysis": "Long-term structural shift away from oil, but current absolute demand is still growing. Weak near-term bearish."},
            {"caption": "Hurricane Hits Gulf of Mexico", "detail": "Category 4 hurricane shuts down 60% of Gulf production. Refineries along the coast evacuated.",
             "impact": 1, "strength": "strong", "analysis": "Gulf shutdowns remove significant production and refining capacity simultaneously. Historically causes sharp spikes."},
            {"caption": "Iran Nuclear Deal Revived", "detail": "Breakthrough in nuclear talks. Iran could return 1.5M bbl/day to market within 6 months.",
             "impact": -1, "strength": "strong", "analysis": "Iranian barrels returning to market represent a major supply increase. One of the most bearish scenarios for oil."},
            {"caption": "India Builds Strategic Reserves", "detail": "India announces plans to triple its strategic petroleum reserves, purchasing 200M barrels over 2 years.",
             "impact": 1, "strength": "weak", "analysis": "Government stockpiling adds demand at the margin but is spread over a long period. Weakly bullish."},
            {"caption": "OPEC Compliance Falls", "detail": "Satellite data shows OPEC members cheating on quotas. Actual production 800K bbl/day above targets.",
             "impact": -1, "strength": "moderate", "analysis": "Quota violations undermine OPEC credibility and add unplanned supply. Moderately bearish on trust breakdown."},
        ],
    },
    "crypto": {
        "name": "Crypto Token",
        "description": "Trade a volatile cryptocurrency as regulation and adoption news breaks.",
        "start_price": 100,
        "duration": 300,
        "news": [
            {"caption": "SEC Approves Spot ETF", "detail": "The SEC approves the first spot cryptocurrency ETF, opening floodgates for institutional investment.",
             "impact": 1, "strength": "strong", "analysis": "ETF approval is the most anticipated catalyst in crypto. Allows pension funds and institutions to gain exposure easily."},
            {"caption": "Major Exchange Hacked", "detail": "Top-3 exchange reports $400M security breach. Withdrawals suspended. Contagion fears spread.",
             "impact": -1, "strength": "strong", "analysis": "Exchange hacks destroy trust in the ecosystem. Users rush to withdraw from other exchanges, creating liquidity crises."},
            {"caption": "Payment Giant Enables Crypto", "detail": "30M+ merchant locations can now accept this token natively. Transaction volume expected to 10x.",
             "impact": 1, "strength": "moderate", "analysis": "Real-world payment adoption is a key milestone, but merchant acceptance doesn't guarantee consumer usage."},
            {"caption": "Major Economy Bans Trading", "detail": "A G20 country announces complete ban on cryptocurrency trading and mining. 15% of hash rate affected.",
             "impact": -1, "strength": "strong", "analysis": "Country-level bans remove significant demand pools and mining capacity. Historically causes sharp selloffs."},
            {"caption": "Network Upgrade Successful", "detail": "The upgrade completes flawlessly, reducing fees by 90% and increasing throughput 10x.",
             "impact": 1, "strength": "moderate", "analysis": "Technical improvements enhance utility but are often priced in ahead of the upgrade. Moderately bullish."},
            {"caption": "Whale Dump Detected", "detail": "On-chain analysis: wallet holding 2% of total supply moved all tokens to exchange wallets in the last hour.",
             "impact": -1, "strength": "moderate", "analysis": "Large holders moving to exchanges signals intent to sell. Creates fear of a large market sell order."},
            {"caption": "Central Bank Endorsement", "detail": "ECB president says the token has 'legitimate store of value properties' in a major speech.",
             "impact": 1, "strength": "strong", "analysis": "Central bank endorsement is unprecedented. Signals potential regulatory acceptance and encourages institutional adoption."},
            {"caption": "Stablecoin Depeg Event", "detail": "A major stablecoin breaks its $1 peg, falling to $0.92. Panic selling spreads across all crypto markets.",
             "impact": -1, "strength": "strong", "analysis": "Stablecoin depegs create systemic risk. The Terra/Luna collapse showed how contagion spirals across all crypto assets."},
            {"caption": "Fortune 500 Treasury Allocation", "detail": "Three Fortune 500 companies announce allocating 5% of treasury to this token as an inflation hedge.",
             "impact": 1, "strength": "moderate", "analysis": "Corporate treasury adoption adds demand and legitimacy, but actual dollar amounts are small relative to market cap."},
            {"caption": "Developer Activity Surges", "detail": "GitHub commits rise 200%. Over 500 new developers contributed in the last month.",
             "impact": 1, "strength": "weak", "analysis": "Developer activity is a leading indicator of future utility, but doesn't immediately translate to price appreciation."},
            {"caption": "Tax Crackdown Announced", "detail": "IRS announces mandatory reporting for all crypto transactions over $100. Exchanges must issue 1099s.",
             "impact": -1, "strength": "weak", "analysis": "Tax reporting increases compliance burden but doesn't ban usage. May reduce speculative trading at the margin."},
            {"caption": "Mining Difficulty Spikes", "detail": "Mining difficulty reaches ATH. Smaller miners forced to sell holdings to cover electricity costs.",
             "impact": -1, "strength": "moderate", "analysis": "High difficulty squeezes miner profitability, forcing sales of held tokens. Increases sell pressure from key stakeholders."},
        ],
    },
}


def generate_join_code():
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=6))


def generate_price_path(template_key: str, duration: int = 300):
    """Generate the full price path and news schedule for a game.

    News creates a directional bias in the random walk. Strength
    determines how biased: strong ~0.88, moderate ~0.72, weak ~0.58.
    Bias lasts for min(45s, time_until_next_news).
    News times use random intervals (10-40s).
    """
    tmpl = TEMPLATES[template_key]
    start_price = tmpl["start_price"]
    news_items = list(tmpl["news"])
    random.shuffle(news_items)

    # Schedule news using random intervals (10-40s between each)
    news_schedule = []
    t = random.randint(15, 30)  # first news between 15-30s
    for item in news_items:
        if t >= duration - 10:  # stop 10s before end
            break
        shift = STRENGTH_TO_PROB.get(item["strength"], 0.22)
        prob_up = 0.5 + (shift if item["impact"] > 0 else -shift)
        news_schedule.append({
            "time": t,
            "caption": item["caption"],
            "detail": item["detail"],
            "impact": item["impact"],
            "strength": item["strength"],
            "prob_up": round(prob_up, 2),
            "analysis": item.get("analysis", ""),
        })
        t += random.randint(10, 40)

    # Pre-compute bias windows
    BIAS_DURATION = 45
    bias_at_tick = {}

    for idx, ns in enumerate(news_schedule):
        start_t = ns["time"]
        if idx + 1 < len(news_schedule):
            end_t = min(start_t + BIAS_DURATION, news_schedule[idx + 1]["time"])
        else:
            end_t = min(start_t + BIAS_DURATION, duration)

        for t_tick in range(start_t, end_t):
            bias_at_tick[t_tick] = ns["prob_up"]

    # Build price path tick by tick
    prices = [start_price]
    noise_std = 0.003

    price = start_price
    for t in range(1, duration + 1):
        abs_move = abs(random.gauss(0, noise_std))
        prob_up = bias_at_tick.get(t, 0.5)

        if random.random() < prob_up:
            price = price * (1 + abs_move)
        else:
            price = price * (1 - abs_move)

        price = max(price, 1)
        prices.append(round(price, 2))

    return {
        "prices": prices,
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
        "status": "lobby",
        "duration": tmpl["duration"],
        "start_price": tmpl["start_price"],
        "prices": [],
        "news_schedule": [],
        "players": [],
        "trades": {},
        "created_by": str(user.id),
        "created_at": dt.datetime.now(dt.timezone.utc),
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
        return {"ok": True, "status": game_data["status"]}

    # Auto-add admin to players if not already there
    players = game_data.get("players", [])
    admin_uid = str(user.id)
    if admin_uid not in [p["user_id"] for p in players]:
        players.append({"user_id": admin_uid, "username": user.username})

    # Generate price path
    path = generate_price_path(game_data["template"], game_data["duration"])

    # Initialize trades for each player
    trades = {}
    for p in players:
        trades[p["user_id"]] = []

    await doc.reference.update({
        "status": "active",
        "prices": path["prices"],
        "news_schedule": path["news_schedule"],
        "players": players,
        "trades": trades,
        "started_at": dt.datetime.now(dt.timezone.utc),
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
    started_at = game_data.get("started_at")
    if started_at:
        now = dt.datetime.now(dt.timezone.utc)
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=dt.timezone.utc)
        elapsed = (now - started_at).total_seconds()
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
    if started_at:
        now = dt.datetime.now(dt.timezone.utc)
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=dt.timezone.utc)
        elapsed = (now - started_at).total_seconds()
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
    released_news = [n for n in news_schedule if n["time"] <= tick]
    # During active, hide analysis; show it only when finished
    if status != "finished":
        result["news"] = [{k: v for k, v in n.items() if k != "analysis"} for n in released_news]
    else:
        result["news"] = released_news
        # Also include ALL news for the analysis page (even unreleased ones)
        result["all_news"] = news_schedule

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
