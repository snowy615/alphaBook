"""
Lone Bid — the lowest number nobody else played wins.
=====================================================

Seven seats, eighteen rounds, cards numbered 1 to 8.

Each round:

1. **Reveal two.** Everyone secretly picks two cards from their hand; the
   pairs are shown together.
2. **Play one.** Having seen every pair, everyone secretly plays one of their
   two.
3. **Resolve.** Every number played more than once cancels out. The lowest
   number left wins: its player scores its face value and a token. The card
   you played is gone for the rest of the set; the one you showed but kept is
   locked for the next round.

Hands come back in full after rounds 6 and 12 (three sets of six rounds). At
the end of rounds 3, 6, 9 and 12 the top scorer survives and leaves the table,
and everyone left drops back to zero points but keeps their tokens. Ties on
points go to whoever has more tokens. That leaves three for rounds 13–18: the
top scorer wins the prize, second survives, third is eliminated.

House readings, where the rules leave room (shown on the rules page):

* A tie on points *and* tokens is settled by a random draw.
* A locked card is cleared when hands are reset, so each set starts clean.
* Anyone who hasn't chosen when the clock runs out gets a random legal choice.

Every round's choices are secret until the phase closes, so the server holds
them and the state endpoint shows only your own; what is left in each hand is
public, because every card played is shown to the table anyway.

The clock is resolved on read, as in the other timed modes here: whichever
request arrives after a deadline moves the game on, so nothing depends on a
background timer. The app runs on one instance, so a lock per table keeps
simultaneous picks from overwriting each other.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import itertools
import logging
import math
import random
import string
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from app import db as db_module
from app import feedback as fb
from app import scores
from app.auth import current_user
from app.models import User

log = logging.getLogger("uvicorn.error")

router = APIRouter(prefix="/lone-bid", tags=["lone-bid"])
BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

COLLECTION = "lone_bid_games"
MODE = "lone_bid"

# ── Rules ────────────────────────────────────────────────────────────────────
SEATS = 7
CARDS = tuple(range(1, 9))
ROUNDS = 18
SET_LENGTH = 6
CHECKPOINTS = (3, 6, 9, 12)            # top scorer survives and leaves
RESETS = (6, 12)                       # full hands back after these rounds

REVEAL_SECONDS = 40
PLAY_SECONDS = 25
RESULT_SECONDS = 7
CHECKPOINT_SECONDS = 11

BOT_LEVELS = ("noob", "normal", "good", "cracked")
BOT_NAMES = ("Ada", "Boris", "Cleo", "Dmitri", "Esme", "Felix", "Gia")
TOKEN_WEIGHT = 1.5                     # what a bot thinks a token is worth, in points

# Final standing → leaderboard value. Second in the final survives just as a
# checkpoint survivor does, so they count the same.
OUTCOME_VALUE = {"winner": 3, "survived": 2, "runner_up": 2, "eliminated": 0}
OUTCOME_LABEL = {"winner": "Won the prize", "survived": "Survived", "runner_up": "Survived the final",
                 "eliminated": "Eliminated"}


def generate_join_code() -> str:
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=6))


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _parse(ts: Any) -> Optional[dt.datetime]:
    if not ts:
        return None
    if isinstance(ts, str):
        ts = dt.datetime.fromisoformat(ts)
    return ts if ts.tzinfo else ts.replace(tzinfo=dt.timezone.utc)


def set_of(round_no: int) -> int:
    return (max(1, round_no) - 1) // SET_LENGTH + 1


def next_checkpoint(round_no: int) -> Optional[int]:
    return next((c for c in CHECKPOINTS if c >= round_no), None)


# ── Players ──────────────────────────────────────────────────────────────────

def new_player(uid: str, username: str, bot_skill: Optional[str] = None) -> Dict[str, Any]:
    p = {"user_id": uid, "username": username, "is_bot": bot_skill is not None,
         "hand": list(CARDS), "locked": None, "score": 0, "tokens": 0, "status": "active",
         "out_round": None, "rounds_played": 0, "rounds_won": 0, "points_total": 0,
         "cancelled": 0, "best_win": 0}
    if bot_skill is not None:
        p["bot_skill"] = bot_skill
    return p


def players(game: dict) -> List[dict]:
    return game.get("players") or []


def player(game: dict, uid: str) -> Optional[dict]:
    return next((p for p in players(game) if p["user_id"] == uid), None)


def active(game: dict) -> List[dict]:
    return [p for p in players(game) if p.get("status") == "active"]


def available(p: dict) -> List[int]:
    """What this player may reveal this round: their hand less the locked card."""
    return sorted(c for c in p.get("hand") or [] if c != p.get("locked"))


def fill_bots(game: dict, level: str) -> None:
    """Fill every empty seat with a house bot of the chosen level."""
    taken = {p["username"] for p in players(game)}
    names = [n for n in BOT_NAMES if n not in taken]
    i = 0
    while len(players(game)) < SEATS:
        name = names[i] if i < len(names) else f"Bot {i + 1}"
        game["players"].append(new_player(f"bot:{i}", f"{name} · {level}", bot_skill=level))
        i += 1


def _log(game: dict, text: str) -> None:
    entries = game.get("log") or []
    entries.append({"t": _now().isoformat(timespec="seconds"), "round": game.get("round"), "text": text})
    game["log"] = entries[-60:]


# ── Who wins a round ─────────────────────────────────────────────────────────

def winning_card(cards: Sequence[int]) -> Optional[int]:
    """The lowest number played exactly once, or None if every number was matched."""
    counts: Dict[int, int] = {}
    for c in cards:
        counts[c] = counts.get(c, 0) + 1
    singles = [c for c, n in counts.items() if n == 1]
    return min(singles) if singles else None


def win_chances(others: Sequence[Sequence[Tuple[int, float]]]) -> Dict[int, float]:
    """For each card 1–8, the chance it wins if I play it, when each other
    player plays from their own list of (card, probability). Exact: at most
    two options each, so a handful of combinations."""
    out = {c: 0.0 for c in CARDS}
    for combo in itertools.product(*others) if others else [()]:
        prob = 1.0
        counts: Dict[int, int] = {}
        for card, pr in combo:
            prob *= pr
            counts[card] = counts.get(card, 0) + 1
        if prob == 0:
            continue
        lower_single = False
        for c in CARDS:
            n = counts.get(c, 0)
            if not lower_single and n == 0:
                out[c] += prob             # nobody else has it, nothing lower stands alone
            if n == 1:
                lower_single = True
    return out


def _utility(card: int, chance: float) -> float:
    return chance * (card + TOKEN_WEIGHT)


# ── House bots ───────────────────────────────────────────────────────────────
# noob plays at random. The others play a mixed strategy, as strong human
# players do here: each assumes the rest of the table is reasoning too, works
# out how likely everyone is to choose each card (a few rounds of "if they
# think that, I'd do this"), and then draws its own choice from those odds
# rather than always taking the single best one. Without the draw, identical
# bots all pile onto the same low cards and cancel each other out.
# Higher levels think more rounds deep and are less noisy.

_BOT_TUNING = {
    "normal": {"samples": 24, "depth": 1, "temperature": 0.9},
    "good": {"samples": 36, "depth": 2, "temperature": 0.5},
    "cracked": {"samples": 44, "depth": 4, "temperature": 0.3},
}


def _softmax(scored: Dict[Any, float], temperature: float) -> Dict[Any, float]:
    top = max(scored.values())
    w = {k: math.exp((v - top) / max(temperature, 1e-6)) for k, v in scored.items()}
    total = sum(w.values())
    return {k: v / total for k, v in w.items()}


def _draw(odds: Dict[Any, float], rng: random.Random) -> Any:
    return rng.choices(list(odds), weights=list(odds.values()))[0]


def _sample_pair(cards: List[int], weight: Dict[int, float], rng: random.Random) -> List[int]:
    if len(cards) <= 2:
        return list(cards)
    first = rng.choices(cards, weights=[weight.get(c, 0.01) for c in cards])[0]
    rest = [c for c in cards if c != first]
    return [first, rng.choices(rest, weights=[weight.get(c, 0.01) for c in rest])[0]]


def _even(pair: Sequence[int]) -> List[Tuple[int, float]]:
    return [(c, 1.0 / len(pair)) for c in pair] if pair else []


def _pair_values(cards: List[int], rivals: List[dict], weight: Dict[int, float], samples: int,
                 rng: random.Random) -> Dict[Tuple[int, ...], float]:
    """How good each pair I could reveal looks, if the others reveal cards in
    proportion to ``weight`` and then play either of theirs."""
    pairs = [tuple(x) for x in itertools.combinations(cards, 2)]
    totals = {pair: 0.0 for pair in pairs}
    for _ in range(samples):
        chance = win_chances([_even(_sample_pair(available(p), weight, rng)) for p in rivals])
        for pair in pairs:
            totals[pair] += max(_utility(c, chance[c]) for c in pair)
    return {pair: v / samples for pair, v in totals.items()}


def bot_reveal(game: dict, me: dict, rng: random.Random) -> List[int]:
    cards = available(me)
    level = me.get("bot_skill") or "normal"
    if len(cards) <= 2:
        return sorted(cards)
    if level not in _BOT_TUNING:
        return sorted(rng.sample(cards, 2))
    tune = _BOT_TUNING[level]
    rivals = [p for p in active(game) if p["user_id"] != me["user_id"]]
    weight = {c: 1.0 for c in CARDS}           # start by assuming the table reveals at random
    odds: Dict[Tuple[int, ...], float] = {}
    for _ in range(tune["depth"]):
        odds = _softmax(_pair_values(cards, rivals, weight, tune["samples"], rng), tune["temperature"])
        seen = {c: 0.0 for c in CARDS}
        for pair, pr in odds.items():
            for c in pair:
                seen[c] += pr / 2
        # Cards I don't hold keep their old weight; the rest move halfway to what I'd do.
        weight = {c: (0.5 * weight[c] + 0.5 * seen[c] * len(cards)) if c in cards else weight[c] for c in CARDS}
    return sorted(_draw(odds, rng))


def play_odds(game: dict, depth: int, temperature: float) -> Dict[str, List[Tuple[int, float]]]:
    """With every pair on the table, how likely each player is to play each of
    their two cards, if everyone reasons ``depth`` rounds deep."""
    reveals = game.get("reveals") or {}
    table = [p["user_id"] for p in active(game) if reveals.get(p["user_id"])]
    odds = {uid: _even(reveals[uid]) for uid in table}
    for _ in range(depth):
        new = {}
        for uid in table:
            chance = win_chances([odds[o] for o in table if o != uid])
            pick = _softmax({c: _utility(c, chance[c]) for c in reveals[uid]}, temperature)
            old = dict(odds[uid])
            new[uid] = [(c, 0.5 * old.get(c, 0) + 0.5 * pick[c]) for c in reveals[uid]]
        odds = new
    return odds


def bot_play(game: dict, me: dict, rng: random.Random) -> int:
    mine = list((game.get("reveals") or {}).get(me["user_id"]) or [])
    level = me.get("bot_skill") or "normal"
    if len(mine) < 2 or level not in _BOT_TUNING:
        return rng.choice(mine)
    tune = _BOT_TUNING[level]
    odds = play_odds(game, tune["depth"], tune["temperature"])
    others = [o for uid, o in odds.items() if uid != me["user_id"]]
    chance = win_chances(others)
    return _draw(_softmax({c: _utility(c, chance[c]) for c in mine}, tune["temperature"]), rng)


# ── The game loop ────────────────────────────────────────────────────────────

def _open(game: dict, phase: str, seconds: int, rng: random.Random, now: Optional[dt.datetime] = None) -> None:
    game["phase"] = phase
    game["deadline"] = ((now or _now()) + dt.timedelta(seconds=seconds)).isoformat()
    if phase == "reveal":
        game["reveals"], game["plays"] = {}, {}
    if phase in ("reveal", "play"):
        _bots_act(game, rng)


def _bots_act(game: dict, rng: random.Random) -> None:
    phase = game.get("phase")
    for p in active(game):
        if not p.get("is_bot"):
            continue
        if phase == "reveal" and p["user_id"] not in (game.get("reveals") or {}):
            game.setdefault("reveals", {})[p["user_id"]] = bot_reveal(game, p, rng)
        elif phase == "play" and p["user_id"] not in (game.get("plays") or {}):
            game.setdefault("plays", {})[p["user_id"]] = bot_play(game, p, rng)


def start(game: dict, bot_level: str, rng: Optional[random.Random] = None) -> None:
    rng = rng or random.Random()
    if bot_level not in BOT_LEVELS:
        bot_level = "normal"
    fill_bots(game, bot_level)
    game["bot_level"] = bot_level
    game["status"] = "playing"
    game["round"] = 1
    game["started_at"] = _now().isoformat()
    game["history"] = []
    game["last"] = None
    _log(game, f"Seven at the table. Round 1 of {ROUNDS}: reveal two cards.")
    _open(game, "reveal", REVEAL_SECONDS, rng)


def submit_reveal(game: dict, uid: str, cards: Sequence[int]) -> None:
    me = player(game, uid)
    if game.get("status") != "playing" or game.get("phase") != "reveal":
        raise ValueError("It isn't time to reveal")
    if not me or me.get("status") != "active":
        raise ValueError("You're not playing this round")
    if uid in (game.get("reveals") or {}):
        raise ValueError("You've already revealed this round")
    pick = sorted(set(int(c) for c in cards))
    allowed = available(me)
    if len(pick) != 2:
        raise ValueError("Pick two different cards")
    if any(c not in allowed for c in pick):
        raise ValueError("You can only reveal cards in your hand that aren't locked")
    game.setdefault("reveals", {})[uid] = pick


def submit_play(game: dict, uid: str, card: int) -> None:
    me = player(game, uid)
    if game.get("status") != "playing" or game.get("phase") != "play":
        raise ValueError("It isn't time to play")
    if not me or me.get("status") != "active":
        raise ValueError("You're not playing this round")
    if uid in (game.get("plays") or {}):
        raise ValueError("You've already played this round")
    if int(card) not in ((game.get("reveals") or {}).get(uid) or []):
        raise ValueError("Play one of the two cards you revealed")
    game.setdefault("plays", {})[uid] = int(card)


def _everyone_in(game: dict, key: str) -> bool:
    chosen = game.get(key) or {}
    return all(p["user_id"] in chosen for p in active(game))


def _deadline_passed(game: dict, now: dt.datetime) -> bool:
    deadline = _parse(game.get("deadline"))
    return deadline is not None and now >= deadline


def _standings(group: List[dict], rng: random.Random) -> Tuple[List[dict], bool]:
    """Best first: points, then tokens, then a draw. Says whether a draw was needed."""
    keyed = [(p["score"], p["tokens"], rng.random(), p) for p in group]
    keyed.sort(key=lambda k: (-k[0], -k[1], k[2]))
    drawn = len(keyed) > 1 and (keyed[0][0], keyed[0][1]) == (keyed[1][0], keyed[1][1])
    return [k[3] for k in keyed], drawn


def decided_by(ahead: dict, behind: dict) -> str:
    """What put ``ahead`` in front: "points", "tokens" or a "draw"."""
    if ahead["score"] != behind["score"]:
        return "points"
    return "tokens" if ahead["tokens"] != behind["tokens"] else "draw"


def _humans_left(game: dict) -> bool:
    return any(not p.get("is_bot") for p in active(game))


def resolve_round(game: dict, rng: random.Random) -> None:
    rnd = int(game["round"])
    reveals, plays = game.get("reveals") or {}, game.get("plays") or {}
    table = active(game)
    played = [plays[p["user_id"]] for p in table]
    win = winning_card(played)
    counts: Dict[int, int] = {}
    for c in played:
        counts[c] = counts.get(c, 0) + 1
    winner = next((p for p in table if plays[p["user_id"]] == win), None) if win is not None else None

    for p in table:
        card = plays[p["user_id"]]
        kept = [c for c in reveals.get(p["user_id"]) or [] if c != card]
        p["hand"] = [c for c in p.get("hand") or [] if c != card]
        p["locked"] = kept[0] if kept else None
        p["rounds_played"] = int(p.get("rounds_played", 0)) + 1
        if counts[card] > 1:
            p["cancelled"] = int(p.get("cancelled", 0)) + 1
    if winner:
        winner["score"] += win
        winner["tokens"] += 1
        winner["rounds_won"] = int(winner.get("rounds_won", 0)) + 1
        winner["points_total"] = int(winner.get("points_total", 0)) + win
        winner["best_win"] = max(int(winner.get("best_win", 0)), win)
        _log(game, f"Round {rnd}: {winner['username']} wins with a lone {win} (+{win} and a token).")
    else:
        _log(game, f"Round {rnd}: every number was matched, so nobody wins.")

    last: Dict[str, Any] = {"round": rnd, "plays": dict(plays), "reveals": {k: list(v) for k, v in reveals.items()},
                            "winner": winner["user_id"] if winner else None, "value": win,
                            "cancelled": sorted(c for c, n in counts.items() if n > 1),
                            "checkpoint": None, "reset": rnd in RESETS, "final": None}
    game.setdefault("history", []).append({"round": rnd, "winner": last["winner"], "value": win,
                                           "plays": dict(plays)})

    if rnd in CHECKPOINTS:
        order, drawn = _standings(table, rng)
        top = order[0]
        top["status"], top["out_round"] = "survived", rnd
        how = decided_by(order[0], order[1]) if len(order) > 1 else "points"
        last["checkpoint"] = {"user_id": top["user_id"], "username": top["username"], "score": top["score"],
                              "tokens": top["tokens"], "draw": drawn, "decided_by": how}
        game["history"][-1]["survivor"] = top["user_id"]
        why = {"tokens": " (level on points, ahead on tokens)",
               "draw": " (level on points and tokens, settled by a draw)"}.get(how, "")
        _log(game, f"Checkpoint after round {rnd}: {top['username']} survives with {top['score']} points{why}. "
                   "Everyone else goes back to zero points and keeps their tokens.")
        for p in active(game):
            p["score"] = 0

    if rnd in RESETS:
        for p in active(game):
            p["hand"], p["locked"] = list(CARDS), None
        _log(game, "Hands reset: everyone has 1 to 8 again.")

    if rnd == ROUNDS:
        order, drawn = _standings(active(game), rng)
        for i, p in enumerate(order):
            p["out_round"] = rnd
            p["status"] = "winner" if i == 0 else ("eliminated" if i == len(order) - 1 else "runner_up")
        last["final"] = [{"user_id": p["user_id"], "username": p["username"], "score": p["score"],
                          "tokens": p["tokens"], "status": p["status"]} for p in order]
        if len(order) > 1:
            last["final_decided"] = {"prize": decided_by(order[0], order[1]),
                                     "elimination": decided_by(order[-2], order[-1])}
        if order:
            _log(game, f"Final: {order[0]['username']} wins the prize"
                       + (f", {order[1]['username']} survives" if len(order) > 2 else "")
                       + (f", {order[-1]['username']} is eliminated." if len(order) > 1 else "."))

    game["last"] = last


def advance(game: dict, now: Optional[dt.datetime] = None, rng: Optional[random.Random] = None) -> bool:
    """Move the table on as far as the clock and the choices allow. Returns
    whether anything changed (and so needs saving).

    Once every human has survived (or there never were any), the bots play the
    remaining rounds out straight away: otherwise a table whose last human
    closed the tab would never finish, and nobody's result would be recorded."""
    if game.get("status") != "playing":
        return False
    rng = rng or random.Random()
    now = now or _now()
    changed = _step(game, now, rng)
    if game.get("status") == "playing" and not _humans_left(game):
        _log(game, "Only bots are left at the table, so the remaining rounds were played out at once.")
        for _ in range(ROUNDS * 4):
            if game.get("status") != "playing":
                break
            _step(game, max(now, _parse(game.get("deadline")) or now) + dt.timedelta(seconds=1), rng)
        game["finished_at"] = now.isoformat()
        changed = True
    return changed


def _step(game: dict, now: dt.datetime, rng: random.Random) -> bool:
    changed = False
    for _ in range(6):
        phase = game.get("phase")
        if phase == "reveal" and (_everyone_in(game, "reveals") or _deadline_passed(game, now)):
            for p in active(game):
                if p["user_id"] not in game["reveals"]:
                    cards = available(p)
                    game["reveals"][p["user_id"]] = sorted(rng.sample(cards, 2)) if len(cards) > 2 else sorted(cards)
            _open(game, "play", PLAY_SECONDS, rng, now)
        elif phase == "play" and (_everyone_in(game, "plays") or _deadline_passed(game, now)):
            for p in active(game):
                if p["user_id"] not in game["plays"]:
                    game["plays"][p["user_id"]] = rng.choice(game["reveals"][p["user_id"]])
            resolve_round(game, rng)
            big = bool((game.get("last") or {}).get("checkpoint") or (game.get("last") or {}).get("final"))
            game["phase"] = "result"
            game["deadline"] = (now + dt.timedelta(seconds=CHECKPOINT_SECONDS if big else RESULT_SECONDS)).isoformat()
        elif phase == "result" and _deadline_passed(game, now):
            if int(game["round"]) >= ROUNDS:
                game["status"] = "finished"
                game["phase"] = "done"
                game["finished_at"] = now.isoformat()
                return True
            game["round"] = int(game["round"]) + 1
            _open(game, "reveal", REVEAL_SECONDS, rng, now)
        else:
            break
        changed = True
    return changed


# ── Views ────────────────────────────────────────────────────────────────────

def _seconds_left(game: dict) -> int:
    deadline = _parse(game.get("deadline"))
    return max(0, math.ceil((deadline - _now()).total_seconds())) if deadline else 0


def _public_player(game: dict, p: dict, viewer: str) -> Dict[str, Any]:
    phase = game.get("phase")
    uid = p["user_id"]
    row = {"user_id": uid, "username": p["username"], "is_bot": bool(p.get("is_bot")),
           "is_me": uid == viewer, "score": int(p.get("score", 0)), "tokens": int(p.get("tokens", 0)),
           "status": p.get("status"), "out_round": p.get("out_round"),
           "hand": sorted(p.get("hand") or []), "locked": p.get("locked")}
    if p.get("status") == "active" and phase in ("reveal", "play"):
        row["ready"] = uid in (game.get("reveals" if phase == "reveal" else "plays") or {})
    # Pairs are secret until the reveal closes; plays until the round resolves.
    if phase == "play" or (phase == "reveal" and uid == viewer):
        row["revealed"] = (game.get("reveals") or {}).get(uid)
    if phase == "play" and uid == viewer:
        row["played"] = (game.get("plays") or {}).get(uid)
    if phase in ("result", "done"):
        last = game.get("last") or {}
        row["revealed"] = (last.get("reveals") or {}).get(uid)
        row["played"] = (last.get("plays") or {}).get(uid)
    return row


def state_view(game: dict, game_id: str, user: User) -> Dict[str, Any]:
    uid = str(user.id)
    me = player(game, uid)
    rnd = int(game.get("round") or 0)
    out: Dict[str, Any] = {
        "game_id": game_id, "status": game.get("status"), "join_code": game.get("join_code", ""),
        "is_host": game.get("created_by") == uid or user.is_admin, "joined": me is not None,
        "me": uid, "seats": SEATS, "round": rnd, "rounds": ROUNDS, "set": set_of(rnd),
        "phase": game.get("phase"), "seconds_left": _seconds_left(game),
        "checkpoints": list(CHECKPOINTS), "resets": list(RESETS),
        "next_checkpoint": next_checkpoint(rnd) if rnd else CHECKPOINTS[0],
        "final_stage": rnd > CHECKPOINTS[-1],
        "bot_level": game.get("bot_level"), "bot_levels": list(BOT_LEVELS),
        "players": [_public_player(game, p, uid) for p in players(game)],
        "history": [{"round": h["round"], "value": h.get("value"),
                     "winner": (player(game, h["winner"]) or {}).get("username") if h.get("winner") else None,
                     "survivor": (player(game, h["survivor"]) or {}).get("username") if h.get("survivor") else None}
                    for h in game.get("history") or []],
        "log": (game.get("log") or [])[-14:],
        "host_name": game.get("host_name", ""),
    }
    if me:
        out["my"] = {"status": me.get("status"), "available": available(me) if me.get("status") == "active" else [],
                     "locked": me.get("locked"), "hand": sorted(me.get("hand") or []),
                     "revealed": (game.get("reveals") or {}).get(uid), "played": (game.get("plays") or {}).get(uid)}
    if game.get("phase") in ("result", "done") and game.get("last"):
        last = game["last"]
        names = {p["user_id"]: p["username"] for p in players(game)}
        out["result"] = {"round": last["round"], "value": last.get("value"),
                         "winner": names.get(last.get("winner")) if last.get("winner") else None,
                         "winner_id": last.get("winner"), "cancelled": last.get("cancelled") or [],
                         "checkpoint": last.get("checkpoint"), "reset": last.get("reset"),
                         "final": last.get("final"), "final_decided": last.get("final_decided")}
    if game.get("status") == "finished":
        out["results"] = final_table(game)
        out["feedback"] = (game.get("feedback") or {}).get(uid)
    return out


def final_table(game: dict) -> List[Dict[str, Any]]:
    """Everyone, best outcome first: the prize, then survivors (earliest
    checkpoint first, then the final's second place), then eliminated."""
    order = {"winner": 0, "survived": 1, "runner_up": 2, "eliminated": 3}
    rows = sorted(players(game), key=lambda p: (order.get(p.get("status"), 4), p.get("out_round") or 99,
                                                  -int(p.get("tokens", 0))))
    return [{"user_id": p["user_id"], "username": p["username"], "is_bot": bool(p.get("is_bot")),
             "status": p.get("status"), "label": OUTCOME_LABEL.get(p.get("status"), ""),
             "out_round": p.get("out_round"), "tokens": int(p.get("tokens", 0)),
             "rounds_won": int(p.get("rounds_won", 0)), "points_total": int(p.get("points_total", 0))}
            for p in rows]


# ── Persistence ──────────────────────────────────────────────────────────────

_locks: Dict[str, asyncio.Lock] = {}


def _lock(game_id: str) -> asyncio.Lock:
    return _locks.setdefault(game_id, asyncio.Lock())


async def _load(game_id: str) -> Tuple[Any, dict]:
    ref = db_module.db.collection(COLLECTION).document(game_id)
    doc = await ref.get()
    if not doc.exists:
        raise HTTPException(404, "Table not found")
    return ref, (doc.to_dict() or {})


async def _record_results(game: dict, game_id: str) -> None:
    """Rate everyone on how far they got, and coach the humans."""
    if game.get("scored"):
        return
    game["scored"] = True
    coaching: Dict[str, Any] = {}
    for p in players(game):
        if p.get("is_bot"):
            continue
        stats = {"outcome": p.get("status"), "out_round": p.get("out_round"), "tokens": int(p.get("tokens", 0)),
                 "rounds_won": int(p.get("rounds_won", 0)), "rounds_played": int(p.get("rounds_played", 0)),
                 "points_total": int(p.get("points_total", 0)), "cancelled": int(p.get("cancelled", 0)),
                 "best_win": int(p.get("best_win", 0)), "bot_level": game.get("bot_level")}
        coaching[p["user_id"]] = fb.analyse(MODE, stats)
        await scores.record_result(MODE, p["user_id"], p["username"], OUTCOME_VALUE.get(p.get("status"), 0),
                                   game_id=game_id, detail=stats, feedback=coaching[p["user_id"]])
    rows = []
    for p in players(game):
        row = {"user_id": p["user_id"], "username": p["username"],
               "value": OUTCOME_VALUE.get(p.get("status"), 0) * 100 + int(p.get("tokens", 0))}
        if p.get("is_bot"):
            row["bot_skill"] = p.get("bot_skill") or "normal"
        rows.append(row)
    await scores.rate_game(MODE, game_id, rows, context=f"bots-{game.get('bot_level') or 'none'}")
    game["feedback"] = coaching


async def _settle(ref, game: dict, game_id: str, changed: bool) -> None:
    if game.get("status") == "finished" and not game.get("scored"):
        await _record_results(game, game_id)
        changed = True
    if changed:
        await ref.set(game)


# ── Requests ─────────────────────────────────────────────────────────────────

class JoinRequest(BaseModel):
    join_code: str


class StartRequest(BaseModel):
    bot_level: str = "normal"


class RevealRequest(BaseModel):
    cards: List[int] = Field(default_factory=list)


class PlayRequest(BaseModel):
    card: int


# ── Pages ────────────────────────────────────────────────────────────────────

@router.get("", include_in_schema=False)
async def rules_page(request: Request):
    return templates.TemplateResponse("lone_bid_rules.html", {
        "request": request, "app_name": "AlphaBook", "seats": SEATS, "rounds": ROUNDS,
        "reveal_seconds": REVEAL_SECONDS, "play_seconds": PLAY_SECONDS,
    })


@router.get("/game/{game_id}", include_in_schema=False)
async def game_page(game_id: str, request: Request):
    await _load(game_id)
    return templates.TemplateResponse("lone_bid_game.html", {
        "request": request, "app_name": "AlphaBook", "game_id": game_id,
    })


# ── Lobby ────────────────────────────────────────────────────────────────────

@router.post("/create")
async def create_game(user: User = Depends(current_user)):
    game_id = str(uuid.uuid4())
    game = {
        "join_code": generate_join_code(), "status": "lobby", "phase": None, "round": 0,
        "players": [new_player(str(user.id), user.username)],
        "reveals": {}, "plays": {}, "history": [], "last": None, "log": [],
        "created_by": str(user.id), "host_name": user.username,
        "created_at": _now().isoformat(), "scored": False,
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
    async with _lock(doc.id):
        ref, game = await _load(doc.id)
        uid = str(user.id)
        if player(game, uid):
            return {"ok": True, "game_id": doc.id}
        if game.get("status") != "lobby":
            raise HTTPException(400, "That table has already started")
        if len(players(game)) >= SEATS:
            raise HTTPException(400, f"That table is full ({SEATS} seats)")
        game["players"].append(new_player(uid, user.username))
        await ref.set(game)
    return {"ok": True, "game_id": doc.id}


@router.post("/game/{game_id}/leave")
async def leave_lobby(game_id: str, user: User = Depends(current_user)):
    """Give up your seat before the deal (the host closes the table instead)."""
    async with _lock(game_id):
        ref, game = await _load(game_id)
        uid = str(user.id)
        if game.get("status") != "lobby":
            raise HTTPException(400, "The game has started")
        if game.get("created_by") == uid:
            raise HTTPException(400, "You're the host: start the game or just leave the page")
        game["players"] = [p for p in players(game) if p["user_id"] != uid]
        await ref.set(game)
    return {"ok": True}


@router.post("/game/{game_id}/start")
async def start_game(game_id: str, req: StartRequest, user: User = Depends(current_user)):
    async with _lock(game_id):
        ref, game = await _load(game_id)
        if game.get("created_by") != str(user.id) and not user.is_admin:
            raise HTTPException(403, "Only the host of this table can start it")
        if game.get("status") != "lobby":
            return {"ok": True, "status": game.get("status")}
        start(game, req.bot_level)
        await ref.set(game)
    return {"ok": True, "status": "playing"}


# ── Play ─────────────────────────────────────────────────────────────────────

async def _act(game_id: str, user: User, fn) -> Dict[str, Any]:
    async with _lock(game_id):
        ref, game = await _load(game_id)
        changed = advance(game)
        try:
            fn(game, str(user.id))
        except ValueError as exc:
            await _settle(ref, game, game_id, changed)
            raise HTTPException(400, str(exc))
        advance(game)
        await _settle(ref, game, game_id, True)
        return state_view(game, game_id, user)


@router.post("/game/{game_id}/reveal")
async def reveal(game_id: str, req: RevealRequest, user: User = Depends(current_user)):
    return await _act(game_id, user, lambda g, uid: submit_reveal(g, uid, req.cards))


@router.post("/game/{game_id}/play")
async def play(game_id: str, req: PlayRequest, user: User = Depends(current_user)):
    return await _act(game_id, user, lambda g, uid: submit_play(g, uid, req.card))


@router.get("/game/{game_id}/state")
async def state(game_id: str, user: User = Depends(current_user)):
    """Polled by the table, and the table's clock: a phase whose time is up
    is closed here, so play moves on without a background task."""
    async with _lock(game_id):
        ref, game = await _load(game_id)
        changed = advance(game)
        await _settle(ref, game, game_id, changed)
        return state_view(game, game_id, user)
