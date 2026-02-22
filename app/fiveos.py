"""
5Os Card Game Router
====================
Endpoints for creating, joining, playing, and managing 5Os games.
"""
import random
import string
import statistics
import json
from pathlib import Path
from typing import Optional
import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app import db as db_module
from app.auth import current_user
from app.models import User, FiveOsGame, FiveOsSubmission

router = APIRouter(prefix="/5os", tags=["5os"])
BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# ---- Card helpers ----
SUITS = ["hearts", "diamonds", "clubs", "spades"]
RANKS = list(range(1, 14))  # 1=Ace .. 13=King
FULL_DECK = [{"suit": s, "rank": r} for s in SUITS for r in RANKS]

RANK_NAMES = {1: "A", 11: "J", 12: "Q", 13: "K"}
SUIT_SYMBOLS = {"hearts": "♥", "diamonds": "♦", "clubs": "♣", "spades": "♠"}


def card_label(card):
    rank = RANK_NAMES.get(card["rank"], str(card["rank"]))
    suit = SUIT_SYMBOLS.get(card["suit"], card["suit"])
    return f"{rank}{suit}"


def compute_actuals(deck_15):
    """Compute the 3 actual values from the 15 cards."""
    ranks_in = [c["rank"] for c in deck_15]
    ranks_present = set(ranks_in)

    # Q1: sum of ranks NOT in the 15 cards
    q1 = sum(r for r in RANKS if r not in ranks_present)

    # Q2: odd-rank sum minus even-rank sum
    odd_sum = sum(r for r in ranks_in if r % 2 == 1)
    even_sum = sum(r for r in ranks_in if r % 2 == 0)
    q2 = odd_sum - even_sum

    # Q3: sum of all 15 card ranks
    q3 = sum(ranks_in)

    return {"q1": q1, "q2": q2, "q3": q3}


def generate_join_code():
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=6))


# ---- Request schemas ----
class JoinRequest(BaseModel):
    join_code: str


class SubmitRequest(BaseModel):
    est_q1: float
    est_q2: float
    est_q3: float


class AssignTeamRequest(BaseModel):
    user_id: str
    team: str  # "A" or "B"


# ---- Pages ----
@router.get("", include_in_schema=False)
async def rules_page(request: Request):
    """5Os rules and join page."""
    return templates.TemplateResponse("fiveos_rules.html", {
        "request": request,
        "app_name": "AlphaBook",
    })


@router.get("/game/{game_id}", include_in_schema=False)
async def game_page(game_id: str, request: Request):
    """5Os game page."""
    doc = await db_module.db.collection("fiveos_games").document(game_id).get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Game not found")
    return templates.TemplateResponse("fiveos_game.html", {
        "request": request,
        "app_name": "AlphaBook",
        "game_id": game_id,
    })


# ---- Admin: Create game ----
@router.post("/create")
async def create_game(user: User = Depends(current_user)):
    """Admin creates a new 5Os game."""
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin only")

    # Build deck and select 15 cards
    deck = FULL_DECK.copy()
    random.shuffle(deck)
    deck_15 = deck[:15]

    # Pre-generate common cards for odd rounds (1, 3, 5)
    # These are cards from the 15 that are revealed to everyone
    common_indices = random.sample(range(15), 3)
    common_cards = {}
    for i, rnd in enumerate([1, 3, 5]):
        common_cards[str(rnd)] = deck_15[common_indices[i]]

    join_code = generate_join_code()

    game_data = {
        "join_code": join_code,
        "status": "lobby",
        "deck_15": deck_15,
        "common_cards": common_cards,
        "player_cards": {},
        "round_medians": {},
        "players": [],
        "created_by": str(user.id),
        "created_at": dt.datetime.utcnow(),
    }

    doc_ref = db_module.db.collection("fiveos_games").document()
    await doc_ref.set(game_data)

    return {"ok": True, "game_id": doc_ref.id, "join_code": join_code}


# ---- Player: Join game ----
@router.post("/join")
async def join_game(req: JoinRequest, user: User = Depends(current_user)):
    """Player joins a 5Os game with a code."""
    code = req.join_code.strip().upper()

    # Find game by code
    q = db_module.db.collection("fiveos_games").where("join_code", "==", code).where("status", "==", "lobby").limit(1)
    docs = await q.get()
    if not docs:
        raise HTTPException(status_code=404, detail="Game not found or already started")

    doc = docs[0]
    game_data = doc.to_dict()

    # Check if already joined
    for p in game_data.get("players", []):
        if p["user_id"] == str(user.id):
            return {"ok": True, "game_id": doc.id, "message": "Already joined"}

    # Add player
    player_entry = {
        "user_id": str(user.id),
        "username": user.username,
        "team": "",  # Admin assigns later
    }
    players = game_data.get("players", [])
    players.append(player_entry)

    await doc.reference.update({"players": players})

    return {"ok": True, "game_id": doc.id}


# ---- Admin: Assign team ----
@router.post("/game/{game_id}/team")
async def assign_team(game_id: str, req: AssignTeamRequest, user: User = Depends(current_user)):
    """Admin assigns a player to a team."""
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin only")

    doc = await db_module.db.collection("fiveos_games").document(game_id).get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Game not found")

    game_data = doc.to_dict()
    players = game_data.get("players", [])

    found = False
    for p in players:
        if p["user_id"] == req.user_id:
            p["team"] = req.team.upper()
            found = True
            break

    if not found:
        raise HTTPException(status_code=404, detail="Player not in game")

    await doc.reference.update({"players": players})
    return {"ok": True}


# ---- Admin: Advance round ----
@router.post("/game/{game_id}/advance")
async def advance_round(game_id: str, user: User = Depends(current_user)):
    """Admin advances the game to the next round or finishes it."""
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin only")

    doc = await db_module.db.collection("fiveos_games").document(game_id).get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Game not found")

    game_data = doc.to_dict()
    status = game_data["status"]

    if status == "lobby":
        next_round = 1
    elif status.startswith("round_"):
        current_round = int(status.split("_")[1])
        # Calculate medians for the ending round before advancing
        await _compute_round_medians(game_id, current_round, game_data)
        if current_round >= 5:
            # Game over
            await doc.reference.update({"status": "finished"})
            return {"ok": True, "status": "finished"}
        next_round = current_round + 1
    elif status == "finished":
        return {"ok": True, "status": "finished", "message": "Game already ended"}
    else:
        raise HTTPException(status_code=400, detail=f"Unknown status: {status}")

    # Generate player cards for the new round
    deck_15 = game_data["deck_15"]
    common_cards = game_data.get("common_cards", {})
    players = game_data.get("players", [])
    player_cards = game_data.get("player_cards", {})

    # Build pool excluding common cards so no player gets a common card
    common_set = set()
    for rnd_key, cc in common_cards.items():
        common_set.add((cc["suit"], cc["rank"]))
    pool = [c for c in deck_15 if (c["suit"], c["rank"]) not in common_set]
    if not pool:
        pool = deck_15  # fallback if all 15 are common (shouldn't happen)

    round_cards = {}
    for p in players:
        card = random.choice(pool)
        round_cards[p["user_id"]] = card

    player_cards[str(next_round)] = round_cards

    await doc.reference.update({
        "status": f"round_{next_round}",
        "player_cards": player_cards,
    })

    return {"ok": True, "status": f"round_{next_round}"}


async def _compute_round_medians(game_id: str, round_num: int, game_data: dict):
    """Compute median of submissions for a round and store it."""
    # Fetch all submissions for this round
    q = db_module.db.collection("fiveos_submissions") \
        .where("game_id", "==", game_id) \
        .where("round", "==", round_num)
    docs = await q.get()

    q1_vals, q2_vals, q3_vals = [], [], []
    for d in docs:
        s = d.to_dict()
        q1_vals.append(s.get("est_q1", 0))
        q2_vals.append(s.get("est_q2", 0))
        q3_vals.append(s.get("est_q3", 0))

    medians = {
        "q1": statistics.median(q1_vals) if q1_vals else 0,
        "q2": statistics.median(q2_vals) if q2_vals else 0,
        "q3": statistics.median(q3_vals) if q3_vals else 0,
    }

    # Store medians
    game_ref = db_module.db.collection("fiveos_games").document(game_id)
    round_medians = game_data.get("round_medians", {})
    round_medians[str(round_num)] = medians
    await game_ref.update({"round_medians": round_medians})


# ---- Player: Submit answers ----
@router.post("/game/{game_id}/submit")
async def submit_answers(game_id: str, req: SubmitRequest, user: User = Depends(current_user)):
    """Player submits expected values and positions for current round."""
    doc = await db_module.db.collection("fiveos_games").document(game_id).get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Game not found")

    game_data = doc.to_dict()

    # Must be in an active round
    if not game_data["status"].startswith("round_"):
        raise HTTPException(status_code=400, detail="Game is not in an active round")

    current_round = int(game_data["status"].split("_")[1])

    # Verify player is in game
    player_ids = [p["user_id"] for p in game_data.get("players", [])]
    if str(user.id) not in player_ids:
        raise HTTPException(status_code=403, detail="You are not in this game")

    # Check for existing submission (prevent double submit)
    q = db_module.db.collection("fiveos_submissions") \
        .where("game_id", "==", game_id) \
        .where("round", "==", current_round) \
        .where("user_id", "==", str(user.id)) \
        .limit(1)
    existing = await q.get()
    if existing:
        raise HTTPException(status_code=400, detail="Already submitted for this round")

    submission = {
        "game_id": game_id,
        "round": current_round,
        "user_id": str(user.id),
        "est_q1": req.est_q1,
        "est_q2": req.est_q2,
        "est_q3": req.est_q3,
    }

    await db_module.db.collection("fiveos_submissions").document().set(submission)

    return {"ok": True}


# ---- Game state (polling) ----
@router.get("/game/{game_id}/state")
async def game_state(game_id: str, user: User = Depends(current_user)):
    """Get current game state for a player."""
    doc = await db_module.db.collection("fiveos_games").document(game_id).get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Game not found")

    game_data = doc.to_dict()
    uid = str(user.id)
    is_admin = user.is_admin
    status = game_data["status"]
    players = game_data.get("players", [])

    # Verify participation (unless admin)
    player_ids = [p["user_id"] for p in players]
    if uid not in player_ids and not is_admin:
        raise HTTPException(status_code=403, detail="Not in this game")

    result = {
        "game_id": game_id,
        "status": status,
        "players": players,
        "join_code": game_data.get("join_code", ""),
        "is_admin": is_admin,
        "my_cards": {},       # cards shown to this player per round
        "common_cards": {},   # common cards per odd round
        "round_medians": game_data.get("round_medians", {}),
        "my_submissions": {},
    }

    # Determine which rounds to show info for
    if status.startswith("round_"):
        current_round = int(status.split("_")[1])
    elif status == "finished":
        current_round = 5
    else:
        current_round = 0

    # Build card info up to current round
    player_cards = game_data.get("player_cards", {})
    common_cards = game_data.get("common_cards", {})

    for rnd in range(1, current_round + 1):
        rnd_key = str(rnd)

        # Player's private card for this round
        rnd_cards = player_cards.get(rnd_key, {})
        if uid in rnd_cards:
            result["my_cards"][rnd_key] = rnd_cards[uid]

        # Common card for odd rounds
        if rnd % 2 == 1 and rnd_key in common_cards:
            result["common_cards"][rnd_key] = common_cards[rnd_key]

    # Admin sees all player cards
    if is_admin:
        result["all_player_cards"] = player_cards

    # Fetch player's submissions
    q = db_module.db.collection("fiveos_submissions") \
        .where("game_id", "==", game_id) \
        .where("user_id", "==", uid)
    sub_docs = await q.get()
    for sd in sub_docs:
        s = sd.to_dict()
        result["my_submissions"][str(s["round"])] = {
            "est_q1": s["est_q1"], "est_q2": s["est_q2"], "est_q3": s["est_q3"],
        }

    # If game is finished, include actual values and PnL
    if status == "finished":
        result["actuals"] = compute_actuals(game_data["deck_15"])
        result["deck_15"] = game_data["deck_15"]
        result["pnl"] = await _compute_pnl(game_id, game_data)
        # Compute optimal estimates for this user based on their known cards
        result["optimal"] = _compute_optimal_estimates(game_data, uid)

    return result


def _compute_optimal_estimates(game_data: dict, user_id: str):
    """Compute optimal estimates per round based on cards known to the player."""
    deck_15 = game_data["deck_15"]
    player_cards = game_data.get("player_cards", {})
    common_cards = game_data.get("common_cards", {})
    all_ranks = list(range(1, 14))
    total_rank_sum = sum(all_ranks)  # 91

    optimal = {}
    known_cards = []  # accumulate known cards across rounds

    for rnd in range(1, 6):
        rnd_key = str(rnd)

        # Add this round's private card
        rnd_player_cards = player_cards.get(rnd_key, {})
        if user_id in rnd_player_cards:
            known_cards.append(rnd_player_cards[user_id])

        # Add common card for odd rounds
        if rnd % 2 == 1 and rnd_key in common_cards:
            known_cards.append(common_cards[rnd_key])

        # Deduplicate known cards (same card could appear twice)
        known_set = set()
        unique_known = []
        for c in known_cards:
            key = (c["suit"], c["rank"])
            if key not in known_set:
                known_set.add(key)
                unique_known.append(c)

        known_count = len(unique_known)
        unknown_count = 15 - known_count
        known_ranks = [c["rank"] for c in unique_known]
        known_rank_sum = sum(known_ranks)

        # Remaining pool: full deck minus known cards
        remaining_pool = [c for c in FULL_DECK if (c["suit"], c["rank"]) not in known_set]
        if remaining_pool:
            avg_remaining = sum(c["rank"] for c in remaining_pool) / len(remaining_pool)
        else:
            avg_remaining = 7  # fallback

        # Q3: sum of 15 cards = known_sum + unknown_count * avg_remaining
        opt_q3 = known_rank_sum + unknown_count * avg_remaining

        # Q2: odd-rank sum minus even-rank sum
        known_odd = sum(r for r in known_ranks if r % 2 == 1)
        known_even = sum(r for r in known_ranks if r % 2 == 0)
        remaining_odd = [c["rank"] for c in remaining_pool if c["rank"] % 2 == 1]
        remaining_even = [c["rank"] for c in remaining_pool if c["rank"] % 2 == 0]
        avg_odd = sum(remaining_odd) / len(remaining_odd) if remaining_odd else 0
        avg_even = sum(remaining_even) / len(remaining_even) if remaining_even else 0
        # Expected count of odd vs even among unknown cards
        total_remaining = len(remaining_pool)
        if total_remaining > 0:
            frac_odd = len(remaining_odd) / total_remaining
        else:
            frac_odd = 0.5
        exp_odd_count = unknown_count * frac_odd
        exp_even_count = unknown_count * (1 - frac_odd)
        opt_q2 = (known_odd + exp_odd_count * avg_odd) - (known_even + exp_even_count * avg_even)

        # Q1: sum of ranks NOT in the 15 cards
        # Expected: for each rank 1-13, probability it doesn't appear in 15
        known_ranks_set = set(known_ranks)
        # Ranks confirmed present → contribute 0 to Q1
        # Ranks not yet seen → may or may not appear in unknown cards
        opt_q1 = 0
        for rank in all_ranks:
            if rank in known_ranks_set:
                continue  # This rank is in the 15, contributes 0
            # Probability none of the unknown cards have this rank
            cards_with_rank = sum(1 for c in remaining_pool if c["rank"] == rank)
            if total_remaining > 0 and unknown_count > 0:
                # Approximate prob this rank is absent from unknowns
                prob_absent_per_draw = 1 - cards_with_rank / total_remaining
                prob_absent_all = prob_absent_per_draw ** unknown_count
            else:
                prob_absent_all = 1
            opt_q1 += rank * prob_absent_all

        optimal[rnd_key] = {
            "q1": round(opt_q1, 2),
            "q2": round(opt_q2, 2),
            "q3": round(opt_q3, 2),
        }

    return optimal


async def _compute_pnl(game_id: str, game_data: dict):
    """Compute PnL for all players with per-round breakdown."""
    actuals = compute_actuals(game_data["deck_15"])
    medians = game_data.get("round_medians", {})
    players = game_data.get("players", [])

    # Fetch all submissions
    q = db_module.db.collection("fiveos_submissions").where("game_id", "==", game_id)
    docs = await q.get()

    # Group submissions by user
    user_subs = {}
    for d in docs:
        s = d.to_dict()
        uid = s["user_id"]
        rnd = str(s["round"])
        if uid not in user_subs:
            user_subs[uid] = {}
        user_subs[uid][rnd] = s

    # Calculate PnL per player with per-round breakdown
    player_pnl = {}
    # Track cumulative team PnL per round for chart
    team_round_pnl = {}  # {team: [round1_cumulative, round2_cumulative, ...]}

    for p in players:
        uid = p["user_id"]
        total_pnl = 0
        round_pnls = []
        subs = user_subs.get(uid, {})

        for rnd in range(1, 6):
            rnd_key = str(rnd)
            sub = subs.get(rnd_key)
            rnd_medians = medians.get(rnd_key, {})
            rnd_pnl = 0

            if sub and rnd_medians:
                for qkey in ["q1", "q2", "q3"]:
                    med = rnd_medians.get(qkey, 0)
                    est = sub.get(f"est_{qkey}", 0)
                    actual = actuals[qkey]

                    # Position: est > median → long, est < median → short
                    # est == median → random 50/50
                    if est > med:
                        pnl = actual - med
                    elif est < med:
                        pnl = med - actual
                    else:
                        # 50/50 random
                        if random.random() < 0.5:
                            pnl = actual - med
                        else:
                            pnl = med - actual
                    fee = abs(est - med) / 3
                    rnd_pnl += pnl - fee

            total_pnl += rnd_pnl
            round_pnls.append(round(total_pnl, 2))  # cumulative

        player_pnl[uid] = {
            "user_id": uid,
            "username": p["username"],
            "team": p.get("team", ""),
            "pnl": round(total_pnl, 2),
            "round_pnls": round_pnls,
        }

        # Add to team cumulative
        team = p.get("team", "")
        if team:
            if team not in team_round_pnl:
                team_round_pnl[team] = [0, 0, 0, 0, 0]
            for i in range(5):
                team_round_pnl[team][i] += round_pnls[i] if i < len(round_pnls) else 0

    # Round team values
    for team in team_round_pnl:
        team_round_pnl[team] = [round(v, 2) for v in team_round_pnl[team]]

    # Total team PnL
    team_pnl = {}
    for uid, data in player_pnl.items():
        team = data["team"]
        if team:
            team_pnl[team] = team_pnl.get(team, 0) + data["pnl"]

    return {
        "players": player_pnl,
        "teams": team_pnl,
        "team_round_pnl": team_round_pnl,
        "winner": max(team_pnl, key=team_pnl.get) if team_pnl else None,
    }
