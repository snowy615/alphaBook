"""Episode data and scoring maths for the Risks game.

Episodes come from the synthetic crash generator (see
scripts/build_risk_episodes.py). Each one is a stressed market: a basket of
names rebased to 100, replayed a day at a time, with a panic window and a
rebound window somewhere inside it.

Everything here is pure and side-effect free so it can be tested without
Firestore or a running clock.
"""
from __future__ import annotations

import functools
import json
import random
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).parent / "data" / "risk_episodes"

# ── Game constants ──────────────────────────────────────────────────────
# Every episode in the library is a crash, so a player who simply shorted the
# whole basket would win every round on direction alone. The net-exposure cap
# takes that trade away: you have to be roughly market-neutral, which turns the
# round into a question of *which* names break rather than whether the market
# falls. Beta is the prior you are given; the realised beta is what you find out.
START_EQUITY = 1_000_000.0
GROSS_LIMIT_MULT = 2.0      # gross exposure <= 2x starting equity
NET_LIMIT_MULT = 0.25       # |net exposure| <= 0.25x starting equity
DRAWDOWN_PENALTY = 0.5      # score = pnl - 0.5 * max drawdown
DEFAULT_SECONDS_PER_DAY = 4
MIN_SECONDS_PER_DAY = 2
MAX_SECONDS_PER_DAY = 30
MAX_TRADES_PER_PLAYER = 2000

GROSS_LIMIT = START_EQUITY * GROSS_LIMIT_MULT
NET_LIMIT = START_EQUITY * NET_LIMIT_MULT


class EpisodeNotFound(Exception):
    pass


@functools.lru_cache(maxsize=1)
def _library() -> dict[str, dict[str, Any]]:
    """All episodes on disk, keyed by episode_id. Cached for the process life."""
    lib: dict[str, dict[str, Any]] = {}
    if not DATA_DIR.is_dir():
        return lib
    for path in sorted(DATA_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        episode_id = data.get("episode_id")
        if episode_id and data.get("names") and data.get("days"):
            lib[episode_id] = data
    return lib


def all_episodes() -> list[dict[str, Any]]:
    return list(_library().values())


def get_episode(episode_id: str) -> dict[str, Any]:
    try:
        return _library()[episode_id]
    except KeyError:
        raise EpisodeNotFound(episode_id) from None


def universes() -> list[dict[str, Any]]:
    """Universe list for the rules page, with episode counts and day ranges."""
    out: dict[str, dict[str, Any]] = {}
    for ep in all_episodes():
        key = ep["universe"]
        entry = out.setdefault(key, {
            "universe": key,
            "label": ep.get("universe_label", key),
            "episodes": 0,
            "min_days": ep["days"],
            "max_days": ep["days"],
        })
        entry["episodes"] += 1
        entry["min_days"] = min(entry["min_days"], ep["days"])
        entry["max_days"] = max(entry["max_days"], ep["days"])
    return sorted(out.values(), key=lambda u: u["label"])


def pick_episode(universe: str | None = None, rng: random.Random | None = None) -> dict[str, Any]:
    """A random episode, optionally restricted to one universe."""
    rng = rng or random
    pool = all_episodes()
    if universe:
        pool = [e for e in pool if e["universe"] == universe]
    if not pool:
        raise EpisodeNotFound(universe or "any")
    return rng.choice(pool)


# ── Pricing helpers ─────────────────────────────────────────────────────
def prices_on(episode: dict[str, Any], day: int) -> dict[str, float]:
    """Closing price of every name on `day`, clamped to the episode's range."""
    day = max(0, min(int(day), episode["days"] - 1))
    return {n["ticker"]: n["closes"][day] for n in episode["names"]}


def public_names(episode: dict[str, Any], day: int) -> list[dict[str, Any]]:
    """What a player may see while the round is live.

    Deliberately excludes the realised beta and the shock group: those are the
    answers, and they are only revealed once the round is finished.
    """
    day = max(0, min(int(day), episode["days"] - 1))
    out = []
    for n in episode["names"]:
        closes = n["closes"]
        price = closes[day]
        prev = closes[day - 1] if day > 0 else closes[0]
        out.append({
            "ticker": n["ticker"],
            "price": round(price, 2),
            "day_pct": round((price / prev - 1) * 100, 2) if prev else 0.0,
            "total_pct": round((price / closes[0] - 1) * 100, 2),
            "published_beta": n["published_beta"],
            # Public: names in a cohort move together, which is the whole
            # point of spreading a book across them.
            "sector": n.get("sector", ""),
        })
    return out


def message_on(episode: dict[str, Any], day: int) -> dict[str, Any] | None:
    """The day's market commentary, when the generator produced a wire.

    Written from that day's move only, so it never looks ahead.
    """
    msgs = episode.get("messages") or []
    if not msgs:
        return None
    day = max(0, min(int(day), len(msgs) - 1))
    row = msgs[day]
    return {"text": row.get("text", ""), "confidence": row.get("confidence", "Low")}


def aftermath(episode: dict[str, Any]) -> dict[str, Any]:
    """The v7 extras that are only safe to show once a round is scored."""
    out: dict[str, Any] = {}
    if episode.get("phases"):
        out["phases"] = episode["phases"]
    if episode.get("blend"):
        out["blend"] = sorted(
            ({"period": k, "weight": v} for k, v in episode["blend"].items()),
            key=lambda r: r["weight"], reverse=True,
        )
    if episode.get("severity") is not None:
        out["severity"] = episode["severity"]
    if episode.get("events"):
        out["events"] = episode["events"]
    return out


def reveal_names(episode: dict[str, Any]) -> list[dict[str, Any]]:
    """The post-round teaching payload: what each name actually did, and why."""
    out = []
    for n in episode["names"]:
        closes = n["closes"]
        trough = min(closes)
        out.append({
            "ticker": n["ticker"],
            "published_beta": n["published_beta"],
            "realised_beta": n["realised_beta"],
            "shock_group": n["shock_group"],
            "total_pct": round((closes[-1] / closes[0] - 1) * 100, 2),
            "drawdown_pct": round((trough / closes[0] - 1) * 100, 2),
        })
    return sorted(out, key=lambda r: r["total_pct"])


# ── Position and P&L maths ──────────────────────────────────────────────
def positions_after(trades: list[dict[str, Any]], day: int | None = None) -> dict[str, int]:
    """Net position per ticker from the trade log, up to and including `day`."""
    pos: dict[str, int] = {}
    for t in trades:
        if day is not None and t["day"] > day:
            continue
        pos[t["ticker"]] = pos.get(t["ticker"], 0) + int(t["delta"])
    return {k: v for k, v in pos.items() if v}


def cash_after(trades: list[dict[str, Any]], day: int | None = None) -> float:
    """Cash balance after financing every fill from the starting equity."""
    cash = START_EQUITY
    for t in trades:
        if day is not None and t["day"] > day:
            continue
        cash -= int(t["delta"]) * float(t["price"])
    return cash


def exposure(positions: dict[str, int], prices: dict[str, float]) -> tuple[float, float]:
    """(gross, net) exposure in currency terms."""
    gross = sum(abs(q) * prices.get(t, 0.0) for t, q in positions.items())
    net = sum(q * prices.get(t, 0.0) for t, q in positions.items())
    return gross, net


def equity_at(episode: dict[str, Any], trades: list[dict[str, Any]], day: int) -> float:
    """Mark-to-market equity on `day`: cash plus the value of the book."""
    prices = prices_on(episode, day)
    pos = positions_after(trades, day)
    return cash_after(trades, day) + sum(q * prices.get(t, 0.0) for t, q in pos.items())


def equity_curve(episode: dict[str, Any], trades: list[dict[str, Any]],
                 through_day: int) -> list[float]:
    """Equity on every day from 0 to `through_day` inclusive."""
    last = max(0, min(int(through_day), episode["days"] - 1))
    return [equity_at(episode, trades, d) for d in range(last + 1)]


def max_drawdown(curve: list[float]) -> float:
    """Largest peak-to-trough fall in the equity curve, as a positive number."""
    peak = curve[0] if curve else START_EQUITY
    worst = 0.0
    for value in curve:
        peak = max(peak, value)
        worst = max(worst, peak - value)
    return worst


def score_player(episode: dict[str, Any], trades: list[dict[str, Any]],
                 day: int) -> dict[str, Any]:
    """P&L, drawdown and the risk-adjusted score a player is ranked on."""
    curve = equity_curve(episode, trades, day)
    equity = curve[-1] if curve else START_EQUITY
    pnl = equity - START_EQUITY
    dd = max_drawdown(curve)
    prices = prices_on(episode, day)
    pos = positions_after(trades, day)
    gross, net = exposure(pos, prices)
    return {
        "equity": round(equity, 2),
        "pnl": round(pnl, 2),
        "max_drawdown": round(dd, 2),
        "score": round(pnl - DRAWDOWN_PENALTY * dd, 2),
        "gross": round(gross, 2),
        "net": round(net, 2),
        "positions": pos,
        "trade_count": len(trades),
    }


def check_trade(positions: dict[str, int], prices: dict[str, float],
                ticker: str, target: int) -> tuple[bool, str]:
    """Would setting `ticker` to `target` stay inside the exposure limits?"""
    if ticker not in prices:
        return False, f"{ticker} is not in this basket"

    proposed = dict(positions)
    if target:
        proposed[ticker] = int(target)
    else:
        proposed.pop(ticker, None)

    gross, net = exposure(proposed, prices)
    if gross > GROSS_LIMIT + 1e-6:
        return False, (f"Gross exposure would be {gross:,.0f}, over the "
                       f"{GROSS_LIMIT:,.0f} limit")
    if abs(net) > NET_LIMIT + 1e-6:
        return False, (f"Net exposure would be {net:,.0f}, outside the "
                       f"±{NET_LIMIT:,.0f} limit — hedge the other side first")
    return True, ""
