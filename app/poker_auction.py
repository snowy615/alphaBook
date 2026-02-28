"""
Poker Auction Game Router
=========================
Multi-round sealed-bid second-price auction card game for 8 teams.
"""
import random
import string
import datetime as dt
from pathlib import Path
from typing import Optional, List
from collections import Counter
from itertools import combinations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app import db as db_module
from app.auth import current_user
from app.models import User

router = APIRouter(prefix="/poker-auction", tags=["poker-auction"])
BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# ---- Card & Deck ----
SUITS = ["hearts", "diamonds", "clubs", "spades"]
RANKS = list(range(1, 14))  # 1=Ace .. 13=King
FULL_DECK = [{"suit": s, "rank": r} for s in SUITS for r in RANKS]

RANK_NAMES = {1: "A", 10: "10", 11: "J", 12: "Q", 13: "K"}
SUIT_SYMBOLS = {"hearts": "♥", "diamonds": "♦", "clubs": "♣", "spades": "♠"}

ROUND_SCHEDULE = [2, 3, 4, 5, 5, 5, 5, 5, 5, 5, 4, 2, 2]  # 13 rounds = 52 cards
HAND_PRIZES = [2000, 1500, 1100, 700, 400, 200, 0, -500]  # rank 1-8


def card_label(card):
    r = RANK_NAMES.get(card["rank"], str(card["rank"]))
    s = SUIT_SYMBOLS.get(card["suit"], card["suit"])
    return f"{r}{s}"


def generate_join_code():
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=6))


# ---- Poker Hand Evaluation ----
# Hand ranks (higher = better):
# 9: Royal Flush, 8: Straight Flush, 7: Four of a Kind, 6: Full House,
# 5: Flush, 4: Straight, 3: Three of a Kind, 2: Two Pair, 1: One Pair, 0: High Card

HAND_RANK_NAMES = {
    9: "Royal Flush", 8: "Straight Flush", 7: "Four of a Kind",
    6: "Full House", 5: "Flush", 4: "Straight", 3: "Three of a Kind",
    2: "Two Pair", 1: "One Pair", 0: "High Card",
}


def _rank_val(r):
    """Convert rank to value (Ace=14 for high comparisons)."""
    return 14 if r == 1 else r


def evaluate_5_card_hand(cards):
    """Evaluate a 5-card poker hand. Returns (hand_rank, tiebreaker_tuple)."""
    if len(cards) != 5:
        return (0, (0,))

    ranks = sorted([_rank_val(c["rank"]) for c in cards], reverse=True)
    suits = [c["suit"] for c in cards]
    rank_counts = Counter(ranks)
    is_flush = len(set(suits)) == 1

    # Check straight (including wheel: A-2-3-4-5)
    unique_ranks = sorted(set(ranks))
    is_straight = False
    straight_high = 0

    if len(unique_ranks) >= 5:
        for i in range(len(unique_ranks) - 4):
            if unique_ranks[i + 4] - unique_ranks[i] == 4:
                is_straight = True
                straight_high = unique_ranks[i + 4]

    # Wheel: A-2-3-4-5
    if set([14, 2, 3, 4, 5]).issubset(set(ranks)):
        is_straight = True
        straight_high = 5  # 5 is the high card in a wheel

    counts = sorted(rank_counts.values(), reverse=True)
    count_ranks = sorted(rank_counts.items(), key=lambda x: (x[1], x[0]), reverse=True)
    ordered = [r for r, c in count_ranks]

    if is_flush and is_straight:
        if straight_high == 14:
            return (9, (straight_high,))  # Royal Flush
        return (8, (straight_high,))  # Straight Flush
    if counts[0] >= 4:  # 4 of a kind (or 5, treated same)
        return (7, tuple(ordered))
    if counts[0] == 3 and counts[1] >= 2:
        return (6, tuple(ordered))  # Full House
    if is_flush:
        return (5, tuple(ranks))
    if is_straight:
        return (4, (straight_high,))
    if counts[0] == 3:
        return (3, tuple(ordered))  # Three of a Kind
    if counts[0] == 2 and counts[1] == 2:
        return (2, tuple(ordered))  # Two Pair
    if counts[0] == 2:
        return (1, tuple(ordered))  # One Pair
    return (0, tuple(ranks))  # High Card


def best_poker_hand(cards):
    """Find the best 5-card poker hand from any number of cards."""
    if len(cards) < 5:
        # Pad with dummy low cards for evaluation
        padded = cards + [{"suit": "none", "rank": 0}] * (5 - len(cards))
        score = evaluate_5_card_hand(padded)
        return {"cards": cards, "rank": score[0], "rank_name": HAND_RANK_NAMES.get(score[0], "High Card"), "score": score}

    best = None
    best_combo = None
    for combo in combinations(range(len(cards)), 5):
        hand = [cards[i] for i in combo]
        score = evaluate_5_card_hand(hand)
        if best is None or score > best:
            best = score
            best_combo = hand

    rank = best[0]
    return {
        "cards": best_combo,
        "rank": rank,
        "rank_name": HAND_RANK_NAMES.get(rank, "High Card"),
        "score": best,
    }


def auction_cost(num_cards):
    """Cost to auction cards to other teams."""
    if num_cards <= 0:
        return 0
    if num_cards == 1:
        return 40
    if num_cards == 2:
        return 70
    if num_cards == 3:
        return 90
    return 90 + (num_cards - 3) * 10


# ---- Request Schemas ----
class JoinRequest(BaseModel):
    join_code: str


class CreateRequest(BaseModel):
    team_name: str = ""


class BidRequest(BaseModel):
    amount: int  # bid amount in dollars


class PostAuctionRequest(BaseModel):
    sell_to_host: list = []  # list of card indices to sell at $20 each
    auction_cards: list = []  # list of card indices to auction to other teams


class PostBidRequest(BaseModel):
    listing_idx: int  # which listing to bid on
    amount: int  # bid amount


# ---- Pages ----
@router.get("", include_in_schema=False)
async def rules_page(request: Request):
    return templates.TemplateResponse("poker_auction_rules.html", {
        "request": request,
        "app_name": "AlphaBook",
    })


@router.get("/game/{game_id}", include_in_schema=False)
async def game_page(game_id: str, request: Request):
    doc = await db_module.db.collection("poker_auction_games").document(game_id).get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Game not found")
    return templates.TemplateResponse("poker_auction_game.html", {
        "request": request,
        "app_name": "AlphaBook",
        "game_id": game_id,
    })


# ---- Admin: Create game ----
@router.post("/create")
async def create_game(user: User = Depends(current_user)):
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin only")

    join_code = generate_join_code()

    game_data = {
        "join_code": join_code,
        "status": "lobby",  # lobby → active → post_auction → post_bidding → finished
        "round": 0,
        "round_phase": "",  # bidding | result
        "teams": [],  # [{team_id, team_name, user_ids, money, cards}]
        "deck": [],  # shuffled deck for market auction
        "round_cards": [],  # cards currently being auctioned
        "round_bids": {},  # {team_id: amount}
        "round_history": [],  # [{round, cards, winner_team, paid}]
        "post_listings": [],  # [{team_id, cards, cost}]
        "post_bids": {},  # {"listing_idx": {team_id: amount}}
        "post_sells": {},  # {team_id: [card indices]}
        "post_submitted": [],  # team_ids that submitted post-auction
        "poker_hands": {},  # {team_id: {cards, rank, rank_name}}
        "created_by": str(user.id),
        "created_at": dt.datetime.now(dt.timezone.utc),
    }

    doc_ref = db_module.db.collection("poker_auction_games").document()
    await doc_ref.set(game_data)

    return {"ok": True, "game_id": doc_ref.id, "join_code": join_code}


# ---- Team: Join game ----
@router.post("/join")
async def join_game(req: JoinRequest, user: User = Depends(current_user)):
    code = req.join_code.strip().upper()
    q = db_module.db.collection("poker_auction_games") \
        .where("join_code", "==", code) \
        .where("status", "==", "lobby").limit(1)
    docs = await q.get()
    if not docs:
        raise HTTPException(status_code=404, detail="Game not found or already started")

    doc = docs[0]
    game_data = doc.to_dict()
    teams = game_data.get("teams", [])
    uid = str(user.id)

    # Check if already joined
    for t in teams:
        if uid in t.get("user_ids", []):
            return {"ok": True, "game_id": doc.id, "team_id": t["team_id"]}

    if len(teams) >= 8:
        raise HTTPException(status_code=400, detail="Game is full (8 teams max)")

    team_id = f"team_{len(teams) + 1}"
    teams.append({
        "team_id": team_id,
        "team_name": user.username,
        "user_ids": [uid],
        "money": 1000,
        "cards": [],
    })

    await doc.reference.update({"teams": teams})
    return {"ok": True, "game_id": doc.id, "team_id": team_id}


# ---- Admin: Start game ----
@router.post("/game/{game_id}/start")
async def start_game(game_id: str, user: User = Depends(current_user)):
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin only")

    doc = await db_module.db.collection("poker_auction_games").document(game_id).get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Game not found")

    game_data = doc.to_dict()
    if game_data["status"] != "lobby":
        return {"ok": True, "status": game_data["status"]}

    teams = game_data.get("teams", [])
    if len(teams) < 2:
        raise HTTPException(status_code=400, detail="Need at least 2 teams")

    # Shuffle a full deck for the market auction (52 cards)
    deck = list(FULL_DECK)
    random.shuffle(deck)

    # Deal one random starting card to each team (from a second copy)
    starting_deck = list(FULL_DECK)
    random.shuffle(starting_deck)
    for i, team in enumerate(teams):
        team["cards"] = [starting_deck[i]]

    await doc.reference.update({
        "status": "active",
        "round": 0,
        "deck": deck,
        "teams": teams,
        "started_at": dt.datetime.now(dt.timezone.utc),
    })

    return {"ok": True, "status": "active"}


# ---- Admin: Advance round/phase ----
@router.post("/game/{game_id}/advance")
async def advance(game_id: str, user: User = Depends(current_user)):
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin only")

    doc = await db_module.db.collection("poker_auction_games").document(game_id).get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Game not found")

    game_data = doc.to_dict()
    status = game_data["status"]
    current_round = game_data.get("round", 0)
    phase = game_data.get("round_phase", "")

    if status == "active":
        if phase == "" or phase == "result":
            # Start next round bidding
            next_round = current_round + 1
            if next_round > 13:
                # Move to post-auction phase
                await doc.reference.update({
                    "status": "post_auction",
                    "round_phase": "",
                    "round_cards": [],
                    "round_bids": {},
                })
                return {"ok": True, "action": "post_auction"}

            # Deal cards for this round
            deck = game_data.get("deck", [])
            num_cards = ROUND_SCHEDULE[next_round - 1]
            round_cards = deck[:num_cards]
            remaining_deck = deck[num_cards:]

            await doc.reference.update({
                "round": next_round,
                "round_phase": "bidding",
                "round_cards": round_cards,
                "round_bids": {},
                "deck": remaining_deck,
            })
            return {"ok": True, "action": "round_bidding", "round": next_round}

        elif phase == "bidding":
            # Close bidding → determine winner
            bids = game_data.get("round_bids", {})
            round_cards = game_data.get("round_cards", [])
            teams = game_data.get("teams", [])
            history = game_data.get("round_history", [])

            if not bids:
                # No bids → cards go to waste
                history.append({
                    "round": current_round,
                    "cards": round_cards,
                    "winner_team": None,
                    "paid": 0,
                })
                await doc.reference.update({
                    "round_phase": "result",
                    "round_history": history,
                })
                return {"ok": True, "action": "no_bids"}

            # Sort bids descending
            sorted_bids = sorted(bids.items(), key=lambda x: x[1], reverse=True)
            winner_id = sorted_bids[0][0]
            highest_bid = sorted_bids[0][1]
            second_bid = sorted_bids[1][1] if len(sorted_bids) > 1 else 0

            # Winner pays second-highest bid
            for team in teams:
                if team["team_id"] == winner_id:
                    team["money"] -= second_bid
                    team["cards"].extend(round_cards)
                    break

            history.append({
                "round": current_round,
                "cards": round_cards,
                "winner_team": winner_id,
                "paid": second_bid,
                "highest_bid": highest_bid,
            })

            await doc.reference.update({
                "round_phase": "result",
                "teams": teams,
                "round_history": history,
            })
            return {"ok": True, "action": "round_result", "winner": winner_id, "paid": second_bid}

    elif status == "post_auction":
        # Move to post-auction bidding phase
        teams = game_data.get("teams", [])
        post_listings = game_data.get("post_listings", [])

        # Process sell-to-host orders
        post_sells = game_data.get("post_sells", {})
        for team in teams:
            tid = team["team_id"]
            sell_indices = post_sells.get(tid, [])
            if sell_indices:
                # Sort indices descending to avoid shift issues
                sell_indices_sorted = sorted(sell_indices, reverse=True)
                for idx in sell_indices_sorted:
                    if 0 <= idx < len(team["cards"]):
                        team["cards"].pop(idx)
                        team["money"] += 20

        # Deduct auction listing costs
        for listing in post_listings:
            for team in teams:
                if team["team_id"] == listing["team_id"]:
                    cost = auction_cost(len(listing["cards"]))
                    team["money"] -= cost
                    # Remove auctioned cards from team
                    # Cards were stored as actual card objects in listing
                    for card in listing["cards"]:
                        for i, tc in enumerate(team["cards"]):
                            if tc["suit"] == card["suit"] and tc["rank"] == card["rank"]:
                                team["cards"].pop(i)
                                break
                    break

        await doc.reference.update({
            "status": "post_bidding",
            "teams": teams,
            "post_bids": {},
        })
        return {"ok": True, "action": "post_bidding"}

    elif status == "post_bidding":
        # Resolve post-auction bids and move to hand evaluation
        teams = game_data.get("teams", [])
        post_listings = game_data.get("post_listings", [])
        post_bids = game_data.get("post_bids", {})

        # Resolve each listing
        for idx, listing in enumerate(post_listings):
            listing_bids = post_bids.get(str(idx), {})
            if not listing_bids:
                continue  # No bids, cards go to waste

            sorted_b = sorted(listing_bids.items(), key=lambda x: x[1], reverse=True)
            winner_id = sorted_b[0][0]
            second_price = sorted_b[1][1] if len(sorted_b) > 1 else 0

            # Winner gets cards, pays second price
            seller_id = listing["team_id"]
            for team in teams:
                if team["team_id"] == winner_id:
                    team["cards"].extend(listing["cards"])
                    team["money"] -= second_price
                if team["team_id"] == seller_id:
                    team["money"] += second_price

            listing["winner"] = winner_id
            listing["paid"] = second_price

        # Evaluate poker hands
        poker_hands = {}
        for team in teams:
            hand = best_poker_hand(team["cards"])
            poker_hands[team["team_id"]] = {
                "cards": [card_label(c) for c in hand["cards"]],
                "rank": hand["rank"],
                "rank_name": hand["rank_name"],
                "score": list(hand["score"]),
            }

        # Rank hands and assign prizes
        hand_rankings = sorted(
            poker_hands.items(),
            key=lambda x: (x[1]["score"][0], x[1]["score"][1]),
            reverse=True
        )

        for i, (tid, hand_info) in enumerate(hand_rankings):
            prize = HAND_PRIZES[i] if i < len(HAND_PRIZES) else -500
            hand_info["prize"] = prize
            hand_info["hand_position"] = i + 1
            for team in teams:
                if team["team_id"] == tid:
                    team["money"] += prize
                    break

        await doc.reference.update({
            "status": "finished",
            "teams": teams,
            "poker_hands": poker_hands,
            "post_listings": post_listings,
        })
        return {"ok": True, "action": "finished"}

    raise HTTPException(status_code=400, detail=f"Cannot advance from status={status}, phase={phase}")


# ---- Team: Submit bid for current round ----
@router.post("/game/{game_id}/bid")
async def submit_bid(game_id: str, req: BidRequest, user: User = Depends(current_user)):
    doc = await db_module.db.collection("poker_auction_games").document(game_id).get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Game not found")

    game_data = doc.to_dict()
    if game_data["status"] != "active" or game_data.get("round_phase") != "bidding":
        raise HTTPException(status_code=400, detail="Not in bidding phase")

    uid = str(user.id)
    teams = game_data.get("teams", [])
    team_id = None
    team_money = 0
    for t in teams:
        if uid in t.get("user_ids", []):
            team_id = t["team_id"]
            team_money = t["money"]
            break

    if not team_id:
        raise HTTPException(status_code=403, detail="Not in this game")

    if req.amount < 0:
        raise HTTPException(status_code=400, detail="Bid must be >= 0")
    if req.amount > team_money:
        raise HTTPException(status_code=400, detail=f"Bid exceeds your budget (${team_money})")

    bids = game_data.get("round_bids", {})
    bids[team_id] = req.amount

    await doc.reference.update({"round_bids": bids})
    return {"ok": True, "team_id": team_id, "amount": req.amount}


# ---- Team: Submit post-auction orders ----
@router.post("/game/{game_id}/post-auction")
async def submit_post_auction(game_id: str, req: PostAuctionRequest, user: User = Depends(current_user)):
    doc = await db_module.db.collection("poker_auction_games").document(game_id).get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Game not found")

    game_data = doc.to_dict()
    if game_data["status"] != "post_auction":
        raise HTTPException(status_code=400, detail="Not in post-auction phase")

    uid = str(user.id)
    teams = game_data.get("teams", [])
    team = None
    for t in teams:
        if uid in t.get("user_ids", []):
            team = t
            break
    if not team:
        raise HTTPException(status_code=403, detail="Not in this game")

    # Check for duplicate indices
    all_indices = req.sell_to_host + req.auction_cards
    if len(all_indices) != len(set(all_indices)):
        raise HTTPException(status_code=400, detail="Cannot sell and auction the same card")

    # Validate indices
    for idx in all_indices:
        if idx < 0 or idx >= len(team["cards"]):
            raise HTTPException(status_code=400, detail=f"Invalid card index: {idx}")

    # Store sell-to-host indices
    post_sells = game_data.get("post_sells", {})
    post_sells[team["team_id"]] = req.sell_to_host

    # Store auction listing
    post_listings = game_data.get("post_listings", [])
    # Remove any previous listing from this team
    post_listings = [l for l in post_listings if l["team_id"] != team["team_id"]]

    if req.auction_cards:
        auction_card_objects = [team["cards"][i] for i in req.auction_cards]
        post_listings.append({
            "team_id": team["team_id"],
            "team_name": team["team_name"],
            "cards": auction_card_objects,
            "cost": auction_cost(len(auction_card_objects)),
        })

    post_submitted = game_data.get("post_submitted", [])
    if team["team_id"] not in post_submitted:
        post_submitted.append(team["team_id"])

    await doc.reference.update({
        "post_sells": post_sells,
        "post_listings": post_listings,
        "post_submitted": post_submitted,
    })

    return {"ok": True}


# ---- Team: Bid on post-auction listing ----
@router.post("/game/{game_id}/post-bid")
async def submit_post_bid(game_id: str, req: PostBidRequest, user: User = Depends(current_user)):
    doc = await db_module.db.collection("poker_auction_games").document(game_id).get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Game not found")

    game_data = doc.to_dict()
    if game_data["status"] != "post_bidding":
        raise HTTPException(status_code=400, detail="Not in post-bidding phase")

    uid = str(user.id)
    teams = game_data.get("teams", [])
    team_id = None
    team_money = 0
    for t in teams:
        if uid in t.get("user_ids", []):
            team_id = t["team_id"]
            team_money = t["money"]
            break
    if not team_id:
        raise HTTPException(status_code=403, detail="Not in this game")

    post_listings = game_data.get("post_listings", [])
    if req.listing_idx < 0 or req.listing_idx >= len(post_listings):
        raise HTTPException(status_code=400, detail="Invalid listing index")

    # Can't bid on own listing
    if post_listings[req.listing_idx]["team_id"] == team_id:
        raise HTTPException(status_code=400, detail="Cannot bid on your own listing")

    if req.amount < 0:
        raise HTTPException(status_code=400, detail="Bid must be >= 0")
    if req.amount > team_money:
        raise HTTPException(status_code=400, detail=f"Bid exceeds budget (${team_money})")

    post_bids = game_data.get("post_bids", {})
    listing_key = str(req.listing_idx)
    if listing_key not in post_bids:
        post_bids[listing_key] = {}
    post_bids[listing_key][team_id] = req.amount

    await doc.reference.update({"post_bids": post_bids})
    return {"ok": True}


# ---- Game state ----
@router.get("/game/{game_id}/state")
async def game_state(game_id: str, user: User = Depends(current_user)):
    doc = await db_module.db.collection("poker_auction_games").document(game_id).get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Game not found")

    game_data = doc.to_dict()
    uid = str(user.id)
    is_admin = user.is_admin
    teams = game_data.get("teams", [])

    # Find user's team
    my_team_id = None
    for t in teams:
        if uid in t.get("user_ids", []):
            my_team_id = t["team_id"]
            break

    if not my_team_id and not is_admin:
        raise HTTPException(status_code=403, detail="Not in this game")

    status = game_data["status"]

    # Build public team info (hide other teams' cards during active play)
    public_teams = []
    my_cards = []
    my_money = 0
    for t in teams:
        info = {
            "team_id": t["team_id"],
            "team_name": t["team_name"],
            "money": t["money"],
            "card_count": len(t.get("cards", [])),
        }
        if t["team_id"] == my_team_id or status == "finished" or is_admin:
            info["cards"] = [card_label(c) for c in t.get("cards", [])]
            info["cards_raw"] = t.get("cards", [])
        public_teams.append(info)

        if t["team_id"] == my_team_id:
            my_cards = [card_label(c) for c in t.get("cards", [])]
            my_money = t["money"]

    result = {
        "game_id": game_id,
        "status": status,
        "round": game_data.get("round", 0),
        "round_phase": game_data.get("round_phase", ""),
        "total_rounds": 13,
        "teams": public_teams,
        "my_team_id": my_team_id,
        "my_cards": my_cards,
        "my_money": my_money,
        "is_admin": is_admin,
        "join_code": game_data.get("join_code", ""),
        "round_schedule": ROUND_SCHEDULE,
    }

    if status == "active":
        result["round_cards"] = [card_label(c) for c in game_data.get("round_cards", [])]
        result["round_cards_count"] = ROUND_SCHEDULE[game_data.get("round", 1) - 1] if game_data.get("round", 0) > 0 else 0

        # Show if current team has bid
        bids = game_data.get("round_bids", {})
        result["my_bid"] = bids.get(my_team_id)
        result["bids_submitted"] = list(bids.keys())  # which teams have bid
        result["num_bids"] = len(bids)

        # Show result data during result phase
        if game_data.get("round_phase") == "result":
            result["round_bids"] = bids
            history = game_data.get("round_history", [])
            if history:
                latest = history[-1]
                result["round_winner"] = latest.get("winner_team")
                result["round_paid"] = latest.get("paid", 0)
                result["round_highest_bid"] = latest.get("highest_bid", 0)

    elif status == "post_auction":
        post_submitted = game_data.get("post_submitted", [])
        result["post_submitted"] = post_submitted
        result["all_submitted"] = len(post_submitted) >= len(teams)

    elif status == "post_bidding":
        post_listings = game_data.get("post_listings", [])
        result["post_listings"] = [{
            "team_id": l["team_id"],
            "team_name": l["team_name"],
            "cards": [card_label(c) for c in l["cards"]],
            "cost": l["cost"],
        } for l in post_listings]

        post_bids = game_data.get("post_bids", {})
        # Show which listings this team has bid on
        my_post_bids = {}
        for lidx, bids in post_bids.items():
            if my_team_id in bids:
                my_post_bids[lidx] = bids[my_team_id]
        result["my_post_bids"] = my_post_bids
        result["post_bids_counts"] = {k: len(v) for k, v in post_bids.items()}

    elif status == "finished":
        result["poker_hands"] = game_data.get("poker_hands", {})
        result["round_history"] = game_data.get("round_history", [])
        post_listings = game_data.get("post_listings", [])
        result["post_results"] = [{
            "team_id": l["team_id"],
            "team_name": l["team_name"],
            "cards": [card_label(c) for c in l["cards"]],
            "winner": l.get("winner"),
            "paid": l.get("paid", 0),
        } for l in post_listings]

    result["round_history"] = [
        {
            "round": h["round"],
            "cards": [card_label(c) for c in h.get("cards", [])],
            "winner_team": h.get("winner_team"),
            "paid": h.get("paid", 0),
        } for h in game_data.get("round_history", [])
    ]

    return result
