"""
Toxic Flow — bluffing with a margin account.
============================================

Cheat/BS with a trading desk bolted on. You play cards face down and declare a
rank; the difference from the pub version is that every claim is *collateralised*.
Posting a lie is cheap only if nobody pays to look.

The loop, per turn:

1. **Post.** Play 1–4 cards face down, declare a rank, and escrow margin equal
   to ``cards × rank value``. Big claims cost more to make.
2. **Audit window.** For 20 seconds the rest of the table can put money up to
   look at your cards. A single player must cover the margin alone, or several
   can pool until the stake reaches it.
3. **Resolution.**
   * *Unaudited and true* — margin comes back, cards stay in the middle.
   * *Unaudited and false* — take the margin back plus a $2 skim (**Conceal**),
     or show the table the lie and hand the played cards to one opponent
     (**Flash**).
   * *Audited and false* — the auditors take the margin, plus $1 from your
     pocket for every card in the middle, and you pick the whole pile up.
   * *Audited and true* (**the Squeeze**) — you keep the margin and take their
     stake, and the auditor of record picks the pile up.

Nobody sees a card until it is paid for, so the server holds every hand and the
face-down claim, and the state endpoint redacts them per viewer. Hands are
*never* serialised to anyone but their owner.

The 20-second window is derived from a timestamp rather than a server timer, so
a restart or a browser going away can't strand a claim — the same approach the
other timed modes here use.

Interpretations, where the rulebook left room (all surfaced in the in-game
rules so a table can house-rule them):

* **"Matching the sequence"** — the first claim of a pile sets the rank and
  each later claim must be the next rank up, wrapping King→Ace→2. Clearing the
  pile starts a fresh sequence.
* **Margin** is priced off the *declared* rank, since that is all the table can
  see when deciding whether to pay to look.
* **The fine** counts every card in the middle including the ones just played.
* **A pooled audit** pays out pro rata, and the largest contributor is the
  auditor of record who picks up the pile on a Squeeze.
* **Bankrupt players'** cards go to the bottom of the middle pile — the only
  pile in play once the deal is done.
"""
from __future__ import annotations

import datetime as dt
import logging
import random
import string
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from app import db as db_module
from app import feedback as fb
from app import scores
from app.auth import current_user
from app.models import User

log = logging.getLogger("uvicorn.error")

router = APIRouter(prefix="/toxic-flow", tags=["toxic-flow"])
BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

COLLECTION = "toxic_flow_games"

# ── Rules constants ───────────────────────────────────────────────────────────
STARTING_CAPITAL = 100
AUDIT_SECONDS = 20
CONCEAL_BONUS = 2
MAX_CARDS_PER_PLAY = 4
MIN_PLAYERS = 3
MAX_PLAYERS = 6
LIQUIDITY_BONUSES = [80, 40, 20]      # 1st, 2nd, 3rd by fewest cards left

SUITS = ["hearts", "diamonds", "clubs", "spades"]
RANKS = list(range(1, 14))             # 1 = Ace … 13 = King
RANK_NAMES = {1: "A", 11: "J", 12: "Q", 13: "K"}
SUIT_SYMBOLS = {"hearts": "♥", "diamonds": "♦", "clubs": "♣", "spades": "♠"}


def rank_name(r: int) -> str:
    return RANK_NAMES.get(r, str(r))


def card_label(c: Dict[str, Any]) -> str:
    return f"{rank_name(c['rank'])}{SUIT_SYMBOLS.get(c['suit'], '')}"


def rank_value(r: int) -> int:
    """Ace is 11, court cards 10, everything else its face value."""
    if r == 1:
        return 11
    return 10 if r >= 11 else r


def next_rank(r: int) -> int:
    """King wraps to Ace, Ace to 2, so a sequence never dead-ends."""
    return 1 if r == 13 else r + 1


def generate_join_code() -> str:
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=6))


def _require_host(game: dict, user: User) -> None:
    if game.get("created_by") != str(user.id) and not user.is_admin:
        raise HTTPException(403, "Only the host of this table can do that")


# ── Request schemas ───────────────────────────────────────────────────────────
class JoinRequest(BaseModel):
    join_code: str


class PlayRequest(BaseModel):
    rank: int
    cards: List[int] = Field(default_factory=list)   # indexes into your hand


class AuditRequest(BaseModel):
    amount: int


class ChoiceRequest(BaseModel):
    action: str                       # "conceal" | "flash"
    target_id: Optional[str] = None   # who eats the cards on a Flash


# ── Helpers over the game document ────────────────────────────────────────────

def _players(game: dict) -> List[dict]:
    return game.get("players") or []


def _player(game: dict, uid: str) -> Optional[dict]:
    return next((p for p in _players(game) if p["user_id"] == uid), None)


def _solvent(game: dict) -> List[dict]:
    """Everyone still in the game, in seat order."""
    return [p for p in _players(game) if not p.get("bankrupt")]


def _advance_turn(game: dict, from_uid: Optional[str] = None) -> None:
    """Hand the turn to the next solvent player after `from_uid`."""
    order = _solvent(game)
    if not order:
        return
    ids = [p["user_id"] for p in order]
    anchor = from_uid or game.get("turn_id")
    idx = ids.index(anchor) if anchor in ids else -1
    game["turn_id"] = ids[(idx + 1) % len(ids)]


def _pay(game: dict, uid: str, amount: int) -> int:
    """
    Move `amount` out of a player's stack, capped at what they hold.

    Returns what was actually taken; a stack that reaches zero is a margin call
    and the player is out.
    """
    p = _player(game, uid)
    if not p or amount <= 0:
        return 0
    taken = min(int(p["chips"]), int(amount))
    p["chips"] = int(p["chips"]) - taken
    if p["chips"] <= 0:
        _bankrupt(game, p)
    return taken


def _credit(game: dict, uid: str, amount: int) -> None:
    p = _player(game, uid)
    if p and amount:
        p["chips"] = int(p["chips"]) + int(amount)


def _bankrupt(game: dict, p: dict) -> None:
    """Margin call: the seat is gone and its cards return to the pile."""
    if p.get("bankrupt"):
        return
    p["bankrupt"] = True
    p["chips"] = 0
    cards = p.get("hand") or []
    p["hand"] = []
    if cards:
        random.shuffle(cards)
        game["pile"] = (game.get("pile") or []) + cards
    _log(game, f"{p['username']} took a margin call and is out.")


def _log(game: dict, text: str) -> None:
    entries = game.get("log") or []
    entries.append({"t": dt.datetime.utcnow().isoformat(timespec="seconds"), "text": text})
    game["log"] = entries[-40:]


def _deal(players: List[dict]) -> List[dict]:
    deck = [{"suit": s, "rank": r} for s in SUITS for r in RANKS]
    random.shuffle(deck)
    for i, card in enumerate(deck):
        players[i % len(players)]["hand"].append(card)
    for p in players:
        p["hand"].sort(key=lambda c: (c["rank"], c["suit"]))
    return players


def _claim_elapsed(game: dict) -> float:
    claim = game.get("claim")
    if not claim or not claim.get("opened_at"):
        return 0.0
    opened = claim["opened_at"]
    if isinstance(opened, str):
        opened = dt.datetime.fromisoformat(opened)
    if opened.tzinfo is None:
        opened = opened.replace(tzinfo=dt.timezone.utc)
    return (dt.datetime.now(dt.timezone.utc) - opened).total_seconds()


def _audit_pool(claim: dict) -> int:
    return sum(int(v) for v in (claim.get("audits") or {}).values())


# ── Resolution ────────────────────────────────────────────────────────────────

def _claim_is_true(claim: dict) -> bool:
    return all(c["rank"] == claim["rank"] for c in claim["cards"])


def _resolve_audited(game: dict) -> None:
    """Someone paid to look."""
    claim = game["claim"]
    audits: Dict[str, int] = claim.get("audits") or {}
    pool = _audit_pool(claim)
    claimant = claim["player_id"]
    who = _player(game, claimant)
    truthful = _claim_is_true(claim)

    # Auditor of record: largest stake, earliest on a tie.
    of_record = max(audits.items(), key=lambda kv: kv[1])[0]

    # The played cards are in the middle either way — that is what was audited.
    game["pile"] = (game.get("pile") or []) + claim["cards"]
    shown = [card_label(c) for c in claim["cards"]]

    if truthful:
        # The Squeeze: margin back, and the stake is forfeit to the claimant.
        _credit(game, claimant, claim["margin"] + pool)
        for uid in audits:
            pass  # stakes were already escrowed on submission
        taker = _player(game, of_record)
        if taker:
            taker["hand"] = (taker.get("hand") or []) + game["pile"]
            taker["hand"].sort(key=lambda c: (c["rank"], c["suit"]))
        picked = len(game["pile"])
        game["pile"] = []
        game["declared_rank"] = None
        _log(game, f"Squeeze — {who['username']} was telling the truth "
                   f"({' '.join(shown)}). {taker['username'] if taker else 'The auditor'} "
                   f"pays {pool} and picks up {picked} cards.")
        game["reveal"] = {"kind": "squeeze", "cards": shown, "claimant": claimant,
                          "auditor": of_record, "pool": pool}
    else:
        # Caught: auditors take the margin, plus $1 a card out of pocket.
        pile_size = len(game["pile"])
        fine = _pay(game, claimant, pile_size)
        spoils = claim["margin"] + fine
        for uid, stake in audits.items():
            share = int(round(spoils * (stake / pool))) if pool else 0
            _credit(game, uid, stake + share)     # stake returned plus winnings
        loser = _player(game, claimant)
        if loser and not loser.get("bankrupt"):
            loser["hand"] = (loser.get("hand") or []) + game["pile"]
            loser["hand"].sort(key=lambda c: (c["rank"], c["suit"]))
            game["pile"] = []
        else:
            game["pile"] = []          # a bankrupt claimant's cards already moved
        game["declared_rank"] = None
        _log(game, f"Audit paid off — {who['username']} lied ({' '.join(shown)}), "
                   f"forfeits {claim['margin']} margin and a {fine} fine, "
                   f"and picks up {pile_size} cards.")
        game["reveal"] = {"kind": "caught", "cards": shown, "claimant": claimant,
                          "auditor": of_record, "fine": fine}

    game["claim"] = None
    _advance_turn(game, claimant)


def _resolve_unaudited_truth(game: dict) -> None:
    claim = game["claim"]
    claimant = claim["player_id"]
    _credit(game, claimant, claim["margin"])
    game["pile"] = (game.get("pile") or []) + claim["cards"]
    who = _player(game, claimant)
    _log(game, f"{who['username']}'s {claim['count']}× {rank_name(claim['rank'])} "
               f"stood unaudited — margin returned.")
    game["reveal"] = {"kind": "unaudited", "claimant": claimant}
    game["claim"] = None
    _advance_turn(game, claimant)


def _open_choice(game: dict) -> None:
    """A lie survived the window: the claimant picks Conceal or Flash."""
    claim = game["claim"]
    game["pending_choice"] = {
        "player_id": claim["player_id"],
        "rank": claim["rank"],
        "count": claim["count"],
        "margin": claim["margin"],
        "cards": claim["cards"],
    }
    game["claim"] = None


def resolve_due(game: dict) -> bool:
    """
    Advance a claim whose window has run out. Returns True if anything changed.

    Called from the polling endpoint rather than a background task, so the game
    keeps moving even with nobody watching and never depends on a live timer.
    """
    claim = game.get("claim")
    if not claim:
        return False
    if _audit_pool(claim) >= claim["margin"]:
        _resolve_audited(game)
        return True
    if _claim_elapsed(game) < AUDIT_SECONDS:
        return False
    if _claim_is_true(claim):
        _resolve_unaudited_truth(game)
    else:
        _open_choice(game)
    return True


def _check_endgame(game: dict) -> None:
    """
    The hand ends the moment someone is empty-handed with nothing pending, or
    when only one solvent player is left.
    """
    if game.get("status") != "playing":
        return
    if game.get("claim") or game.get("pending_choice"):
        return

    solvent = _solvent(game)
    out = [p for p in solvent if not (p.get("hand") or [])]
    if not out and len(solvent) > 1:
        return

    _pay_liquidity_bonuses(game)
    game["status"] = "finished"
    game["finished_at"] = dt.datetime.utcnow()


def _pay_liquidity_bonuses(game: dict) -> None:
    """
    Bonuses by finishing order — fewest cards left first, tiers split on a tie.

    Bankrupt players are out of the money; they already lost everything.
    """
    solvent = _solvent(game)
    if not solvent:
        return

    by_cards: Dict[int, List[dict]] = {}
    for p in solvent:
        by_cards.setdefault(len(p.get("hand") or []), []).append(p)

    awarded = []
    for tier, count in enumerate(sorted(by_cards)):
        if tier >= len(LIQUIDITY_BONUSES):
            break
        group = by_cards[count]
        share = LIQUIDITY_BONUSES[tier] // len(group)
        for p in group:
            _credit(game, p["user_id"], share)
            awarded.append(f"{p['username']} +${share}")
    if awarded:
        _log(game, "Liquidity bonuses: " + ", ".join(awarded))


# ── Views ─────────────────────────────────────────────────────────────────────

def _public_player(game: dict, p: dict, viewer: str) -> Dict[str, Any]:
    row = {
        "user_id": p["user_id"],
        "username": p["username"],
        "chips": int(p["chips"]),
        "cards": len(p.get("hand") or []),
        "bankrupt": bool(p.get("bankrupt")),
        "is_turn": game.get("turn_id") == p["user_id"] and not game.get("claim"),
        "is_me": p["user_id"] == viewer,
    }
    return row


def _state_view(game: dict, game_id: str, user: User) -> Dict[str, Any]:
    uid = str(user.id)
    me = _player(game, uid)
    claim = game.get("claim")
    choice = game.get("pending_choice")

    out: Dict[str, Any] = {
        "game_id": game_id,
        "status": game.get("status"),
        "join_code": game.get("join_code", ""),
        "is_host": game.get("created_by") == uid or user.is_admin,
        "joined": me is not None,
        "players": [_public_player(game, p, uid) for p in _players(game)],
        "pile": len(game.get("pile") or []),
        "declared_rank": game.get("declared_rank"),
        "next_rank": (rank_name(next_rank(game["declared_rank"]))
                      if game.get("declared_rank") else None),
        "turn_id": game.get("turn_id"),
        "my_turn": game.get("turn_id") == uid and not claim and not choice,
        "log": (game.get("log") or [])[-12:],
        "reveal": game.get("reveal"),
        "starting_capital": STARTING_CAPITAL,
        "audit_seconds": AUDIT_SECONDS,
        "max_cards": MAX_CARDS_PER_PLAY,
    }

    # Your hand, and only ever yours.
    if me:
        out["hand"] = [{"rank": c["rank"], "suit": c["suit"], "label": card_label(c)}
                       for c in (me.get("hand") or [])]
        out["chips"] = int(me["chips"])
        out["bankrupt"] = bool(me.get("bankrupt"))

    if claim:
        pool = _audit_pool(claim)
        out["claim"] = {
            "player_id": claim["player_id"],
            "username": (_player(game, claim["player_id"]) or {}).get("username", ""),
            "rank": rank_name(claim["rank"]),
            "count": claim["count"],
            "margin": claim["margin"],
            "pool": pool,
            "needed": max(0, claim["margin"] - pool),
            "seconds_left": max(0, round(AUDIT_SECONDS - _claim_elapsed(game))),
            "my_stake": int((claim.get("audits") or {}).get(uid, 0)),
            "is_mine": claim["player_id"] == uid,
            # The cards themselves stay hidden until somebody pays to see them.
        }

    if choice:
        out["choice"] = {
            "player_id": choice["player_id"],
            "is_mine": choice["player_id"] == uid,
            "rank": rank_name(choice["rank"]),
            "count": choice["count"],
            "margin": choice["margin"],
            "conceal_bonus": CONCEAL_BONUS,
        }

    if game.get("status") == "finished":
        table = sorted(_players(game), key=lambda p: -int(p["chips"]))
        out["results"] = [{
            "user_id": p["user_id"], "username": p["username"],
            "chips": int(p["chips"]), "cards": len(p.get("hand") or []),
            "bankrupt": bool(p.get("bankrupt")),
            "rank": i + 1,
        } for i, p in enumerate(table)]
        out["feedback"] = (game.get("feedback") or {}).get(uid)

    return out


# ── Persistence helpers ───────────────────────────────────────────────────────

async def _load(game_id: str) -> Tuple[Any, dict]:
    ref = db_module.db.collection(COLLECTION).document(game_id)
    doc = await ref.get()
    if not doc.exists:
        raise HTTPException(404, "Table not found")
    return ref, (doc.to_dict() or {})


async def _save(ref, game: dict) -> None:
    await ref.set(game)


async def _record_results(game: dict, game_id: str) -> None:
    """Rate everyone on the capital they walked away with, and coach them."""
    if game.get("scored"):
        return
    game["scored"] = True
    feedback_by_user = {}
    for p in _players(game):
        coaching = fb.analyse("toxic_flow", {
            "chips": int(p["chips"]),
            "starting": STARTING_CAPITAL,
            "cards_left": len(p.get("hand") or []),
            "bankrupt": bool(p.get("bankrupt")),
            "players": len(_players(game)),
        })
        feedback_by_user[p["user_id"]] = coaching
        await scores.record_result(
            "toxic_flow", p["user_id"], p["username"], int(p["chips"]),
            game_id=game_id,
            detail={"cards_left": len(p.get("hand") or []),
                    "bankrupt": bool(p.get("bankrupt"))},
            feedback=coaching,
        )
    game["feedback"] = feedback_by_user


# ── Pages ─────────────────────────────────────────────────────────────────────

@router.get("", include_in_schema=False)
async def rules_page(request: Request):
    return templates.TemplateResponse("toxic_flow_rules.html", {
        "request": request,
        "app_name": "AlphaBook",
        "starting_capital": STARTING_CAPITAL,
        "audit_seconds": AUDIT_SECONDS,
        "min_players": MIN_PLAYERS,
        "max_players": MAX_PLAYERS,
        "max_cards": MAX_CARDS_PER_PLAY,
        "bonuses": LIQUIDITY_BONUSES,
        "conceal_bonus": CONCEAL_BONUS,
    })


@router.get("/game/{game_id}", include_in_schema=False)
async def game_page(game_id: str, request: Request):
    await _load(game_id)
    return templates.TemplateResponse("toxic_flow_game.html", {
        "request": request,
        "app_name": "AlphaBook",
        "game_id": game_id,
    })


# ── Lobby ─────────────────────────────────────────────────────────────────────

@router.post("/create")
async def create_game(user: User = Depends(current_user)):
    game_id = str(uuid.uuid4())
    game = {
        "join_code": generate_join_code(),
        "status": "lobby",
        "players": [{
            "user_id": str(user.id), "username": user.username,
            "chips": STARTING_CAPITAL, "hand": [], "bankrupt": False,
        }],
        "pile": [],
        "declared_rank": None,
        "turn_id": None,
        "claim": None,
        "pending_choice": None,
        "log": [],
        "reveal": None,
        "created_by": str(user.id),
        "host_name": user.username,
        "created_at": dt.datetime.utcnow(),
        "scored": False,
    }
    await db_module.db.collection(COLLECTION).document(game_id).set(game)
    return {"ok": True, "game_id": game_id, "join_code": game["join_code"]}


@router.post("/join")
async def join_game(req: JoinRequest, user: User = Depends(current_user)):
    code = (req.join_code or "").strip().upper()
    docs = await db_module.db.collection(COLLECTION) \
        .where("join_code", "==", code).where("status", "==", "lobby").limit(1).get()
    if not docs:
        raise HTTPException(404, "No table waiting on that code")

    doc = docs[0]
    game = doc.to_dict() or {}
    uid = str(user.id)

    if _player(game, uid):
        return {"ok": True, "game_id": doc.id}
    if len(_players(game)) >= MAX_PLAYERS:
        raise HTTPException(400, f"That table is full ({MAX_PLAYERS} seats)")

    game["players"].append({
        "user_id": uid, "username": user.username,
        "chips": STARTING_CAPITAL, "hand": [], "bankrupt": False,
    })
    await doc.reference.set(game)
    return {"ok": True, "game_id": doc.id}


@router.post("/game/{game_id}/start")
async def start_game(game_id: str, user: User = Depends(current_user)):
    ref, game = await _load(game_id)
    _require_host(game, user)
    if game["status"] != "lobby":
        return {"ok": True, "status": game["status"]}
    if len(_players(game)) < MIN_PLAYERS:
        raise HTTPException(400, f"Toxic Flow needs at least {MIN_PLAYERS} players")

    _deal(game["players"])
    game["status"] = "playing"
    game["started_at"] = dt.datetime.utcnow()
    game["turn_id"] = game["players"][0]["user_id"]
    _log(game, f"Deal complete — {len(_players(game))} players, "
               f"${STARTING_CAPITAL} each.")
    await _save(ref, game)
    return {"ok": True, "status": "playing"}


# ── Play ──────────────────────────────────────────────────────────────────────

@router.post("/game/{game_id}/play")
async def play(game_id: str, req: PlayRequest, user: User = Depends(current_user)):
    ref, game = await _load(game_id)
    resolve_due(game)

    uid = str(user.id)
    me = _player(game, uid)
    if game["status"] != "playing":
        raise HTTPException(400, "This table isn't running")
    if not me or me.get("bankrupt"):
        raise HTTPException(403, "You're not in this hand")
    if game.get("claim") or game.get("pending_choice"):
        raise HTTPException(400, "There's a claim on the table already")
    if game.get("turn_id") != uid:
        raise HTTPException(400, "It isn't your turn")

    idxs = sorted(set(req.cards))
    hand = me.get("hand") or []
    if not 1 <= len(idxs) <= MAX_CARDS_PER_PLAY:
        raise HTTPException(400, f"Play between 1 and {MAX_CARDS_PER_PLAY} cards")
    if any(i < 0 or i >= len(hand) for i in idxs):
        raise HTTPException(400, "You don't hold those cards")
    if req.rank not in RANKS:
        raise HTTPException(400, "That isn't a rank")

    # The sequence: a fresh pile is free, otherwise you must claim the next rank.
    if game.get("declared_rank") is not None:
        want = next_rank(game["declared_rank"])
        if req.rank != want:
            raise HTTPException(400, f"The sequence is on {rank_name(want)} — "
                                     f"claim that or nothing")

    margin = len(idxs) * rank_value(req.rank)
    if int(me["chips"]) < margin:
        raise HTTPException(400, f"That claim needs ${margin} of margin and you "
                                 f"hold ${int(me['chips'])}")

    cards = [hand[i] for i in idxs]
    me["hand"] = [c for i, c in enumerate(hand) if i not in set(idxs)]
    me["chips"] = int(me["chips"]) - margin

    game["claim"] = {
        "player_id": uid,
        "rank": req.rank,
        "count": len(cards),
        "margin": margin,
        "cards": cards,
        "opened_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "audits": {},
    }
    game["declared_rank"] = req.rank
    game["reveal"] = None
    _log(game, f"{me['username']} posts {len(cards)}× {rank_name(req.rank)} "
               f"for ${margin} margin.")

    await _save(ref, game)
    return {"ok": True}


@router.post("/game/{game_id}/audit")
async def audit(game_id: str, req: AuditRequest, user: User = Depends(current_user)):
    ref, game = await _load(game_id)
    if resolve_due(game):
        await _save(ref, game)
        raise HTTPException(400, "Too late — that claim has already resolved")

    uid = str(user.id)
    me = _player(game, uid)
    claim = game.get("claim")
    if not claim:
        raise HTTPException(400, "Nothing to audit")
    if not me or me.get("bankrupt"):
        raise HTTPException(403, "You're not in this hand")
    if claim["player_id"] == uid:
        raise HTTPException(400, "You can't audit yourself")

    amount = int(req.amount)
    if amount <= 0:
        raise HTTPException(400, "Put something up")
    if amount > int(me["chips"]):
        raise HTTPException(400, f"You only hold ${int(me['chips'])}")

    # Escrow the stake now, so a pool can't be promised and withdrawn.
    me["chips"] = int(me["chips"]) - amount
    audits = claim.get("audits") or {}
    audits[uid] = int(audits.get(uid, 0)) + amount
    claim["audits"] = audits
    _log(game, f"{me['username']} puts ${amount} up to audit.")

    resolved = False
    if _audit_pool(claim) >= claim["margin"]:
        _resolve_audited(game)
        _check_endgame(game)
        resolved = True
        if game["status"] == "finished":
            await _record_results(game, game_id)

    await _save(ref, game)
    return {"ok": True, "resolved": resolved}


@router.post("/game/{game_id}/choose")
async def choose(game_id: str, req: ChoiceRequest, user: User = Depends(current_user)):
    ref, game = await _load(game_id)
    choice = game.get("pending_choice")
    uid = str(user.id)
    if not choice:
        raise HTTPException(400, "Nothing to decide")
    if choice["player_id"] != uid:
        raise HTTPException(403, "Not your decision")

    me = _player(game, uid)
    shown = [card_label(c) for c in choice["cards"]]

    if req.action == "conceal":
        _credit(game, uid, choice["margin"] + CONCEAL_BONUS)
        game["pile"] = (game.get("pile") or []) + choice["cards"]
        _log(game, f"{me['username']} concealed the lie and skimmed ${CONCEAL_BONUS}.")
        game["reveal"] = {"kind": "conceal", "claimant": uid}

    elif req.action == "flash":
        target = _player(game, req.target_id or "")
        if not target or target["user_id"] == uid or target.get("bankrupt"):
            raise HTTPException(400, "Pick an opponent still in the hand")
        _credit(game, uid, choice["margin"])
        target["hand"] = (target.get("hand") or []) + choice["cards"]
        target["hand"].sort(key=lambda c: (c["rank"], c["suit"]))
        _log(game, f"{me['username']} flashed the lie ({' '.join(shown)}) — "
                   f"{target['username']} eats {len(shown)} cards.")
        game["reveal"] = {"kind": "flash", "claimant": uid, "cards": shown,
                          "target": target["user_id"]}
    else:
        raise HTTPException(400, "Choose conceal or flash")

    game["pending_choice"] = None
    _advance_turn(game, uid)
    _check_endgame(game)
    if game["status"] == "finished":
        await _record_results(game, game_id)

    await _save(ref, game)
    return {"ok": True}


@router.get("/game/{game_id}/state")
async def state(game_id: str, user: User = Depends(current_user)):
    """
    Polled by the table. Also the clock: a claim whose window has expired is
    resolved here, so play advances without a background task.
    """
    ref, game = await _load(game_id)

    changed = resolve_due(game)
    if changed:
        _check_endgame(game)
        if game["status"] == "finished":
            await _record_results(game, game_id)
        await _save(ref, game)

    return _state_view(game, game_id, user)
