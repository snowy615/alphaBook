"""
Applications to the Bootcamp and Analyst programmes (Fundamental or Quant).
===========================================================================

A general-public or general-member account applies to a programme, puts an
up-to-date CV on their profile, and then sits a written assessment. Every
reviewer (an admin, or any Analyst member) reads and scores the CV and the
writing by hand — nothing here is auto-graded, so the ranking on the review
page is the average of those human scores, not a computed one.

Only the Quant track is currently taking applicants — the Fundamental side
(and the combined "Both" Bootcamp option) is shown on the apply page as
coming next term rather than removed outright; see
:data:`app.membership.DISABLED_PROGRAMMES`.

The shape of the assessment:

* **One sitting, on the server's clock.** ``started_at`` is stamped once and
  the deadline is derived from it, so closing the tab, reloading, or signing
  in on another device does not buy more time. There is no pause and no
  second attempt — the point of "one sitting" is that the window is the same
  for everyone regardless of what they do with it.
* **Two written questions, back to back.** ``MOTIVATION_SECONDS`` on "why do
  you want to join, and why you", then ``ESTIMATION_SECONDS`` on an
  estimation question — pick something large and hard to count exactly (the
  classic example: how many bicycles are in Oxford) and reason your way to a
  number. Submitting the first one early moves straight on to the second
  rather than banking the leftover time, so nobody is rewarded for rushing
  either answer.
* **Plain text or light LaTeX.** Both boxes accept ordinary prose; anyone who
  wants to show a formula can type it in LaTeX (``$x^2$`` or ``$$\\sum...$$``)
  and preview how it will render, on the same page rather than a separate
  tool. The reviewer's copy renders it the same way.
* **No AI.** Said plainly on the gate, acknowledged with a tick before the
  clock starts, and backed by the clock itself: pasting into either answer is
  blocked, and leaving the page is counted — both surfaced to the reviewer as
  signals, never as an automatic disqualification, because a dropped
  connection looks the same as a second monitor.

Everything is resolved on read, the same approach ``interview_oa`` uses: the
state endpoint expires whichever question's own timer has run out, and
finishes the sitting when the last one does. There is no separate overall
clock. A candidate who closes the tab at the buzzer still gets an honest,
un-strandable result.

Results are admin-only. A candidate sees a plain "submitted" screen, never a
score, so applicants can't compare notes on how they were rated.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import html
import io
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from fastapi.templating import Jinja2Templates
from openpyxl import Workbook
from openpyxl.styles import Alignment
from openpyxl.utils import get_column_letter
from pydantic import BaseModel

from app import db as db_module
from app import gcal
from app import mailer
from app import membership as mb
from app import outreach
from app.outreach import (
    EVENT_TICKET_FAST_TRACK, EVENT_TICKET_GENERAL, EVENT_TICKET_NONE, EVENT_TICKETS,
    FAST_TRACK_CAPACITY,
)
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
    every Analyst member (Fundamental or Quant).

    Every decision in :func:`decide` — shortlist, accept, reject — rides on
    this same check. There's no narrower gate on accept even though it
    grants membership: the safeguard is a heavier client-side confirmation
    and the fact that every decision is attributed and visible to every
    reviewer, not a 403.
    """
    if user.is_admin:
        return user
    doc = await db_module.db.collection("users").document(str(user.id)).get()
    data = doc.to_dict() if doc.exists else {}
    if mb.membership_of(data) in mb.ANALYST_MEMBERSHIPS:
        return user
    raise HTTPException(403, "The application review page is open to Analyst members and admins")


# ── Clocks (seconds) ─────────────────────────────────────────────────────────
# "Motivation" and "estimation" are the historical field/section names —
# kept as-is so existing stored applications (oa.motivation / oa.estimation)
# still read correctly — but the prompts and timings are this cycle's
# behavioural and estimation questions.
MOTIVATION_SECONDS = 5 * 60         # the behavioural question
ESTIMATION_SECONDS = 3 * 60         # the estimation question
# Not a clock: each question has its own timer and nothing else ends the
# sitting. This is only the most time the two can take together, used where
# the copy tells people how long to set aside.
SESSION_SECONDS = MOTIVATION_SECONDS + ESTIMATION_SECONDS  # 480, 8 minutes

# How late a part-one submit may arrive and still be banked into the part-one
# answer once the server clock has already moved on to part two. The browser
# fires its own submit the instant its local clock hits zero, but the
# once-a-second poll often reaches the server first and closes the question;
# the few seconds between the two would otherwise lose everything typed since
# the last autosave. Short enough to buy nobody any real thinking time.
LATE_SUBMIT_GRACE_SECONDS = 5

MOTIVATION_PROMPT = (
    "Tell us about something you have pursued seriously because you were genuinely "
    "interested in it. What did you contribute, how did you stay committed when it "
    "became difficult, and how could that experience help you contribute to Oxford "
    "Alpha Fund? (Aim for around 200 words.)"
)

ESTIMATION_PROMPT = (
    "Estimate how many laptops are currently switched on across the University of Oxford."
)

# Statuses an application moves through, in order. A fast-tracked application
# (see EVENT_TICKET_FAST_TRACK below) skips straight from S_CV to S_SUBMITTED
# — no oa_ready/oa_active in between, since there's no written assessment to sit.
S_CV = "cv"                    # applied; waiting on an up-to-date CV
S_OA_READY = "oa_ready"        # CV on file; assessment not started
S_OA_ACTIVE = "oa_active"      # clock running
S_SUBMITTED = "submitted"      # assessment finished (or fast-tracked), awaiting a decision
S_SHORTLISTED = "shortlisted"  # invited to interview; not yet a final decision
S_ACCEPTED = "accepted"
S_REJECTED = "rejected"

# The outreach event: two ticket types, chosen right after picking a
# programme and before the CV step (see app/outreach.py — the choice is the
# event sign-up itself, shared with the events page). Fast-Track is
# capacity-limited and skips the written assessment entirely — an analyst
# reviews the CV instead (at the clinic, or on the admin page) and the normal
# shortlist/accept/reject decision still applies from there. General
# Attendance and "not attending" both continue through the ordinary
# CV -> assessment flow.

YEAR_OF_STUDY_OPTIONS = ["1st year", "2nd year", "3rd year", "4th year", "Master's", "DPhil / PhD"]

DECIDED = {S_ACCEPTED, S_REJECTED}
# Statuses a reviewer can attach a CV/written score to — once the CV and
# written response actually exist to be read, through to a final decision.
SCORABLE = {S_SUBMITTED, S_SHORTLISTED, S_ACCEPTED, S_REJECTED}

# The CV round is scored against ONLINE_CV_RUBRIC or ONSITE_CV_RUBRIC and
# the interview against INTERVIEW_RUBRIC, all below. SCORE_MAX is the common
# 10-point scale they're both put on for the combined ranking.
SCORE_MIN, SCORE_MAX = 1, 10
NOTE_MAX_CHARS = 2000   # a reviewer's general comments on one applicant

# The CV round, out of 18 either way, in one of two forms. Each criterion is
# scored by picking exactly one option; the CV score is the total. Options
# are listed lowest first, in the order reviewers see them.
#
# * ONLINE_CV_RUBRIC for everyone who applied online: it includes the two
#   written answers.
# * ONSITE_CV_RUBRIC for Fast-Track applicants, whose CV round happens in
#   person at the CV clinic: a quick interview question takes the place of
#   the two written answers.
#
# The criteria from Major down are the same in both.
def _shared_cv_criteria() -> List[Dict[str, Any]]:
    return [
        {"key": "major", "label": "Major", "options": [
            {"points": 0, "label": "Not a relevant major."},
            {"points": 1, "label": "STEM or Economics (e.g. other science, statistics, economics)."},
            {"points": 2, "label": "Mathematics, Computer Science, Physics or Engineering."},
        ]},
        {"key": "stem_achievements", "label": "STEM achievements", "options": [
            {"points": 0, "label": "No notable awards."},
            {"points": 1, "label": "Awards from less well-known competitions, or a non-final national round."},
            {"points": 2, "label": "Final-round national medallist (e.g. national Olympiad final round)."},
            {"points": 3, "label": "International medallist (e.g. IMO, IPhO, IOI, IBO)."},
        ]},
        {"key": "stem_research", "label": "STEM research / technical projects", "options": [
            {"points": 0, "label": "None."},
            {"points": 1, "label": "Scattered or less well-known projects (e.g. small school or coursework projects, "
                                   "hackathons) with limited depth."},
            {"points": 2, "label": "National-level research or a significant technical project without coding "
                                   "(e.g. theoretical research, mathematical modelling, data or policy analysis, "
                                   "experimental work)."},
            {"points": 3, "label": "International or national-level research, or a significant technical project "
                                   "involving coding (e.g. research with code, data-driven projects, machine learning, "
                                   "app or web development, quantitative modelling, algorithmic work)."},
        ]},
        {"key": "market", "label": "Previous market experience", "options": [
            {"points": 0, "label": "None."},
            {"points": 1, "label": "Personal project in finance, but no trading and no internship."},
            {"points": 2, "label": "Either an internship in a relevant area, or personal trading experience."},
            {"points": 3, "label": "Both a relevant internship and personal trading experience."},
        ]},
        {"key": "academic", "label": "Academic performance",
         "hint": "First years: A Level / IB (or equivalent). Years 2+: Prelims result.", "options": [
            {"points": 0, "label": "A Level / IB: below A*AA, or IB below 39. Prelims: below upper 2:1 (USM below 65)."},
            {"points": 1, "label": "A Level / IB: A*AA or A*A*B, or IB 39+. Prelims: upper 2:1 (USM 65+)."},
            {"points": 2, "label": "A Level / IB: A*A*A, or IB 41+. Prelims: first class."},
            {"points": 3, "label": "A Level / IB: A*A*A*, or IB 43+. Prelims: top 10% of year."},
        ]},
    ]


ONLINE_CV_RUBRIC: List[Dict[str, Any]] = [
    {"key": "motivation", "label": "Response to motivation question", "options": [
        {"points": 0, "label": "Very little effort; unclear or irrelevant response."},
        {"points": 1, "label": "Generic response; limited depth or personal insight."},
        {"points": 2, "label": "Insightful and well written, showing genuine motivation and strong personal "
                               "insight (this score is rare)."},
    ]},
    {"key": "fermi", "label": "Response to Fermi question", "options": [
        {"points": 0, "label": "Very short and low effort; little to no reasoning."},
        {"points": 1, "label": "Some reasonable reasoning, but limited depth."},
        {"points": 2, "label": "Insightful and well reasoned, with a clear and thoughtful approach."},
    ]},
    *_shared_cv_criteria(),
]

ONSITE_CV_RUBRIC: List[Dict[str, Any]] = [
    {"key": "quick_question", "label": "Response to quick interview question",
     "hint": "The 4 key aspects: contribution potential, learning potential, commitment level, and interest "
             "in trading / finance.", "options": [
        {"points": 0, "label": "Doesn't address any of the key aspects."},
        {"points": 1, "label": "Clearly addresses 1 of the 4 key aspects."},
        {"points": 2, "label": "Clearly addresses 2 of the 4 key aspects."},
        {"points": 3, "label": "Clearly addresses 3 of the 4 key aspects."},
        {"points": 4, "label": "Clear on their motivations for joining OAF, including contribution potential, "
                               "learning potential, commitment level and interest in trading / finance."},
    ]},
    *_shared_cv_criteria(),
]

# Candidates who meet any one of these are shortlisted for interview straight
# away, without a CV score (see decide: "auto_shortlist").
AUTO_SHORTLIST_REASONS: Dict[str, str] = {
    "prelims_top5": "Top 5% of their year in Prelims (Years 2+)",
    "olympiad_medal": "IMO / IPhO / IOI / ISEF medallist",
    "finance_internship": "Relevant finance internship (including trading firms)",
}


# The interview (the standard rubric): likability, communication, and two
# questions from the question bank, easy then hard, 10 minutes each. Every
# candidate attempts both, with hints only on the fixed schedule in
# INTERVIEW_TIMERS, and each question is scored on how far the candidate
# got within its time. Out of 15 (3 + 2 + 5 + 5), less a CV-project penalty
# of 0 to -5.
def _problem_solving() -> List[Dict[str, Any]]:
    """The levels both questions share, lowest first."""
    return [
        {"points": 0, "label": "Little or no meaningful progress (e.g. no relevant ideas even after some time)."},
        {"points": 1, "label": "Some progress or identifies a relevant idea, but can't develop it far."},
        {"points": 2, "label": "Develops a sensible approach and makes moderate progress, but still significant gaps."},
        {"points": 3, "label": "Makes strong progress and gets close to a complete solution; only minor errors or gaps remain."},
        {"points": 4, "label": "Almost complete solution; only very small mistakes or missing details."},
        {"points": 5, "label": "Fully solves the problem independently within 10 minutes, with correct reasoning."},
    ]


INTERVIEW_RUBRIC: List[Dict[str, Any]] = [
    {"key": "likability", "label": "Likability / easy to work with", "options": [
        {"points": 0, "label": "Poor attitude: dismissive, arrogant, unprofessional, unreceptive, or otherwise difficult to work with."},
        {"points": 1, "label": "Some concerns about attitude or collaboration (e.g. somewhat closed-off, limited engagement), but nothing severe."},
        {"points": 2, "label": "Normal, positive interaction: professional, respectful and easy to work with. This should be the typical score."},
        {"points": 3, "label": "Exceptionally likable, collaborative and receptive; someone you'd particularly want on the team."},
    ]},
    {"key": "communication", "label": "Communication", "options": [
        {"points": 0, "label": "Has significant difficulty articulating thoughts; explanations are unclear or hard to follow."},
        {"points": 1, "label": "Communicates reasoning adequately; generally understandable, but may be somewhat unstructured."},
        {"points": 2, "label": "Exceptionally clear, concise and well structured; makes their thought process easy to follow."},
    ]},
    {"key": "easy", "label": "Problem solving: easy question (10 minutes)", "options": _problem_solving()},
    {"key": "hard", "label": "Problem solving: hard question (10 minutes)", "options": _problem_solving()},
    # A penalty only, any whole number from 0 to -5 at the interviewer's
    # judgement; the labelled points are anchors, the others sit between.
    {"key": "cv_project", "label": "CV project discussion (penalty, 0 to −5)", "scale": True, "options": [
        {"points": 0, "label": "Clearly understands and can explain the project(s) listed on their CV. Minor forgotten details are fine."},
        {"points": -1, "label": "Some gaps or minor inconsistencies, but generally understands what they did and their contribution."},
        {"points": -2, "label": ""},
        {"points": -3, "label": "Materially overstated their contribution, or claimed substantial work they can't adequately explain."},
        {"points": -4, "label": ""},
        {"points": -5, "label": "Clear evidence that a significant claim about the project was fabricated or falsely attributed to themselves."},
    ]},
]
# The interview is out of 15; the CV-project penalty can take up to 5 off.
INTERVIEW_MAX = 15

# The interview's three timed parts, in order, and what's due when. Each
# question is timed from when it has been read out and any clarifying
# questions answered, and stops at 10 minutes whether or not it's finished.
# Hints come only on this schedule, the same for every candidate: each is
# given at its time only if the candidate hasn't yet reached that hint's
# checkpoint (each question in the bank lists Hint 1, Hint 2 and the
# checkpoint each one gets you to), and never earlier, even if asked. The
# scoring view's timer and the interview guide both read this.
def _question_cues(end: str) -> List[Dict[str, Any]]:
    return [
        {"at": 0, "label": "No hints", "detail": "clarifying questions only"},
        {"at": 3 * 60, "label": "Hint 1", "detail": "if they haven't reached Checkpoint 1"},
        {"at": 6 * 60, "label": "Hint 2", "detail": "if they haven't reached Checkpoint 2"},
        {"at": 10 * 60, "label": "Stop", "detail": end},
    ]


INTERVIEW_TIMERS: List[Dict[str, Any]] = [
    {"key": "cv", "button": "Start CV", "label": "CV discussion", "seconds": 8 * 60, "cues": [
        {"at": 0, "label": "CV discussion", "detail": "talk through the projects on their CV"},
        {"at": 8 * 60, "label": "Time", "detail": "move on to the easy question"},
    ]},
    {"key": "easy", "button": "Start easy", "label": "Easy question", "seconds": 10 * 60,
     "cues": _question_cues("move on to the hard question, whatever happened on this one")},
    {"key": "hard", "button": "Start hard", "label": "Hard question", "seconds": 10 * 60,
     "cues": _question_cues("the interview's questions are done")},
]


def _cv_rubric_for(fast_tracked: bool) -> List[Dict[str, Any]]:
    """The rubric this applicant's CV round is scored on: the onsite one for
    Fast-Track (their CV round is in person), the online one otherwise."""
    return ONSITE_CV_RUBRIC if fast_tracked else ONLINE_CV_RUBRIC


def _cv_max(fast_tracked: bool) -> int:
    return sum(max(o["points"] for o in c["options"]) for c in _cv_rubric_for(fast_tracked))

# How an interview record moves: a reviewer proposes a time, and the
# candidate either confirms it (a calendar invite follows) or declines it
# (the assigned interviewer is emailed to sort out an alternative directly).
INTERVIEW_PROPOSED = "proposed"
INTERVIEW_CONFIRMED = "confirmed"
INTERVIEW_DECLINED = "declined"
INTERVIEW_MINUTES = 30   # default slot length for the calendar invite

# The availability grid a shortlisted candidate fills in: a fixed window on
# the calendar, in half-hour London slots (one slot = one interview). Fixed (not anchored to when someone
# was shortlisted) so every viewer — the candidate, and every analyst on the
# admin page — computes exactly the same set of slots regardless of when
# each of them happens to load the page; the only thing that moves it is the
# real calendar date, which advances the same way for everyone. Update the
# two dates below for the next admissions cycle.
AVAILABILITY_WINDOW_START = dt.date(2026, 10, 1)
AVAILABILITY_WINDOW_END = dt.date(2026, 10, 23)     # inclusive
AVAILABILITY_START_HOUR = 7     # London wall-clock, first slot 07:00
AVAILABILITY_END_HOUR = 18      # London wall-clock, last slot 18:30 (ends 19:00)
AVAILABILITY_SLOT_MINUTES = INTERVIEW_MINUTES   # 30: a slot is exactly one interview
AVAILABILITY_MAX_SLOTS = 700    # generous ceiling against a malformed payload
LONDON_TZ = ZoneInfo("Europe/London")


class StartApplication(BaseModel):
    programme: str
    # Only required when the account's own email isn't already an Oxford
    # address; see is_oxford_email() and the /apply/start handler.
    oxford_email: Optional[str] = None
    # Only required for a General public applicant — see the eligibility
    # check in the /apply/start handler.
    confirms_oxford_student: bool = False


class EventTicketChoice(BaseModel):
    ticket: str   # "none" | "general" | "fast_track"


class ConfirmCv(BaseModel):
    # Both required (checked in confirm_cv): the account's own full_name is
    # optional and often blank or just a username, and reviewers need a real
    # name to put to the CV and to address emails to.
    first_name: str = ""
    last_name: str = ""
    college: str
    degree: str
    year_of_study: str
    linkedin: Optional[str] = None


class ReviewScore(BaseModel):
    """One reviewer's scores for one applicant, saved as they go.

    Only the fields actually sent are changed (the scoring view autosaves
    each edit on its own), and sending null clears that field. The CV is
    scored criterion by criterion ({criterion key: points}); the CV score is
    their total once every criterion is scored, and the interview the same
    way (interview_rubric). A bare cv_score or interview_score is refused.
    The written answers are scored inside the CV rubric (its two response
    criteria), so there's no separate written score. ``note`` is the
    reviewer's general comments on the applicant, shared by every section."""
    cv_rubric: Optional[Dict[str, Optional[int]]] = None
    cv_score: Optional[int] = None
    interview_rubric: Optional[Dict[str, Optional[int]]] = None
    interview_score: Optional[int] = None
    note: Optional[str] = None


class RemindRequest(BaseModel):
    note: Optional[str] = None


class WrittenSubmit(BaseModel):
    text: str = ""
    final: bool = False
    # Which question the text belongs to ("motivation" / "estimation"), as
    # drawn on the candidate's screen. Optional so an old client still works,
    # but without it a submit that races the server clock across the
    # motivation -> estimation boundary lands in the wrong box — see
    # submit_written.
    section: Optional[str] = None


class FlagEvent(BaseModel):
    kind: str          # "paste" | "left_page"


class Decision(BaseModel):
    decision: str      # "shortlist" | "auto_shortlist" | "accept" | "reject"
    note: Optional[str] = None
    reason: Optional[str] = None   # auto_shortlist only: a key of AUTO_SHORTLIST_REASONS


class ScheduleInterview(BaseModel):
    interviewer_id: str
    when: dt.datetime          # candidate-facing slot, any ISO 8601 the browser sends
    message: Optional[str] = None


class InterviewDecline(BaseModel):
    note: Optional[str] = None


class AvailabilitySubmit(BaseModel):
    slots: List[str] = []


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


def _availability_window() -> Tuple[dt.date, dt.date]:
    """
    Today's London date through AVAILABILITY_WINDOW_END, floored at
    AVAILABILITY_WINDOW_START — never offer a slot in the past, and never
    before the window officially opens. Computed fresh from the real
    calendar date rather than stored per-application, so the candidate's
    grid and every analyst's grid always agree: the only thing that can
    move this window is the date itself, which advances the same way for
    everyone looking at it.
    """
    today = _now().astimezone(LONDON_TZ).date()
    start = max(today, AVAILABILITY_WINDOW_START)
    return start, AVAILABILITY_WINDOW_END


def _parse_slot(value: str) -> Optional[dt.datetime]:
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return _as_utc(parsed)


def _valid_availability_slots(raw: List[str]) -> List[str]:
    """
    Keep only half-hour slots (on :00 or :30) that land on 7am-7pm London
    time inside the fixed availability window, deduplicated and sorted. Silently drops
    anything malformed or out-of-window rather than rejecting the whole
    submission — the grid on the frontend only ever generates well-formed
    slots, so this is a backstop against a stale or tampered client, not the
    primary validation.
    """
    start, end = _availability_window()
    seen = set()
    out: List[str] = []
    for value in raw:
        slot = _parse_slot(value)
        if slot is None:
            continue
        london = slot.astimezone(LONDON_TZ)
        if london.minute % AVAILABILITY_SLOT_MINUTES or london.second or london.microsecond:
            continue
        if not (AVAILABILITY_START_HOUR <= london.hour <= AVAILABILITY_END_HOUR):
            continue
        if not (start <= london.date() <= end):
            continue
        key = slot.astimezone(dt.timezone.utc).isoformat()
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
        if len(out) >= AVAILABILITY_MAX_SLOTS:
            break
    out.sort()
    return out


def _availability_of(application: Dict[str, Any]) -> List[str]:
    """The candidate's free slots as half-hour slot starts.

    Availability saved before the grid moved to half-hour slots was one
    entry per whole hour; each of those meant "free for the whole hour", so
    it reads back as both of that hour's half-hour slots. New saves are
    stamped ``availability_slot_minutes`` and read back as they are."""
    slots = list(application.get("availability") or [])
    if application.get("availability_slot_minutes") == AVAILABILITY_SLOT_MINUTES:
        return slots
    out: List[str] = []
    for value in slots:
        start = _parse_slot(value)
        if start is None:
            continue
        out.append(start.isoformat())
        out.append((start + dt.timedelta(minutes=30)).isoformat())
    return sorted(set(out))


def _fmt_slot(value: Any) -> str:
    slot = _as_utc(value)
    if slot is None:
        return str(value)
    london = slot.astimezone(LONDON_TZ)
    end = london + dt.timedelta(minutes=AVAILABILITY_SLOT_MINUTES)
    return f"{london.strftime('%a %d %b %Y, %H:%M')}–{end.strftime('%H:%M')} London"


# ── Storage ───────────────────────────────────────────────────────────────────

async def _load(user_id: str) -> Optional[dict]:
    doc = await db_module.db.collection(COLLECTION).document(user_id).get()
    return doc.to_dict() if doc.exists else None


async def _save(user_id: str, application: dict) -> None:
    await db_module.db.collection(COLLECTION).document(user_id).set(application)


async def _save_review(user_id: str, reviewer_id: str, entry: dict) -> None:
    """Write one reviewer's entry and nothing else. Scores autosave on every
    click, often with several reviewers on the same applicant at once;
    writing the whole application back (as _save does) would let one
    reviewer's save wipe out another's that landed a moment earlier."""
    await db_module.db.collection(COLLECTION).document(user_id).update({f"reviews.{reviewer_id}": entry})


async def _user_data(user_id: str) -> Dict[str, Any]:
    doc = await db_module.db.collection("users").document(user_id).get()
    return (doc.to_dict() or {}) if doc.exists else {}


async def _fast_track_count() -> int:
    """How many places are held — by an outreach sign-up or an application
    (see outreach.fast_track_holders). A soft ~50-place cap: counted by scan
    at claim time, not meant to defend against a burst of simultaneous
    submissions down to the person."""
    return len(await outreach.fast_track_holders())


# ── The assessment state machine ─────────────────────────────────────────────

def _close_section(oa: dict, key: str, started: Any, limit_s: float) -> None:
    """Bank whatever is in a written box — used for both questions."""
    section = oa.setdefault(key, {})
    section.setdefault("text", "")
    section["submitted_at"] = _now()
    section["seconds_used"] = round(min(limit_s - _left(started, limit_s), limit_s), 1)
    section["word_count"] = len(section["text"].split())


def _close_motivation(oa: dict, expired: bool = False) -> None:
    """Bank the motivation answer and open the estimation question.

    Submitted early, the estimation clock starts now. Run out, it starts at
    the motivation deadline itself, not whenever the server next happens to
    look: the clock runs whether the tab is open or not, so someone who
    closes the tab mid-question and comes back later finds part two has been
    running since part one ended, exactly as if they'd stayed."""
    _close_section(oa, "motivation", oa.get("started_at"), MOTIVATION_SECONDS)
    oa["section"] = "estimation"
    started = _as_utc(oa.get("started_at"))
    if expired and started is not None:
        oa["estimation_started_at"] = started + dt.timedelta(seconds=MOTIVATION_SECONDS)
    else:
        oa["estimation_started_at"] = _now()


def _close_estimation(oa: dict) -> None:
    _close_section(oa, "estimation", oa.get("estimation_started_at"), ESTIMATION_SECONDS)


def _finish(application: dict, reason: str) -> None:
    """Close the assessment out, banking whichever question was still open."""
    oa = application["oa"]
    if oa.get("section") == "motivation":
        _close_motivation(oa)
    if oa.get("section") == "estimation":
        _close_estimation(oa)
    oa["section"] = "done"
    oa["finished_at"] = _now()
    oa["finish_reason"] = reason
    application["status"] = S_SUBMITTED
    application["submitted_at"] = _now()


def resolve(application: dict) -> bool:
    """
    Bring a stored application up to date with the wall clock.

    Called on every read. Each question has its own timer and nothing else:
    an expired motivation question closes into the estimation one (whose
    clock started at the motivation deadline, see _close_motivation), and an
    expired estimation question finishes the sitting. Both can happen in one
    call, so a candidate who closes the tab at the start still gets an
    honest, finished result the next time anything reads it, and one who
    leaves it open all afternoon does not get an afternoon's thinking time.

    Returns True if anything changed and the document needs writing back.
    """
    if application.get("status") != S_OA_ACTIVE:
        return False
    oa = application.get("oa") or {}
    changed = False

    if oa.get("section") == "motivation":
        if _left(oa.get("started_at"), MOTIVATION_SECONDS) > 0:
            return False
        _close_motivation(oa, expired=True)
        changed = True

    if oa.get("section") == "estimation":
        # Estimation is the last question, so running out on it finishes the
        # sitting as a whole.
        if _left(oa.get("estimation_started_at"), ESTIMATION_SECONDS) <= 0:
            _finish(application, "completed")
            return True

    return changed


async def _send_submission_confirmation(application: dict) -> None:
    """The one email every candidate gets: proof their assessment went in."""
    to = application.get("oxford_email") or application.get("email")
    if not to:
        return
    name = html.escape(application.get("full_name") or application.get("username") or "there")
    programme = html.escape(application.get("programme", "the programme"))
    await mailer.send_email(
        to=to,
        subject=f"Alpha Fund: your {programme} application is in",
        title="Application received",
        body_html=(
            f"<p>Hi {name},</p>"
            f"<p>This confirms your CV and assessment for <strong>{programme}</strong> "
            f"have both been submitted. The committee will review your application and "
            f"get back to you soon.</p>"
        ),
    )


async def _send_fast_track_confirmation(application: dict) -> None:
    """The Fast-Track counterpart to _send_submission_confirmation: no
    online written questions, since their CV round happens in person."""
    to = application.get("oxford_email") or application.get("email")
    if not to:
        return
    name = html.escape(application.get("full_name") or application.get("username") or "there")
    programme = html.escape(application.get("programme", "the programme"))
    await mailer.send_email(
        to=to,
        subject=f"Alpha Fund: your {programme} application is in",
        title="Application received",
        body_html=(
            f"<p>Hi {name},</p>"
            f"<p>This confirms your CV for <strong>{programme}</strong> has been submitted. "
            f"As a Fast-Track applicant, your CV round happens in person at the CV clinic at "
            f"Quant Outreach, in place of the online written questions. We'll be in touch "
            f"after that.</p>"
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


def _oa_view(application: dict) -> Dict[str, Any]:
    oa = application["oa"]
    out: Dict[str, Any] = {
        "section": oa["section"],
    }
    if oa["section"] == "motivation":
        out["motivation"] = {
            "prompt": MOTIVATION_PROMPT,
            "text": (oa.get("motivation") or {}).get("text", ""),
            "seconds_left": round(_left(oa.get("started_at"), MOTIVATION_SECONDS), 1),
            "seconds_total": MOTIVATION_SECONDS,
        }
    elif oa["section"] == "estimation":
        out["estimation"] = {
            "prompt": ESTIMATION_PROMPT,
            "text": (oa.get("estimation") or {}).get("text", ""),
            "seconds_left": round(_left(oa.get("estimation_started_at"), ESTIMATION_SECONDS), 1),
            "seconds_total": ESTIMATION_SECONDS,
        }
    return out


# ── Pages ─────────────────────────────────────────────────────────────────────

@router.get("", include_in_schema=False)
async def apply_page(request: Request):
    return templates.TemplateResponse("apply.html", {
        "request": request,
        "app_name": "AlphaBook",
        "motivation_minutes": MOTIVATION_SECONDS // 60,
        "estimation_minutes": ESTIMATION_SECONDS // 60,
        "session_minutes": SESSION_SECONDS // 60,
    })


# ── Candidate API ─────────────────────────────────────────────────────────────

@router.get("/state")
async def state(user: User = Depends(current_user)):
    """Everything the apply page needs: eligibility, CV, and the live clocks."""
    uid = str(user.id)
    data = await _user_data(uid)
    membership = mb.membership_of(data)
    is_reviewer = bool(user.is_admin) or membership in mb.ANALYST_MEMBERSHIPS

    account_email = data.get("email") or ""
    out: Dict[str, Any] = {
        "eligible": mb.can_apply({**data, "is_admin": user.is_admin}),
        "membership": membership,
        "programmes": mb.apply_programmes_for(membership),
        # Shown on the choose-programme step as "not available right now —
        # will become available next term" rather than removed outright.
        "disabled_programmes": sorted(mb.DISABLED_PROGRAMMES),
        "cv_uploaded": bool(data.get("cv_blob_path")),
        "full_name": data.get("full_name") or "",
        "graduation_year": data.get("graduation_year"),
        "motivation_prompt": MOTIVATION_PROMPT,
        "estimation_prompt": ESTIMATION_PROMPT,
        "year_of_study_options": YEAR_OF_STUDY_OPTIONS,
        "account_email": account_email,
        # Whether the choose-programme step needs to ask for an Oxford email:
        # false when the account itself signed up with one.
        "needs_oxford_email": not is_oxford_email(account_email),
        # Analyst (either track) is the ceiling — someone already there has
        # nothing left to apply for, so the page points them at reviewing.
        "is_reviewer": is_reviewer,
        # Lets the "nothing to apply for" screen say "You're an admin"
        # rather than assuming every reviewer is a Quant Analyst.
        "is_admin": bool(user.is_admin),
        "rules": {
            "session_seconds": SESSION_SECONDS,
            "motivation_seconds": MOTIVATION_SECONDS,
            "estimation_seconds": ESTIMATION_SECONDS,
        },
    }

    if membership in mb.ANALYST_MEMBERSHIPS:
        # Nothing below matters once someone has reached the ceiling — not
        # even an old application record, which the review-page link
        # replaces entirely rather than showing a stale "accepted" screen.
        out["status"] = "analyst"
        return out

    application = await _load(uid)
    if application is None:
        out["status"] = "none"
        return out

    await _resolve_and_notify(uid, application)

    out["status"] = application["status"]
    out["programme"] = application.get("programme")
    out["oxford_email"] = application.get("oxford_email") or ""
    out["applied_at"] = application.get("created_at")
    # Carried at every status, not just "cv" — the submitted/shortlisted/
    # decided screens all need to know whether this was a Fast-Track
    # application to explain why there's no written assessment to show.
    out["event_ticket"] = application.get("event_ticket")
    out["is_fast_tracked"] = application.get("event_ticket") == EVENT_TICKET_FAST_TRACK
    if application["status"] == S_CV:
        remaining = max(0, FAST_TRACK_CAPACITY - await _fast_track_count())
        out["fast_track_remaining"] = remaining
        out["fast_track_full"] = remaining <= 0
        out["fast_track_capacity"] = FAST_TRACK_CAPACITY
        # Whatever they've already signed up for on the events page, so the
        # choice step can show it and let them simply continue.
        out["event_signup"] = await outreach.ticket_of(uid)
        out["event"] = await outreach.event_summary()
        out["fast_track_used"] = _fast_track_used(application)
        profile_first, _, profile_last = (data.get("full_name") or "").strip().partition(" ")
        out["first_name"] = application.get("first_name") or profile_first
        out["last_name"] = application.get("last_name") or profile_last.strip()
        out["college"] = application.get("college") or ""
        out["degree"] = application.get("degree") or ""
        out["year_of_study"] = application.get("year_of_study") or ""
        out["linkedin"] = application.get("linkedin") or ""
    if application["status"] == S_OA_ACTIVE:
        out["oa"] = _oa_view(application)
    elif application["status"] in (S_SUBMITTED, S_SHORTLISTED, *DECIDED):
        out["submitted_at"] = application.get("submitted_at")
        out["decision"] = application["status"] if application["status"] in DECIDED else None
        # Lets the pipeline view show whether a decided application passed
        # through the interview stage or was decided straight from submitted.
        out["was_shortlisted"] = bool(application.get("shortlisted_at"))
        interview = application.get("interview")
        if interview:
            out["interview"] = _interview_view(interview)
        if application["status"] == S_SHORTLISTED:
            out["availability"] = _availability_of(application)
            out["availability_locked"] = bool(interview and interview.get("status") == INTERVIEW_CONFIRMED)
        if application["status"] in DECIDED:
            # A decided application doesn't disappear — the candidate keeps
            # seeing the outcome — but if they're still eligible (accepted
            # into Bootcamp, or rejected and free to try again), the page
            # offers a button to start a fresh one rather than that ever
            # happening silently.
            out["can_apply_again"] = out["eligible"]
    return out


def _interview_view(interview: Dict[str, Any]) -> Dict[str, Any]:
    """The candidate-facing slice of an interview record — everything here is
    already meant for them, since it's their own application."""
    return {
        "status": interview.get("status"),
        "when": interview.get("when"),
        "interviewer_name": interview.get("interviewer_name"),
        "interviewer_email": interview.get("interviewer_email"),
        "message": interview.get("message") or "",
        "responded_at": interview.get("responded_at"),
        "meet_link": interview.get("meet_link") or "",
    }


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
    """
    Open an application to a Bootcamp or Analyst programme.

    Also how a Bootcamp member applies on to Analyst, and how anyone whose
    last application was decided (accepted into Bootcamp, or rejected) opens
    a fresh one — see the DECIDED branch below.
    """
    uid = str(user.id)
    data = await _user_data(uid)
    if not mb.can_apply({**data, "is_admin": user.is_admin}):
        raise HTTPException(
            403,
            "Applications are open to general accounts below Analyst. "
            "You are already at the top of the programme, or hold a recruiter "
            "or host role.",
        )
    membership = mb.membership_of(data)
    allowed_programmes = mb.apply_programmes_for(membership)
    if req.programme not in allowed_programmes:
        raise HTTPException(400, f"Choose one of: {', '.join(allowed_programmes)}")
    if not mb.is_programme_open(req.programme):
        raise HTTPException(
            400,
            f"{req.programme} applications use a separate form, which isn't currently "
            "open on this platform.",
        )

    # General public applicants aren't Alpha Fund members yet, so nothing else
    # on the account vouches for them being current Oxford students — ask
    # directly. Anyone already a member (general, or progressing from
    # Bootcamp) has already cleared that bar, so there's nothing to re-confirm.
    if membership == mb.M_PUBLIC and not req.confirms_oxford_student:
        raise HTTPException(
            400,
            "Confirm you are currently studying at the University of Oxford to continue.",
        )

    oxford_email = _resolve_oxford_email(data.get("email") or "", req.oxford_email)

    existing = await _load(uid)
    if existing is not None and existing["status"] not in DECIDED:
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
        "applicant_category": membership,
        "confirmed_oxford_student": membership == mb.M_PUBLIC,
        "programme": req.programme,
        # Always start at the CV step, even for someone whose profile already
        # has one on file — jumping straight to oa_ready here used to skip
        # the explicit "is this still up to date?" question entirely, and
        # skip stamping this application's own CV snapshot along with it
        # (cv-confirm is the only place that happens).
        "status": S_CV,
        "created_at": _now(),
        "flags": {"paste": 0, "left_page": 0},
    }
    if existing is not None and existing["status"] in DECIDED:
        # A fresh application over a decided one — keep a breadcrumb of the
        # prior outcome for the admin rather than silently discarding it.
        application["previous_application"] = {
            "programme": existing.get("programme"),
            "status": existing["status"],
            "decided_at": existing.get("decided_at"),
            # Remembered so Fast-Track can't be used a second time: the
            # outreach sign-up outlives the decision, and would otherwise
            # carry straight into this fresh application (see
            # _fast_track_used).
            "event_ticket": existing.get("event_ticket"),
        }
    await _save(uid, application)
    return {"ok": True, "status": application["status"], "programme": req.programme}


def _fast_track_used(application: dict) -> bool:
    """True when this person's previous (decided) application already went
    through Fast-Track. It's a one-off route past the written assessment, not
    a standing pass — someone who used it and was turned down sits the
    assessment like everyone else next time. The same goes for an application
    an admin sent back through a redo (see redo_application): it has been
    moved onto the written route and mustn't be able to pick Fast-Track again
    from the CV step."""
    previous = application.get("previous_application") or {}
    return (previous.get("event_ticket") == EVENT_TICKET_FAST_TRACK
            or application.get("redo_previous_ticket") == EVENT_TICKET_FAST_TRACK)


async def fast_track_refusal(uid: str) -> Optional[str]:
    """Why the events page must not hand this person a Fast-Track place, or
    None if it may. Two cases, both about an application still in flight:

    * It has already chosen its ticket and moved past the CV step on the
      ordinary route. Taking a place then would count against the 50 and put
      them on the Fast-Track roster while their application still requires
      the assessment.
    * It's a reapplication after one that already used Fast-Track — the same
      once-only rule choose_event_ticket enforces, which the events page
      would otherwise get around (a sign-up made here carries into the
      application's choice screen).

    Moving between General and not attending stays allowed — neither changes
    the route. Kept here, next to event_ticket_locked, so events.py doesn't
    need to know the statuses."""
    application = await _load(uid)
    if not application or application.get("status") in DECIDED:
        return None
    if application.get("event_ticket") == EVENT_TICKET_FAST_TRACK:
        return None
    if application.get("status") != S_CV:
        return "You've already chosen your ticket in your application."
    if _fast_track_used(application):
        return "Fast-Track can only be used once — choose General attendance or Not attending."
    return None


async def event_ticket_locked(uid: str) -> bool:
    """True once an application has been fast-tracked past the CV step: the
    ticket is fixed then (it's what skipped the written assessment), so the
    events page mustn't be able to change it underneath the application."""
    application = await _load(uid)
    return bool(application
                and application.get("status") != S_CV
                and application.get("event_ticket") == EVENT_TICKET_FAST_TRACK)


async def sync_event_ticket(uid: str, ticket: str) -> None:
    """Carry a change made on the events page into an application that has
    already been through the choice step. One that hasn't is left alone, so
    it still shows the choice screen with the sign-up pre-selected."""
    application = await _load(uid)
    if application and application.get("status") == S_CV and application.get("event_ticket"):
        application["event_ticket"] = ticket
        application["event_registered_at"] = _now()
        await _save(uid, application)


@router.post("/event-ticket")
async def choose_event_ticket(req: EventTicketChoice, user: User = Depends(current_user)):
    """
    Register for the outreach event — General Attendance, Fast-Track CV
    Clinic, or not attending — as the first step of the application, before
    the CV step. Only available before a CV has been confirmed: once the
    application has moved on, the ticket (and what it unlocks) is fixed.
    """
    uid = str(user.id)
    application = await _load(uid)
    if application is None:
        raise HTTPException(400, "Start an application first")
    if application["status"] != S_CV:
        raise HTTPException(400, "Event registration is only available before you confirm your CV")
    if req.ticket not in EVENT_TICKETS:
        raise HTTPException(400, "Unknown ticket type")
    if req.ticket != EVENT_TICKET_NONE and await outreach.event_has_ended():
        # Same rule as the events page: once the event is over there's no
        # ticket left to take — and Fast-Track in particular must not become
        # a way past the written assessment after the CV clinic has happened.
        raise HTTPException(400, "The Quant Outreach event has already taken place")
    if req.ticket == EVENT_TICKET_FAST_TRACK and _fast_track_used(application):
        raise HTTPException(400, "Fast-Track can only be used once — choose General attendance "
                                 "or Not attending.")

    # The choice *is* the event sign-up (shared with the events page), so it
    # lands there too. Capacity is checked inside — only for claiming a new
    # Fast-Track place, never for keeping or leaving one.
    await outreach.set_ticket(uid, user.username, req.ticket)

    application["event_ticket"] = req.ticket
    application["event_registered_at"] = _now()
    await _save(uid, application)
    return {"ok": True, "event_ticket": req.ticket}


@router.post("/cv-confirm")
async def confirm_cv(payload: ConfirmCv, user: User = Depends(current_user)):
    """
    Confirm the CV now on the profile is the one to review, along with the
    college/degree/year/LinkedIn every applicant gives regardless of event
    ticket. A Fast-Track applicant skips straight to submitted from here —
    no written assessment — everyone else moves on to it as usual.
    """
    uid = str(user.id)
    application = await _load(uid)
    if application is None:
        raise HTTPException(400, "Start an application first")
    if application["status"] not in (S_CV, S_OA_READY):
        return {"ok": True, "status": application["status"]}

    data = await _user_data(uid)
    if not data.get("cv_blob_path"):
        raise HTTPException(400, "Upload your CV before continuing")

    first_name = " ".join((payload.first_name or "").split())[:100]
    last_name = " ".join((payload.last_name or "").split())[:100]
    if not first_name or not last_name:
        raise HTTPException(400, "Enter your first and last name to continue")
    college = (payload.college or "").strip()[:200]
    degree = (payload.degree or "").strip()[:200]
    year_of_study = (payload.year_of_study or "").strip()[:50]
    if not college or not degree or not year_of_study:
        raise HTTPException(400, "Fill in your college, degree and year of study to continue")

    application["cv_blob_path"] = data["cv_blob_path"]
    application["cv_confirmed_at"] = _now()
    # The name typed here is the one reviewers see and emails use — not the
    # profile's, which may be blank or a nickname. It only fills the profile
    # in when the profile has none, rather than overwriting someone's choice.
    application["first_name"] = first_name
    application["last_name"] = last_name
    application["full_name"] = f"{first_name} {last_name}"
    if not (data.get("full_name") or "").strip():
        await db_module.db.collection("users").document(uid).update({"full_name": application["full_name"]})
    application["email"] = data.get("email") or application.get("email", "")
    application["college"] = college
    application["degree"] = degree
    application["year_of_study"] = year_of_study
    application["linkedin"] = (payload.linkedin or "").strip()[:300] or None

    if application.get("event_ticket") == EVENT_TICKET_FAST_TRACK:
        # Fast-Track replaces the online written questions with a CV round
        # in person at the CV clinic, so there's nothing more to sit online:
        # the application is submitted now, and scored on the onsite rubric.
        application["status"] = S_SUBMITTED
        application["submitted_at"] = _now()
        await _save(uid, application)
        if not application.get("confirmation_sent_at"):
            await _send_fast_track_confirmation(application)
            application["confirmation_sent_at"] = _now()
            await _save(uid, application)
        return {"ok": True, "status": S_SUBMITTED}

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
        "section": "motivation",
        "motivation": {"prompt": MOTIVATION_PROMPT, "text": ""},
        "estimation": {"prompt": ESTIMATION_PROMPT, "text": ""},
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
    Save whichever question is currently open — as a draft while they type,
    or as final. Submitting the motivation question early moves straight on
    to the estimation one; submitting the estimation question finishes the
    sitting.

    Drafts matter: the clock runs whether the tab is open or not, so a
    crash mid-answer should not cost the whole thing.
    """
    uid = str(user.id)
    application = await _load(uid)
    if application is None or application.get("status") != S_OA_ACTIVE:
        raise HTTPException(400, "No assessment in progress")

    changed = await _resolve_and_notify(uid, application)
    oa = application.get("oa") or {}
    section = oa.get("section")

    if req.section and req.section != section:
        # Written for a question the server has already closed — typically
        # the browser's zero-second auto-submit of part one landing just after
        # a poll moved the sitting on to part two. Never let it fill in (let
        # alone finish) the question that's open now; at most it tops up the
        # part-one answer it was actually written for.
        await _bank_late_motivation(uid, application, req)
        return {"ok": True, "status": application["status"], "section": section, "stale": True}

    if changed:
        # The clock beat this submission; whatever was last autosaved stands.
        return {"ok": True, "status": application["status"], "section": section}

    if section not in ("motivation", "estimation"):
        return {"ok": True, "status": application["status"], "section": section}

    part = oa.setdefault(section, {})
    part["prompt"] = MOTIVATION_PROMPT if section == "motivation" else ESTIMATION_PROMPT
    part["text"] = (req.text or "")[:20000]

    if req.final:
        if section == "motivation":
            _close_motivation(oa)
            await _save(uid, application)
        else:
            await _finish_and_persist(uid, application, "completed")
        return {"ok": True, "status": application["status"], "section": oa["section"]}

    await _save(uid, application)
    return {"ok": True, "status": application["status"], "section": oa["section"]}


async def _bank_late_motivation(uid: str, application: dict, req: WrittenSubmit) -> None:
    """
    Keep a part-one answer that arrived a moment after its own deadline.

    Only when the question was closed *by the clock* (it used its full
    allotment — a candidate who submitted early has already said that was
    their answer, and a stale draft mustn't overwrite it) and only within
    LATE_SUBMIT_GRACE_SECONDS of that deadline. The text is replaced and its
    word count recomputed; ``seconds_used`` and every clock stay exactly as
    they were, so this never buys extra time on either question.
    """
    if req.section != "motivation":
        return
    oa = application.get("oa") or {}
    motivation = oa.get("motivation") or {}
    if "submitted_at" not in motivation:
        return
    if (motivation.get("seconds_used") or 0) < MOTIVATION_SECONDS:
        return
    if _overdue(oa.get("started_at"), MOTIVATION_SECONDS) > LATE_SUBMIT_GRACE_SECONDS:
        return
    motivation["text"] = (req.text or "")[:20000]
    motivation["word_count"] = len(motivation["text"].split())
    await _save(uid, application)


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


@router.post("/availability")
async def submit_availability(req: AvailabilitySubmit, user: User = Depends(current_user)):
    """
    Save which hours a shortlisted candidate is free for an interview.

    Whole-hour clicks on a two-week grid, not free text — an analyst picks
    one of these slots to actually schedule the interview, so the shape has
    to match exactly what the admin page renders. Locked once the interview
    itself is confirmed, since changing availability after a time is fixed
    has nothing left to do.
    """
    uid = str(user.id)
    application = await _load(uid)
    if application is None:
        raise HTTPException(404, "No application on file")
    if application.get("status") != S_SHORTLISTED:
        raise HTTPException(400, "Availability can only be set once you've been shortlisted for interview")
    interview = application.get("interview")
    if interview and interview.get("status") == INTERVIEW_CONFIRMED:
        raise HTTPException(400, "Your interview is already confirmed — there's nothing left to set")

    application["availability"] = _valid_availability_slots(req.slots)
    application["availability_slot_minutes"] = AVAILABILITY_SLOT_MINUTES
    application["availability_updated_at"] = _now()
    await _save(uid, application)
    return {"ok": True, "availability": application["availability"]}


# ── Admin / reviewer ─────────────────────────────────────────────────────────

def _review_summary(application: Dict[str, Any], viewer_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Every reviewer's CV/written/interview scores, and the averages across them.

    Several Quant Analyst members can score the same applicant independently;
    nothing here picks a single reviewer's word over another's, which is the
    whole point of averaging instead of just taking the latest score entered.
    Interview scores apply to everyone alike, fast-tracked or not — unlike
    the written score, there's no route through the pipeline that skips the
    interview itself.
    """
    reviews = application.get("reviews") or {}
    # A CV score only counts once that reviewer has scored every criterion
    # of the rubric this applicant is scored on, and it's always the total
    # of those criteria, so a rubric change can't leave a stale total behind.
    fast_tracked = application.get("event_ticket") == EVENT_TICKET_FAST_TRACK
    cv_keys = [c["key"] for c in _cv_rubric_for(fast_tracked)]
    cv_scores = [sum(r["cv_rubric"][k] for k in cv_keys) for r in reviews.values()
                 if isinstance(r.get("cv_rubric"), dict) and all(k in r["cv_rubric"] for k in cv_keys)]

    def _given(key: str) -> List[int]:
        return [r[key] for r in reviews.values() if r.get(key) is not None]

    def _avg(scores: List[int]) -> Optional[float]:
        return round(sum(scores) / len(scores), 1) if scores else None

    interview_scores = [r["interview_score"] for r in reviews.values()
                        if r.get("interview_rubric") and r.get("interview_score") is not None]
    return {
        "count": len(reviews),
        "cv_avg": _avg(cv_scores),
        "cv_max": _cv_max(fast_tracked),
        "interview_avg": _avg(interview_scores),
        "interview_max": INTERVIEW_MAX,
        # How many reviewers each average is over — each section is scored
        # by whoever chose to score it, not necessarily everyone.
        "cv_n": len(cv_scores), "interview_n": len(interview_scores),
        "entries": sorted(
            [{"reviewer_id": rid, **r} for rid, r in reviews.items()],
            key=lambda r: r.get("updated_at") or _now(),
        ),
        "mine": reviews.get(viewer_id) if viewer_id else None,
    }


def _admin_interview_view(interview: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The interview record with its datetimes normalised to plain aware
    UTC — Firestore can hand back a provider-specific datetime subclass, and
    the admin template calls .strftime() on `when` directly. `when_london` is
    the same instant converted for display, since the template shows it in
    London time to match the availability grid."""
    if not interview:
        return None
    when = _as_utc(interview.get("when"))
    return {
        **interview,
        "when": when,
        "when_london": when.astimezone(LONDON_TZ) if when else None,
        "scheduled_at": _as_utc(interview.get("scheduled_at")),
        "responded_at": _as_utc(interview.get("responded_at")),
    }


def _review_row(uid: str, application: Dict[str, Any], viewer_id: Optional[str] = None) -> Dict[str, Any]:
    oa = application.get("oa") or {}
    motivation = oa.get("motivation") or {}
    estimation = oa.get("estimation") or {}
    interview_view = _admin_interview_view(application.get("interview"))
    review_summary = _review_summary(application, viewer_id)
    # Surfaced as its own tab on the admin page: an interview assigned to me
    # that I haven't yet logged a score for. Drops off once I score it, once
    # it's declined (nothing to do here until it's rescheduled), or once
    # someone else is proposed instead.
    is_my_pending_interview = bool(
        interview_view and viewer_id
        and interview_view.get("interviewer_id") == viewer_id
        and interview_view.get("status") != INTERVIEW_DECLINED
        and (review_summary.get("mine") or {}).get("interview_score") is None
    )
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
        "event_ticket": application.get("event_ticket") or EVENT_TICKET_NONE,
        "is_fast_tracked": application.get("event_ticket") == EVENT_TICKET_FAST_TRACK,
        "college": application.get("college") or "",
        "degree": application.get("degree") or "",
        "year_of_study": application.get("year_of_study") or "",
        "linkedin": application.get("linkedin") or "",
        "created_at": _as_utc(application.get("created_at")),
        "submitted_at": _as_utc(application.get("submitted_at")),
        "last_reminded_at": _as_utc(application.get("last_reminded_at")),
        "decided_at": _as_utc(application.get("decided_at")),
        "decided_by": application.get("decided_by") or "",
        "cv_uploaded": bool(application.get("cv_blob_path")),
        "motivation_text": motivation.get("text", ""),
        "motivation_words": motivation.get("word_count") or len((motivation.get("text") or "").split()),
        "motivation_seconds": motivation.get("seconds_used"),
        "estimation_text": estimation.get("text", ""),
        "estimation_words": estimation.get("word_count") or len((estimation.get("text") or "").split()),
        "estimation_seconds": estimation.get("seconds_used"),
        "flags": application.get("flags") or {},
        "finish_reason": oa.get("finish_reason"),
        "decision_note": application.get("decision_note") or "",
        "review": review_summary,
        "interview": interview_view,
        "is_my_pending_interview": is_my_pending_interview,
        "shortlisted_at": _as_utc(application.get("shortlisted_at")),
        "shortlisted_by": application.get("shortlisted_by") or "",
        "auto_shortlist": application.get("auto_shortlist"),
        "availability": _availability_of(application),
        "availability_updated_at": _as_utc(application.get("availability_updated_at")),
        "previous_application": (
            {**application["previous_application"],
             "decided_at": _as_utc(application["previous_application"].get("decided_at"))}
            if application.get("previous_application") else None
        ),
    }


async def _list_reviewers() -> List[Dict[str, str]]:
    """Everyone who could plausibly be assigned to interview a candidate:
    admins and Analyst members (Fundamental or Quant), provided they have an
    email on file — without one there's nothing to put in the invite."""
    docs = await db_module.db.collection("users").get()
    out: List[Dict[str, str]] = []
    for d in docs:
        data = d.to_dict() or {}
        email = data.get("email") or ""
        if not email:
            continue
        if data.get("is_admin") or mb.membership_of(data) in mb.ANALYST_MEMBERSHIPS:
            out.append({
                "id": d.id,
                "name": data.get("full_name") or data.get("username") or d.id,
                "email": email,
            })
    out.sort(key=lambda r: r["name"].lower())
    return out


async def _ranked_rows(viewer_id: str) -> List[Dict[str, Any]]:
    """Every applicant, ranked by CV round score (the CV screen's ranking) —
    shared by the admin page and the Excel export so the two never disagree.

    Two scores per row, both on a 10-point scale so applicants scored out of
    different totals compare fairly (the CV round is out of 15, or 11 for
    Fast-Track; the interview out of 15):

    * ``cv_rank_score``: the CV round alone. What ``rank`` is by.
    * ``combined_score``: the average of the CV round and the interview
      round, which the page ranks the shortlist by. With no interview
      scored yet it's just the CV round.

    Someone nobody has finished scoring sorts to the bottom rather than
    reading as a zero, since they haven't had their turn.
    """
    docs = await db_module.db.collection(COLLECTION).get()
    rows = [_review_row(d.id, d.to_dict() or {}, viewer_id=viewer_id) for d in docs]

    def _on_ten(avg: Optional[float], out_of: int) -> Optional[float]:
        return round(avg * SCORE_MAX / out_of, 2) if avg is not None else None

    for r in rows:
        review = r["review"]
        cv = _on_ten(review["cv_avg"], review["cv_max"])
        interview = _on_ten(review["interview_avg"], review["interview_max"])
        parts = [v for v in (cv, interview) if v is not None]
        r["cv_rank_score"] = cv
        r["combined_score"] = round(sum(parts) / len(parts), 2) if parts else None
    rows.sort(key=lambda r: (
        r["cv_rank_score"] is None,
        -(r["cv_rank_score"] or 0),
        (r["username"] or "").lower(),
    ))
    for i, r in enumerate(rows, start=1):
        r["rank"] = i if r["cv_rank_score"] is not None else None
    return rows


async def _outreach_roster(rows: List[Dict[str, Any]]) -> tuple:
    """Who's coming to the outreach event, by ticket, oldest sign-up first.

    Built from the event sign-ups (so someone who registered on the events
    page but hasn't started an application is here too) plus any application
    whose ticket says so, with the application's college/degree/CV where one
    exists. Not the score ranking — a roster reads as "who signed up"."""
    by_uid = {r["user_id"]: r for r in rows}
    entries: Dict[str, Dict[str, Any]] = {}
    for s in await outreach.confirmed_signups():
        uid = s.get("user_id")
        entries[uid] = {"ticket": s.get("ticket"), "at": s.get("created_at"), "signup": s}
    for r in rows:
        if r["event_ticket"] in (EVENT_TICKET_FAST_TRACK, EVENT_TICKET_GENERAL) and r["user_id"] not in entries:
            entries[r["user_id"]] = {"ticket": r["event_ticket"], "at": r["created_at"], "signup": None}

    fast, general = [], []
    for uid, e in entries.items():
        app_row = by_uid.get(uid)
        if app_row is not None:
            row = {**app_row, "has_application": True}
        else:
            s = e["signup"] or {}
            row = {"user_id": uid, "has_application": False, "username": s.get("username", ""),
                   "full_name": s.get("full_name", ""), "college": "", "degree": "",
                   "year_of_study": "", "cv_uploaded": False}
        row["_at"] = _as_utc(e["at"]) or _now()
        (fast if e["ticket"] == EVENT_TICKET_FAST_TRACK else general).append(row)
    fast.sort(key=lambda r: r["_at"])
    general.sort(key=lambda r: r["_at"])
    return fast, general


@router.get("/admin", include_in_schema=False)
async def admin_applications(request: Request, reviewer: User = Depends(require_reviewer)):
    rows = await _ranked_rows(str(reviewer.id))
    scored = [r for r in rows if r["review"]["count"] > 0]
    fast_track_rows, general_rows = await _outreach_roster(rows)
    return templates.TemplateResponse("applications_admin.html", {
        "request": request,
        "app_name": "AlphaBook",
        "rows": rows,
        "total": len(rows),
        "scored": len(scored),
        "motivation_prompt": MOTIVATION_PROMPT,
        "estimation_prompt": ESTIMATION_PROMPT,
        "is_admin": reviewer.is_admin,
        "score_min": SCORE_MIN,
        "score_max": SCORE_MAX,
        "online_cv_rubric": ONLINE_CV_RUBRIC,
        "onsite_cv_rubric": ONSITE_CV_RUBRIC,
        "auto_shortlist_reasons": AUTO_SHORTLIST_REASONS,
        "note_max_chars": NOTE_MAX_CHARS,
        "interview_rubric": INTERVIEW_RUBRIC,
        "interview_max": INTERVIEW_MAX,
        "interview_timers": INTERVIEW_TIMERS,
        "reviewers": await _list_reviewers(),
        "viewer_id": str(reviewer.id),
        "interview_minutes": INTERVIEW_MINUTES,
        "fast_track_rows": fast_track_rows,
        "general_rows": general_rows,
        "fast_track_capacity": FAST_TRACK_CAPACITY,
    })


@router.get("/admin/interview-guide", include_in_schema=False)
async def interview_guide(request: Request, reviewer: User = Depends(require_reviewer)):
    """The scoring guide: both CV-round rubrics with the automatic shortlist
    criteria, then the interview rubric and its rules. The scoring view uses
    the same rubrics and hint schedule."""
    return templates.TemplateResponse("interview_guide.html", {
        "request": request,
        "app_name": "AlphaBook",
        "online_cv_rubric": ONLINE_CV_RUBRIC,
        "onsite_cv_rubric": ONSITE_CV_RUBRIC,
        "auto_shortlist_reasons": AUTO_SHORTLIST_REASONS,
        "cv_max": _cv_max(False),
        "interview_rubric": INTERVIEW_RUBRIC,
        "interview_max": INTERVIEW_MAX,
        "interview_timers": INTERVIEW_TIMERS,
    })


@router.get("/admin/export.xlsx", include_in_schema=False)
async def export_applications(reviewer: User = Depends(require_reviewer)):
    """
    Every applicant as one spreadsheet row: contact details, the combined
    and per-category reviewer scores, both written answers, every reviewer's
    individual note, interview status, and the availability they submitted.
    Open to the same audience as the admin page itself — anyone who can read
    this on screen can already see everything that ends up in the file.
    """
    rows = await _ranked_rows(str(reviewer.id))

    wb = Workbook()
    ws = wb.active
    ws.title = "Applicants"
    headers = [
        "Rank", "Username", "Full name", "Email", "Oxford email", "Category",
        "Event ticket", "College", "Degree", "Year of study", "LinkedIn",
        "Programme", "Status", "Combined score (/10)", "CV avg (/18)", "Interview avg (/15)",
        "Reviewer count", "Reviewer notes", "Created at", "Submitted at",
        "Shortlisted at", "Decided at", "Decided by", "Decision note",
        "CV on file", "Motivation minutes", "Motivation text",
        "Estimation minutes", "Estimation text", "Paste flags", "Left-page flags",
        "Interview status", "Interview when", "Interviewer", "Interview message",
        "Availability submitted",
    ]
    ws.append(headers)

    def _dt(value: Any) -> str:
        v = _as_utc(value)
        return v.strftime("%Y-%m-%d %H:%M UTC") if v else ""

    for r in rows:
        def _score(e: Dict[str, Any], key: str) -> Any:
            # A key that exists with a None value (scored CV, not yet the
            # interview) reads as a dash, not the word "None".
            return "—" if e.get(key) is None else e[key]

        def _cv(e: Dict[str, Any]) -> str:
            if e.get("cv_score") is None:
                return "—"
            return f"{e['cv_score']}/{e.get('cv_max') or r['review']['cv_max']}"

        def _interview(e: Dict[str, Any]) -> str:
            if e.get("interview_score") is None:
                return "—"
            return f"{e['interview_score']}/{INTERVIEW_MAX}"

        reviewer_notes = "; ".join(
            f"{e['reviewer_name']}: CV {_cv(e)}, interview {_interview(e)}"
            + (f' ("{e["note"]}")' if e.get("note") else "")
            for e in r["review"]["entries"]
        )
        interview = r.get("interview") or {}
        availability = "; ".join(_fmt_slot(v) for v in r.get("availability") or [])
        fast_tracked = r["is_fast_tracked"]
        ws.append([
            r["rank"] or "", r["username"], r["full_name"], r["email"], r["oxford_email"],
            r["applicant_category"],
            r["event_ticket"], r["college"], r["degree"], r["year_of_study"], r["linkedin"],
            r["programme"], r["status"],
            r["combined_score"] if r["combined_score"] is not None else "",
            r["review"]["cv_avg"] if r["review"]["cv_avg"] is not None else "",
            r["review"]["interview_avg"] if r["review"]["interview_avg"] is not None else "",
            r["review"]["count"], reviewer_notes,
            _dt(r["created_at"]), _dt(r["submitted_at"]), _dt(r["shortlisted_at"]),
            _dt(r["decided_at"]), r["decided_by"], r["decision_note"],
            "Yes" if r["cv_uploaded"] else "No",
            "Fast-tracked" if fast_tracked else (round(r["motivation_seconds"] / 60, 1) if r["motivation_seconds"] else ""),
            "Fast-Track: CV round in person, no online answers" if fast_tracked else r["motivation_text"],
            "Fast-tracked" if fast_tracked else (round(r["estimation_seconds"] / 60, 1) if r["estimation_seconds"] else ""),
            "Fast-Track: CV round in person, no online answers" if fast_tracked else r["estimation_text"],
            r["flags"].get("paste", 0), r["flags"].get("left_page", 0),
            interview.get("status") or "", _dt(interview.get("when")),
            interview.get("interviewer_name") or "", interview.get("message") or "",
            availability or "None submitted",
        ])

    widths = [6, 14, 18, 24, 24, 16, 14, 20, 18, 12, 26,
              16, 12, 14, 8, 12, 14, 40, 17, 17, 17, 17, 14, 24,
              10, 12, 50, 12, 50, 10, 12, 14, 17, 16, 30, 40]
    for i, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = width

    # Long free text (the two written answers, reviewers' notes, the
    # interview message, availability) wraps within its column rather than
    # running on as one line, and every row reads from the top.
    wrapped = {headers.index(h) + 1 for h in (
        "Reviewer notes", "Decision note", "Motivation text", "Estimation text",
        "Interview message", "Availability submitted")}
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(wrap_text=cell.column in wrapped, vertical="top")
    ws.freeze_panes = "C2"   # headers and names stay put while scrolling

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    filename = f"alpha_fund_applicants_{_now().strftime('%Y%m%d')}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


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


def _checked_cv_rubric(raw: Dict[str, Optional[int]], fast_tracked: bool) -> Dict[str, int]:
    """The criteria scored so far, each with one of its own options' points.
    May be partial (the scoring view saves as each one is picked); a null
    value un-scores that criterion."""
    criteria = _cv_rubric_for(fast_tracked)
    allowed = {c["key"]: {o["points"] for o in c["options"]} for c in criteria}
    out: Dict[str, int] = {}
    for key, points in raw.items():
        # Not part of this applicant's rubric (e.g. left over from an older
        # version of it): dropped rather than refused, so it can't block a
        # reviewer's autosave.
        if points is None or key not in allowed:
            continue
        if points not in allowed[key]:
            raise HTTPException(400, f"That isn't one of the options for {key}")
        out[key] = int(points)
    return out


def _checked_interview_rubric(raw: Dict[str, Optional[int]]) -> Dict[str, int]:
    """Like _checked_cv_rubric: the criteria scored so far, each with one of
    its own options' points; a null value un-scores that criterion."""
    allowed = {c["key"]: {o["points"] for o in c["options"]} for c in INTERVIEW_RUBRIC}
    out: Dict[str, int] = {}
    for key, points in raw.items():
        if points is None and key in allowed:
            continue
        if key not in allowed:
            raise HTTPException(400, f"Unknown interview criterion: {key}")
        if points not in allowed[key]:
            raise HTTPException(400, f"That isn't one of the options for {key}")
        out[key] = int(points)
    return out


@router.post("/admin/{user_id}/score")
async def submit_score(user_id: str, payload: ReviewScore, reviewer: User = Depends(require_reviewer)):
    """
    Save this reviewer's scores and comments for one applicant.

    One entry per reviewer, keyed by their own id, so any number of
    reviewers can score the same applicant and the averages always reflect
    everyone's latest. Only the fields in the request change — the scoring
    view autosaves every click and keystroke on its own — and null clears a
    field, so everything stays editable at any time.
    """
    sent = payload.model_fields_set
    if "cv_score" in sent and payload.cv_score is not None:
        raise HTTPException(400, "Score the CV using the criteria")
    if "interview_score" in sent and payload.interview_score is not None:
        raise HTTPException(400, "Score the interview using the criteria")
    fields = sent & {"cv_rubric", "interview_rubric", "note"}
    if not fields:
        raise HTTPException(400, "Enter at least one score")

    application = await _load(user_id)
    if application is None:
        raise HTTPException(404, "No such application")
    if application.get("status") not in SCORABLE:
        raise HTTPException(400, "This application hasn't been submitted yet — nothing to score")

    fast_tracked = application.get("event_ticket") == EVENT_TICKET_FAST_TRACK
    reviews = application.setdefault("reviews", {})
    entry = dict(reviews.get(str(reviewer.id), {}))
    entry["reviewer_name"] = reviewer.username

    if "cv_rubric" in fields:
        cv_rubric = _checked_cv_rubric(payload.cv_rubric or {}, fast_tracked)
        complete = all(c["key"] in cv_rubric for c in _cv_rubric_for(fast_tracked))
        entry.update({
            "cv_rubric": cv_rubric,
            # Only a finished rubric has a score: a half-scored CV's running
            # total would read as a low score, not an unfinished one.
            "cv_score": sum(cv_rubric.values()) if complete else None,
            "cv_max": _cv_max(fast_tracked),
        })
    if "interview_rubric" in fields:
        interview_rubric = _checked_interview_rubric(payload.interview_rubric or {})
        complete = len(interview_rubric) == len(INTERVIEW_RUBRIC)
        entry.update({
            "interview_rubric": interview_rubric,
            "interview_score": sum(interview_rubric.values()) if complete else None,
            "interview_max": INTERVIEW_MAX,
        })
    if "note" in fields:
        entry["note"] = (payload.note or "").strip()[:NOTE_MAX_CHARS]
    entry["updated_at"] = _now()

    reviews[str(reviewer.id)] = entry
    await _save_review(user_id, str(reviewer.id), entry)
    return {"ok": True, "review": _review_summary(application, str(reviewer.id))}


@router.post("/admin/{user_id}/interview")
async def schedule_interview(user_id: str, payload: ScheduleInterview,
                              reviewer: User = Depends(require_reviewer)):
    """
    Propose an interview slot — open to any reviewer, not just admins,
    since deciding *who* interviews someone is exactly the kind of call a
    Quant Analyst member should be able to make without needing an admin in
    the loop. Only available once shortlisted: that's the whole point of the
    shortlist stage. Re-submitting (a different interviewer, a different
    time) overwrites whatever was pending and sends a fresh proposal.
    """
    application = await _load(user_id)
    if application is None:
        raise HTTPException(404, "No such application")
    if application.get("status") != S_SHORTLISTED:
        raise HTTPException(400, "Only a shortlisted applicant can have an interview scheduled")

    reviewers_by_id = {r["id"]: r for r in await _list_reviewers()}
    interviewer = reviewers_by_id.get(payload.interviewer_id)
    if interviewer is None:
        raise HTTPException(400, "Choose a valid interviewer")

    when = payload.when
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    end = when + dt.timedelta(minutes=INTERVIEW_MINUTES)

    # A re-proposal (a different time for the same pending interview) moves
    # the existing Calendar event instead of minting a fresh Meet link and
    # leaving the old event stray on the calendar. Only with the same
    # interviewer, though: moving the event changes its time, not its guest
    # list, so a new interviewer would never be invited and the old one would
    # keep a slot they're no longer in. Then the old event goes and a fresh
    # one is made with the right people on it.
    previous = application.get("interview") or {}
    meet_link = previous.get("meet_link")
    gcal_event_id = previous.get("gcal_event_id")
    # What the .ics attachment needs to point at the real Calendar event.
    ics_identity = {k: previous.get(k) for k in ("gcal_ical_uid", "gcal_organizer", "gcal_sequence")}
    same_interviewer = previous.get("interviewer_id") == interviewer["id"]
    moved = False
    if gcal_event_id and same_interviewer:
        moved = await gcal.update_event_time(gcal_event_id, when, end)
        if isinstance(moved, dict):
            ics_identity["gcal_sequence"] = moved.get("sequence")
    elif gcal_event_id:
        await _cancel_interview_event(application)
    if not moved:
        candidate_email = application.get("oxford_email") or application.get("email") or ""
        created = await gcal.create_meet_event(
            summary=f"Alpha Fund interview with {application.get('full_name') or application.get('username')}",
            description=f"{application.get('programme') or 'Alpha Fund'} interview.",
            start=when, end=end,
            attendee_emails=[e for e in (candidate_email, interviewer["email"]) if e],
        )
        meet_link = created["meet_link"] if created else None
        gcal_event_id = created["event_id"] if created else None
        ics_identity = {
            "gcal_ical_uid": (created or {}).get("ical_uid"),
            "gcal_organizer": (created or {}).get("organizer_email"),
            "gcal_sequence": (created or {}).get("sequence"),
        }

    interview = {
        "interviewer_id": interviewer["id"],
        "interviewer_name": interviewer["name"],
        "interviewer_email": interviewer["email"],
        "message": (payload.message or "").strip()[:1000],
        "when": when,
        "status": INTERVIEW_PROPOSED,
        "scheduled_by": reviewer.username,
        "scheduled_at": _now(),
        "responded_at": None,
        "candidate_note": None,
        "meet_link": meet_link,
        "gcal_event_id": gcal_event_id,
        **ics_identity,
    }
    application["interview"] = interview
    await _save(user_id, application)
    await _send_interview_proposal_email(application, interview)
    return {"ok": True, "interview": interview}


async def _cancel_interview_event(application: dict) -> None:
    """Take the interview's Calendar event (and its Meet link) off the
    calendar, if it has one — on a rejection, a redo or a deletion, where the
    interview is no longer going to happen and a live invite would just
    mislead both sides. Not on accept: that interview has already been held.
    Best effort: a Google failure is logged and never blocks the decision."""
    event_id = (application.get("interview") or {}).get("gcal_event_id")
    if not event_id:
        return
    try:
        if not await gcal.delete_event(event_id):
            log.warning("applications: couldn't remove interview event %s", event_id)
    except Exception:
        log.exception("applications: failed to remove interview event %s", event_id)


# What a reminder says, by where the applicant is stuck. Nothing to send for
# a status with no gap to close (mid-assessment, already decided).
_REMINDER_COPY: Dict[str, Dict[str, str]] = {
    S_CV: {
        "subject": "Alpha Fund: finish the CV step of your application",
        "body": ("<p>You started an application to <strong>{programme}</strong>. Sign in to "
                 "finish the CV step of your application (upload or confirm your CV and add "
                 "your details), and you can carry straight on from there.</p>"),
        "cta": "Continue your application",
    },
    S_OA_READY: {
        "subject": "Alpha Fund: your assessment is ready when you are",
        "body": ("<p>Your CV is in for <strong>{programme}</strong>, and your {minutes}-minute "
                 "assessment is ready. There's no deadline on starting it, but once you do "
                 "it runs straight through in one sitting, so pick a quiet {minutes} minutes.</p>"),
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

    name = html.escape(application.get("full_name") or application.get("username") or "there")
    programme = html.escape(application.get("programme") or "the programme")
    body = f"<p>Hi {name},</p>" + copy["body"].format(programme=programme, minutes=SESSION_SECONDS // 60)
    if payload.note:
        body += (f'<p style="color:#555;">A note from the committee: '
                 f'{html.escape(payload.note.strip()[:400])}</p>')

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
        "subject": "Alpha Fund: you've been shortlisted for interview",
        "title": "Shortlisted for interview",
        "body": ("<p>Your <strong>{programme}</strong> application has been shortlisted.</p>"
                 "<p>Please enter your availability on the portal so the committee can schedule "
                 "an interview with you.</p>"),
        "cta_label": "Enter your availability",
        "cta_url": "/apply",
    },
    S_ACCEPTED: {
        "subject": "Alpha Fund: you're in",
        "title": "Application accepted",
        "body": ("<p>Congratulations! You've been accepted onto <strong>{programme}</strong>. "
                 "Welcome to Alpha Fund.</p>"),
    },
    S_REJECTED: {
        "subject": "Alpha Fund: an update on your application",
        "title": "Application decision",
        "body": ("<p>Thank you for applying to <strong>{programme}</strong>. On this occasion "
                 "we won't be taking your application further, but we'd encourage you to keep "
                 "playing and apply again in a future round.</p>"),
    },
}

# Analyst rejections read "next cycle" rather than "a future round" — Analyst
# recruiting genuinely runs in cycles, unlike Bootcamp, which is the more
# casual/rolling track.
_REJECTED_BODY_ANALYST = (
    "<p>Thank you for applying to <strong>{programme}</strong>. On this occasion "
    "we won't be taking your application further, but we'd encourage you to apply "
    "again in the next cycle.</p>"
)


async def _send_decision_email(application: dict, status: str) -> None:
    to = application.get("oxford_email") or application.get("email")
    copy = _DECISION_COPY.get(status)
    if not to or not copy:
        return
    name = html.escape(application.get("full_name") or application.get("username") or "there")
    programme = application.get("programme") or "the programme"
    body_template = copy["body"]
    if status == S_REJECTED and programme in mb.ANALYST_MEMBERSHIPS:
        body_template = _REJECTED_BODY_ANALYST
    body = f"<p>Hi {name},</p>" + body_template.format(programme=html.escape(programme))
    cta_url = copy.get("cta_url")
    await mailer.send_email(
        to=to, subject=copy["subject"], title=copy["title"], body_html=body,
        cta_label=copy.get("cta_label"), cta_url=f"{BASE_URL}{cta_url}" if cta_url else None,
    )


# ── Interview scheduling ─────────────────────────────────────────────────────
# Stored in UTC (an absolute instant), shown in London time in every email —
# the availability grid candidates and analysts both work from is already in
# London hours, so a proposal or confirmation email showing a different zone
# would just be confusing. The .ics attachment always carries the raw UTC
# timestamp regardless, so each recipient's own calendar still localises it
# correctly no matter what the email text says.

def _fmt_when(when: dt.datetime) -> str:
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    return when.astimezone(LONDON_TZ).strftime("%a %d %b %Y, %H:%M") + " London"


def _build_interview_ics(application: dict, interview: dict) -> bytes:
    """The one calendar invite both the proposal and the confirmation emails
    attach — built fresh each time so a re-proposed time (a different slot
    overwriting a pending one) produces its own invite rather than reusing a
    stale one, and SEQUENCE:0 is fine either way since each carries a UID
    derived from the actual start time."""
    candidate_name = application.get("full_name") or application.get("username") or "Candidate"
    interviewer_name = interview.get("interviewer_name") or "Interviewer"
    interviewer_email = interview.get("interviewer_email") or ""
    candidate_to = application.get("oxford_email") or application.get("email") or ""
    programme = application.get("programme") or "the programme"
    when = interview["when"]
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    end = when + dt.timedelta(minutes=INTERVIEW_MINUTES)
    meet_link = interview.get("meet_link")
    description = f"{programme} interview with {interviewer_name}."
    if meet_link:
        description += f"\nJoin with Google Meet: {meet_link}"
    common = dict(
        summary=f"Alpha Fund interview with {candidate_name}",
        description=description,
        start=when, end=end,
        attendee_name=candidate_name, attendee_email=candidate_to,
        location=meet_link or "Online, details to follow",
    )
    if interview.get("gcal_ical_uid") and interview.get("gcal_organizer"):
        # The real Calendar event: its own UID, organised by the account whose
        # calendar it's on, with both people as guests (they already are, on
        # Google's side). That's what lets Gmail's card load the event and
        # offer to add it, rather than "Unable to load event".
        return mailer.build_ics_invite(
            uid=interview["gcal_ical_uid"], uid_domain=None,
            organizer_name="Alpha Fund", organizer_email=interview["gcal_organizer"],
            more_attendees=[(interviewer_name, interviewer_email)],
            sequence=interview.get("gcal_sequence") or 0,
            **common,
        )
    # No Calendar event behind it (Google not connected, or an interview
    # scheduled before this was recorded). Published rather than sent as a
    # request: there's no organiser's calendar for a mail client to look the
    # event up on, so it's offered as a plain event to add.
    return mailer.build_ics_invite(
        uid=f"interview-{application.get('user_id')}-{int(when.timestamp())}",
        organizer_name=interviewer_name, organizer_email=interviewer_email or mailer.sender() or "",
        method="PUBLISH",
        **common,
    )


def _meet_block(interview: dict) -> str:
    """The "Meeting link" line, when there is one. The link comes from
    Google, but it's escaped all the same — it lands inside an attribute."""
    link = interview.get("meet_link")
    if not link:
        return ""
    link = html.escape(link)
    return f'<p><strong>Meeting link:</strong> <a href="{link}">{link}</a></p>'


async def _send_interview_proposal_email(application: dict, interview: dict) -> None:
    """To the candidate, with the interviewer cc'd, the same as the
    confirmation: the interviewer gets the proposed slot (and its calendar
    invite) straight away, rather than only hearing once it's confirmed."""
    to = application.get("oxford_email") or application.get("email")
    if not to:
        return
    raw_interviewer_email = interview.get("interviewer_email") or ""
    cc = raw_interviewer_email if raw_interviewer_email and raw_interviewer_email != to else None
    # Every value below is user- or reviewer-entered (names, the note, an
    # email address) — escaped so none of it can become markup in an email
    # sent from our own address.
    name = html.escape(application.get("full_name") or application.get("username") or "there")
    programme = html.escape(application.get("programme") or "the programme")
    interviewer_name = html.escape(interview.get("interviewer_name") or "")
    interviewer_email = html.escape(interview.get("interviewer_email") or "")
    note_block = ""
    if interview.get("message"):
        note_block = (f'<p style="color:#555;">A note from {interviewer_name}: '
                      f'&ldquo;{html.escape(interview["message"])}&rdquo;</p>')
    body = (
        f"<p>Hi {name},</p>"
        f"<p>The committee would like to interview you for <strong>{programme}</strong>.</p>"
        f"<p><strong>Proposed time:</strong> {_fmt_when(interview['when'])}<br>"
        f"<strong>Interviewer:</strong> {interviewer_name} "
        f"(<a href=\"mailto:{interviewer_email}\">{interviewer_email}</a>), copied in</p>"
        f"{_meet_block(interview)}"
        f"{note_block}"
        f"<p>A calendar invite for this slot is attached, so you can hold it while you decide.</p>"
        f"<p>Sign in and open your application to confirm this time. If it doesn't work, "
        f"you can say so there too, and {interviewer_name} will be in touch "
        f"directly to find another.</p>"
    )
    await mailer.send_email(
        to=to, cc=cc, subject=f"Alpha Fund interview proposed for {_fmt_when(interview['when'])}",
        title="Interview time proposed", body_html=body,
        cta_label="Review and confirm", cta_url=f"{BASE_URL}/apply",
        ics=_build_interview_ics(application, interview),
    )


async def _send_interview_confirmed_emails(application: dict, interview: dict) -> None:
    """One email, to the candidate with the interviewer cc'd — a shared
    thread rather than two separate copies, so a reply-all from either side
    reaches the other directly (to share a call link, say) instead of
    landing on the noreply address neither of them can do anything with."""
    candidate_to = application.get("oxford_email") or application.get("email")
    candidate_name = application.get("full_name") or application.get("username") or "Candidate"
    interviewer_name = interview.get("interviewer_name") or "Interviewer"
    interviewer_email = interview.get("interviewer_email") or ""
    programme = application.get("programme") or "the programme"
    when = interview["when"]

    to = candidate_to or interviewer_email
    if not to:
        return
    cc = interviewer_email if (candidate_to and interviewer_email and interviewer_email != to) else None

    # Escaped for the body only — the raw values still address the email.
    e_candidate, e_interviewer = html.escape(candidate_name), html.escape(interviewer_name)
    body = (
        f"<p>Hi {e_candidate} and {e_interviewer},</p>"
        f"<p>This confirms the <strong>{html.escape(programme)}</strong> interview for "
        f"<strong>{_fmt_when(when)}</strong>.</p>"
        f"<p>{e_candidate}: {html.escape(candidate_to or 'no email on file')}<br>"
        f"{e_interviewer}: {html.escape(interviewer_email or 'no email on file')}</p>"
        f"{_meet_block(interview)}"
        f"<p>A calendar invite is attached. Reply-all on this email to "
        f"{'sort out any last details' if interview.get('meet_link') else 'share a call link or sort out any last details'}"
        f" directly.</p>"
    )
    await mailer.send_email(
        to=to, cc=cc, subject=f"Alpha Fund interview confirmed for {_fmt_when(when)}",
        title="Interview confirmed", body_html=body,
        ics=_build_interview_ics(application, interview),
    )


async def _send_interview_declined_email(application: dict, interview: dict) -> None:
    interviewer_email = interview.get("interviewer_email")
    if not interviewer_email:
        return
    interviewer_name = interview.get("interviewer_name") or "there"
    candidate_name = application.get("full_name") or application.get("username") or "The candidate"
    candidate_email = application.get("oxford_email") or application.get("email") or ""
    note_block = ""
    if interview.get("candidate_note"):
        note_block = (f'<p style="color:#555;">Their note: '
                      f'&ldquo;{html.escape(interview["candidate_note"])}&rdquo;</p>')
    e_email = html.escape(candidate_email)
    body = (
        f"<p>Hi {html.escape(interviewer_name)},</p>"
        f"<p><strong>{html.escape(candidate_name)}</strong> can't make the proposed interview time "
        f"({_fmt_when(interview['when'])}).</p>"
        f"{note_block}"
        f"<p>Please reach out directly to arrange another time. Their email is "
        f"<a href=\"mailto:{e_email}\">{e_email}</a>.</p>"
    )
    await mailer.send_email(
        to=interviewer_email,
        subject=f"Alpha Fund: {candidate_name} needs a different interview time",
        title="Interview time declined", body_html=body,
    )


@router.post("/interview/confirm")
async def confirm_interview(user: User = Depends(current_user)):
    """The candidate accepts the proposed time — both sides get a calendar invite."""
    uid = str(user.id)
    application = await _load(uid)
    if application is None:
        raise HTTPException(404, "No application on file")
    interview = application.get("interview")
    if not interview or interview.get("status") != INTERVIEW_PROPOSED:
        raise HTTPException(400, "There's no interview time waiting for a response")

    interview["status"] = INTERVIEW_CONFIRMED
    interview["responded_at"] = _now()
    application["interview"] = interview
    await _save(uid, application)
    await _send_interview_confirmed_emails(application, interview)
    return {"ok": True, "interview": _interview_view(interview)}


@router.post("/interview/decline")
async def decline_interview(payload: InterviewDecline, user: User = Depends(current_user)):
    """The candidate can't make the proposed time — the assigned interviewer
    is emailed directly to sort out an alternative."""
    uid = str(user.id)
    application = await _load(uid)
    if application is None:
        raise HTTPException(404, "No application on file")
    interview = application.get("interview")
    if not interview or interview.get("status") != INTERVIEW_PROPOSED:
        raise HTTPException(400, "There's no interview time waiting for a response")

    interview["status"] = INTERVIEW_DECLINED
    interview["responded_at"] = _now()
    interview["candidate_note"] = (payload.note or "").strip()[:500]
    application["interview"] = interview
    await _save(uid, application)
    await _send_interview_declined_email(application, interview)
    return {"ok": True, "interview": _interview_view(interview)}


@router.post("/admin/{user_id}/decide")
async def decide(user_id: str, payload: Decision, reviewer: User = Depends(require_reviewer)):
    """
    Move an application to shortlisted, accepted or rejected.

    Open to any reviewer, same as scoring a CV or proposing an interview
    time — an Analyst member can shortlist, accept or reject on their own.
    The client is expected to make Accept a deliberately heavier click (its
    own confirmation, checking they actually have approval) since it's the
    one call here that grants membership and can't be walked back from this
    page, but that's a UI safeguard, not an authorization boundary — every
    decision is attributed (``shortlisted_by`` / ``decided_by``) and visible
    to every reviewer, which is the real check on a bad call.

    Shortlisting is the interview stage: a submitted application can be
    shortlisted or rejected outright, but can only be *accepted* once it has
    been shortlisted — the interview is the chance to actually meet someone
    before the fund commits to them. Rejecting is available at either point,
    since not everyone who applies gets an interview. Accepting sets the
    member's programme; each transition emails the applicant.
    """
    if payload.decision not in ("shortlist", "auto_shortlist", "accept", "reject"):
        raise HTTPException(400, "Decision must be shortlist, auto_shortlist, accept or reject")

    application = await _load(user_id)
    if application is None:
        raise HTTPException(404, "No such application")
    status = application.get("status")

    if payload.decision == "shortlist":
        if status != S_SUBMITTED:
            raise HTTPException(400, "Only a newly submitted application can be shortlisted")
        application["status"] = S_SHORTLISTED
        application["shortlisted_at"] = _now()
        application["shortlisted_by"] = reviewer.username
    elif payload.decision == "auto_shortlist":
        # Meets one of the automatic criteria, so it goes to interview
        # without needing a CV score. Which criterion, and the details that
        # back it up, are recorded for checking at interview.
        if status != S_SUBMITTED:
            raise HTTPException(400, "Only a newly submitted application can be shortlisted")
        if payload.reason not in AUTO_SHORTLIST_REASONS:
            raise HTTPException(400, "Choose which automatic shortlist criterion they meet")
        details = (payload.note or "").strip()[:500]
        if payload.reason == "finance_internship" and not details:
            raise HTTPException(400, "Note the internship's key details (firm, role, duration, responsibilities) "
                                     "so they can be checked at interview")
        application["status"] = S_SHORTLISTED
        application["shortlisted_at"] = _now()
        application["shortlisted_by"] = reviewer.username
        application["auto_shortlist"] = {
            "reason": payload.reason, "label": AUTO_SHORTLIST_REASONS[payload.reason],
            "details": details, "by": reviewer.username, "at": _now(),
        }
    elif payload.decision == "accept":
        if status != S_SHORTLISTED:
            raise HTTPException(400, "Shortlist the applicant and hold the interview before accepting")
        application["status"] = S_ACCEPTED
        application["decided_at"] = _now()
        application["decided_by"] = reviewer.username
    else:  # reject
        if status not in (S_SUBMITTED, S_SHORTLISTED):
            raise HTTPException(400, "That application has already been decided")
        application["status"] = S_REJECTED
        application["decided_at"] = _now()
        application["decided_by"] = reviewer.username

    if payload.note is not None:
        application["decision_note"] = (payload.note or "").strip()[:500]
    await _save(user_id, application)
    await _send_decision_email(application, application["status"])
    if application["status"] == S_REJECTED:
        await _cancel_interview_event(application)

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
    "interview", "availability", "availability_updated_at",
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

    await _cancel_interview_event(application)
    if application.get("event_ticket") == EVENT_TICKET_FAST_TRACK:
        # A redo sends them through the written assessment, which a
        # Fast-Track application never had. Left as fast_track, the review
        # page and export would keep saying "no written assessment" and hide
        # the answers they're about to write — so the application now reads
        # as the ordinary route, with the original ticket kept for the record.
        application["redo_previous_ticket"] = EVENT_TICKET_FAST_TRACK
        application["event_ticket"] = EVENT_TICKET_GENERAL

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
    await _cancel_interview_event(application)
    await db_module.db.collection(COLLECTION).document(user_id).delete()
    return {"ok": True}
