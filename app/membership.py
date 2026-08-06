"""
Who someone is on the platform: role, membership, club, and what they've
opted into.
=========================================================================

Three independent things, deliberately kept apart:

* **Role** — what the site lets you *do*. General accounts play; recruiters
  can see the directory of students who opted in; hosts can run competitions.
  Roles are granted by an admin (users request them), never self-selected,
  because a recruiter sees other people's performance and contact details.
* **Membership** — where someone stands with the club. Self-selected, but the
  analyst and bootcamp tiers stay password-gated exactly as the old `track`
  field was.
* **Club** — which society they belong to. Only Alpha Fund today; the field
  exists so a second club doesn't need a migration.

Opt-ins are separate again, and default to off: being a Quant Analyst does not
imply wanting recruiter contact.

Migration: this replaces the older free-text `track` field. Nothing rewrites
existing documents — :func:`membership_of` reads `membership` when present and
otherwise maps the legacy track, so old accounts keep working and are upgraded
the next time they save their profile.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

# ── Roles ─────────────────────────────────────────────────────────────────────
ROLE_GENERAL = "general"
ROLE_RECRUITER = "recruiter"
ROLE_HOST = "host"

ROLES: List[Dict[str, str]] = [
    {"key": ROLE_GENERAL, "label": "General account",
     "blurb": "Play every mode, track your own performance."},
    {"key": ROLE_RECRUITER, "label": "Recruiter",
     "blurb": "See students who opted in to be contacted, with their performance."},
    {"key": ROLE_HOST, "label": "Host",
     "blurb": "Run competitions and manage the rooms in them."},
]
ROLE_KEYS = {r["key"] for r in ROLES}
# Roles a user may ask for; general is the default nobody needs to request.
REQUESTABLE_ROLES = {ROLE_RECRUITER, ROLE_HOST}

# ── Memberships ───────────────────────────────────────────────────────────────
M_PUBLIC = "General public"
M_SPONSOR = "Sponsor"
M_MEMBER = "General Alpha Fund member"
M_FUND_BOOTCAMP = "Fundamental Bootcamp"
M_QUANT_BOOTCAMP = "Quant Bootcamp"
M_FUND_ANALYST = "Fundamental Analyst"
M_QUANT_ANALYST = "Quant Analyst"

MEMBERSHIPS: List[str] = [
    M_PUBLIC, M_SPONSOR, M_MEMBER,
    M_FUND_BOOTCAMP, M_QUANT_BOOTCAMP,
    M_FUND_ANALYST, M_QUANT_ANALYST,
]

# Tiers that need a password to select, mirroring the old track rules.
ANALYST_MEMBERSHIPS = {M_FUND_ANALYST, M_QUANT_ANALYST}
BOOTCAMP_MEMBERSHIPS = {M_FUND_BOOTCAMP, M_QUANT_BOOTCAMP}
# Only analysts are eligible for the CV book at all; the opt-in decides.
CV_BOOK_ELIGIBLE = ANALYST_MEMBERSHIPS

# ── Clubs ─────────────────────────────────────────────────────────────────────
CLUB_ALPHA_FUND = "Alpha Fund"
CLUBS: List[str] = [CLUB_ALPHA_FUND]

# ── Legacy ────────────────────────────────────────────────────────────────────
# The old profile stored `track`; these are the equivalent memberships.
LEGACY_TRACK_TO_MEMBERSHIP: Dict[str, str] = {
    "Fundamental": M_FUND_ANALYST,
    "Quant": M_QUANT_ANALYST,
    "Fundamental Bootcamp": M_FUND_BOOTCAMP,
    "Quant Bootcamp": M_QUANT_BOOTCAMP,
}


def membership_of(data: Dict[str, Any]) -> str:
    """This user's membership, falling back to their legacy track."""
    m = (data or {}).get("membership")
    if m in MEMBERSHIPS:
        return m
    return LEGACY_TRACK_TO_MEMBERSHIP.get((data or {}).get("track") or "", M_PUBLIC)


def club_of(data: Dict[str, Any]) -> str:
    club = (data or {}).get("club")
    return club if club in CLUBS else CLUB_ALPHA_FUND


def role_of(data: Dict[str, Any]) -> str:
    """Admins are treated as hosts so they can run competitions without a grant."""
    if (data or {}).get("is_admin"):
        return ROLE_HOST
    role = (data or {}).get("role")
    return role if role in ROLE_KEYS else ROLE_GENERAL


def is_recruiter(data: Dict[str, Any]) -> bool:
    return bool((data or {}).get("is_admin")) or (data or {}).get("role") == ROLE_RECRUITER


def can_host(data: Dict[str, Any]) -> bool:
    return bool((data or {}).get("is_admin")) or (data or {}).get("role") == ROLE_HOST


def contactable(data: Dict[str, Any]) -> bool:
    """Whether this user appears in the recruiter directory.

    On by default: students join to be seen by firms, and an opt-in that
    nobody finds leaves the directory empty and the sponsorship pitch hollow.
    A missing value therefore means yes; only an explicit False opts out, so
    turning it off in settings is durable and is never re-defaulted on.
    """
    if data is None:
        return False
    value = data.get("opt_in_contact")
    return True if value is None else bool(value)


def cv_book_included(data: Dict[str, Any]) -> bool:
    """
    Whether this member belongs in the CV book.

    Analyst membership makes someone eligible; the opt-in decides. Accounts
    from before the opt-in existed have no flag stored, and the CV book used
    to include every analyst — so an unset flag keeps that behaviour rather
    than silently dropping people out of a book they expect to be in.
    """
    if membership_of(data) not in CV_BOOK_ELIGIBLE:
        return False
    opt = (data or {}).get("opt_in_cv_book")
    return True if opt is None else bool(opt)


def public_profile(uid: str, data: Dict[str, Any]) -> Dict[str, Any]:
    """The identity fields shown on a profile or in a directory row."""
    return {
        "id": uid,
        "username": (data or {}).get("username", ""),
        "full_name": (data or {}).get("full_name") or "",
        "graduation_year": (data or {}).get("graduation_year"),
        "role": role_of(data),
        "membership": membership_of(data),
        "club": club_of(data),
    }


def vocabulary() -> Dict[str, Any]:
    """Everything a form needs to render the choices."""
    return {
        "roles": ROLES,
        "memberships": MEMBERSHIPS,
        "clubs": CLUBS,
        "analyst_memberships": sorted(ANALYST_MEMBERSHIPS),
        "bootcamp_memberships": sorted(BOOTCAMP_MEMBERSHIPS),
        "cv_book_eligible": sorted(CV_BOOK_ELIGIBLE),
        "requestable_roles": sorted(REQUESTABLE_ROLES),
    }


def validate_membership_change(
    membership: str,
    password: Optional[str],
    analyst_password: str,
    bootcamp_password: str,
) -> Optional[str]:
    """Return an error message if this membership can't be self-selected."""
    if membership not in MEMBERSHIPS:
        return f"Unknown membership. Choose from: {', '.join(MEMBERSHIPS)}"
    if membership in ANALYST_MEMBERSHIPS and password != analyst_password:
        return "Incorrect password for analyst membership"
    if membership in BOOTCAMP_MEMBERSHIPS and password != bootcamp_password:
        return "Incorrect password for bootcamp membership"
    return None
