"""
The Bootcamp board: lectures and announcements for Bootcamp members.
====================================================================

A page of its own (/bootcamp) that only the Bootcamp sees. Analysts (and
admins) write it: they post lectures and announcements, attach the
materials (slides, notebooks, problem sheets), and can edit or delete any
post. Bootcamp members read it and download the materials. Everyone else,
general members included, is turned away, and nothing on it is linked for
them.

Who's who comes from the membership on the account (membership.py): it's
set to the Bootcamp when someone accepts their offer, so the people who see
this board are the same ones who see the session schedule on /apply.

An Analyst can also email a post to the whole Bootcamp (each member at the
addresses they get application emails at, less any they've opted out of).
The server only has CPU while it's answering a request, so the page sends
it in batches of a few, one request each; the post keeps who has been
emailed, so a send that's interrupted picks up where it stopped and nobody
gets it twice.

Files can be viewed without downloading. PDFs, images and plain text open
in the browser itself. Browsers can't show PowerPoint, Word or Excel, so
those open in Microsoft's free Office viewer, which has to fetch the file
itself: it gets a link to just that file, signed by the server and good for
VIEW_LINK_SECONDS (it can't sign anyone in, and it works for nothing else).

Storage: one ``bootcamp_posts`` document per post, its attachments listed on
it; the files themselves go in the storage bucket under
``bootcamp/{post_id}/{file_id}/``. Downloads are streamed through the server,
so the bucket stays private and every download is checked against the
membership at the time.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import html
import io
import re
import secrets
import string
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse, StreamingResponse
from jose import JWTError, jwt
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app import db as db_module
from app import mailer
from app import membership as mb
from app.applications import BASE_URL, COLLECTION as APPLICATIONS, _cached_collection, bootcamp_schedule
from app.auth import ALGORITHM, SECRET_KEY, current_user
from app.models import User

router = APIRouter(prefix="/bootcamp", tags=["bootcamp"])
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))

POSTS = "bootcamp_posts"
KIND_LECTURE, KIND_ANNOUNCEMENT = "lecture", "announcement"
KINDS = (KIND_LECTURE, KIND_ANNOUNCEMENT)
TITLE_MAX = 150
BODY_MAX = 10_000
FILES_PER_POST = 20
# Cloud Run takes requests up to 32 MB; leave room for the form around the file.
FILE_MAX_BYTES = 25 * 1024 * 1024
# What a lecture's materials tend to be. Anything else is refused rather
# than stored and handed out to forty people.
ALLOWED_EXTENSIONS = {
    "pdf", "ppt", "pptx", "key", "doc", "docx", "xls", "xlsx", "csv", "txt", "md",
    "ipynb", "py", "r", "zip", "png", "jpg", "jpeg", "gif",
}

# Viewing in the browser: what the browser shows itself (and as what), and
# what goes to Microsoft's Office viewer, which takes files up to these sizes.
TEXT = "text/plain; charset=utf-8"
INLINE_TYPES = {"pdf": "application/pdf", "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                "gif": "image/gif", "txt": TEXT, "md": TEXT, "csv": TEXT, "py": TEXT, "r": TEXT}
OFFICE_MAX_BYTES = {"ppt": 10, "pptx": 10, "doc": 10, "docx": 10, "xls": 5, "xlsx": 5}
OFFICE_VIEWER = "https://view.officeapps.live.com/op/view.aspx?src="
VIEW_LINK_SECONDS = 15 * 60
# Downloads and in-browser views come straight from storage through a signed
# link this long-lived: much faster than passing through this server.
DIRECT_LINK_SECONDS = 5 * 60

ACCESS_EDITOR, ACCESS_READER = "editor", "reader"
EMAIL_BATCH = 8                 # emails per request, well inside the hosting timeout


# ── Who can see and write it ─────────────────────────────────────────────────

async def _user_data(uid: str) -> Dict[str, Any]:
    doc = await db_module.db.collection("users").document(uid).get()
    return (doc.to_dict() or {}) if doc.exists else {}


async def access_of(user: Optional[User]) -> Optional[str]:
    """editor (admins and Analyst members), reader (Bootcamp members), or
    None for everyone else."""
    if user is None:
        return None
    if user.is_admin:
        return ACCESS_EDITOR
    membership = mb.membership_of(await _user_data(str(user.id)))
    if membership in mb.ANALYST_MEMBERSHIPS:
        return ACCESS_EDITOR
    if membership in mb.BOOTCAMP_MEMBERSHIPS:
        return ACCESS_READER
    return None


async def require_reader(user: User = Depends(current_user)) -> User:
    if await access_of(user) is None:
        raise HTTPException(403, "The Bootcamp page is for Bootcamp members and the Analysts who run it")
    return user


async def require_editor(user: User = Depends(current_user)) -> User:
    if await access_of(user) != ACCESS_EDITOR:
        raise HTTPException(403, "Only Analysts can post on the Bootcamp page")
    return user


# ── Helpers ──────────────────────────────────────────────────────────────────

def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _iso(value: Any) -> Optional[str]:
    return value.isoformat() if isinstance(value, dt.datetime) else None


async def _name_of(user: User) -> str:
    data = await _user_data(str(user.id))
    return (data.get("full_name") or "").strip() or user.username


async def _load(post_id: str) -> tuple:
    ref = db_module.db.collection(POSTS).document(post_id)
    doc = await ref.get()
    if not doc.exists:
        raise HTTPException(404, "No such post")
    return ref, doc.to_dict() or {}


def _email_summary(post: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    e = post.get("email")
    if not e:
        return None
    return {"sent": len(e.get("sent") or []), "failed": len(e.get("failed") or []), "total": e.get("total") or 0,
            "done": bool(e.get("finished_at")), "at": _iso(e.get("finished_at") or e.get("started_at")),
            "by_name": e.get("by_name") or ""}


def _view(post_id: str, post: Dict[str, Any], editor: bool = False) -> Dict[str, Any]:
    return {
        **({"email": _email_summary(post)} if editor else {}),
        "id": post_id, "kind": post.get("kind") or KIND_ANNOUNCEMENT,
        "title": post.get("title") or "", "body": post.get("body") or "",
        "author_name": post.get("author_name") or "",
        "created_at": _iso(post.get("created_at")), "updated_at": _iso(post.get("updated_at")),
        "updated_by_name": post.get("updated_by_name") or "",
        "files": [{**{k: f.get(k) for k in ("id", "name", "size", "content_type")},
                   "view": viewer_of(f.get("name") or "", f.get("size") or 0)} for f in post.get("files") or []],
    }


_UNSAFE = re.compile(r"[^A-Za-z0-9._ ()+-]+")


def _safe_name(name: str) -> str:
    """A file name fit for a storage path and a download header: no folders,
    no odd characters, not too long, extension kept."""
    base = (name or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    base = _UNSAFE.sub("_", base).strip(" ._") or "file"
    if len(base) > 120:
        stem, dot, ext = base.rpartition(".")
        base = (stem[:110] + dot + ext) if dot else base[:120]
    return base


def _extension(name: str) -> str:
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def viewer_of(name: str, size: int) -> Optional[str]:
    """How a file can be viewed without downloading it: "browser", "office"
    (Microsoft's viewer), or None (download only: zips, notebooks, and Office
    files too big for the viewer)."""
    ext = _extension(name)
    if ext in INLINE_TYPES:
        return "browser"
    if ext in OFFICE_MAX_BYTES and size <= OFFICE_MAX_BYTES[ext] * 1024 * 1024:
        return "office"
    return None


def _view_token(post_id: str, file_id: str) -> str:
    """A link for one file, for a short while. No "sub", so it can't sign
    anyone in; sign-in tokens have no "bv", so they can't open files."""
    exp = _now() + dt.timedelta(seconds=VIEW_LINK_SECONDS)
    return jwt.encode({"bv": f"{post_id}/{file_id}", "exp": exp}, SECRET_KEY, algorithm=ALGORITHM)


def _file_response(entry: Dict[str, Any], data: bytes, inline: bool) -> StreamingResponse:
    name = entry.get("name") or "file"
    ascii_name = name.encode("ascii", "replace").decode().replace('"', "_")
    inline_type = INLINE_TYPES.get(_extension(name)) if inline else None
    disposition = "inline" if inline else "attachment"
    return StreamingResponse(
        io.BytesIO(data), media_type=inline_type or entry.get("content_type") or "application/octet-stream",
        headers={"Content-Disposition": f"{disposition}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name)}",
                 "X-Content-Type-Options": "nosniff",
                 # Not kept by the browser: on a shared computer the next person to
                 # sign in mustn't be handed it from the cache.
                 "Cache-Control": "no-store"},
    )


# ── Pages ────────────────────────────────────────────────────────────────────

@router.get("", include_in_schema=False)
async def bootcamp_page(request: Request):
    """The page itself; what's on it (and whether you may see it) comes from
    /bootcamp/posts, so a general member gets a polite no, not the posts."""
    return templates.TemplateResponse("bootcamp.html", {"request": request, "app_name": "AlphaBook"})


# ── API ──────────────────────────────────────────────────────────────────────

@router.get("/access")
async def my_access(user: User = Depends(current_user)):
    """Whether to show this person the Bootcamp link, and as what."""
    return {"access": await access_of(user)}


@router.get("/posts")
async def list_posts(user: User = Depends(require_reader)):
    """Every post, newest first, with the session schedule for the top of the page."""
    editor = await access_of(user) == ACCESS_EDITOR
    docs = await db_module.db.collection(POSTS).get()
    posts = [_view(d.id, d.to_dict() or {}, editor) for d in docs]
    posts.sort(key=lambda p: p["created_at"] or "", reverse=True)
    out = {"can_edit": editor, "posts": posts, "sessions": bootcamp_schedule()}
    if editor:
        out["member_count"] = len(await _members())
    return out


class PostIn(BaseModel):
    kind: str = KIND_ANNOUNCEMENT
    title: str
    body: str = ""


def _checked(req: PostIn) -> Dict[str, str]:
    if req.kind not in KINDS:
        raise HTTPException(400, "A post is a lecture or an announcement")
    title = " ".join((req.title or "").split())
    if not title:
        raise HTTPException(400, "Give it a title")
    if len(title) > TITLE_MAX:
        raise HTTPException(400, f"Keep the title under {TITLE_MAX} characters")
    body = (req.body or "").strip()
    if len(body) > BODY_MAX:
        raise HTTPException(400, f"Keep the post under {BODY_MAX:,} characters")
    return {"kind": req.kind, "title": title, "body": body}


@router.post("/posts")
async def create_post(req: PostIn, user: User = Depends(require_editor)):
    post = {**_checked(req), "files": [], "created_at": _now(), "created_by": str(user.id),
            "author_name": await _name_of(user)}
    ref = db_module.db.collection(POSTS).document()
    await ref.set(post)
    return {"ok": True, "post": _view(ref.id, post, True)}


@router.put("/posts/{post_id}")
async def update_post(post_id: str, req: PostIn, user: User = Depends(require_editor)):
    """Any Analyst can edit any post; the last edit is shown with who made it."""
    ref, post = await _load(post_id)
    change = {**_checked(req), "updated_at": _now(), "updated_by": str(user.id),
              "updated_by_name": await _name_of(user)}
    await ref.update(change)
    post.update(change)
    return {"ok": True, "post": _view(post_id, post, True)}


def _delete_blobs(paths: List[str]) -> None:
    for path in paths:
        try:
            db_module.bucket.blob(path).delete()
        except Exception:
            pass        # already gone; the post is what matters


@router.delete("/posts/{post_id}")
async def delete_post(post_id: str, user: User = Depends(require_editor)):
    ref, post = await _load(post_id)
    paths = [f["blob"] for f in post.get("files") or [] if f.get("blob")]
    if paths and db_module.bucket:
        await asyncio.to_thread(_delete_blobs, paths)
    await ref.delete()
    return {"ok": True}


@router.post("/posts/{post_id}/files")
async def attach_file(post_id: str, file: UploadFile = File(...), user: User = Depends(require_editor)):
    """Attach one file to a post (slides, a notebook, a problem sheet)."""
    if not db_module.bucket:
        raise HTTPException(500, "Storage not configured")
    ref, post = await _load(post_id)
    files = list(post.get("files") or [])
    if len(files) >= FILES_PER_POST:
        raise HTTPException(400, f"A post can have up to {FILES_PER_POST} files")
    name = _safe_name(file.filename or "")
    if _extension(name) not in ALLOWED_EXTENSIONS:
        raise HTTPException(400, "That kind of file can't be attached. Slides, documents, spreadsheets, notebooks, "
                                 "code, images and zips are fine.")
    content = await file.read(FILE_MAX_BYTES + 1)
    if len(content) > FILE_MAX_BYTES:
        raise HTTPException(400, f"Files can be up to {FILE_MAX_BYTES // (1024 * 1024)} MB")
    if not content:
        raise HTTPException(400, "That file is empty")
    file_id = uuid.uuid4().hex[:12]
    blob_path = f"bootcamp/{post_id}/{file_id}/{name}"
    content_type = file.content_type or "application/octet-stream"
    await asyncio.to_thread(db_module.bucket.blob(blob_path).upload_from_string, content, content_type=content_type)
    entry = {"id": file_id, "name": name, "size": len(content), "content_type": content_type, "blob": blob_path,
             "uploaded_at": _now(), "uploaded_by": str(user.id)}
    files.append(entry)
    await ref.update({"files": files})
    post["files"] = files
    return {"ok": True, "post": _view(post_id, post, True)}


@router.delete("/posts/{post_id}/files/{file_id}")
async def remove_file(post_id: str, file_id: str, user: User = Depends(require_editor)):
    ref, post = await _load(post_id)
    files = list(post.get("files") or [])
    gone = [f for f in files if f.get("id") == file_id]
    if not gone:
        raise HTTPException(404, "No such file")
    files = [f for f in files if f.get("id") != file_id]
    await ref.update({"files": files})
    if db_module.bucket and gone[0].get("blob"):
        await asyncio.to_thread(_delete_blobs, [gone[0]["blob"]])
    post["files"] = files
    return {"ok": True, "post": _view(post_id, post, True)}


def _signed_url(entry: Dict[str, Any], inline: bool) -> Optional[str]:
    """A short-lived link straight to the file in storage, saying how the
    browser should treat it; None where the credentials can't sign (then the
    file is streamed through here instead)."""
    name = entry.get("name") or "file"
    ascii_name = name.encode("ascii", "replace").decode().replace('"', "_")
    inline_type = INLINE_TYPES.get(_extension(name)) if inline else None
    try:
        return db_module.bucket.blob(entry["blob"]).generate_signed_url(
            version="v4", method="GET", expiration=dt.timedelta(seconds=DIRECT_LINK_SECONDS),
            response_disposition=f"{'inline' if inline_type else 'attachment'}; filename=\"{ascii_name}\"; "
                                 f"filename*=UTF-8''{quote(name)}",
            **({"response_type": inline_type} if inline_type else {}))
    except Exception:
        return None


async def _entry(post_id: str, file_id: str) -> Dict[str, Any]:
    if not db_module.bucket:
        raise HTTPException(500, "Storage not configured")
    _, post = await _load(post_id)
    entry = next((f for f in post.get("files") or [] if f.get("id") == file_id), None)
    if entry is None:
        raise HTTPException(404, "No such file")
    return entry


def _no_store(response):
    response.headers["Cache-Control"] = "no-store"
    return response


async def _file(post_id: str, file_id: str) -> tuple:
    if not db_module.bucket:
        raise HTTPException(500, "Storage not configured")
    _, post = await _load(post_id)
    entry = next((f for f in post.get("files") or [] if f.get("id") == file_id), None)
    if entry is None:
        raise HTTPException(404, "No such file")
    data = await asyncio.to_thread(db_module.bucket.blob(entry["blob"]).download_as_bytes)
    return entry, data


@router.get("/posts/{post_id}/files/{file_id}", include_in_schema=False)
async def download_file(post_id: str, file_id: str, inline: bool = False, user: User = Depends(require_reader)):
    """Download one attachment, checked against the membership now; with
    ?inline=1, show it in the browser instead if the browser can."""
    entry = await _entry(post_id, file_id)
    url = _signed_url(entry, inline)
    if url:
        return _no_store(RedirectResponse(url, status_code=303))
    data = await asyncio.to_thread(db_module.bucket.blob(entry["blob"]).download_as_bytes)
    return _file_response(entry, data, inline)


@router.get("/posts/{post_id}/files/{file_id}/view", include_in_schema=False)
async def view_file(post_id: str, file_id: str, user: User = Depends(require_reader)):
    """Open a file to read without downloading it: in the browser itself, or
    in Microsoft's Office viewer through a short-lived link to just this file."""
    _, post = await _load(post_id)
    entry = next((f for f in post.get("files") or [] if f.get("id") == file_id), None)
    if entry is None:
        raise HTTPException(404, "No such file")
    how = viewer_of(entry.get("name") or "", entry.get("size") or 0)
    if how == "browser":
        url = _signed_url(entry, inline=True) if db_module.bucket else None
        return _no_store(RedirectResponse(
            url or f"/bootcamp/posts/{quote(post_id)}/files/{quote(file_id)}?inline=1", status_code=303))
    if how == "office":
        src = f"{BASE_URL}/bootcamp/shared/{_view_token(post_id, file_id)}/{quote(entry.get('name') or 'file')}"
        return RedirectResponse(OFFICE_VIEWER + quote(src, safe=""), status_code=303)
    raise HTTPException(400, "That file can't be opened in the browser: download it instead")


@router.get("/shared/{token}/{name}", include_in_schema=False)
async def shared_file(token: str, name: str):
    """The file behind a view link, for the Office viewer to fetch. Only with
    a valid, unexpired link made for this one file."""
    try:
        claim = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM]).get("bv") or ""
    except JWTError:
        claim = ""
    post_id, _, file_id = claim.partition("/")
    if not post_id or not file_id:
        raise HTTPException(404, "This link has expired. Open the file again from the Bootcamp page.")
    entry, data = await _file(post_id, file_id)
    return _file_response(entry, data, inline=True)


# ── Emailing a post to the Bootcamp ──────────────────────────────────────────

_email_locks: Dict[str, asyncio.Lock] = {}


async def _members() -> Dict[str, Dict[str, Any]]:
    """Every Bootcamp member: uid -> their first name and the addresses they
    hear from us at (the Oxford address on their application and their
    account's, less any they've opted out of)."""
    # Both from the minute-old cache the review page keeps: one scan each,
    # not a read per member (which made the page take ten seconds to load).
    members = {uid: u for uid, u in await _cached_collection("users") if mb.membership_of(u) in mb.BOOTCAMP_MEMBERSHIPS}
    oxford_of = {uid: (a.get("oxford_email") or "") for uid, a in await _cached_collection(APPLICATIONS)}
    out: Dict[str, Dict[str, Any]] = {}
    for uid, u in members.items():
        oxford = oxford_of.get(uid, "")
        addresses: List[str] = []
        for a in (oxford, u.get("email") or ""):
            a = a.strip()
            if a and a.lower() not in (x.lower() for x in addresses):
                addresses.append(a)
        if addresses:
            first = ((u.get("full_name") or "").split() or [u.get("username") or "there"])[0]
            out[uid] = {"first": first, "to": ", ".join(mailer.apply_preferences(addresses, u.get("email_opt_out")))}
    return out


def _linked(text_html: str) -> str:
    return re.sub(r"https?://[^\s<]+[^\s<.,;:!?)]", lambda m: f'<a href="{m.group(0)}">{m.group(0)}</a>', text_html)


def _post_email(post: Dict[str, Any], first: str) -> Dict[str, str]:
    kind = "New lecture" if post.get("kind") == KIND_LECTURE else "Announcement"
    blocks = [b.strip() for b in re.split(r"\n\s*\n", html.escape(post.get("body") or "")) if b.strip()]
    body = f"<p>Hi {html.escape(first)},</p>"
    body += f"<p><strong>{html.escape(post.get('title') or '')}</strong></p>"
    body += "".join(f"<p>{_linked(b).replace(chr(10), '<br>')}</p>" for b in blocks)
    files = post.get("files") or []
    if files:
        body += ("<p>Materials (download them on the Bootcamp page): "
                 + ", ".join(html.escape(f.get("name") or "") for f in files) + "</p>")
    body += f"<p>{html.escape(post.get('author_name') or 'Oxford Alpha Fund')}</p>"
    return {"subject": f"Quant Bootcamp: {post.get('title') or kind}", "title": kind, "body": body}


class EmailRequest(BaseModel):
    again: bool = False      # start over for a post that's already been emailed


@router.post("/posts/{post_id}/email")
async def email_post(post_id: str, req: EmailRequest, user: User = Depends(require_editor)):
    """Email the post to the next few Bootcamp members who haven't had it.
    The page calls this until ``remaining`` is 0."""
    async with _email_locks.setdefault(post_id, asyncio.Lock()):
        ref, post = await _load(post_id)
        state = post.get("email") or {}
        if state.get("finished_at") and not req.again:
            raise HTTPException(409, f"Already emailed to {len(state.get('sent') or [])} members. Use Email again to resend.")
        members = await _members()
        if not state or req.again and state.get("finished_at"):
            state = {"started_at": _now(), "by": str(user.id), "by_name": await _name_of(user), "sent": [], "failed": []}
        done = set(state.get("sent") or []) | set(state.get("failed") or [])
        todo = [uid for uid in sorted(members) if uid not in done]
        for uid in todo[:EMAIL_BATCH]:
            m = members[uid]
            mail = _post_email(post, m["first"])
            ok = await mailer.send_email(to=m["to"], subject=mail["subject"], title=mail["title"], body_html=mail["body"],
                                         cta_label="Open the Bootcamp page", cta_url=f"{BASE_URL}/bootcamp")
            state.setdefault("sent" if ok else "failed", []).append(uid)
        remaining = max(0, len(todo) - EMAIL_BATCH)
        state["total"] = len(state.get("sent") or []) + len(state.get("failed") or []) + remaining
        if not remaining:
            state["finished_at"] = _now()
        await ref.update({"email": state})
        post["email"] = state
    return {"ok": True, "remaining": remaining, "post": _view(post_id, post, True)}


# ── Attendance ───────────────────────────────────────────────────────────────
# One ``bootcamp_attendance`` document per session (id: its date), holding a
# mark per member. Analysts mark it; each member sees their own marks and
# percentage, nobody else's. Present counts as a full session and late as
# LATE_CREDIT of one; excused sessions don't count either way; a session counts for someone only once
# they've been marked for it, so nobody's percentage drops for a session that
# hasn't been taken yet.

ATTENDANCE = "bootcamp_attendance"
PRESENT, LATE, ABSENT, EXCUSED = "present", "late", "absent", "excused"
MARKS = (PRESENT, LATE, ABSENT, EXCUSED)
CREDIT = {PRESENT: 1.0, LATE: 0.8, ABSENT: 0.0}     # how much of a session each mark counts for
_attendance_lock = asyncio.Lock()


def _session_ids() -> List[str]:
    return [s["date"] for s in bootcamp_schedule()]


async def _roster() -> Dict[str, Dict[str, str]]:
    """Every Bootcamp member: uid -> name and Oxford email, for the register."""
    oxford_of = {uid: (a.get("oxford_email") or "") for uid, a in await _cached_collection(APPLICATIONS)}
    return {uid: {"name": (u.get("full_name") or "").strip() or u.get("username") or uid,
                  "email": oxford_of.get(uid) or u.get("email") or ""}
            for uid, u in await _cached_collection("users") if mb.membership_of(u) in mb.BOOTCAMP_MEMBERSHIPS}


async def _registers() -> Dict[str, Dict[str, str]]:
    """session id -> {uid: mark}, for every session in the schedule."""
    out = {}
    for sid in _session_ids():
        doc = await db_module.db.collection(ATTENDANCE).document(sid).get()
        out[sid] = dict(((doc.to_dict() or {}).get("marks") or {}) if doc.exists else {})
    return out


def attendance_summary(marks: Dict[str, str]) -> Dict[str, Any]:
    """{session: mark} -> attended, counted and the percentage (None until a
    session counts)."""
    counted = [m for m in marks.values() if m in CREDIT]
    attended = round(sum(CREDIT[m] for m in counted), 1)
    return {"attended": attended, "counted": len(counted),
            "percent": round(100 * attended / len(counted)) if counted else None}


@router.get("/attendance")
async def attendance(user: User = Depends(require_reader)):
    """An Analyst gets the whole register; a member gets their own line."""
    sessions = bootcamp_schedule()
    registers = await _registers()
    if await access_of(user) == ACCESS_EDITOR:
        roster = await _roster()
        # Anyone marked who has since left the Bootcamp still shows, so marks don't vanish.
        for marks in registers.values():
            for uid in marks:
                roster.setdefault(uid, {"name": "(no longer in the Bootcamp)", "email": ""})
        members = []
        for uid, who in roster.items():
            marks = {sid: registers[sid][uid] for sid in registers if uid in registers[sid]}
            members.append({"uid": uid, **who, "marks": marks, **attendance_summary(marks)})
        members.sort(key=lambda m: m["name"].lower())
        return {"can_edit": True, "sessions": sessions, "members": members, "marks": list(MARKS)}
    uid = str(user.id)
    marks = {sid: registers[sid][uid] for sid in registers if uid in registers[sid]}
    return {"can_edit": False, "sessions": sessions, "mine": {"marks": marks, **attendance_summary(marks)}}


class AttendanceIn(BaseModel):
    marks: Dict[str, Optional[str]]     # uid -> mark, or None to clear it


@router.put("/attendance/{session_id}")
async def mark_attendance(session_id: str, req: AttendanceIn, user: User = Depends(require_editor)):
    """Set (or clear) marks for one session; the other marks are kept."""
    if session_id not in _session_ids():
        raise HTTPException(404, "No such session")
    roster = await _roster()
    async with _attendance_lock:
        ref = db_module.db.collection(ATTENDANCE).document(session_id)
        doc = await ref.get()
        marks = dict(((doc.to_dict() or {}).get("marks") or {}) if doc.exists else {})
        for uid, mark in req.marks.items():
            if mark is None:
                marks.pop(uid, None)
                continue
            if mark not in MARKS:
                raise HTTPException(400, "A mark is present, late, absent or excused")
            if uid not in roster and uid not in marks:
                raise HTTPException(400, "That person isn't in the Bootcamp")
            marks[uid] = mark
        await ref.set({"marks": marks, "updated_at": _now(), "updated_by": str(user.id),
                       "updated_by_name": await _name_of(user)})
    return {"ok": True, "session": session_id, "marks": marks}



# ── Group projects ───────────────────────────────────────────────────────────
# Bootcamp members start projects (a name and a description, both editable
# by whoever runs it). Each project has a join code its owner shares:
# entering it joins straight away. Anyone else can ask to join instead, and
# the owner accepts or declines. Everyone on the Bootcamp page sees every
# project and who's in it, with their Oxford emails, so groups can get in
# touch. A member is in one project at a time; joining one withdraws their
# requests to the others. When the owner leaves, the longest-standing member
# takes over; a project nobody is left in is removed. Analysts see them all
# and can remove one.

PROJECTS = "bootcamp_projects"
PROJECT_NAME_MAX = 80
PROJECT_DESCRIPTION_MAX = 2000
PROJECTS_PER_MEMBER = 1
_CODE_ALPHABET = "".join(c for c in string.ascii_uppercase + string.digits if c not in "O0I1")
_projects_lock = asyncio.Lock()


async def require_member(user: User = Depends(current_user)) -> User:
    """Bootcamp members: the people who run and join group projects."""
    if await access_of(user) != ACCESS_READER:
        raise HTTPException(403, "Group projects are for Bootcamp members")
    return user


async def _all_projects() -> Dict[str, Dict[str, Any]]:
    return {d.id: (d.to_dict() or {}) for d in await db_module.db.collection(PROJECTS).get()}


def _projects_of(projects: Dict[str, Dict[str, Any]], uid: str) -> List[str]:
    return [pid for pid, p in projects.items() if uid in (p.get("members") or [])]


def _new_code(projects: Dict[str, Dict[str, Any]]) -> str:
    taken = {p.get("code") for p in projects.values()}
    while True:
        code = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(6))
        if code not in taken:
            return code


def _checked_project(name: str, description: str) -> Dict[str, str]:
    name = " ".join((name or "").split())
    if not name:
        raise HTTPException(400, "Give the project a name")
    if len(name) > PROJECT_NAME_MAX:
        raise HTTPException(400, f"Keep the name under {PROJECT_NAME_MAX} characters")
    description = (description or "").strip()
    if len(description) > PROJECT_DESCRIPTION_MAX:
        raise HTTPException(400, f"Keep the description under {PROJECT_DESCRIPTION_MAX:,} characters")
    return {"name": name, "description": description}


def _room_for(projects: Dict[str, Dict[str, Any]], uid: str) -> None:
    if len(_projects_of(projects, uid)) >= PROJECTS_PER_MEMBER:
        raise HTTPException(400, "You're already in a project. Leave it first to join another.")


async def _load_project(project_id: str) -> tuple:
    ref = db_module.db.collection(PROJECTS).document(project_id)
    doc = await ref.get()
    if not doc.exists:
        raise HTTPException(404, "No such project")
    return ref, doc.to_dict() or {}


def _owner_only(project: Dict[str, Any], user: User) -> None:
    if project.get("owner") != str(user.id):
        raise HTTPException(403, "Only the person running the project can do that")


async def _withdraw_requests(uid: str, keep: Optional[str] = None) -> None:
    """Someone who's joined a project no longer wants into the others."""
    for pid, p in (await _all_projects()).items():
        if pid != keep and uid in (p.get("requests") or {}):
            requests = dict(p["requests"])
            requests.pop(uid)
            await db_module.db.collection(PROJECTS).document(pid).update({"requests": requests})


def _add_member(project: Dict[str, Any], uid: str) -> Dict[str, Any]:
    members = list(project.get("members") or [])
    if uid not in members:
        members.append(uid)
    joined = dict(project.get("joined") or {})
    joined.setdefault(uid, _now())
    requests = dict(project.get("requests") or {})
    requests.pop(uid, None)
    return {"members": members, "joined": joined, "requests": requests}


@router.get("/projects")
async def list_projects(user: User = Depends(require_reader)):
    """Every project, with who's in it and their Oxford emails. The owner
    also gets the join code and the requests waiting on them."""
    uid = str(user.id)
    access = await access_of(user)
    roster = await _roster()
    who = lambda u: {"uid": u, **roster.get(u, {"name": "(no longer in the Bootcamp)", "email": ""})}  # noqa: E731
    out = []
    projects = await _all_projects()
    for pid, p in projects.items():
        members = list(p.get("members") or [])
        is_owner = p.get("owner") == uid
        view = {
            "id": pid, "name": p.get("name") or "", "description": p.get("description") or "",
            "owner": p.get("owner"), "owner_name": who(p.get("owner"))["name"] if p.get("owner") else "",
            "members": [{**who(u), "is_owner": u == p.get("owner")} for u in members],
            "is_member": uid in members, "is_owner": is_owner,
            "requested": uid in (p.get("requests") or {}),
            "created_at": _iso(p.get("created_at")),
        }
        if is_owner:
            view["code"] = p.get("code")
            view["requests"] = [{**who(u), "at": _iso(r.get("at"))} for u, r in (p.get("requests") or {}).items()]
        out.append(view)
    out.sort(key=lambda v: (not v["is_member"], v["created_at"] or ""))
    # Everyone starts here: the members not in a project yet, so people
    # looking for a group (or for teammates) can find each other.
    placed = {u for p in projects.values() for u in p.get("members") or []}
    undecided = sorted(({"uid": u, **who(u)} for u in roster if u not in placed), key=lambda m: m["name"].lower())
    return {"projects": out, "is_member": access == ACCESS_READER, "can_moderate": access == ACCESS_EDITOR,
            "in_project": any(v["is_member"] for v in out), "undecided": undecided, "me": uid}


class ProjectIn(BaseModel):
    name: str
    description: str = ""


@router.post("/projects")
async def create_project(req: ProjectIn, user: User = Depends(require_member)):
    uid = str(user.id)
    fields = _checked_project(req.name, req.description)
    async with _projects_lock:
        projects = await _all_projects()
        _room_for(projects, uid)
        project = {**fields, "owner": uid, "members": [uid], "joined": {uid: _now()}, "requests": {},
                   "code": _new_code(projects), "created_at": _now()}
        ref = db_module.db.collection(PROJECTS).document()
        await ref.set(project)
    await _withdraw_requests(uid, keep=ref.id)
    return {"ok": True, "id": ref.id, "code": project["code"]}


@router.put("/projects/{project_id}")
async def update_project(project_id: str, req: ProjectIn, user: User = Depends(require_member)):
    """Rename it or change the description: whoever runs it."""
    ref, project = await _load_project(project_id)
    _owner_only(project, user)
    await ref.update({**_checked_project(req.name, req.description), "updated_at": _now()})
    return {"ok": True}


@router.post("/projects/{project_id}/code")
async def new_code(project_id: str, user: User = Depends(require_member)):
    """A fresh join code (the old one stops working), say if it got around."""
    async with _projects_lock:
        ref, project = await _load_project(project_id)
        _owner_only(project, user)
        code = _new_code(await _all_projects())
        await ref.update({"code": code})
    return {"ok": True, "code": code}


class JoinCode(BaseModel):
    code: str


@router.post("/projects/join")
async def join_with_code(req: JoinCode, user: User = Depends(require_member)):
    uid = str(user.id)
    code = "".join((req.code or "").split()).upper()
    async with _projects_lock:
        projects = await _all_projects()
        match = next((pid for pid, p in projects.items() if code and p.get("code") == code), None)
        if match is None:
            raise HTTPException(404, "No project has that code. Check it with the person running the project.")
        if uid in (projects[match].get("members") or []):
            return {"ok": True, "id": match}
        _room_for(projects, uid)
        await db_module.db.collection(PROJECTS).document(match).update(_add_member(projects[match], uid))
    await _withdraw_requests(uid, keep=match)
    return {"ok": True, "id": match, "name": projects[match].get("name")}


@router.post("/projects/{project_id}/request")
async def request_to_join(project_id: str, user: User = Depends(require_member)):
    uid = str(user.id)
    async with _projects_lock:
        ref, project = await _load_project(project_id)
        if uid in (project.get("members") or []):
            raise HTTPException(400, "You're already in this project")
        _room_for(await _all_projects(), uid)
        requests = dict(project.get("requests") or {})
        requests[uid] = {"at": _now()}
        await ref.update({"requests": requests})
    return {"ok": True}


@router.delete("/projects/{project_id}/request")
async def cancel_request(project_id: str, user: User = Depends(require_member)):
    ref, project = await _load_project(project_id)
    requests = dict(project.get("requests") or {})
    if requests.pop(str(user.id), None) is not None:
        await ref.update({"requests": requests})
    return {"ok": True}


class RequestDecision(BaseModel):
    accept: bool


@router.post("/projects/{project_id}/requests/{member_uid}")
async def decide_request(project_id: str, member_uid: str, req: RequestDecision, user: User = Depends(require_member)):
    """The owner lets someone in, or doesn't."""
    async with _projects_lock:
        ref, project = await _load_project(project_id)
        _owner_only(project, user)
        requests = dict(project.get("requests") or {})
        if member_uid not in requests:
            raise HTTPException(404, "That request isn't there any more")
        if not req.accept:
            requests.pop(member_uid)
            await ref.update({"requests": requests})
            return {"ok": True}
        projects = await _all_projects()
        if _projects_of(projects, member_uid):
            requests.pop(member_uid)
            await ref.update({"requests": requests})
            raise HTTPException(400, "They've joined another project since asking")
        await ref.update(_add_member(project, member_uid))
    await _withdraw_requests(member_uid, keep=project_id)
    return {"ok": True}


@router.post("/projects/{project_id}/leave")
async def leave_project(project_id: str, user: User = Depends(require_member)):
    uid = str(user.id)
    async with _projects_lock:
        ref, project = await _load_project(project_id)
        members = list(project.get("members") or [])
        if uid not in members:
            raise HTTPException(400, "You're not in this project")
        members.remove(uid)
        if not members:
            await ref.delete()             # nobody left
            return {"ok": True, "removed": True}
        joined = dict(project.get("joined") or {})
        joined.pop(uid, None)
        change: Dict[str, Any] = {"members": members, "joined": joined}
        if project.get("owner") == uid:   # the longest-standing member takes over
            change["owner"] = min(members, key=lambda u: (_iso(joined.get(u)) or "", members.index(u)))
        await ref.update(change)
    return {"ok": True, "removed": False}


@router.delete("/projects/{project_id}")
async def remove_project(project_id: str, user: User = Depends(require_editor)):
    """Analysts can take a project down (say it was a test, or abandoned)."""
    ref, _ = await _load_project(project_id)
    await ref.delete()
    return {"ok": True}
