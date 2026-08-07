"""
Per-game coaching.
==================

Every mode ends with a number, which tells a player how they did but not why.
This module turns a finished game's detail into something a coach would say:
one headline, a few stat tiles, and notes that name the specific habit that
earned or cost points.

Each mode has its own analyser because the useful observation is different in
each — being picked off on one side of your markets, guessing the same rank
every round, or trading against the news are not the same mistake. Analysers
share one output shape so the front ends and the profile can render them all
the same way:

    {"headline": str,
     "grade": "great" | "good" | "mixed" | "poor",
     "stats": [{"label": str, "value": str}],
     "notes": [{"kind": "win" | "gap" | "tip", "text": str}]}

Notes are ordered: what went well, then what cost the most, then one concrete
thing to do differently. Analysers never raise — a coaching failure must not
take down the end-of-game screen.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List

log = logging.getLogger("uvicorn.error")

Note = Dict[str, str]


def _fb(headline: str, grade: str, stats: List[Dict[str, str]],
        notes: List[Note]) -> Dict[str, Any]:
    return {"headline": headline, "grade": grade, "stats": stats, "notes": notes[:4]}


def _pct(n: float) -> str:
    return f"{n:.0f}%"


def _money(n: float) -> str:
    sign = "-" if n < 0 else ""
    return f"{sign}${abs(n):,.0f}"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


# ─────────────────────────────────────────────────────────────────────────────
# Crash Call — making markets on crash statistics
# ─────────────────────────────────────────────────────────────────────────────

def _crash_ledger(p: Dict[str, Any]) -> Dict[str, Any]:
    rounds = p.get("rounds") or []
    if not rounds:
        return _fb("No rounds to review.", "mixed", [], [])

    total = len(rounds)
    held = [r for r in rounds if r.get("side") == "held" and r.get("tradeable")]
    no_trade = [r for r in rounds if r.get("side") == "held" and not r.get("tradeable")]
    lifted = [r for r in rounds if r.get("side") == "lifted"]
    hit = [r for r in rounds if r.get("side") == "hit"]
    score = int(p.get("score", 0))

    widths = [float(r.get("width_units", 0)) for r in rounds]
    avg_width = sum(widths) / len(widths)
    held_rate = len(held) / total

    if held_rate >= 0.7 and score > 800:
        grade, headline = "great", "You priced these like someone who knows the names."
    elif score > 300:
        grade, headline = "good", "Solid session — your levels were mostly right."
    elif score > 0:
        grade, headline = "mixed", "You finished ahead, but the spread did the work, not the read."
    else:
        grade, headline = "poor", "The house took you apart on levels."

    stats = [
        {"label": "Markets held", "value": f"{len(held)} / {total}"},
        {"label": "Picked off", "value": str(len(lifted) + len(hit))},
        {"label": "Avg spread", "value": f"{avg_width:.2f}× typical"},
        {"label": "Score", "value": f"{score:,}"},
    ]

    notes: List[Note] = []

    # What went well
    best = max(rounds, key=lambda r: r.get("points", 0))
    if best.get("points", 0) > 0:
        notes.append({"kind": "win", "text":
                      f"Your best call was {best['ticker']} on {best['label']}: "
                      f"a {best['width']:.1f} wide market that held for {best['points']} points."})

    # The dominant failure mode
    if no_trade:
        notes.append({"kind": "gap", "text":
                      f"{len(no_trade)} of your markets were so wide nobody traded them — "
                      f"they scored zero even though the answer was inside. A market only "
                      f"pays if someone would actually deal on it."})
    # Directional bias. "Lifted" means the truth came in above the ask, so the
    # whole market was too low; "hit" means it came in below the bid, so the
    # market was too high. Phrased in terms of the number, not the severity,
    # because "worse" flips sign between drawdown and volatility.
    elif len(lifted) >= 2 and len(lifted) > len(hit) * 2:
        notes.append({"kind": "gap", "text":
                      f"You were lifted {len(lifted)} times and hit {len(hit)}: the true "
                      f"numbers kept coming in above your market. You are pricing these too "
                      f"low — move the whole quote up before you worry about the width."})
    elif len(hit) >= 2 and len(hit) > len(lifted) * 2:
        notes.append({"kind": "gap", "text":
                      f"You were hit {len(hit)} times and lifted {len(lifted)}: the true "
                      f"numbers kept coming in below your market. You are pricing these too "
                      f"high — move the whole quote down."})
    elif (len(lifted) + len(hit)) > total / 2 and avg_width < 0.6:
        notes.append({"kind": "gap", "text":
                      f"You quoted tight ({avg_width:.2f}× the typical spread) and got picked "
                      f"off {len(lifted) + len(hit)} times. Tight only pays when the level is "
                      f"right — widen until your hit rate recovers."})

    # Which statistic is costing them
    by_stat: Dict[str, List[int]] = {}
    for r in rounds:
        by_stat.setdefault(r.get("label", "?"), []).append(int(r.get("points", 0)))
    if len(by_stat) > 1:
        worst_stat = min(by_stat.items(), key=lambda kv: sum(kv[1]) / len(kv[1]))
        if sum(worst_stat[1]) < 0:
            notes.append({"kind": "gap", "text":
                          f"{worst_stat[0].capitalize()} cost you "
                          f"{sum(worst_stat[1])} points across "
                          f"{_plural(len(worst_stat[1]), 'round')} — "
                          f"that's the statistic you have least feel for."})

    # One concrete thing to change
    if avg_width > 1.6:
        notes.append({"kind": "tip", "text":
                      "Your markets average wider than the spread of the names themselves. "
                      "Try halving your width on the names you actually recognise — that's "
                      "where the points are."})
    elif len(held) == total:
        notes.append({"kind": "tip", "text":
                      "You were never picked off. That usually means there's free money in "
                      "tightening — go narrower until you start getting caught occasionally."})
    else:
        notes.append({"kind": "tip", "text":
                      "Use the cohort average as a starting point, then move off it for what "
                      "you know about the company — the outliers are where rounds are won."})

    return _fb(headline, grade, stats, notes)


# ─────────────────────────────────────────────────────────────────────────────
# Mental Math
# ─────────────────────────────────────────────────────────────────────────────

def _mental_math(p: Dict[str, Any]) -> Dict[str, Any]:
    questions = p.get("questions") or []
    answers = {a["index"]: a for a in (p.get("answers") or []) if "index" in a}
    total = len(questions) or 1
    correct = sum(1 for a in answers.values() if a.get("correct"))
    attempted = len(answers)
    accuracy = 100.0 * correct / total

    if accuracy >= 90:
        grade, headline = "great", "Fast and accurate — this difficulty is no longer testing you."
    elif accuracy >= 70:
        grade, headline = "good", "Reliable under the clock, with a couple of leaks."
    elif accuracy >= 45:
        grade, headline = "mixed", "The arithmetic is there; the speed isn't yet."
    else:
        grade, headline = "poor", "This set got away from you."

    stats = [
        {"label": "Correct", "value": f"{correct} / {total}"},
        {"label": "Accuracy", "value": _pct(accuracy)},
        {"label": "Attempted", "value": f"{attempted} / {total}"},
        {"label": "Difficulty", "value": str(p.get("difficulty", "—")).title()},
    ]

    # Accuracy per question type is where the actual coaching is.
    by_type: Dict[str, List[bool]] = {}
    for i, q in enumerate(questions):
        a = answers.get(i)
        by_type.setdefault(q.get("type", "?"), []).append(bool(a and a.get("correct")))

    notes: List[Note] = []
    scored = {t: (sum(v) / len(v), len(v)) for t, v in by_type.items() if v}
    if scored:
        best_t = max(scored.items(), key=lambda kv: kv[1][0])
        worst_t = min(scored.items(), key=lambda kv: kv[1][0])
        if best_t[1][0] >= 0.8:
            notes.append({"kind": "win", "text":
                          f"{best_t[0].replace('_', ' ').title()} is solid: "
                          f"{int(best_t[1][0] * 100)}% of {best_t[1][1]}."})
        if worst_t[0] != best_t[0] and worst_t[1][0] <= 0.6:
            notes.append({"kind": "gap", "text":
                          f"{worst_t[0].replace('_', ' ').title()} is the weak spot: "
                          f"{int(worst_t[1][0] * 100)}% of {worst_t[1][1]}. "
                          f"Drill that type on its own before mixing it back in."})

    if attempted < total:
        notes.append({"kind": "gap", "text":
                      f"You ran out of time on {total - attempted} questions. "
                      f"An answered guess beats a blank — commit earlier."})

    if accuracy >= 90:
        notes.append({"kind": "tip", "text":
                      "Move up a difficulty. Accuracy this high means the ceiling is the "
                      "timer, not the arithmetic."})
    else:
        notes.append({"kind": "tip", "text":
                      "Estimate the magnitude first and sanity-check the last digit — most "
                      "misses at this level are slips, not gaps in method."})

    return _fb(headline, grade, stats, notes)


# ─────────────────────────────────────────────────────────────────────────────
# 5Os
# ─────────────────────────────────────────────────────────────────────────────

def _fiveos(p: Dict[str, Any]) -> Dict[str, Any]:
    rounds = p.get("rounds") or []
    pnl = float(p.get("pnl", 0.0))
    actuals = p.get("actuals") or {}

    if pnl > 15:
        grade, headline = "great", "Your estimates were tight and your positions followed them."
    elif pnl > 0:
        grade, headline = "good", "Positive session — the reads were mostly sound."
    elif pnl > -15:
        grade, headline = "mixed", "Close, but the fees ate the edge."
    else:
        grade, headline = "poor", "The estimates drifted a long way from the cards."

    stats = [
        {"label": "P&L", "value": f"{pnl:+.1f}"},
        {"label": "Rounds", "value": str(len(rounds))},
    ]

    # Which of the three statistics they read worst, and in which direction.
    err: Dict[str, List[float]] = {"q1": [], "q2": [], "q3": []}
    for r in rounds:
        for q in ("q1", "q2", "q3"):
            est, act = r.get(f"est_{q}"), actuals.get(q)
            if est is not None and act is not None:
                err[q].append(float(est) - float(act))

    labels = {"q1": "the missing-rank sum", "q2": "odd minus even", "q3": "the 15-card sum"}
    notes: List[Note] = []

    scored = {q: (sum(abs(e) for e in v) / len(v), sum(v) / len(v))
              for q, v in err.items() if v}
    if scored:
        best_q = min(scored.items(), key=lambda kv: kv[1][0])
        worst_q = max(scored.items(), key=lambda kv: kv[1][0])
        notes.append({"kind": "win", "text":
                      f"You read {labels[best_q[0]]} best — off by "
                      f"{best_q[1][0]:.1f} on average."})
        if worst_q[0] != best_q[0]:
            bias = worst_q[1][1]
            direction = "over" if bias > 0 else "under"
            notes.append({"kind": "gap", "text":
                          f"{labels[worst_q[0]].capitalize()} was your blind spot: off by "
                          f"{worst_q[1][0]:.1f} on average, and you {direction}-estimated it "
                          f"in most rounds."})
            notes.append({"kind": "tip", "text":
                          f"Before each round, write down what {labels[worst_q[0]]} would be "
                          f"if the unseen cards were perfectly average, then adjust from there."})

    return _fb(headline, grade, stats, notes)


# ─────────────────────────────────────────────────────────────────────────────
# Headline Trading
# ─────────────────────────────────────────────────────────────────────────────

def _headline(p: Dict[str, Any]) -> Dict[str, Any]:
    pnl = float(p.get("pnl", 0.0))
    trades = p.get("trades") or []
    news = p.get("news") or []
    position = int(p.get("position", 0))

    if pnl > 200:
        grade, headline = "great", "You were on the right side of the tape."
    elif pnl > 0:
        grade, headline = "good", "Finished up — the direction was right more often than not."
    elif pnl > -200:
        grade, headline = "mixed", "Roughly flat; the news didn't pay you."
    else:
        grade, headline = "poor", "The story ran against your position all session."

    stats = [
        {"label": "P&L", "value": f"{pnl:+,.0f}"},
        {"label": "Trades", "value": str(len(trades))},
        {"label": "Final position", "value": f"{position:+d}"},
    ]

    notes: List[Note] = []

    # Did their trades follow the news, or fight it?
    aligned = against = 0
    for t in trades:
        recent = [n for n in news if 0 <= t.get("tick", 0) - n.get("time", 0) <= 45]
        if not recent:
            continue
        impact = recent[-1].get("impact", 0)
        delta = t.get("delta", 0)
        if impact and delta:
            if (impact > 0) == (delta > 0):
                aligned += 1
            else:
                against += 1

    if aligned + against >= 3:
        if aligned > against * 2:
            notes.append({"kind": "win", "text":
                          f"{aligned} of your {aligned + against} reactive trades went with the "
                          f"story — you read the headlines the right way round."})
        elif against > aligned:
            notes.append({"kind": "gap", "text":
                          f"{against} of your {aligned + against} reactive trades fought the "
                          f"news. Fading a fresh headline needs a reason; by default, trade with it."})

    if len(trades) > 12:
        notes.append({"kind": "gap", "text":
                      f"{len(trades)} position changes in one session is a lot of churn — "
                      f"each one paid the spread. Fewer, larger decisions usually beat it."})
    elif len(trades) <= 2 and pnl <= 0:
        notes.append({"kind": "gap", "text":
                      "You traded twice or less. The market moved all session; sitting out "
                      "means only the opening view mattered."})

    notes.append({"kind": "tip", "text":
                  "Size to conviction: a strong headline deserves a bigger clip than a "
                  "vague one, and the analysis after the game tells you which was which."})

    return _fb(headline, grade, stats, notes)


# ─────────────────────────────────────────────────────────────────────────────
# Risks
# ─────────────────────────────────────────────────────────────────────────────

def _risks(p: Dict[str, Any]) -> Dict[str, Any]:
    pnl = float(p.get("pnl", 0.0))
    dd = float(p.get("max_drawdown", 0.0))
    score = float(p.get("score", 0.0))
    gross = float(p.get("gross", 0.0))
    trades = int(p.get("trade_count", 0))
    limit = float(p.get("gross_limit", 0.0))

    if score > 0 and dd < abs(pnl) * 0.5:
        grade, headline = "great", "You made money and kept the drawdown small — that's the whole game."
    elif score > 0:
        grade, headline = "good", "Positive, but you paid for it in drawdown."
    elif pnl > 0:
        grade, headline = "mixed", "You finished up on P&L, and the drawdown penalty ate it."
    else:
        grade, headline = "poor", "The crash got you."

    stats = [
        {"label": "P&L", "value": _money(pnl)},
        {"label": "Max drawdown", "value": _money(dd)},
        {"label": "Score", "value": f"{score:,.0f}"},
        {"label": "Trades", "value": str(trades)},
    ]

    notes: List[Note] = []
    if pnl > 0:
        notes.append({"kind": "win", "text": f"You finished {_money(pnl)} up through the episode."})

    if dd > max(abs(pnl), 1) * 0.8:
        notes.append({"kind": "gap", "text":
                      f"Your worst drawdown ({_money(dd)}) was as large as your whole P&L. "
                      f"You're being scored on both — cutting gross into the panic window is "
                      f"what separates the top of this board."})
    if limit and gross > limit * 0.9:
        notes.append({"kind": "gap", "text":
                      f"You ran at {gross / limit * 100:.0f}% of the gross limit. Full size "
                      f"through a crash leaves nothing to add with when it's actually cheap."})
    if trades <= 2:
        notes.append({"kind": "tip", "text":
                      "You barely rebalanced. The episode has a panic and a rebound — "
                      "trading the phases is where the risk-adjusted score comes from."})
    else:
        notes.append({"kind": "tip", "text":
                      "Watch the wire commentary each day: it describes the move that has "
                      "already happened, which is your only clue to what phase you're in."})

    return _fb(headline, grade, stats, notes)


# ─────────────────────────────────────────────────────────────────────────────
# Poker Auction
# ─────────────────────────────────────────────────────────────────────────────

def _poker_auction(p: Dict[str, Any]) -> Dict[str, Any]:
    money = float(p.get("money", 0.0))
    start = float(p.get("start_money", 1000.0))
    change = money - start
    hand = p.get("hand") or "High Card"
    award = float(p.get("award", 0.0))
    spent = float(p.get("spent", 0.0))
    cards = int(p.get("cards", 0))

    if change > 500:
        grade, headline = "great", f"You built a {hand} and still came out well ahead."
    elif change > 0:
        grade, headline = "good", f"Finished up with a {hand}."
    elif change > -300:
        grade, headline = "mixed", f"A {hand} didn't quite cover what you paid for it."
    else:
        grade, headline = "poor", "You overpaid for cards that didn't come together."

    stats = [
        {"label": "Bankroll", "value": _money(money)},
        {"label": "Change", "value": _money(change)},
        {"label": "Hand", "value": str(hand)},
        {"label": "Cards held", "value": str(cards)},
    ]

    notes: List[Note] = []
    if award > 0:
        notes.append({"kind": "win", "text": f"Your {hand} paid {_money(award)} at showdown."})
    if spent > award and award >= 0:
        notes.append({"kind": "gap", "text":
                      f"You spent {_money(spent)} at auction to win {_money(award)}. "
                      f"In a second-price auction the winner's curse is real — bid your "
                      f"true value, not what you think it takes to win."})
    if cards >= 8 and award < 500:
        notes.append({"kind": "gap", "text":
                      f"You collected {cards} cards but only made a {hand}. Volume doesn't "
                      f"make hands — the cards that complete a draw are worth far more than "
                      f"the ones that don't."})
    notes.append({"kind": "tip", "text":
                  "Work out what each lot is worth to your hand before the bidding, and "
                  "remember you only pay the second-highest bid — so bidding your honest "
                  "value costs nothing extra."})

    return _fb(headline, grade, stats, notes)


# ─────────────────────────────────────────────────────────────────────────────
# Coding modes (Market Sim Py)
# ─────────────────────────────────────────────────────────────────────────────

def _bot_run(p: Dict[str, Any]) -> Dict[str, Any]:
    pnl = float(p.get("pnl", 0.0))
    fills = int(p.get("fills", 0))
    volume = float(p.get("volume", 0.0))
    accepted = int(p.get("orders_accepted", 0))
    rejected = int(p.get("orders_rejected", 0))
    error = p.get("error")

    if error:
        grade, headline = "poor", "Your strategy stopped early."
    elif pnl > 500:
        grade, headline = "great", "Your bot traded well and finished clearly ahead."
    elif pnl > 0:
        grade, headline = "good", "Positive run — the logic held up."
    elif fills == 0:
        grade, headline = "poor", "Your bot never got a fill."
    else:
        grade, headline = "mixed", "It traded, but the edge wasn't there."

    stats = [
        {"label": "P&L", "value": _money(pnl)},
        {"label": "Fills", "value": str(fills)},
        {"label": "Volume", "value": f"{volume:,.0f}"},
        {"label": "Rejected", "value": f"{rejected}"},
    ]

    notes: List[Note] = []
    if error:
        notes.append({"kind": "gap", "text": f"The run ended with: {str(error)[:160]}"})
    if fills == 0 and not error:
        notes.append({"kind": "gap", "text":
                      "No fills all run. Either your quotes never crossed the book or you "
                      "only ever rested far from the touch — check the prices you're sending "
                      "against the market snapshot."})
    elif fills:
        notes.append({"kind": "win", "text":
                      f"{fills} fills for {volume:,.0f} of volume — the bot was genuinely in "
                      f"the market."})
    total_orders = accepted + rejected
    if total_orders and rejected / total_orders > 0.25:
        notes.append({"kind": "gap", "text":
                      f"{rejected} of {total_orders} orders were rejected "
                      f"({rejected / total_orders * 100:.0f}%). Rejections usually mean price "
                      f"or size limits — validate before sending rather than firing and hoping."})
    notes.append({"kind": "tip", "text":
                  "Log your own fair value next to each fill. Losing runs almost always show "
                  "the same thing: you kept quoting after the market moved away from you."})

    return _fb(headline, grade, stats, notes)


# ─────────────────────────────────────────────────────────────────────────────
# Market Simulation (live order book) — session summary, not a finished game
# ─────────────────────────────────────────────────────────────────────────────

def _market_sim(p: Dict[str, Any]) -> Dict[str, Any]:
    pnl = float(p.get("pnl", 0.0))
    trades = int(p.get("trades", 0))

    if pnl > 1000:
        grade, headline = "great", "Your book is well ahead."
    elif pnl > 0:
        grade, headline = "good", "You're up on the session."
    elif trades == 0:
        grade, headline = "mixed", "You haven't traded the live book yet."
    else:
        grade, headline = "mixed", "You're behind on the live book."

    stats = [
        {"label": "P&L", "value": _money(pnl)},
        {"label": "Trades", "value": str(trades)},
    ]

    notes: List[Note] = []
    if trades == 0:
        notes.append({"kind": "tip", "text":
                      "The market maker is always quoting, so there's a counterparty whenever "
                      "you want one — place a resting order inside the spread and see if it "
                      "gets filled."})
    else:
        if pnl < 0:
            notes.append({"kind": "gap", "text":
                          "Down on the session. Check whether you're crossing the spread to "
                          "get in — paying the offer every time is a cost you can avoid by "
                          "resting orders instead."})
        notes.append({"kind": "tip", "text":
                      "Your P&L here is marked against the live reference price, so an open "
                      "position keeps moving after you stop watching."})

    return _fb(headline, grade, stats, notes)


ANALYSERS: Dict[str, Callable[[Dict[str, Any]], Dict[str, Any]]] = {
    "crash_ledger": _crash_ledger,
    "mental_math": _mental_math,
    "fiveos": _fiveos,
    "headline": _headline,
    "risks": _risks,
    "poker_auction": _poker_auction,
    "market_sim_py": _bot_run,
    "market_sim": _market_sim,
}


def analyse(mode: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Coach one finished game. Never raises."""
    fn = ANALYSERS.get(mode)
    if fn is None:
        return _fb("Game complete.", "mixed", [], [])
    try:
        return fn(payload or {})
    except Exception:
        log.warning("feedback failed for mode=%s", mode, exc_info=True)
        return _fb("Game complete.", "mixed", [], [])
