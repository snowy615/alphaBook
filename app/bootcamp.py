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
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app import db as db_module
from app import mailer
from app import membership as mb
from app.applications import BASE_URL, COLLECTION as APPLICATIONS, bootcamp_schedule
from app.auth import current_user
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
        "files": [{k: f.get(k) for k in ("id", "name", "size", "content_type")} for f in post.get("files") or []],
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


@router.get("/posts/{post_id}/files/{file_id}", include_in_schema=False)
async def download_file(post_id: str, file_id: str, user: User = Depends(require_reader)):
    """Download one attachment, checked against the membership now."""
    if not db_module.bucket:
        raise HTTPException(500, "Storage not configured")
    _, post = await _load(post_id)
    entry = next((f for f in post.get("files") or [] if f.get("id") == file_id), None)
    if entry is None:
        raise HTTPException(404, "No such file")
    data = await asyncio.to_thread(db_module.bucket.blob(entry["blob"]).download_as_bytes)
    name = entry.get("name") or "file"
    ascii_name = name.encode("ascii", "replace").decode().replace('"', "_")
    return StreamingResponse(
        io.BytesIO(data), media_type=entry.get("content_type") or "application/octet-stream",
        headers={"Content-Disposition": f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name)}",
                 # Not kept by the browser: on a shared computer the next person to
                 # sign in mustn't be handed it from the cache.
                 "Cache-Control": "no-store"},
    )


# ── Emailing a post to the Bootcamp ──────────────────────────────────────────

_email_locks: Dict[str, asyncio.Lock] = {}


async def _members() -> Dict[str, Dict[str, Any]]:
    """Every Bootcamp member: uid -> their first name and the addresses they
    hear from us at (the Oxford address on their application and their
    account's, less any they've opted out of)."""
    users = {d.id: (d.to_dict() or {}) for d in await db_module.db.collection("users").get()}
    members = {uid: u for uid, u in users.items() if mb.membership_of(u) in mb.BOOTCAMP_MEMBERSHIPS}
    out: Dict[str, Dict[str, Any]] = {}
    for uid, u in members.items():
        app_doc = await db_module.db.collection(APPLICATIONS).document(uid).get()
        oxford = ((app_doc.to_dict() or {}).get("oxford_email") if app_doc.exists else "") or ""
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
