"""
User guides: one address, the right guide for whoever opens it.
=================================================================

``/guide`` shows the applicant guide to everyone, signed in or not, and the
analyst guide to Analyst members and admins (the same people who can open
the review page). The analyst guide's screenshots live outside ``app/static``
(Firebase Hosting publishes that folder as-is) and are served only to them.

The rubric summaries and interview timings are read from the same constants
the review page scores with, so the guide can't drift from the site.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.templating import Jinja2Templates

from app import applications as ap
from app.events import _is_reviewer, optional_user
from app.models import User

router = APIRouter(tags=["guides"])
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
PRIVATE_DIR = Path(__file__).parent / "guide_private"

# Who sees what varies by the signed-in account, so never let a cache
# (the browser's or Firebase Hosting's) hand one person's guide to another.
_NO_SHARED_CACHE = {"Cache-Control": "private, no-store", "Vary": "Cookie"}


def _cv_summary() -> list:
    """Each CV criterion with its maximum on the online and onsite rubrics
    (None where it doesn't apply), in the online rubric's order."""
    def maxes(rubric):
        return {c["key"]: (c["label"], max(o["points"] for o in c["options"])) for c in rubric}
    online, onsite = maxes(ap.ONLINE_CV_RUBRIC), maxes(ap.ONSITE_CV_RUBRIC)
    keys = list(online) + [k for k in onsite if k not in online]
    return [{"label": (online.get(k) or onsite.get(k))[0],
             "online": online[k][1] if k in online else None,
             "onsite": onsite[k][1] if k in onsite else None} for k in keys]


@router.get("/guide", include_in_schema=False)
async def guide(request: Request, user: Optional[User] = Depends(optional_user)):
    """The analyst guide for analysts and admins; the applicant guide for
    everyone else. ``?for=applicant`` lets an analyst see what applicants see."""
    analyst = await _is_reviewer(user)
    if analyst and request.query_params.get("for") != "applicant":
        return templates.TemplateResponse("guide_analyst.html", {
            "request": request,
            "app_name": "AlphaBook",
            "cv_max": ap._cv_max(False),
            "cv_summary": _cv_summary(),
            "auto_shortlist_reasons": ap.AUTO_SHORTLIST_REASONS,
            "interview_rubric": ap.INTERVIEW_RUBRIC,
            "interview_max": ap.INTERVIEW_MAX,
            "interview_timers": ap.INTERVIEW_TIMERS,
            "interview_minutes": ap.INTERVIEW_MINUTES,
        }, headers=_NO_SHARED_CACHE)
    return templates.TemplateResponse("guide_applicant.html", {
        "request": request,
        "app_name": "AlphaBook",
        "is_analyst": analyst,
        "minutes": ap.SESSION_SECONDS // 60,
        "motivation_minutes": ap.MOTIVATION_SECONDS // 60,
        "estimation_minutes": ap.ESTIMATION_SECONDS // 60,
        "fast_track_capacity": ap.FAST_TRACK_CAPACITY,
        "interview_minutes": ap.INTERVIEW_MINUTES,
    }, headers=_NO_SHARED_CACHE)


@router.get("/guide/img/{name}", include_in_schema=False)
async def analyst_guide_image(name: str, user: Optional[User] = Depends(optional_user)):
    """A screenshot from the analyst guide, for analysts and admins only."""
    if not await _is_reviewer(user):
        raise HTTPException(404, "Not found")
    path = (PRIVATE_DIR / name).resolve()
    if path.parent != PRIVATE_DIR.resolve() or not path.is_file() or path.suffix != ".jpg":
        raise HTTPException(404, "Not found")
    return FileResponse(path, media_type="image/jpeg", headers=_NO_SHARED_CACHE)
