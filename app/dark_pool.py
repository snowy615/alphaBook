"""
Dark Pool — a hidden-liquidity trading game with a calibration desk bolted on.
===============================================================================

Structurally this is a community-book betting game: two private tiles per
desk, five shared tiles revealed in three waves, a round of betting after
each reveal, best-five-of-seven wins the pot. Anyone who has played a
community-card game will recognise the shape. The part that is not a reskin
is the checkpoint: every time a desk actually faces a bet (not a free check),
the table stops and asks them to price the spot themselves — a win-probability
slider, a 90%-confidence range on that same number, and a chip EV guess for
calling — before their fold/call/raise buttons unlock. The server already
knows the exact equity (it can see every live hand), so it can grade the
guess immediately and keep a running tally across the session: average
estimation error, whether their confidence intervals actually captured the
truth, and whether their own EV number and their actual action ever
contradicted each other.

Terminology, deliberately not poker's:

* **Tiles**, not cards — 4 sectors (rates / equities / commods / fx) × 13
  strengths. Sector and strength combine exactly like suit and rank; only the
  names differ, and the hand tiers below are renamed to match.
* **Desks**, not players; a **print** is one dealt hand, not a "hand".
* **The Open / Revision / Close** replace flop / turn / river.
* No blinds or button. Every live desk posts a flat **listing fee** (ante) at
  the start of a print, and action always starts from seat 1 each street —
  there is no positional structure to exploit, which keeps the game about
  reading the board and the betting, not seat draw.
* **Desk limit**: a bet can never be raised past the shortest stack still live
  in the print. This is a simplified stand-in for side pots — nobody is ever
  asked to call more than they can cover, so there is only ever one pot, at
  the cost of a whale not being able to fully leverage a short stack. Real
  cash tables solve this with side pots; here that complexity would not have
  bought anything the calibration checkpoints care about.

Ground truth for a checkpoint is computed against opponents' *actual* dealt
tiles, run out over the unseen deck (exact enumeration when the remaining
board is small enough to be cheap, Monte Carlo otherwise) — the best
equity estimate the server can honestly produce, matching the spec's request
for "the game engine's exact calculated probability."
"""
from __future__ import annotations

import datetime as dt
import logging
import random
import string
import uuid
from collections import Counter
from itertools import combinations
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app import db as db_module
from app import feedback as fb
from app import scores
from app.auth import current_user
from app.models import User

log = logging.getLogger("uvicorn.error")

router = APIRouter(prefix="/dark-pool", tags=["dark-pool"])
BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

COLLECTION = "dark_pool_games"

# ── Rules constants ───────────────────────────────────────────────────────────
STARTING_CAPITAL = 500
ANTE = 10
TOTAL_PRINTS = 6
MIN_DESKS = 3
MAX_DESKS = 6
EQUITY_EXACT_LIMIT = 4000     # enumerate exactly if the remaining board space is this small
EQUITY_SIM_ITERS = 500        # otherwise Monte Carlo this many run-outs

STAGE_ORDER = ["preopen", "open", "revision", "close", "showdown"]
STAGE_REVEAL = {"preopen": 0, "open": 3, "revision": 1, "close": 1}   # tiles revealed on entry
STAGE_LABEL = {"preopen": "Pre-Open", "open": "The Open",
               "revision": "The Revision", "close": "The Close"}

SECTORS = ["rates", "equities", "commods", "fx"]
STRENGTHS = list(range(1, 14))     # 1 = low-anchor, behaves like an Ace (can be high)
FULL_DECK = [{"sector": s, "strength": r} for s in SECTORS for r in STRENGTHS]

STRENGTH_LABEL = {1: "A", 11: "J", 12: "Q", 13: "K"}
SECTOR_SYMBOL = {"rates": "◆R", "equities": "●E", "commods": "▲C", "fx": "■X"}

TIER_NAMES = {
    9: "Peak Convergence", 8: "Sector Run", 7: "Quad Signal", 6: "Cross Consensus",
    5: "Sector Lock", 4: "Trend Run", 3: "Triple Signal", 2: "Dual Consensus",
    1: "Consensus Pair", 0: "No Read",
}


def strength_label(v: int) -> str:
    return STRENGTH_LABEL.get(v, str(v))


def tile_label(t: Dict[str, Any]) -> str:
    return f"{strength_label(t['strength'])}{SECTOR_SYMBOL.get(t['sector'], '')}"


def generate_join_code() -> str:
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=6))


def _require_host(game: dict, user: User) -> None:
    if game.get("created_by") != str(user.id) and not user.is_admin:
        raise HTTPException(403, "Only the host of this desk can do that")


# ── Hand evaluation (best 5 of up to 7 tiles) ─────────────────────────────────

def _strength_val(v: int) -> int:
    return 14 if v == 1 else v


def evaluate_5(tiles: List[Dict[str, Any]]) -> Tuple[int, tuple]:
    """Score a 5-tile read. Returns (tier, tiebreaker tuple), higher is better."""
    vals = sorted([_strength_val(t["strength"]) for t in tiles], reverse=True)
    sectors = [t["sector"] for t in tiles]
    counts = Counter(vals)
    is_lock = len(set(sectors)) == 1     # "flush" equivalent

    unique_vals = sorted(set(vals))
    is_run = False
    run_high = 0
    if len(unique_vals) >= 5:
        for i in range(len(unique_vals) - 4):
            if unique_vals[i + 4] - unique_vals[i] == 4:
                is_run, run_high = True, unique_vals[i + 4]
    if {14, 2, 3, 4, 5}.issubset(set(vals)):     # the wheel
        is_run, run_high = True, 5

    by_count = sorted(counts.items(), key=lambda kv: (kv[1], kv[0]), reverse=True)
    ordered = tuple(v for v, _ in by_count)
    top_counts = sorted(counts.values(), reverse=True)

    if is_lock and is_run:
        return (9, (run_high,)) if run_high == 14 else (8, (run_high,))
    if top_counts[0] >= 4:
        return (7, ordered)
    if top_counts[0] == 3 and top_counts[1] >= 2:
        return (6, ordered)
    if is_lock:
        return (5, tuple(vals))
    if is_run:
        return (4, (run_high,))
    if top_counts[0] == 3:
        return (3, ordered)
    if top_counts[0] == 2 and top_counts[1] == 2:
        return (2, ordered)
    if top_counts[0] == 2:
        return (1, ordered)
    return (0, tuple(vals))


def best_read(tiles: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Best 5-tile read out of any number (2–7) of tiles."""
    if len(tiles) < 5:
        padded = tiles + [{"sector": "none", "strength": 0}] * (5 - len(tiles))
        score = evaluate_5(padded)
        return {"tier": score[0], "tier_name": TIER_NAMES.get(score[0], "No Read"), "score": score}
    best = None
    for combo in combinations(tiles, 5):
        score = evaluate_5(list(combo))
        if best is None or score > best:
            best = score
    return {"tier": best[0], "tier_name": TIER_NAMES.get(best[0], "No Read"), "score": best}


def _tkey(t: Dict[str, Any]) -> tuple:
    return (t["sector"], t["strength"])


# ── Equity: this desk's true win/tie share against known opponent tiles ──────

def compute_equity(my_tiles: List[Dict[str, Any]],
                    opp_tiles_list: List[List[Dict[str, Any]]],
                    public: List[Dict[str, Any]],
                    unseen: List[Dict[str, Any]],
                    remaining: int) -> float:
    """
    P(this desk wins or splits) if the print ran to showdown from here, against
    the *actual* tiles the still-live opponents hold. Exact enumeration when
    the remaining-board space is small, Monte Carlo otherwise.
    """
    if remaining <= 0:
        return _showdown_share(my_tiles, opp_tiles_list, public)

    from math import comb
    space = comb(len(unseen), remaining) if len(unseen) >= remaining else 0

    if 0 < space <= EQUITY_EXACT_LIMIT:
        total = 0.0
        n = 0
        for combo in combinations(unseen, remaining):
            board = public + list(combo)
            total += _showdown_share(my_tiles, opp_tiles_list, board)
            n += 1
        return total / n if n else 0.0

    n = min(EQUITY_SIM_ITERS, max(1, space) if space else EQUITY_SIM_ITERS)
    total = 0.0
    pool = list(unseen)
    for _ in range(n):
        draw = random.sample(pool, remaining)
        board = public + draw
        total += _showdown_share(my_tiles, opp_tiles_list, board)
    return total / n


def _showdown_share(my_tiles, opp_tiles_list, board) -> float:
    my_score = best_read(my_tiles + board)["score"]
    opp_scores = [best_read(o + board)["score"] for o in opp_tiles_list]
    best_opp = max(opp_scores) if opp_scores else (-1, ())
    if my_score > best_opp:
        return 1.0
    if my_score < best_opp:
        return 0.0
    winners = 1 + sum(1 for s in opp_scores if s == my_score)
    return 1.0 / winners


# ── Request schemas ───────────────────────────────────────────────────────────
class JoinRequest(BaseModel):
    join_code: str


class CheckpointRequest(BaseModel):
    est_prob: float          # 0-100
    ci_low: float             # 0-100
    ci_high: float            # 0-100
    est_ev: float             # chips


class ActRequest(BaseModel):
    action: str               # "fold" | "check" | "call" | "raise"
    amount: Optional[int] = None   # for "raise": the new total stage bet


# ── Helpers over the game document ────────────────────────────────────────────

def _desks(game: dict) -> List[dict]:
    return game.get("desks") or []


def _desk(game: dict, uid: str) -> Optional[dict]:
    return next((d for d in _desks(game) if d["user_id"] == uid), None)


def _live_desks(game: dict) -> List[dict]:
    """Desks with chips left, eligible to play the next print."""
    return [d for d in _desks(game) if not d.get("bankrupt")]


def _log(game: dict, text: str) -> None:
    entries = game.get("log") or []
    entries.append({"t": dt.datetime.utcnow().isoformat(timespec="seconds"), "text": text})
    game["log"] = entries[-50:]


def _active(hand: dict) -> List[str]:
    """Desks still live in this print (not folded)."""
    return [uid for uid in hand["order"] if uid not in hand["folded"]]


def _to_act(hand: dict) -> List[str]:
    """Active desks that can still take an action this street (excludes all-in)."""
    return [uid for uid in _active(hand) if uid not in hand["all_in"]]


def _desk_limit(game: dict, hand: dict) -> int:
    """The shortest total (stack + already committed this street) among active desks."""
    totals = []
    for uid in _active(hand):
        d = _desk(game, uid)
        totals.append(int(d["chips"]) + int(hand["committed"].get(uid, 0)))
    return min(totals) if totals else 0


def _pot_total(hand: dict) -> int:
    return int(hand.get("pot", 0)) + sum(hand.get("committed", {}).values())


# ── Starting a print ──────────────────────────────────────────────────────────

def _start_print(game: dict) -> None:
    live = _live_desks(game)
    order = [d["user_id"] for d in live]

    deck = list(FULL_DECK)
    random.shuffle(deck)

    hole: Dict[str, List[dict]] = {}
    for uid in order:
        hole[uid] = [deck.pop(), deck.pop()]

    pot = 0
    for uid in order:
        d = _desk(game, uid)
        pay = min(ANTE, int(d["chips"]))
        d["chips"] -= pay
        pot += pay

    game["print_no"] = int(game.get("print_no", 0)) + 1
    game["hand"] = {
        "print_no": game["print_no"],
        "deck": deck,
        "hole": hole,
        "public": [],
        "stage": "preopen",
        "pot": pot,
        "current_bet": 0,
        "min_raise": ANTE,
        "committed": {uid: 0 for uid in order},
        "folded": [],
        "all_in": [uid for uid in order if _desk(game, uid)["chips"] <= 0],
        "acted": [],
        "order": order,
        "turn_id": order[0],
        "decision_no": 0,
        "pending_checkpoint": None,
        "checkpoint_log": [],
        "reveal": None,
        "winners": None,
        "turn_id": None,
    }
    _log(game, f"Print {game['print_no']} — {len(order)} desks post ${ANTE}, pot ${pot}.")
    _seat_first_actor(game["hand"])
    if game["hand"]["turn_id"] is None:
        _advance_to_actionable(game)   # nobody can act (all posted their whole stack) — run it out
    else:
        _maybe_open_checkpoint(game)


def _seat_first_actor(hand: dict) -> None:
    """Set turn_id to the first eligible actor in seat order, or None if nobody can act."""
    to_act = _to_act(hand)
    for uid in hand["order"]:
        if uid in to_act:
            hand["turn_id"] = uid
            return
    hand["turn_id"] = None


def _advance_turn(hand: dict) -> None:
    """Move the turn on from whoever just acted to the next eligible actor."""
    to_act = _to_act(hand)
    if not to_act:
        hand["turn_id"] = None
        return
    ids = hand["order"]
    idx = ids.index(hand["turn_id"]) if hand.get("turn_id") in ids else -1
    n = len(ids)
    for step in range(1, n + 1):
        nxt = ids[(idx + step) % n]
        if nxt in to_act:
            hand["turn_id"] = nxt
            return
    hand["turn_id"] = to_act[0]


def _stage_settled(hand: dict) -> bool:
    """Everyone still able to act has matched the current bet since the last raise."""
    to_act = _to_act(hand)
    if len(_active(hand)) <= 1:
        return True
    if not to_act:
        return True
    for uid in to_act:
        if uid not in hand["acted"]:
            return False
        if hand["committed"].get(uid, 0) != hand["current_bet"]:
            return False
    return True


def _advance_to_actionable(game: dict) -> None:
    """
    Push the print forward through settled streets / showdown until either a
    real decision is waiting on someone, or the print is over.
    """
    hand = game["hand"]
    while True:
        if len(_active(hand)) <= 1:
            _end_print_by_fold(game)
            return
        if not _stage_settled(hand):
            _advance_turn(hand)
            return

        idx = STAGE_ORDER.index(hand["stage"])
        if hand["stage"] == "close":
            _showdown(game)
            return

        next_stage = STAGE_ORDER[idx + 1]
        n_reveal = STAGE_REVEAL[next_stage]
        revealed = [hand["deck"].pop() for _ in range(n_reveal) if hand["deck"]]
        hand["public"].extend(revealed)
        hand["pot"] = _pot_total(hand)
        hand["committed"] = {uid: 0 for uid in hand["order"]}
        hand["current_bet"] = 0
        hand["min_raise"] = ANTE
        hand["acted"] = []
        hand["stage"] = next_stage

        if not _to_act(hand):
            continue   # everyone left is all-in — run it out with no more betting
        _seat_first_actor(hand)
        return


def _end_print_by_fold(game: dict) -> None:
    hand = game["hand"]
    hand["pot"] = _pot_total(hand)
    survivor = _active(hand)[0]
    d = _desk(game, survivor)
    d["chips"] += hand["pot"]
    hand["stage"] = "showdown"
    hand["winners"] = [survivor]
    hand["turn_id"] = None
    hand["reveal"] = {"kind": "fold", "winners": [survivor], "awarded": {survivor: hand["pot"]},
                       "reads": {}}
    _log(game, f"Everyone else folds — {d['username']} takes ${hand['pot']}.")
    _record_print_history(game, awarded={survivor: hand["pot"]}, reads=None)


def _showdown(game: dict) -> None:
    hand = game["hand"]
    hand["pot"] = _pot_total(hand)
    live = _active(hand)
    reads = {uid: best_read(hand["hole"][uid] + hand["public"]) for uid in live}
    best_score = max(r["score"] for r in reads.values())
    winners = [uid for uid in live if reads[uid]["score"] == best_score]
    share = hand["pot"] // len(winners)
    remainder = hand["pot"] - share * len(winners)
    awarded = {}
    for i, uid in enumerate(winners):
        amt = share + (1 if i < remainder else 0)
        _desk(game, uid)["chips"] += amt
        awarded[uid] = amt

    hand["stage"] = "showdown"
    hand["winners"] = winners
    hand["turn_id"] = None
    hand["reveal"] = {
        "kind": "showdown", "winners": winners, "awarded": awarded,
        "reads": {uid: {"tier_name": r["tier_name"], "tiles": [tile_label(t) for t in hand["hole"][uid]]}
                  for uid, r in reads.items()},
    }
    names = ", ".join(f"{_desk(game, uid)['username']} ({reads[uid]['tier_name']})" for uid in winners)
    _log(game, f"Showdown — {names} split ${hand['pot']}.")
    _record_print_history(game, awarded=awarded, reads=reads)


def _record_print_history(game: dict, awarded: Dict[str, int],
                           reads: Optional[Dict[str, dict]]) -> None:
    hand = game["hand"]
    history = game.get("print_history") or []
    history.append({
        "print_no": hand["print_no"],
        "public": [tile_label(t) for t in hand["public"]],
        "awarded": awarded,
        "reads": {uid: r["tier_name"] for uid, r in (reads or {}).items()},
        "pot": hand["pot"],
    })
    game["print_history"] = history
    for d in _desks(game):
        if d["chips"] <= 0:
            d["bankrupt"] = True


# ── Checkpoint: create, score, gate the action behind it ─────────────────────

def _maybe_open_checkpoint(game: dict) -> None:
    """If the player to act is facing a real bet, stage a checkpoint for them."""
    hand = game["hand"]
    uid = hand.get("turn_id")
    if not uid or hand.get("pending_checkpoint"):
        return
    to_call = hand["current_bet"] - hand["committed"].get(uid, 0)
    if to_call <= 0:
        return   # a free check needs no read

    opp_ids = [o for o in _active(hand) if o != uid]
    my_tiles = hand["hole"][uid]
    opp_tiles = [hand["hole"][o] for o in opp_ids]
    seen = {_tkey(t) for t in my_tiles + hand["public"]}
    for o in opp_tiles:
        seen |= {_tkey(t) for t in o}
    unseen = [t for t in FULL_DECK if _tkey(t) not in seen]
    remaining = 5 - len(hand["public"])

    true_prob = compute_equity(my_tiles, opp_tiles, hand["public"], unseen, remaining)
    pot_before = hand["pot"] + sum(hand["committed"].values())
    true_ev_call = true_prob * (pot_before + to_call) - (1 - true_prob) * to_call

    hand["decision_no"] += 1
    hand["pending_checkpoint"] = {
        "decision_no": hand["decision_no"],
        "user_id": uid,
        "stage": hand["stage"],
        "to_call": to_call,
        "pot_before": pot_before,
        "breakeven_pct": round(100.0 * to_call / (pot_before + to_call), 1) if (pot_before + to_call) else 0.0,
        "true_prob": true_prob,
        "true_ev_call": true_ev_call,
        "submitted": False,
    }


def _score_checkpoint(cp: dict) -> dict:
    true_pct = cp["true_prob"] * 100.0
    prob_error = abs(cp["est_prob"] - true_pct)
    lo, hi = min(cp["ci_low"], cp["ci_high"]), max(cp["ci_low"], cp["ci_high"])
    ci_width = hi - lo
    ci_hit = lo <= true_pct <= hi
    if ci_hit:
        cal_points = max(0.0, 100.0 - ci_width * 0.6)
    else:
        miss_by = (lo - true_pct) if true_pct < lo else (true_pct - hi)
        cal_points = max(0.0, 40.0 - miss_by * 1.5)
    ev_error = abs(cp["est_ev"] - cp["true_ev_call"])
    return {
        "prob_error": round(prob_error, 1),
        "ci_hit": ci_hit,
        "ci_width": round(ci_width, 1),
        "cal_points": round(cal_points, 1),
        "ev_error": round(ev_error, 1),
        "implied_action": "fold" if cp["est_ev"] < 0 else "call",
    }


# ── Views ─────────────────────────────────────────────────────────────────────

def _public_desk(game: dict, d: dict, hand: Optional[dict], viewer: str) -> Dict[str, Any]:
    row = {
        "user_id": d["user_id"], "username": d["username"], "chips": int(d["chips"]),
        "bankrupt": bool(d.get("bankrupt")), "is_me": d["user_id"] == viewer,
    }
    if hand:
        row["in_print"] = d["user_id"] in hand["order"]
        row["folded"] = d["user_id"] in hand.get("folded", [])
        row["all_in"] = d["user_id"] in hand.get("all_in", [])
        row["committed"] = hand.get("committed", {}).get(d["user_id"], 0)
        row["is_turn"] = hand.get("turn_id") == d["user_id"]
        if hand["stage"] == "showdown" and d["user_id"] in hand["order"] \
                and (d["user_id"] not in hand.get("folded", []) or len(hand["order"]) == 1):
            row["tiles"] = [tile_label(t) for t in hand["hole"].get(d["user_id"], [])]
    return row


def _state_view(game: dict, game_id: str, user: User) -> Dict[str, Any]:
    uid = str(user.id)
    me = _desk(game, uid)
    hand = game.get("hand")

    out: Dict[str, Any] = {
        "game_id": game_id,
        "status": game.get("status"),
        "join_code": game.get("join_code", ""),
        "is_host": game.get("created_by") == uid or user.is_admin,
        "joined": me is not None,
        "desks": [_public_desk(game, d, hand, uid) for d in _desks(game)],
        "print_no": game.get("print_no", 0),
        "total_prints": TOTAL_PRINTS,
        "ante": ANTE,
        "starting_capital": STARTING_CAPITAL,
        "min_desks": MIN_DESKS,
        "max_desks": MAX_DESKS,
        "log": (game.get("log") or [])[-14:],
    }

    if me:
        out["chips"] = int(me["chips"])
        out["bankrupt"] = bool(me.get("bankrupt"))

    if hand:
        my_in_print = uid in hand["order"]
        out["hand"] = {
            "stage": hand["stage"],
            "stage_label": STAGE_LABEL.get(hand["stage"], hand["stage"]),
            "public": [tile_label(t) for t in hand["public"]],
            "pot": _pot_total(hand),
            "current_bet": hand["current_bet"],
            "desk_limit": _desk_limit(game, hand),
            "min_raise": hand["min_raise"],
            "turn_id": hand.get("turn_id"),
            "my_turn": hand.get("turn_id") == uid,
            "reveal": hand.get("reveal"),
            "winners": hand.get("winners"),
        }
        if my_in_print:
            out["hand"]["my_tiles"] = [tile_label(t) for t in hand["hole"][uid]]
            out["hand"]["my_committed"] = hand["committed"].get(uid, 0)
            out["hand"]["my_folded"] = uid in hand["folded"]

        cp = hand.get("pending_checkpoint")
        if cp:
            if cp["user_id"] == uid and not cp["submitted"]:
                # The read is hidden until they answer — that's the whole point.
                out["checkpoint"] = {
                    "decision_no": cp["decision_no"], "stage": cp["stage"],
                    "to_call": cp["to_call"], "pot_before": cp["pot_before"],
                    "breakeven_pct": cp["breakeven_pct"], "needs_submit": True,
                }
            elif cp["user_id"] == uid and cp["submitted"]:
                out["checkpoint"] = {
                    "decision_no": cp["decision_no"], "needs_submit": False,
                    "result": cp.get("result"),
                }
            else:
                out["checkpoint"] = {"waiting_on": cp["user_id"], "needs_submit": False}

    if game.get("status") == "finished":
        table = sorted(_desks(game), key=lambda d: -int(d["chips"]))
        out["results"] = [{
            "user_id": d["user_id"], "username": d["username"], "chips": int(d["chips"]),
            "bankrupt": bool(d.get("bankrupt")), "rank": i + 1,
        } for i, d in enumerate(table)]
        out["feedback"] = (game.get("feedback") or {}).get(uid)
        out["assessment"] = (game.get("assessment") or {}).get(uid)

    return out


# ── Persistence helpers ───────────────────────────────────────────────────────

async def _load(game_id: str) -> Tuple[Any, dict]:
    ref = db_module.db.collection(COLLECTION).document(game_id)
    doc = await ref.get()
    if not doc.exists:
        raise HTTPException(404, "Desk not found")
    return ref, (doc.to_dict() or {})


async def _save(ref, game: dict) -> None:
    await ref.set(game)


# ── Session-end scoring ───────────────────────────────────────────────────────

def _followups(entries: List[dict]) -> List[str]:
    """Templated interview prompts, aimed at the biggest calibration misses."""
    narrow_misses = [e for e in entries if not e["ci_hit"] and e["ci_width"] <= 30]
    pool = narrow_misses or [e for e in entries if not e["ci_hit"]]
    pool = sorted(pool, key=lambda e: -e["prob_error"])[:2]
    out = []
    for e in pool:
        out.append(
            f"On print #{e['print_no']} at {STAGE_LABEL.get(e['stage'], e['stage'])}, you set a "
            f"{e['ci_low']:.0f}–{e['ci_high']:.0f}% range for being ahead, but the true odds "
            f"were {e['true_prob'] * 100:.0f}%. Walk through how you got to that range."
        )
    breaks = [e for e in entries if e["rationality_break"]]
    if breaks:
        e = breaks[0]
        out.append(
            f"On print #{e['print_no']} at {STAGE_LABEL.get(e['stage'], e['stage'])}, your own EV "
            f"slider said calling was -{abs(e['est_ev']):.0f} chips on average, but you "
            f"{e['action']}d anyway. What made you go against your own number?"
        )
    return out[:3]


async def _finish_session(game: dict, game_id: str) -> None:
    if game.get("scored"):
        return
    game["scored"] = True
    session_log = game.get("session_log") or []
    by_user: Dict[str, List[dict]] = {}
    for e in session_log:
        by_user.setdefault(e["user_id"], []).append(e)

    feedback_by_user, assessment_by_user = {}, {}
    for d in _desks(game):
        uid = d["user_id"]
        entries = by_user.get(uid, [])
        n = len(entries)
        avg_prob_error = sum(e["prob_error"] for e in entries) / n if n else 0.0
        avg_ev_error = sum(e["ev_error"] for e in entries) / n if n else 0.0
        calibration_score = sum(e["cal_points"] for e in entries) / n if n else 0.0
        breaks = sum(1 for e in entries if e["rationality_break"])
        rationality_index = 100.0 * (1 - breaks / n) if n else 100.0

        coaching = fb.analyse("dark_pool", {
            "chips": int(d["chips"]), "starting": STARTING_CAPITAL,
            "bankrupt": bool(d.get("bankrupt")), "decisions": n,
            "avg_prob_error": avg_prob_error, "calibration_score": calibration_score,
            "rationality_index": rationality_index, "avg_ev_error": avg_ev_error,
        })
        feedback_by_user[uid] = coaching
        assessment_by_user[uid] = {
            "decisions": n,
            "avg_prob_error": round(avg_prob_error, 1),
            "avg_ev_error": round(avg_ev_error, 1),
            "calibration_score": round(calibration_score, 1),
            "rationality_index": round(rationality_index, 1),
            "rationality_breaks": breaks,
            "followups": _followups(entries),
        }
        await scores.record_result(
            "dark_pool", uid, d["username"], int(d["chips"]),
            game_id=game_id,
            detail={"decisions": n, "calibration_score": round(calibration_score, 1)},
            feedback=coaching,
        )

    game["feedback"] = feedback_by_user
    game["assessment"] = assessment_by_user


def _maybe_advance_print(game: dict) -> None:
    """Between prints: deal the next one, or close the session out."""
    live = _live_desks(game)
    if len(live) < 2 or game.get("print_no", 0) >= TOTAL_PRINTS:
        game["status"] = "finished"
        game["finished_at"] = dt.datetime.utcnow()
        return
    _start_print(game)


# ── Pages ─────────────────────────────────────────────────────────────────────

@router.get("", include_in_schema=False)
async def rules_page(request: Request):
    return templates.TemplateResponse("dark_pool_rules.html", {
        "request": request, "app_name": "AlphaBook",
        "starting_capital": STARTING_CAPITAL, "ante": ANTE, "total_prints": TOTAL_PRINTS,
        "min_desks": MIN_DESKS, "max_desks": MAX_DESKS,
    })


@router.get("/game/{game_id}", include_in_schema=False)
async def game_page(game_id: str, request: Request):
    await _load(game_id)
    return templates.TemplateResponse("dark_pool_game.html", {
        "request": request, "app_name": "AlphaBook", "game_id": game_id,
    })


# ── Lobby ─────────────────────────────────────────────────────────────────────

@router.post("/create")
async def create_game(user: User = Depends(current_user)):
    game_id = str(uuid.uuid4())
    game = {
        "join_code": generate_join_code(),
        "status": "lobby",
        "desks": [{"user_id": str(user.id), "username": user.username,
                   "chips": STARTING_CAPITAL, "bankrupt": False}],
        "print_no": 0,
        "hand": None,
        "log": [],
        "session_log": [],
        "print_history": [],
        "created_by": str(user.id),
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
        raise HTTPException(404, "No desk waiting on that code")

    doc = docs[0]
    game = doc.to_dict() or {}
    uid = str(user.id)

    if _desk(game, uid):
        return {"ok": True, "game_id": doc.id}
    if len(_desks(game)) >= MAX_DESKS:
        raise HTTPException(400, f"That desk is full ({MAX_DESKS} seats)")

    game["desks"].append({"user_id": uid, "username": user.username,
                           "chips": STARTING_CAPITAL, "bankrupt": False})
    await doc.reference.set(game)
    return {"ok": True, "game_id": doc.id}


@router.post("/game/{game_id}/start")
async def start_game(game_id: str, user: User = Depends(current_user)):
    ref, game = await _load(game_id)
    _require_host(game, user)
    if game["status"] != "lobby":
        return {"ok": True, "status": game["status"]}
    if len(_desks(game)) < MIN_DESKS:
        raise HTTPException(400, f"Dark Pool needs at least {MIN_DESKS} desks")

    game["status"] = "playing"
    game["started_at"] = dt.datetime.utcnow()
    _start_print(game)
    await _save(ref, game)
    return {"ok": True, "status": "playing"}


# ── Play ──────────────────────────────────────────────────────────────────────

@router.post("/game/{game_id}/checkpoint")
async def submit_checkpoint(game_id: str, req: CheckpointRequest, user: User = Depends(current_user)):
    ref, game = await _load(game_id)
    hand = game.get("hand")
    uid = str(user.id)
    if not hand:
        raise HTTPException(400, "No print in progress")
    cp = hand.get("pending_checkpoint")
    if not cp or cp["user_id"] != uid:
        raise HTTPException(400, "Nothing is waiting on your read right now")
    if cp["submitted"]:
        raise HTTPException(400, "Already submitted for this decision")

    lo, hi = min(req.ci_low, req.ci_high), max(req.ci_low, req.ci_high)
    if not (0 <= req.est_prob <= 100 and 0 <= lo <= 100 and 0 <= hi <= 100):
        raise HTTPException(400, "Estimates must be between 0 and 100")

    cp.update({"est_prob": req.est_prob, "ci_low": lo, "ci_high": hi, "est_ev": req.est_ev})
    result = _score_checkpoint(cp)
    cp["submitted"] = True
    cp["result"] = {**result, "true_prob_pct": round(cp["true_prob"] * 100, 1),
                    "true_ev_call": round(cp["true_ev_call"], 1)}

    await _save(ref, game)
    return {"ok": True, "result": cp["result"]}


@router.post("/game/{game_id}/act")
async def act(game_id: str, req: ActRequest, user: User = Depends(current_user)):
    ref, game = await _load(game_id)
    hand = game.get("hand")
    uid = str(user.id)
    if not hand or hand["stage"] == "showdown":
        raise HTTPException(400, "No decision pending")
    if hand.get("turn_id") != uid:
        raise HTTPException(400, "It isn't your turn")

    cp = hand.get("pending_checkpoint")
    to_call = hand["current_bet"] - hand["committed"].get(uid, 0)
    if to_call > 0 and (not cp or cp["user_id"] != uid or not cp["submitted"]):
        raise HTTPException(400, "Submit your read before acting")

    d = _desk(game, uid)
    action = req.action

    if action == "fold":
        hand["folded"].append(uid)
        hand["acted"].append(uid)
        _log(game, f"{d['username']} folds.")

    elif action == "check":
        if to_call > 0:
            raise HTTPException(400, "There's a bet to you — call, raise, or fold")
        hand["acted"].append(uid)
        _log(game, f"{d['username']} checks.")

    elif action == "call":
        if to_call <= 0:
            raise HTTPException(400, "Nothing to call — check instead")
        pay = min(to_call, int(d["chips"]))
        d["chips"] -= pay
        hand["committed"][uid] = hand["committed"].get(uid, 0) + pay
        if d["chips"] <= 0:
            hand["all_in"].append(uid)
        hand["acted"].append(uid)
        _log(game, f"{d['username']} calls ${pay}.")

    elif action == "raise":
        limit = _desk_limit(game, hand)
        new_total = req.amount
        if new_total is None:
            raise HTTPException(400, "Specify the new total bet")
        min_legal = hand["current_bet"] + max(hand["min_raise"], ANTE)
        if new_total > limit:
            raise HTTPException(400, f"The desk limit caps the bet at ${limit} this print")
        if new_total < min(min_legal, limit):
            raise HTTPException(400, f"Minimum raise is to ${min(min_legal, limit)}")
        already = hand["committed"].get(uid, 0)
        pay = new_total - already
        if pay > int(d["chips"]):
            raise HTTPException(400, "You don't have that many chips")
        d["chips"] -= pay
        hand["committed"][uid] = new_total
        hand["min_raise"] = new_total - hand["current_bet"]
        hand["current_bet"] = new_total
        if d["chips"] <= 0:
            hand["all_in"].append(uid)
        hand["acted"] = [uid]
        _log(game, f"{d['username']} raises to ${new_total}.")

    else:
        raise HTTPException(400, "Unknown action")

    if cp and cp["user_id"] == uid and cp["submitted"]:
        entry = {**cp, "action": action, "user_id": uid, "print_no": hand["print_no"],
                  "rationality_break": cp["result"]["implied_action"] == "fold" and action in ("call", "raise")}
        hand["checkpoint_log"].append(entry)
        game.setdefault("session_log", []).append({
            **cp["result"], "action": action, "user_id": uid, "print_no": hand["print_no"],
            "stage": cp["stage"], "true_prob": cp["true_prob"], "est_ev": cp["est_ev"],
            "ci_low": cp["ci_low"], "ci_high": cp["ci_high"],
            "rationality_break": entry["rationality_break"],
        })
    hand["pending_checkpoint"] = None

    _advance_to_actionable(game)
    if game["hand"]["stage"] != "showdown":
        _maybe_open_checkpoint(game)
    # A finished print stays visible (board, reveal, winners) until the host
    # deals the next one via /next-print — nobody should have the showdown
    # snapped away from under them the instant it resolves.

    await _save(ref, game)
    return {"ok": True}


@router.post("/game/{game_id}/next-print")
async def next_print(game_id: str, user: User = Depends(current_user)):
    """Host manually advances past a finished print's recap."""
    ref, game = await _load(game_id)
    _require_host(game, user)
    hand = game.get("hand")
    if not hand or hand["stage"] != "showdown":
        raise HTTPException(400, "The current print isn't finished yet")
    _maybe_advance_print(game)
    if game["status"] == "finished":
        await _finish_session(game, game_id)
    await _save(ref, game)
    return {"ok": True, "status": game["status"]}


@router.get("/game/{game_id}/state")
async def state(game_id: str, user: User = Depends(current_user)):
    ref, game = await _load(game_id)
    return _state_view(game, game_id, user)
