"""
Recruiter directory.
====================

Students who opt in to being contacted appear here, with their performance and
an email address; recruiters get in touch from their own inbox. Nothing is
sent on anyone's behalf.

Two gates, both required:

* the viewer must hold the **recruiter** role (granted by an admin, on request
  — a shared code would leak this to anyone it reached), and
* the student must have **opted in**, which is off by default and reversible
  at any time from their profile.

Blacklisted accounts and staff never appear.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.templating import Jinja2Templates
from firebase_admin import auth as fb_auth

from app import db as db_module
from app import membership as mb
from app import scores
from app.auth import current_user
from app.models import User

log = logging.getLogger("uvicorn.error")

router = APIRouter(prefix="/recruiters", tags=["recruiters"])
BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


async def _contact_email(doc_id: str, data: Dict[str, Any]) -> str:
    """The student's address, healing the record if Firestore has not got one.

    `users.email` only started being written when this directory was built, and
    older documents fill in lazily on the owner's next sign-in. A student who
    opted in but has not signed in since would appear here with no way to reach
    them — which defeats the point. Firebase Auth has held the address all
    along, so read it from there and write it back, once.
    """
    email = (data or {}).get("email")
    if email:
        return email

    uid = str((data or {}).get("firebase_uid") or doc_id)
    try:
        email = await asyncio.to_thread(lambda: fb_auth.get_user(uid).email)
    except Exception as exc:
        log.info("recruiters: no Firebase address for %s: %s", uid, exc)
        return ""

    if not email:
        return ""
    try:
        await db_module.db.collection("users").document(doc_id).update({"email": email})
    except Exception as exc:
        log.warning("recruiters: could not cache email for %s: %s", doc_id, exc)
    return email


async def _require_recruiter(user: User) -> Dict[str, Any]:
    doc = await db_module.db.collection("users").document(str(user.id)).get()
    data = doc.to_dict() or {}
    if not mb.is_recruiter({**data, "is_admin": user.is_admin}):
        raise HTTPException(
            403,
            "The recruiter directory is open to approved recruiters. "
            "You can request the role from your profile.",
        )
    return data


@router.get("", include_in_schema=False)
async def directory_page(request: Request):
    return templates.TemplateResponse("recruiters.html", {
        "request": request,
        "app_name": "AlphaBook",
    })


@router.get("/directory")
async def directory(user: User = Depends(current_user)):
    """Opted-in students, with performance and contact details."""
    await _require_recruiter(user)

    board = await scores.leaderboard()
    ratings = {p["user_id"]: p for p in board["players"]}
    total = len(board["players"])

    docs = await db_module.db.collection("users").get()
    rows: List[Dict[str, Any]] = []
    for d in docs:
        data = d.to_dict() or {}
        if data.get("is_admin") or data.get("is_blacklisted"):
            continue
        if not mb.contactable(data):
            continue

        me = ratings.get(d.id)
        modes = []
        if me:
            for key, r in (me.get("ratings") or {}).items():
                meta = scores.mode_meta(key)
                modes.append({
                    "key": key,
                    "label": meta["label"],
                    "rating": r["rating"],
                    "games": r["games"],
                    "provisional": r["provisional"],
                })
            modes.sort(key=lambda m: m["rating"], reverse=True)

        rows.append({
            **mb.public_profile(d.id, data),
            "email": await _contact_email(d.id, data),
            "cv_uploaded": bool(data.get("cv_blob_path")),
            "in_cv_book": mb.cv_book_included(data),
            "overall": me["overall"] if me else None,
            "rank": me["rank"] if me else None,
            "modes_played": me["modes_played"] if me else 0,
            "total_games": me["total_games"] if me else 0,
            "modes": modes,
        })

    # Strongest first, then everyone still unranked.
    rows.sort(key=lambda r: (r["overall"] is None, -(r["overall"] or 0), r["username"].lower()))
    return {
        "students": rows,
        "total_ranked": total,
        "memberships": mb.MEMBERSHIPS,
    }


@router.get("/students/{user_id}/cv", include_in_schema=False)
async def student_cv(user_id: str, user: User = Depends(current_user)):
    """Stream an opted-in student's CV to an approved recruiter."""
    from fastapi.responses import StreamingResponse
    import asyncio
    import io

    await _require_recruiter(user)

    if not db_module.bucket:
        raise HTTPException(500, "Storage not configured")

    doc = await db_module.db.collection("users").document(user_id).get()
    data = doc.to_dict() if doc.exists else {}
    if not data or not mb.contactable(data):
        raise HTTPException(404, "No such student, or they have not opted in")

    blob_name = data.get("cv_blob_path")
    if not blob_name:
        raise HTTPException(404, "That student has not uploaded a CV")

    pdf = await asyncio.get_event_loop().run_in_executor(
        None, db_module.bucket.blob(blob_name).download_as_bytes
    )
    return StreamingResponse(
        io.BytesIO(pdf),
        media_type="application/pdf",
        headers={"Content-Disposition": 'inline; filename="cv.pdf"'},
    )
