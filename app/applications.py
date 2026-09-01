"""
Applications to the Quant Bootcamp and Quant Analyst programmes.
================================================================

A general-public or general-member account applies to one of the two quant
programmes, puts an up-to-date CV on their profile, and then sits a 15-minute
assessment. An admin reads the results ranked by the numerical score.

The shape of the assessment:

* **One sitting, 15 minutes, on the server's clock.** ``started_at`` is
  stamped once and the deadline is derived from it, so closing the tab,
  reloading, or signing in on another device does not buy more time. There is
  no pause and no second attempt — the point of "one sitting" is that the
  window is the same for everyone regardless of what they do with it.
* **Five minutes of writing, then ten minutes of numbers.** The written
  question comes first, on its own 5-minute clock; submitting it early moves
  straight on to the numerical section rather than banking the leftover time,
  so nobody is rewarded for rushing the essay. The numerical section is 20
  questions at 30 seconds each, which is exactly the remaining ten minutes.
* **Whole-number answers only.** Every numerical question is written so the
  answer is a plain integer — a count of outcomes, an expected value that
  comes out whole, a "1 in N" probability, the next term of a sequence, or
  (for a multiple-choice quant-concepts question) the option number. That
  makes grading exact instead of tolerance-based, and it means a candidate
  never loses a mark to rounding or to how they chose to write a fraction.
* **A random draw, not a fixed set.** Each topic carries an even four easy,
  four medium and four hard questions (see :data:`QUESTION_BANK`); a paper
  draws its slots randomly within each difficulty tier and shuffles the
  result, so the difficulty spread is the same for every candidate but the
  actual questions differ. The mix of topics itself depends on the
  programme applied for — see :data:`PAPER_MIX`. Quant Bootcamp keeps the
  original probability/expectation/pattern set; Quant Analyst trims
  pattern-finding to make room for basic quant-concept questions.
* **No AI.** Said plainly on the gate, acknowledged with a tick before the
  clock starts, and backed by the clocks themselves: 30 seconds is enough to
  think through one of these questions and not enough to consult a chatbot.
  Paste into the written box is blocked and leaving the page is counted, both
  surfaced to the reviewer as signals — never as an automatic disqualification,
  because a dropped connection looks the same as a second monitor.

Everything is resolved on read, the same approach ``interview_oa`` uses: the
state endpoint expires the written section, times out an unanswered question,
and force-finishes a session past its deadline. A candidate who closes the tab
at the buzzer still gets an honest, un-strandable result.

Results are admin-only. A candidate sees a plain "submitted" screen, never a
score, so applicants can't compare notes on which questions they nailed.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import io
import logging
import os
import random
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app import db as db_module
from app import mailer
from app import membership as mb
from app.admin import require_admin
from app.auth import current_user
from app.models import User

log = logging.getLogger("uvicorn.error")

router = APIRouter(prefix="/apply", tags=["applications"])
BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

COLLECTION = "applications"

# Where email CTAs point. Defaults to the production domain so a deploy with
# no override still sends usable links; set APP_BASE_URL to override in any
# other environment.
BASE_URL = os.getenv("APP_BASE_URL", "https://alphabook.uk").rstrip("/")

# Oxford addresses are always under this suffix, whatever the college or
# department subdomain — jo@some-college.ox.ac.uk, jo@some-dept.ox.ac.uk,
# jo@admin.ox.ac.uk all match; a lookalike like jo@ox.ac.uk.evil.com does not,
# because the check is anchored to the end of the string.
_OXFORD_SUFFIX = "ox.ac.uk"


def is_oxford_email(addr: Optional[str]) -> bool:
    addr = (addr or "").strip().lower()
    return "@" in addr and (addr == _OXFORD_SUFFIX or addr.endswith("." + _OXFORD_SUFFIX)
                             or addr.endswith("@" + _OXFORD_SUFFIX))


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _valid_oxford_email(addr: Optional[str]) -> bool:
    addr = (addr or "").strip()
    return bool(_EMAIL_RE.match(addr)) and is_oxford_email(addr)


async def require_reviewer(user: User = Depends(current_user)) -> User:
    """
    Anyone who can read the review page and score applicants: admins, and
    every Quant Analyst member.

    Deliberately wider than the accept/shortlist/reject decision itself
    (still :func:`app.admin.require_admin`) — reading CVs and scoring them is
    exactly what Quant Analyst members are there to do, several of them
    independently, so the average means something. Making the actual call is
    still a smaller, named decision.
    """
    if user.is_admin:
        return user
    doc = await db_module.db.collection("users").document(str(user.id)).get()
    data = doc.to_dict() if doc.exists else {}
    if mb.membership_of(data) == mb.M_QUANT_ANALYST:
        return user
    raise HTTPException(403, "The application review page is open to Quant Analyst members and admins")


# ── Clocks (seconds) ─────────────────────────────────────────────────────────
WRITTEN_SECONDS = 5 * 60          # the essay's own limit
SECONDS_PER_QUESTION = 30         # each numerical question
NUMERICAL_QUESTIONS = 20          # 20 x 30s = the remaining ten minutes
SESSION_SECONDS = WRITTEN_SECONDS + NUMERICAL_QUESTIONS * SECONDS_PER_QUESTION  # 900
ANSWER_GRACE = 3                  # network slack before a late /answer is ignored

WRITTEN_PROMPT = (
    "Why do you want to join Alpha Fund, and why you? "
    "Tell us what draws you to the programme and what you would bring to it."
)

# Statuses an application moves through, in order.
S_CV = "cv"                    # applied; waiting on an up-to-date CV
S_OA_READY = "oa_ready"        # CV on file; assessment not started
S_OA_ACTIVE = "oa_active"      # clock running
S_SUBMITTED = "submitted"      # assessment finished, awaiting a decision
S_SHORTLISTED = "shortlisted"  # invited to interview; not yet a final decision
S_ACCEPTED = "accepted"
S_REJECTED = "rejected"

DECIDED = {S_ACCEPTED, S_REJECTED}
# Statuses a reviewer can attach a CV/written score to — once the CV and
# written response actually exist to be read, through to a final decision.
SCORABLE = {S_SUBMITTED, S_SHORTLISTED, S_ACCEPTED, S_REJECTED}

# Reviewer scores are on a 1-10 scale — familiar from any CV-review process
# and coarse enough that an average across several reviewers means something.
SCORE_MIN, SCORE_MAX = 1, 10


# ── Question bank ─────────────────────────────────────────────────────────────
# Every answer is a whole number, so grading is an exact match. Probability
# questions are framed as a count of outcomes or as "1 in N" precisely so the
# answer stays an integer; a multiple-choice question's answer is the option
# number. "note" is the one-line justification shown only to the reviewer.
#
# Each topic carries exactly four "easy", four "medium" and four "hard"
# questions — the even split is what lets a paper draw randomly within a
# topic and still land on a predictable difficulty spread every time (see
# build_paper). The easy tier is deliberately not the easiest imaginable
# version of each topic: a plain complement or a memorised card count reads
# as "recall" rather than "reasoning", so easy here still means one genuine
# step of combinatorics or expectation, just the shortest one in the topic.
DIFFICULTIES: List[str] = ["easy", "medium", "hard"]

QUESTION_BANK: List[Dict[str, Any]] = [
    # ── Probability ──────────────────────────────────────────────────────────
    {"id": "p_heart", "kind": "probability", "difficulty": "easy",
     "prompt": "You draw one card from a standard 52-card deck. The probability it is a heart is 1 in N. What is N?",
     "answer": 4, "note": "13/52 = 1/4"},
    {"id": "p_dice_doubles", "kind": "probability", "difficulty": "easy",
     "prompt": "Two fair six-sided dice are rolled. Of the 36 equally likely outcomes, how many show the same number on both dice?",
     "answer": 6, "note": "(1,1)…(6,6)"},
    {"id": "p_coin3_2h", "kind": "probability", "difficulty": "easy",
     "prompt": "A fair coin is flipped 3 times. Of the 8 equally likely outcomes, how many have exactly 2 heads?",
     "answer": 3, "note": "C(3,2) = 3"},
    {"id": "p_two_coin_atleast1", "kind": "probability", "difficulty": "easy",
     "prompt": "Two fair coins are flipped. Of the 4 equally likely outcomes, how many have at least one head?",
     "answer": 3, "note": "4 - 1 all-tails"},

    {"id": "p_dice_sum7", "kind": "probability", "difficulty": "medium",
     "prompt": "Two fair six-sided dice are rolled. Of the 36 equally likely outcomes, how many give a sum of 7?",
     "answer": 6, "note": "(1,6)…(6,1)"},
    {"id": "p_two_red", "kind": "probability", "difficulty": "medium",
     "prompt": "A jar holds 3 red and 2 blue balls. You draw 2 without replacement. Of the 10 possible pairs, how many are both red?",
     "answer": 3, "note": "C(3,2) = 3"},
    {"id": "p_two_kings", "kind": "probability", "difficulty": "medium",
     "prompt": "How many different 2-card hands from a standard deck consist of two kings? (Order does not matter.)",
     "answer": 6, "note": "C(4,2) = 6"},
    {"id": "p_coin3_atleast1", "kind": "probability", "difficulty": "medium",
     "prompt": "A fair coin is flipped 3 times. In how many of the 8 equally likely outcomes is there at least one head?",
     "answer": 7, "note": "8 - 1 all-tails"},

    {"id": "p_dice_over9", "kind": "probability", "difficulty": "hard",
     "prompt": "Two fair six-sided dice are rolled. Of the 36 equally likely outcomes, how many give a sum greater than 9?",
     "answer": 6, "note": "sums 10, 11, 12 → 3 + 2 + 1"},
    {"id": "p_coin5_3h", "kind": "probability", "difficulty": "hard",
     "prompt": "A fair coin is flipped 5 times. In how many of the 32 equally likely outcomes are there exactly 3 heads?",
     "answer": 10, "note": "C(5,3) = 10"},
    {"id": "p_alphabetical", "kind": "probability", "difficulty": "hard",
     "prompt": "Four distinct letters are shuffled into a random order. The probability they land in alphabetical order is 1 in N. What is N?",
     "answer": 24, "note": "4! = 24 orderings, 1 of them sorted"},
    {"id": "p_coin4_more_heads", "kind": "probability", "difficulty": "hard",
     "prompt": "A fair coin is flipped 4 times. In how many of the 16 equally likely outcomes do heads outnumber tails?",
     "answer": 5, "note": "C(4,3) + C(4,4) = 4 + 1"},

    # ── Expectation ──────────────────────────────────────────────────────────
    {"id": "e_two_dice_sum", "kind": "expectation", "difficulty": "easy",
     "prompt": "You roll two fair six-sided dice. What is the expected value of their sum?",
     "answer": 7, "note": "2 x 3.5"},
    {"id": "e_eight_coins", "kind": "expectation", "difficulty": "easy",
     "prompt": "You flip 8 fair coins. What is the expected number of heads?",
     "answer": 4, "note": "np = 8 x 0.5"},
    {"id": "e_die_thirty", "kind": "expectation", "difficulty": "easy",
     "prompt": "A fair six-sided die is rolled once. You win £30 if it shows a 6 and nothing otherwise. What are your expected winnings, in pounds?",
     "answer": 5, "note": "30 x 1/6"},
    {"id": "e_card_ace_52", "kind": "expectation", "difficulty": "easy",
     "prompt": "You draw one card from a standard 52-card deck. You win £52 if it's an ace, and nothing otherwise. What are your expected winnings, in pounds?",
     "answer": 4, "note": "P(ace) = 4/52, 52 x 4/52 = 4"},

    {"id": "e_flips_to_heads", "kind": "expectation", "difficulty": "medium",
     "prompt": "You flip a fair coin repeatedly until it lands heads. What is the expected number of flips?",
     "answer": 2, "note": "1/p with p = 1/2"},
    {"id": "e_rolls_to_six", "kind": "expectation", "difficulty": "medium",
     "prompt": "You roll a fair six-sided die repeatedly until you see a 6. What is the expected number of rolls?",
     "answer": 6, "note": "1/p with p = 1/6"},
    {"id": "e_biased_twelve", "kind": "expectation", "difficulty": "medium",
     "prompt": "A biased coin lands heads 1/4 of the time. You flip it 12 times. What is the expected number of heads?",
     "answer": 3, "note": "np = 12 x 1/4"},
    {"id": "e_lottery", "kind": "expectation", "difficulty": "medium",
     "prompt": "A lottery ticket pays £1000 with probability 1/500 and nothing otherwise. What is its expected value, in pounds?",
     "answer": 2, "note": "1000/500"},

    {"id": "e_second_heads", "kind": "expectation", "difficulty": "hard",
     "prompt": "You flip a fair coin repeatedly until it has landed heads twice. What is the expected number of flips?",
     "answer": 4, "note": "r/p with r = 2, p = 1/2"},
    {"id": "e_hypergeometric", "kind": "expectation", "difficulty": "hard",
     "prompt": "A box holds 10 balls, 4 of them white. You draw 5 without replacement. What is the expected number of white balls drawn?",
     "answer": 2, "note": "nK/N = 5 x 4/10"},
    {"id": "e_draws_to_ace", "kind": "expectation", "difficulty": "hard",
     "prompt": "You draw a card from a full deck, note it, and replace it, repeating until you draw an ace. What is the expected number of draws?",
     "answer": 13, "note": "1/p with p = 4/52"},
    {"id": "e_rolls_over_four", "kind": "expectation", "difficulty": "hard",
     "prompt": "You roll a fair six-sided die repeatedly until it shows a number greater than 4. What is the expected number of rolls?",
     "answer": 3, "note": "1/p with p = 2/6"},

    # ── Pattern finding ──────────────────────────────────────────────────────
    {"id": "n_squares", "kind": "pattern", "difficulty": "easy",
     "prompt": "What comes next?   1, 4, 9, 16, 25, ?",
     "answer": 36, "note": "square numbers"},
    {"id": "n_doubling", "kind": "pattern", "difficulty": "easy",
     "prompt": "What comes next?   3, 6, 12, 24, 48, ?",
     "answer": 96, "note": "x2 each step"},
    {"id": "n_triangular", "kind": "pattern", "difficulty": "easy",
     "prompt": "What comes next?   1, 3, 6, 10, 15, ?",
     "answer": 21, "note": "triangular numbers"},
    {"id": "n_powers_of_three", "kind": "pattern", "difficulty": "easy",
     "prompt": "What comes next?   81, 27, 9, 3, ?",
     "answer": 1, "note": "divide by 3"},

    {"id": "n_oblong", "kind": "pattern", "difficulty": "medium",
     "prompt": "What comes next?   2, 6, 12, 20, 30, ?",
     "answer": 42, "note": "n(n+1)"},
    {"id": "n_fib", "kind": "pattern", "difficulty": "medium",
     "prompt": "What comes next?   1, 1, 2, 3, 5, 8, ?",
     "answer": 13, "note": "Fibonacci"},
    {"id": "n_primes", "kind": "pattern", "difficulty": "medium",
     "prompt": "What comes next?   2, 3, 5, 7, 11, 13, ?",
     "answer": 17, "note": "primes"},
    {"id": "n_cubes", "kind": "pattern", "difficulty": "medium",
     "prompt": "What comes next?   1, 8, 27, 64, 125, ?",
     "answer": 216, "note": "cubes"},

    {"id": "n_factorial", "kind": "pattern", "difficulty": "hard",
     "prompt": "What comes next?   1, 2, 6, 24, 120, ?",
     "answer": 720, "note": "n!"},
    {"id": "n_2n_plus_1", "kind": "pattern", "difficulty": "hard",
     "prompt": "What comes next?   2, 5, 11, 23, 47, ?",
     "answer": 95, "note": "double then add 1"},
    {"id": "n_lazy_caterer", "kind": "pattern", "difficulty": "hard",
     "prompt": "What comes next?   1, 2, 4, 7, 11, 16, ?",
     "answer": 22, "note": "gaps grow by 1"},
    {"id": "n_zigzag", "kind": "pattern", "difficulty": "hard",
     "prompt": "What comes next?   9, 7, 10, 8, 11, 9, ?",
     "answer": 12, "note": "-2 then +3, alternating"},

    # ── Quant concepts (Quant Analyst only) ────────────────────────────────────
    # Basic finance/quant vocabulary and one-step calculations — multiple
    # choice (answer = option number) or a plain arithmetic answer, nothing
    # requiring a calculator or prior modelling experience.
    {"id": "qc_delta_def", "kind": "quant concepts", "difficulty": "easy",
     "prompt": ("Which of these best describes an option's delta? "
                "1) Its time decay per day  2) Its price sensitivity to a $1 move in the underlying  "
                "3) Its sensitivity to volatility  4) Its sensitivity to interest rates "
                "— enter the option number."),
     "answer": 2, "note": "delta = d(option price)/d(underlying price)"},
    {"id": "qc_long_profit", "kind": "quant concepts", "difficulty": "easy",
     "prompt": ("A trader is 'long' a stock. They profit when the price does what? "
                "1) Falls  2) Rises  3) Stays perfectly flat  4) Becomes illiquid "
                "— enter the option number."),
     "answer": 2, "note": "long = owns the asset, wants it to rise"},
    {"id": "qc_position_value", "kind": "quant concepts", "difficulty": "easy",
     "prompt": "A stock is priced at £50. You buy 100 shares. What is your total position value, in pounds?",
     "answer": 5000, "note": "50 x 100"},
    {"id": "qc_pct_return", "kind": "quant concepts", "difficulty": "easy",
     "prompt": "You buy a stock at £20 and sell it at £26. What is your percentage return, to the nearest whole percent?",
     "answer": 30, "note": "6/20 = 30%"},

    {"id": "qc_delta_calc", "kind": "quant concepts", "difficulty": "medium",
     "prompt": "A call option has delta 0.5. If the underlying stock rises by £4, what is the approximate change in the option's price, in pounds?",
     "answer": 2, "note": "0.5 x 4"},
    {"id": "qc_gamma_def", "kind": "quant concepts", "difficulty": "medium",
     "prompt": ("Which Greek measures the sensitivity of an option's delta to a $1 move in the underlying? "
                "1) Vega  2) Gamma  3) Theta  4) Rho — enter the option number."),
     "answer": 2, "note": "gamma = d(delta)/d(underlying price)"},
    {"id": "qc_short_pnl", "kind": "quant concepts", "difficulty": "medium",
     "prompt": "You short-sell a stock at £30 and buy it back at £22. What is your profit per share, in pounds?",
     "answer": 8, "note": "30 - 22"},
    {"id": "qc_delta_calc2", "kind": "quant concepts", "difficulty": "medium",
     "prompt": "A call option has delta 0.4. The stock rises by £5. What is the approximate change in the option's price, in pounds, to the nearest whole pound?",
     "answer": 2, "note": "0.4 x 5 = 2"},

    {"id": "qc_sharpe", "kind": "quant concepts", "difficulty": "hard",
     "prompt": ("A portfolio has a Sharpe ratio of 1.5 and an annual standard deviation of 20%. "
                "If the risk-free rate is 2%, what is the portfolio's expected annual return, "
                "to the nearest whole percent?"),
     "answer": 32, "note": "R = Sharpe x sigma + Rf = 1.5x20 + 2"},
    {"id": "qc_putcall", "kind": "quant concepts", "difficulty": "hard",
     "prompt": ("Under put-call parity, if the strike and expiry are the same and the underlying pays no "
                "dividends, what happens to a call's price relative to a put's as the stock price rises, "
                "all else equal? 1) Call rises, put falls  2) Both rise  3) Both fall  "
                "4) Call falls, put rises — enter the option number."),
     "answer": 1, "note": "call gains intrinsic value, put loses it"},
    {"id": "qc_duration", "kind": "quant concepts", "difficulty": "hard",
     "prompt": ("A bond has a modified duration of 5. If interest rates rise by 1 percentage point, "
                "what is the approximate percentage fall in the bond's price, to the nearest whole percent?"),
     "answer": 5, "note": "-duration x rate change"},
    {"id": "qc_replication", "kind": "quant concepts", "difficulty": "hard",
     "prompt": ("A stock and a risk-free bond are combined to exactly replicate an option's payoff. "
                "This is an example of which concept? 1) Put-call parity  2) Risk-neutral valuation  "
                "3) Delta hedging / replication  4) Arbitrage-free bootstrapping — enter the option number."),
     "answer": 3, "note": "replicating portfolio argument"},
]
QUESTION_BY_ID = {q["id"]: q for q in QUESTION_BANK}

# How many questions of each topic go into a paper, by programme. Both sum to
# NUMERICAL_QUESTIONS, so the sitting is the same length either way. Quant
# Bootcamp keeps the original probability/expectation/pattern mix untouched.
# Quant Analyst keeps that same probability/expectation weight — the harder
# math is exactly as present as it is for Bootcamp — and takes the room for
# quant concepts entirely out of pattern-finding, which is the least
# job-relevant of the three for that programme.
PAPER_MIX: Dict[str, Dict[str, int]] = {
    mb.M_QUANT_BOOTCAMP: {"probability": 7, "expectation": 6, "pattern": 7},
    mb.M_QUANT_ANALYST: {"probability": 7, "expectation": 6, "pattern": 3, "quant concepts": 4},
}


class StartApplication(BaseModel):
    programme: str
    # Only required when the account's own email isn't already an Oxford
    # address; see is_oxford_email() and the /apply/start handler.
    oxford_email: Optional[str] = None
    # Only required for a General public applicant — see the eligibility
    # check in the /apply/start handler.
    confirms_oxford_student: bool = False


class ReviewScore(BaseModel):
    cv_score: Optional[int] = None
    written_score: Optional[int] = None
    note: Optional[str] = None


class RemindRequest(BaseModel):
    note: Optional[str] = None


class WrittenSubmit(BaseModel):
    text: str = ""
    final: bool = False


class AnswerRequest(BaseModel):
    index: int
    value: str = ""


class FlagEvent(BaseModel):
    kind: str          # "paste" | "left_page"


class Decision(BaseModel):
    decision: str      # "shortlist" | "accept" | "reject"
    note: Optional[str] = None


# ── Time helpers ──────────────────────────────────────────────────────────────

def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _as_utc(value: Any) -> Optional[dt.datetime]:
    """Coerce whatever Firestore hands back into an aware UTC datetime."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = dt.datetime.fromisoformat(value)
        except ValueError:
            return None
    if not isinstance(value, dt.datetime):
        return None
    return value.replace(tzinfo=dt.timezone.utc) if value.tzinfo is None else value


def _left(since: Any, limit_s: float) -> float:
    """Seconds remaining on a clock that started at `since` and runs `limit_s`."""
    started = _as_utc(since)
    if started is None:
        return 0.0
    return max(0.0, limit_s - (_now() - started).total_seconds())


def _overdue(since: Any, limit_s: float) -> float:
    started = _as_utc(since)
    if started is None:
        return 0.0
    return max(0.0, (_now() - started).total_seconds() - limit_s)


# ── Answer parsing and grading ────────────────────────────────────────────────

def parse_answer(raw: Optional[str]) -> Optional[int]:
    """
    Read a candidate's typed answer as a whole number, or None.

    Every question in the bank has an integer answer and the input box says so,
    so anything that is not a plain integer is a non-answer rather than
    something to round: "3.5" on a question whose answer is 3 is a different
    claim, not a near miss. Commas and a leading + are tolerated because they
    are typing habits, not answers.
    """
    s = (raw or "").strip().replace(",", "").replace(" ", "")
    if s.startswith("+"):
        s = s[1:]
    if not s:
        return None
    negative = s.startswith("-")
    digits = s[1:] if negative else s
    if not digits.isdigit():
        return None
    value = int(digits)
    return -value if negative else value


def grade(question: Dict[str, Any], parsed: Optional[int]) -> bool:
    return parsed is not None and parsed == question["answer"]


def _even_split(n: int, parts: int = len(DIFFICULTIES)) -> List[int]:
    """n as `parts` whole-number shares, as equal as possible (extras go first)."""
    base, extra = divmod(n, parts)
    return [base + (1 if i < extra else 0) for i in range(parts)]


def build_paper(programme: str, rng: Optional[random.Random] = None) -> List[str]:
    """
    Pick one paper for this programme: each topic's slots are split as evenly
    as possible across easy/medium/hard, a question is drawn at random within
    each slice, and the whole thing is shuffled together — so every sitting
    has a predictable difficulty spread but a different set of questions.
    """
    rng = rng or random
    mix = PAPER_MIX.get(programme, PAPER_MIX[mb.M_QUANT_BOOTCAMP])
    chosen: List[str] = []
    for kind, count in mix.items():
        for difficulty, want in zip(DIFFICULTIES, _even_split(count)):
            pool = [q["id"] for q in QUESTION_BANK
                    if q["kind"] == kind and q["difficulty"] == difficulty]
            rng.shuffle(pool)
            chosen.extend(pool[:want])
    rng.shuffle(chosen)
    return chosen


# ── Storage ───────────────────────────────────────────────────────────────────

async def _load(user_id: str) -> Optional[dict]:
    doc = await db_module.db.collection(COLLECTION).document(user_id).get()
    return doc.to_dict() if doc.exists else None


async def _save(user_id: str, application: dict) -> None:
    await db_module.db.collection(COLLECTION).document(user_id).set(application)


async def _user_data(user_id: str) -> Dict[str, Any]:
    doc = await db_module.db.collection("users").document(user_id).get()
    return (doc.to_dict() or {}) if doc.exists else {}


# ── The assessment state machine ─────────────────────────────────────────────

def _score(oa: dict) -> Dict[str, Any]:
    correct = sum(1 for a in oa.get("answers", []) if a["correct"])
    total = len(oa.get("question_ids", [])) or NUMERICAL_QUESTIONS
    return {"correct": correct, "total": total,
            "pct": round(100 * correct / total, 1) if total else 0.0}


def _record_answer(oa: dict, raw: Optional[str], timed_out: bool) -> None:
    index = oa["current_index"]
    qid = oa["question_ids"][index]
    question = QUESTION_BY_ID[qid]
    parsed = parse_answer(raw) if raw is not None else None
    served_at = _as_utc(oa.get("question_started_at"))

    if timed_out:
        used = float(SECONDS_PER_QUESTION)
        # The next question's clock starts the moment this one ran out, not
        # whenever the server got round to noticing. Otherwise a candidate who
        # closes the tab across several questions comes back to find each of
        # them waiting with a full 30 seconds on it.
        next_started = (served_at + dt.timedelta(seconds=SECONDS_PER_QUESTION)
                        if served_at else _now())
    else:
        used = SECONDS_PER_QUESTION - _left(served_at, SECONDS_PER_QUESTION)
        next_started = _now()

    oa["answers"].append({
        "question_id": qid,
        "raw": (raw or "")[:40],
        "parsed": parsed,
        "correct": grade(question, parsed),
        "timed_out": timed_out,
        "time_taken_s": round(min(max(used, 0.0), SECONDS_PER_QUESTION), 1),
    })
    oa["current_index"] = index + 1
    if oa["current_index"] < len(oa["question_ids"]):
        oa["question_started_at"] = next_started


def _finish(application: dict, reason: str) -> None:
    """Close the assessment out, filling any unreached questions as timeouts."""
    oa = application["oa"]
    if oa.get("section") == "written":
        _close_written(oa)
    while oa["section"] == "numerical" and oa["current_index"] < len(oa["question_ids"]):
        _record_answer(oa, raw=None, timed_out=True)
    oa["section"] = "done"
    oa["score"] = _score(oa)
    oa["finished_at"] = _now()
    oa["finish_reason"] = reason
    application["status"] = S_SUBMITTED
    application["submitted_at"] = _now()


def _close_written(oa: dict) -> None:
    """Bank whatever is in the written box and open the numerical section."""
    written = oa.setdefault("written", {})
    written.setdefault("text", "")
    written["submitted_at"] = _now()
    written["seconds_used"] = round(
        min(WRITTEN_SECONDS - _left(oa.get("started_at"), WRITTEN_SECONDS), WRITTEN_SECONDS), 1)
    written["word_count"] = len(written["text"].split())
    oa["section"] = "numerical"
    oa["numerical_started_at"] = _now()
    oa["question_started_at"] = _now()


def resolve(application: dict) -> bool:
    """
    Bring a stored application up to date with the wall clock.

    Called on every read. It expires the written section, times out an
    unanswered question, and force-finishes a session past its 15 minutes —
    so a candidate who closes the tab at the buzzer still gets an honest
    result, and one who leaves it open all afternoon does not get an
    afternoon's worth of thinking time.

    Returns True if anything changed and the document needs writing back.
    """
    if application.get("status") != S_OA_ACTIVE:
        return False
    oa = application.get("oa") or {}
    changed = False

    # The hard stop. Nothing below it can extend the sitting.
    if _left(oa.get("started_at"), SESSION_SECONDS) <= 0:
        _finish(application, "session_expired")
        return True

    if oa.get("section") == "written":
        if _left(oa.get("started_at"), WRITTEN_SECONDS) <= 0:
            _close_written(oa)
            changed = True
        else:
            return False

    if oa.get("section") == "numerical":
        # A question can expire while the tab is closed, and the next read may
        # land several questions later; roll forward until the clock catches up.
        while (oa["current_index"] < len(oa["question_ids"])
               and _left(oa.get("question_started_at"), SECONDS_PER_QUESTION) <= 0
               and _overdue(oa.get("question_started_at"), SECONDS_PER_QUESTION) >= ANSWER_GRACE):
            _record_answer(oa, raw=None, timed_out=True)
            changed = True
        if oa["current_index"] >= len(oa["question_ids"]):
            _finish(application, "completed")
            return True

    return changed


async def _send_submission_confirmation(application: dict) -> None:
    """The one email every candidate gets: proof their assessment went in."""
    to = application.get("oxford_email") or application.get("email")
    if not to:
        return
    name = application.get("full_name") or application.get("username") or "there"
    programme = application.get("programme", "the programme")
    await mailer.send_email(
        to=to,
        subject=f"Alpha Fund — your {programme} application is in",
        title="Application received",
        body_html=(
            f"<p>Hi {name},</p>"
            f"<p>This confirms your CV and assessment for <strong>{programme}</strong> "
            f"have both been submitted. There is nothing else to do — the committee "
            f"reviews complete applications, and we will be in touch with a decision.</p>"
        ),
    )


async def _resolve_and_notify(uid: str, application: dict) -> bool:
    """
    ``resolve()``, plus the one-time email that fires the moment a sitting
    actually closes out.

    Centralised here rather than in ``resolve()`` itself so the state machine
    stays a pure function over a dict; every candidate-facing endpoint that
    can cause a submission to complete calls this instead of calling
    ``resolve()`` and ``_save()`` separately. The ``confirmation_sent_at``
    flag makes it idempotent — ``/apply/state`` is polled once a second while
    the OA is live, so this runs far more often than the submission itself
    changes state.
    """
    changed = resolve(application)
    if changed and application["status"] == S_SUBMITTED and not application.get("confirmation_sent_at"):
        await _send_submission_confirmation(application)
        application["confirmation_sent_at"] = _now()
    if changed:
        await _save(uid, application)
    return changed


async def _finish_and_persist(uid: str, application: dict, reason: str) -> None:
    """The direct-completion counterpart to ``_resolve_and_notify``: used where
    the last numerical answer finishes the sitting on the spot, rather than
    the clock catching up to it on a later read."""
    _finish(application, reason)
    if not application.get("confirmation_sent_at"):
        await _send_submission_confirmation(application)
        application["confirmation_sent_at"] = _now()
    await _save(uid, application)


def _question_view(oa: dict) -> Optional[Dict[str, Any]]:
    index = oa["current_index"]
    if index >= len(oa["question_ids"]):
        return None
    q = QUESTION_BY_ID[oa["question_ids"][index]]
    return {
        "index": index,
        "total": len(oa["question_ids"]),
        "kind": q["kind"],
        "prompt": q["prompt"],
        "seconds_left": round(_left(oa.get("question_started_at"), SECONDS_PER_QUESTION), 1),
        "seconds_per_question": SECONDS_PER_QUESTION,
    }


def _oa_view(application: dict) -> Dict[str, Any]:
    oa = application["oa"]
    out: Dict[str, Any] = {
        "section": oa["section"],
        "session_seconds_left": round(_left(oa.get("started_at"), SESSION_SECONDS), 1),
        "session_seconds": SESSION_SECONDS,
    }
    if oa["section"] == "written":
        out["written"] = {
            "prompt": WRITTEN_PROMPT,
            "text": (oa.get("written") or {}).get("text", ""),
            "seconds_left": round(_left(oa.get("started_at"), WRITTEN_SECONDS), 1),
            "seconds_total": WRITTEN_SECONDS,
        }
    elif oa["section"] == "numerical":
        out["question"] = _question_view(oa)
    return out


# ── Pages ─────────────────────────────────────────────────────────────────────

@router.get("", include_in_schema=False)
async def apply_page(request: Request):
    return templates.TemplateResponse("apply.html", {
        "request": request,
        "app_name": "AlphaBook",
        "written_minutes": WRITTEN_SECONDS // 60,
        "numerical_questions": NUMERICAL_QUESTIONS,
        "seconds_per_question": SECONDS_PER_QUESTION,
        "session_minutes": SESSION_SECONDS // 60,
    })


# ── Candidate API ─────────────────────────────────────────────────────────────

@router.get("/state")
async def state(user: User = Depends(current_user)):
    """Everything the apply page needs: eligibility, CV, and the live clocks."""
    uid = str(user.id)
    data = await _user_data(uid)
    application = await _load(uid)

    account_email = data.get("email") or ""
    out: Dict[str, Any] = {
        "eligible": mb.can_apply({**data, "is_admin": user.is_admin}),
        "membership": mb.membership_of(data),
        "programmes": list(mb.APPLY_PROGRAMMES),
        "cv_uploaded": bool(data.get("cv_blob_path")),
        "full_name": data.get("full_name") or "",
        "graduation_year": data.get("graduation_year"),
        "written_prompt": WRITTEN_PROMPT,
        "account_email": account_email,
        # Whether the choose-programme step needs to ask for an Oxford email:
        # false when the account itself signed up with one.
        "needs_oxford_email": not is_oxford_email(account_email),
        "rules": {
            "session_seconds": SESSION_SECONDS,
            "written_seconds": WRITTEN_SECONDS,
            "seconds_per_question": SECONDS_PER_QUESTION,
            "numerical_questions": NUMERICAL_QUESTIONS,
        },
    }

    if application is None:
        out["status"] = "none"
        return out

    await _resolve_and_notify(uid, application)

    out["status"] = application["status"]
    out["programme"] = application.get("programme")
    out["oxford_email"] = application.get("oxford_email") or ""
    out["applied_at"] = application.get("created_at")
    if application["status"] == S_OA_ACTIVE:
        out["oa"] = _oa_view(application)
    elif application["status"] in (S_SUBMITTED, S_SHORTLISTED, *DECIDED):
        out["submitted_at"] = application.get("submitted_at")
        out["decision"] = application["status"] if application["status"] in DECIDED else None
    return out


def _resolve_oxford_email(account_email: str, provided: Optional[str]) -> str:
    """
    The Oxford address to send this candidate's updates to.

    An account that already signed up with an Oxford address uses that one,
    with nothing to type. Everyone else — a personal Gmail, most sign-ups
    before the college terms start — has to name one explicitly, because
    that's the only address the committee is willing to send decisions to.
    """
    if is_oxford_email(account_email):
        return account_email.strip().lower()
    candidate = (provided or "").strip().lower()
    if not _valid_oxford_email(candidate):
        raise HTTPException(
            400,
            "Enter the Oxford email address (ending in ox.ac.uk) we should use "
            "for updates about your application.",
        )
    return candidate


@router.post("/start")
async def start_application(req: StartApplication, user: User = Depends(current_user)):
    """Open an application to one of the quant programmes."""
    uid = str(user.id)
    data = await _user_data(uid)
    if not mb.can_apply({**data, "is_admin": user.is_admin}):
        raise HTTPException(
            403,
            "Applications are open to general accounts. You are already on a "
            "programme, or hold a recruiter or host role.",
        )
    if req.programme not in mb.APPLY_PROGRAMMES:
        raise HTTPException(400, f"Choose one of: {', '.join(mb.APPLY_PROGRAMMES)}")

    # General public applicants aren't Alpha Fund members yet, so nothing else
    # on the account vouches for them being current Oxford students — ask
    # directly. A General Alpha Fund member has already cleared that bar to
    # get their membership, so there's nothing to re-confirm.
    membership = mb.membership_of(data)
    if membership == mb.M_PUBLIC and not req.confirms_oxford_student:
        raise HTTPException(
            400,
            "Confirm you are currently studying at the University of Oxford to continue.",
        )

    oxford_email = _resolve_oxford_email(data.get("email") or "", req.oxford_email)

    existing = await _load(uid)
    if existing is not None:
        # Switching programme (or fixing the Oxford address) before the
        # assessment starts is free; afterwards the paper has already been
        # sat and both are fixed.
        if existing["status"] not in (S_CV, S_OA_READY):
            raise HTTPException(400, "Your application is already under way")
        existing["programme"] = req.programme
        existing["oxford_email"] = oxford_email
        if membership == mb.M_PUBLIC:
            existing["confirmed_oxford_student"] = True
        await _save(uid, existing)
        return {"ok": True, "status": existing["status"], "programme": req.programme}

    application = {
        "user_id": uid,
        "username": user.username,
        "full_name": data.get("full_name") or "",
        "email": data.get("email") or "",
        "oxford_email": oxford_email,
        "applicant_category": membership,   # "General public" or "General Alpha Fund member"
        "confirmed_oxford_student": membership == mb.M_PUBLIC,
        "programme": req.programme,
        "status": S_OA_READY if data.get("cv_blob_path") else S_CV,
        "created_at": _now(),
        "flags": {"paste": 0, "left_page": 0},
    }
    await _save(uid, application)
    return {"ok": True, "status": application["status"], "programme": req.programme}


@router.post("/cv-confirm")
async def confirm_cv(user: User = Depends(current_user)):
    """Confirm the CV now on the profile is the one to review."""
    uid = str(user.id)
    application = await _load(uid)
    if application is None:
        raise HTTPException(400, "Start an application first")
    if application["status"] not in (S_CV, S_OA_READY):
        return {"ok": True, "status": application["status"]}

    data = await _user_data(uid)
    if not data.get("cv_blob_path"):
        raise HTTPException(400, "Upload your CV before continuing")

    application["cv_blob_path"] = data["cv_blob_path"]
    application["cv_confirmed_at"] = _now()
    application["full_name"] = data.get("full_name") or application.get("full_name", "")
    application["email"] = data.get("email") or application.get("email", "")
    application["status"] = S_OA_READY
    await _save(uid, application)
    return {"ok": True, "status": S_OA_READY}


@router.post("/oa/start")
async def start_oa(user: User = Depends(current_user)):
    """
    Start the clock. One attempt, and it does not stop for anything.
    """
    uid = str(user.id)
    application = await _load(uid)
    if application is None:
        raise HTTPException(400, "Start an application first")
    if application["status"] == S_CV:
        raise HTTPException(400, "Upload your CV before starting the assessment")
    if application["status"] != S_OA_READY:
        raise HTTPException(400, "You have already sat the assessment")

    # Re-check the CV at the last moment: it can be deleted between confirming
    # and starting, and an application without one is not reviewable.
    data = await _user_data(uid)
    if not data.get("cv_blob_path"):
        application["status"] = S_CV
        await _save(uid, application)
        raise HTTPException(400, "Your CV is no longer on file — upload it again")

    now = _now()
    application["status"] = S_OA_ACTIVE
    application["oa"] = {
        "started_at": now,
        "section": "written",
        "written": {"prompt": WRITTEN_PROMPT, "text": ""},
        "question_ids": build_paper(application.get("programme", "")),
        "current_index": 0,
        "answers": [],
    }
    await _save(uid, application)
    return {"ok": True, "status": S_OA_ACTIVE}


@router.get("/oa/state")
async def oa_state(user: User = Depends(current_user)):
    """The live assessment, resolved against the server clock on every read."""
    uid = str(user.id)
    application = await _load(uid)
    if application is None:
        return {"status": "none"}
    await _resolve_and_notify(uid, application)
    if application["status"] != S_OA_ACTIVE:
        return {"status": application["status"]}
    return {"status": S_OA_ACTIVE, "oa": _oa_view(application)}


@router.post("/oa/written")
async def submit_written(req: WrittenSubmit, user: User = Depends(current_user)):
    """
    Save the written answer — as a draft while they type, or as final.

    Drafts matter: the 15 minutes run whether the tab is open or not, so a
    crash three minutes in should not cost the whole essay.
    """
    uid = str(user.id)
    application = await _load(uid)
    if application is None or application.get("status") != S_OA_ACTIVE:
        raise HTTPException(400, "No assessment in progress")

    if await _resolve_and_notify(uid, application):
        # The clock beat this submission; whatever was last autosaved stands.
        return {"ok": True, "status": application["status"],
                "section": (application.get("oa") or {}).get("section")}

    oa = application["oa"]
    if oa["section"] != "written":
        return {"ok": True, "status": application["status"], "section": oa["section"]}

    oa.setdefault("written", {})["prompt"] = WRITTEN_PROMPT
    oa["written"]["text"] = (req.text or "")[:20000]
    if req.final:
        _close_written(oa)
    await _save(uid, application)
    return {"ok": True, "status": application["status"], "section": oa["section"]}


@router.post("/oa/answer")
async def answer(req: AnswerRequest, user: User = Depends(current_user)):
    uid = str(user.id)
    application = await _load(uid)
    if application is None or application.get("status") != S_OA_ACTIVE:
        raise HTTPException(400, "No assessment in progress")

    if await _resolve_and_notify(uid, application):
        # Already recorded as a timeout — this stale POST is a no-op rather
        # than a double answer.
        return {"ok": True, "status": application["status"]}

    oa = application["oa"]
    if oa["section"] != "numerical":
        return {"ok": True, "status": application["status"]}
    if req.index != oa["current_index"]:
        return {"ok": True, "status": application["status"]}   # already moved on

    _record_answer(oa, raw=req.value, timed_out=False)
    if oa["current_index"] >= len(oa["question_ids"]):
        await _finish_and_persist(uid, application, "completed")
    else:
        await _save(uid, application)
    return {"ok": True, "status": application["status"]}


@router.post("/oa/flag")
async def flag(req: FlagEvent, user: User = Depends(current_user)):
    """
    Count a paste attempt or a tab-away during the assessment.

    Recorded as a signal for the reviewer, never as an automatic penalty — a
    dropped connection and a second monitor look identical from here.
    """
    if req.kind not in ("paste", "left_page"):
        raise HTTPException(400, "Unknown event")
    uid = str(user.id)
    application = await _load(uid)
    if application is None or application.get("status") != S_OA_ACTIVE:
        return {"ok": True}
    flags = application.setdefault("flags", {"paste": 0, "left_page": 0})
    flags[req.kind] = int(flags.get(req.kind, 0)) + 1
    await _save(uid, application)
    return {"ok": True}


# ── Admin / reviewer ─────────────────────────────────────────────────────────

def _review_summary(application: Dict[str, Any], viewer_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Every reviewer's CV/written scores, and the averages across them.

    Several Quant Analyst members can score the same applicant independently;
    nothing here picks a single reviewer's word over another's, which is the
    whole point of averaging instead of just taking the latest score entered.
    """
    reviews = application.get("reviews") or {}
    cv_scores = [r["cv_score"] for r in reviews.values() if r.get("cv_score") is not None]
    written_scores = [r["written_score"] for r in reviews.values() if r.get("written_score") is not None]
    return {
        "count": len(reviews),
        "cv_avg": round(sum(cv_scores) / len(cv_scores), 1) if cv_scores else None,
        "written_avg": round(sum(written_scores) / len(written_scores), 1) if written_scores else None,
        "entries": sorted(
            [{"reviewer_id": rid, **r} for rid, r in reviews.items()],
            key=lambda r: r.get("updated_at") or _now(),
        ),
        "mine": reviews.get(viewer_id) if viewer_id else None,
    }


def _review_row(uid: str, application: Dict[str, Any], viewer_id: Optional[str] = None) -> Dict[str, Any]:
    oa = application.get("oa") or {}
    written = oa.get("written") or {}
    answers = [{
        **a,
        "prompt": QUESTION_BY_ID[a["question_id"]]["prompt"],
        "kind": QUESTION_BY_ID[a["question_id"]]["kind"],
        "answer_key": QUESTION_BY_ID[a["question_id"]]["answer"],
        "note": QUESTION_BY_ID[a["question_id"]]["note"],
    } for a in oa.get("answers", []) if a.get("question_id") in QUESTION_BY_ID]

    score = oa.get("score") or {}
    return {
        "user_id": uid,
        "username": application.get("username", "?"),
        "full_name": application.get("full_name") or "",
        "email": application.get("email") or "",
        "oxford_email": application.get("oxford_email") or "",
        "applicant_category": application.get("applicant_category") or "",
        "confirmed_oxford_student": bool(application.get("confirmed_oxford_student")),
        "programme": application.get("programme", ""),
        "status": application.get("status", S_CV),
        "created_at": _as_utc(application.get("created_at")),
        "submitted_at": _as_utc(application.get("submitted_at")),
        "last_reminded_at": _as_utc(application.get("last_reminded_at")),
        "cv_uploaded": bool(application.get("cv_blob_path")),
        "written_text": written.get("text", ""),
        "written_words": written.get("word_count") or len((written.get("text") or "").split()),
        "written_seconds": written.get("seconds_used"),
        "score": score,
        "correct": score.get("correct"),
        "total": score.get("total", NUMERICAL_QUESTIONS),
        "answers": answers,
        "flags": application.get("flags") or {},
        "finish_reason": oa.get("finish_reason"),
        "decision_note": application.get("decision_note") or "",
        "review": _review_summary(application, viewer_id),
    }


@router.get("/admin", include_in_schema=False)
async def admin_applications(request: Request, reviewer: User = Depends(require_reviewer)):
    """Every applicant, strongest numerical score first."""
    docs = await db_module.db.collection(COLLECTION).get()
    rows = [_review_row(d.id, d.to_dict() or {}, viewer_id=str(reviewer.id)) for d in docs]

    # Ranked by the numerical score, as asked. Applications with no score yet
    # (still on the CV step, or mid-assessment) sort to the bottom rather than
    # being read as a zero, since they have not had their turn.
    rows.sort(key=lambda r: (
        r["correct"] is None,
        -(r["correct"] or 0),
        (r["username"] or "").lower(),
    ))
    for i, r in enumerate(rows, start=1):
        r["rank"] = i if r["correct"] is not None else None

    scored = [r for r in rows if r["correct"] is not None]
    return templates.TemplateResponse("applications_admin.html", {
        "request": request,
        "app_name": "AlphaBook",
        "rows": rows,
        "total": len(rows),
        "scored": len(scored),
        "questions_total": NUMERICAL_QUESTIONS,
        "written_prompt": WRITTEN_PROMPT,
        "is_admin": reviewer.is_admin,
        "score_min": SCORE_MIN,
        "score_max": SCORE_MAX,
    })


@router.get("/admin/{user_id}/cv", include_in_schema=False)
async def applicant_cv(user_id: str, reviewer: User = Depends(require_reviewer)):
    """Stream an applicant's CV for review."""
    if not db_module.bucket:
        raise HTTPException(500, "Storage not configured")

    application = await _load(user_id)
    if application is None:
        raise HTTPException(404, "No such application")

    # Prefer whatever is on the profile now — the reviewer wants the current
    # CV — and fall back to the path snapshotted when they applied.
    data = await _user_data(user_id)
    blob_name = data.get("cv_blob_path") or application.get("cv_blob_path")
    if not blob_name:
        raise HTTPException(404, "That applicant has no CV on file")

    pdf = await asyncio.to_thread(db_module.bucket.blob(blob_name).download_as_bytes)
    return StreamingResponse(
        io.BytesIO(pdf),
        media_type="application/pdf",
        headers={"Content-Disposition": 'inline; filename="cv.pdf"'},
    )


@router.post("/admin/{user_id}/score")
async def submit_score(user_id: str, payload: ReviewScore, reviewer: User = Depends(require_reviewer)):
    """
    Record this reviewer's CV and written-response scores.

    One entry per reviewer, keyed by their own id — resubmitting updates your
    own score rather than adding a second one, and the average on display
    always reflects everyone's latest.
    """
    for label, value in (("CV", payload.cv_score), ("Written", payload.written_score)):
        if value is not None and not (SCORE_MIN <= value <= SCORE_MAX):
            raise HTTPException(400, f"{label} score must be between {SCORE_MIN} and {SCORE_MAX}")
    if payload.cv_score is None and payload.written_score is None:
        raise HTTPException(400, "Enter at least one score")

    application = await _load(user_id)
    if application is None:
        raise HTTPException(404, "No such application")
    if application.get("status") not in SCORABLE:
        raise HTTPException(400, "This application hasn't been submitted yet — nothing to score")

    reviews = application.setdefault("reviews", {})
    existing = reviews.get(str(reviewer.id), {})
    reviews[str(reviewer.id)] = {
        "reviewer_name": reviewer.username,
        "cv_score": payload.cv_score if payload.cv_score is not None else existing.get("cv_score"),
        "written_score": payload.written_score if payload.written_score is not None else existing.get("written_score"),
        "note": (payload.note or "").strip()[:300] or existing.get("note", ""),
        "updated_at": _now(),
    }
    await _save(user_id, application)
    return {"ok": True, "review": _review_summary(application, str(reviewer.id))}


# What a reminder says, by where the applicant is stuck. Nothing to send for
# a status with no gap to close (mid-assessment, already decided).
_REMINDER_COPY: Dict[str, Dict[str, str]] = {
    S_CV: {
        "subject": "Alpha Fund — upload your CV to continue your application",
        "body": ("<p>You started an application to <strong>{programme}</strong>, but we don't "
                 "have a CV on file yet. Upload one on your AlphaBook profile and you can "
                 "carry straight on to the assessment.</p>"),
        "cta": "Continue your application",
    },
    S_OA_READY: {
        "subject": "Alpha Fund — your assessment is ready when you are",
        "body": ("<p>Your CV is in for <strong>{programme}</strong>, and your 15-minute "
                 "assessment is ready. There's no deadline on starting it, but once you do "
                 "it runs straight through in one sitting, so pick a quiet 15 minutes.</p>"),
        "cta": "Start the assessment",
    },
}


@router.post("/admin/{user_id}/remind")
async def remind(user_id: str, payload: RemindRequest, admin: User = Depends(require_admin)):
    """Nudge an applicant who has stalled before the CV or the OA step."""
    application = await _load(user_id)
    if application is None:
        raise HTTPException(404, "No such application")

    status = application.get("status")
    copy = _REMINDER_COPY.get(status)
    if copy is None:
        raise HTTPException(
            400,
            "There's nothing to remind them about — they're mid-assessment, "
            "already submitted, or already decided.",
        )

    to = application.get("oxford_email") or application.get("email")
    if not to:
        raise HTTPException(400, "This applicant has no email address on file")

    name = application.get("full_name") or application.get("username") or "there"
    programme = application.get("programme") or "the programme"
    body = f"<p>Hi {name},</p>" + copy["body"].format(programme=programme)
    if payload.note:
        body += f'<p style="color:#555;">A note from the committee: {payload.note.strip()[:400]}</p>'

    sent = await mailer.send_email(
        to=to, subject=copy["subject"], title="A nudge on your application",
        body_html=body, cta_label=copy["cta"], cta_url=f"{BASE_URL}/apply",
    )
    if not sent:
        raise HTTPException(502, "Could not send the reminder — check the SMTP configuration")

    application["last_reminded_at"] = _now()
    application["last_reminded_by"] = admin.username
    await _save(user_id, application)
    return {"ok": True, "sent_to": to}


# Email copy for each stage of the decision. Shortlisting isn't final, so its
# note is deliberately open — an interview is still to come.
_DECISION_COPY: Dict[str, Dict[str, str]] = {
    S_SHORTLISTED: {
        "subject": "Alpha Fund — you've been shortlisted for interview",
        "title": "Shortlisted for interview",
        "body": ("<p>Your <strong>{programme}</strong> application has been shortlisted. "
                 "The committee will be in touch separately to arrange an interview.</p>"),
    },
    S_ACCEPTED: {
        "subject": "Alpha Fund — you're in",
        "title": "Application accepted",
        "body": ("<p>Congratulations — you've been accepted onto <strong>{programme}</strong>. "
                 "Welcome to Alpha Fund.</p>"),
    },
    S_REJECTED: {
        "subject": "Alpha Fund — an update on your application",
        "title": "Application decision",
        "body": ("<p>Thank you for applying to <strong>{programme}</strong>. On this occasion "
                 "we won't be taking your application further, but we'd encourage you to keep "
                 "playing and apply again in a future round.</p>"),
    },
}


async def _send_decision_email(application: dict, status: str) -> None:
    to = application.get("oxford_email") or application.get("email")
    copy = _DECISION_COPY.get(status)
    if not to or not copy:
        return
    name = application.get("full_name") or application.get("username") or "there"
    programme = application.get("programme") or "the programme"
    body = f"<p>Hi {name},</p>" + copy["body"].format(programme=programme)
    await mailer.send_email(to=to, subject=copy["subject"], title=copy["title"], body_html=body)


@router.post("/admin/{user_id}/decide")
async def decide(user_id: str, payload: Decision, admin: User = Depends(require_admin)):
    """
    Move an application to shortlisted, accepted or rejected.

    Shortlisting is the interview stage: a submitted application can be
    shortlisted or rejected outright, but can only be *accepted* once it has
    been shortlisted — the interview is the chance to actually meet someone
    before the fund commits to them. Rejecting is available at either point,
    since not everyone who applies gets an interview. Accepting sets the
    member's programme; each transition emails the applicant.
    """
    if payload.decision not in ("shortlist", "accept", "reject"):
        raise HTTPException(400, "Decision must be shortlist, accept or reject")

    application = await _load(user_id)
    if application is None:
        raise HTTPException(404, "No such application")
    status = application.get("status")

    if payload.decision == "shortlist":
        if status != S_SUBMITTED:
            raise HTTPException(400, "Only a newly submitted application can be shortlisted")
        application["status"] = S_SHORTLISTED
        application["shortlisted_at"] = _now()
        application["shortlisted_by"] = admin.username
    elif payload.decision == "accept":
        if status != S_SHORTLISTED:
            raise HTTPException(400, "Shortlist the applicant and hold the interview before accepting")
        application["status"] = S_ACCEPTED
        application["decided_at"] = _now()
        application["decided_by"] = admin.username
    else:  # reject
        if status not in (S_SUBMITTED, S_SHORTLISTED):
            raise HTTPException(400, "That application has already been decided")
        application["status"] = S_REJECTED
        application["decided_at"] = _now()
        application["decided_by"] = admin.username

    if payload.note is not None:
        application["decision_note"] = (payload.note or "").strip()[:500]
    await _save(user_id, application)
    await _send_decision_email(application, application["status"])

    if application["status"] == S_ACCEPTED:
        programme = application.get("programme")
        if programme in mb.MEMBERSHIPS:
            legacy = {v: k for k, v in mb.LEGACY_TRACK_TO_MEMBERSHIP.items()}.get(programme, "")
            await db_module.db.collection("users").document(user_id).update({
                "membership": programme, "track": legacy,
            })
    return {"ok": True, "status": application["status"]}


# Everything an OA sitting produces — cleared on redo so the applicant lands
# back on "your assessment is ready" with a genuinely blank slate. Identity,
# CV and the programme they applied for are deliberately not in this list.
_OA_PRODUCED_FIELDS = (
    "oa", "flags", "reviews", "submitted_at", "confirmation_sent_at",
    "shortlisted_at", "shortlisted_by", "decided_at", "decided_by", "decision_note",
)


@router.post("/admin/{user_id}/redo")
async def redo_application(user_id: str, admin: User = Depends(require_admin)):
    """
    Let an applicant sit the assessment again, from a clean slate.

    For the rare case that deserves an exception to "one attempt" — a
    technical failure during the sitting, or the committee wants another
    look before deciding. Everything the previous sitting produced (answers,
    written response, reviewer scores, any shortlist/decision) is cleared;
    their CV, programme choice and Oxford email stay exactly as they were,
    so they land back on "your assessment is ready" rather than having to
    reapply from scratch. If they had already been accepted, their granted
    membership is *not* reverted automatically — that's a separate call for
    an admin to make deliberately.
    """
    application = await _load(user_id)
    if application is None:
        raise HTTPException(404, "No such application")
    if application.get("status") in (S_CV, S_OA_READY):
        raise HTTPException(400, "They haven't started the assessment yet — nothing to redo")

    for field in _OA_PRODUCED_FIELDS:
        application.pop(field, None)
    application["status"] = S_OA_READY if application.get("cv_blob_path") else S_CV
    application["flags"] = {"paste": 0, "left_page": 0}
    application["redone_at"] = _now()
    application["redone_by"] = admin.username
    await _save(user_id, application)
    return {"ok": True, "status": application["status"]}


@router.delete("/admin/{user_id}")
async def delete_application(user_id: str, admin: User = Depends(require_admin)):
    """
    Remove an application from the record entirely.

    Unlike redo, there is nothing left afterward — the CV snapshot, every
    answer, every reviewer's score and any decision are gone, and the person
    would need to start a fresh application to appear here again. The
    uploaded CV file itself lives on their profile, not here, so this does
    not touch it.
    """
    application = await _load(user_id)
    if application is None:
        raise HTTPException(404, "No such application")
    await db_module.db.collection(COLLECTION).document(user_id).delete()
    return {"ok": True}
