from __future__ import annotations
import asyncio, os, datetime as dt, hashlib, html, logging, secrets
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, Request, Form, HTTPException, status
from fastapi.responses import RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import jwt, JWTError
from firebase_admin import auth as fb_auth

# Import Firestore module
from app import db as db_module
from app import mailer
from app.models import User

# ----- config -----
log = logging.getLogger("auth")
SECRET_KEY = os.getenv("SECRET_KEY", "devsecret_change_me")
ALGORITHM = "HS256"
COOKIE_NAME = "__session"  # Firebase Hosting ONLY forwards cookies named __session
COOKIE_MAX_AGE = 60 * 60 * 24 * 7  # 7 days
BASE_URL = os.getenv("APP_BASE_URL", "https://alphabook.uk").rstrip("/")

# Cookie + Bearer support
http_bearer = HTTPBearer(auto_error=False)

# ----- exports for main.py -----
router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

@router.get("/auth/config")
def auth_config():
    return {
        "apiKey": os.getenv("FIREBASE_API_KEY", ""),
        "authDomain": os.getenv("FIREBASE_AUTH_DOMAIN", ""),
        "projectId": os.getenv("FIREBASE_PROJECT_ID", ""),
        "storageBucket": os.getenv("FIREBASE_STORAGE_BUCKET", ""),
        "messagingSenderId": os.getenv("FIREBASE_MESSAGING_SENDER_ID", ""),
        "appId": os.getenv("FIREBASE_APP_ID", ""),
    }

# ----- helpers -----
# Removed hash_pw / verify_pw as we use Firebase Auth


async def _send_branded_verification_email(email: str, username: str) -> None:
    """
    A verification email that actually looks like it's from AlphaBook,
    instead of Firebase's default template (sender "project-<numbers>",
    a raw firebaseapp.com address) — which is exactly why it was landing
    in Junk. The link itself is still a Firebase-issued action link (that
    part isn't ours to restyle), but the email around it now carries our
    own logo, subject and copy, sent from the same SMTP account as every
    other AlphaBook email.

    Never raises — a failure here should not block account creation, the
    same reasoning as every other email in this app.
    """
    try:
        link = await asyncio.to_thread(
            fb_auth.generate_email_verification_link,
            email,
            fb_auth.ActionCodeSettings(url=f"{BASE_URL}/login?verified=1", handle_code_in_app=False),
        )
    except Exception:
        log.exception("auth: failed to generate a verification link for %s", email)
        return
    await mailer.send_email(
        to=email,
        subject="Verify your email",
        title="Welcome to AlphaBook",
        body_html=(
            f"<p>Hi {html.escape(username)},</p>"
            f"<p>One last step: confirm this is your email address so we know where to reach you.</p>"
        ),
        cta_label="Verify my email",
        cta_url=link,
    )

def create_token(user_id: str, max_age: int = COOKIE_MAX_AGE) -> str:
    # user_id is the Firestore Document ID string
    exp = dt.datetime.utcnow() + dt.timedelta(seconds=max_age)
    return jwt.encode({"sub": user_id, "exp": exp}, SECRET_KEY, algorithm=ALGORITHM)

# One person, one account: when someone ended up with two (a Gmail and an
# Oxford sign-up, say), an admin merges them. The extra account's user doc
# gets ``merged_into`` = the main account's id, and every way in (a session,
# a password or Google sign-in, an emailed link) lands in the main account.
# Both logins keep working; nothing is deleted.
MAX_MERGE_HOPS = 3


async def resolve_account(uid: str):
    """The main account behind ``uid`` (itself unless it was merged into
    another) and its user data, or (uid, None) if there's no such user."""
    data = None
    for _ in range(MAX_MERGE_HOPS + 1):
        doc = await db_module.db.collection("users").document(uid).get()
        if not doc.exists:
            return uid, None
        data = doc.to_dict() or {}
        target = data.get("merged_into")
        if not target or target == uid:
            return uid, data
        uid = target
    return uid, data


async def get_user_from_token(token: str) -> Optional[User]:
    try:
        data = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        uid = data.get("sub")
        if not uid: return None
    except JWTError as e:
        log.warning("JWT decode failed: %s", e)
        return None
    
    uid, u_data = await resolve_account(uid)
    if u_data is not None:
        return User(id=uid, **u_data)
    return None

def _is_https(request: Request) -> bool:
    """Check if the original client connection is HTTPS (handles proxies)."""
    # Cloud Run/Firebase Hosting sets X-Forwarded-Proto
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    return proto == "https"

def _make_redirect_with_cookie(request: Request, token: str, url: str = "/") -> RedirectResponse:
    # Use 303 for POST -> GET redirect
    resp = RedirectResponse(url=url, status_code=303)
    secure = _is_https(request)
    resp.set_cookie(
        COOKIE_NAME,
        token,
        httponly=True,
        secure=secure,
        samesite="lax",
        max_age=COOKIE_MAX_AGE,
        path="/",
    )
    return resp

async def current_user(
    request: Request,
    creds: HTTPAuthorizationCredentials = Depends(http_bearer),
) -> User:
    # 1) Cookie
    token = request.cookies.get(COOKIE_NAME)
    # 2) Bearer
    if not token and creds and creds.scheme.lower() == "bearer":
        token = creds.credentials
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)

    user = await get_user_from_token(token)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)

    # Check if user is blacklisted
    if user.is_blacklisted:
        raise HTTPException(status_code=403, detail="Account has been suspended")

    return user

# ----- one person, one account, one application -----
# The same student can reach us at a Gmail, a college address and a central
# abcd1234@ox.ac.uk one, and has more than once made a second account and
# started a second application. An Oxford email already on an application
# belongs to that application's account: it can't be used to sign up again or
# to apply again from another account. The refusal names the existing account
# only masked, since at that point nobody has proven they own the Oxford email,
# and points to the emailed sign-in link, which goes to that inbox.

def mask_email(addr: str) -> str:
    """"s***********8@gmail.com": enough to recognise your own address,
    not enough to learn someone else's."""
    local, _, domain = (addr or "").partition("@")
    if not domain:
        return "another address"
    shown = local[0] + "*" * max(len(local) - 2, 1) + (local[-1] if len(local) > 1 else "")
    return f"{shown}@{domain}"


async def account_hint(uid: str) -> str:
    """How that account signs in, e.g. "Google sign-in, s***8@gmail.com"."""
    try:
        fb_user = await asyncio.to_thread(fb_auth.get_user, uid)
        email = fb_user.email or ""
        google = any(p.provider_id == "google.com" for p in (fb_user.provider_data or []))
    except Exception:
        doc = await db_module.db.collection("users").document(uid).get()
        email, google = ((doc.to_dict() or {}).get("email") or "") if doc.exists else "", False
    return f"{'Google sign-in' if google else 'email and password'}, {mask_email(email)}"


async def application_using_email(email: str, exclude_uid: Optional[str] = None) -> Optional[str]:
    """The user id of another account whose application has ``email`` as
    its Oxford email, if there is one."""
    email = (email or "").strip()
    if not email:
        return None
    for v in {email, email.lower()}:
        for d in await db_module.db.collection("applications").where("oxford_email", "==", v).get():
            if d.id != exclude_uid:
                return d.id
    return None


def already_linked_message(hint: str) -> str:
    return (f"This email is already linked to an AlphaBook account that has an application "
            f"({hint}). Sign in with that account instead. If you're not sure how, use "
            f"\"Email me a sign-in link\" on the sign-in page with this email.")


# ----- HTML forms -----
@router.get("/signup", include_in_schema=False)
def signup_form(request: Request):
    return templates.TemplateResponse("signup.html", {"request": request, "error": None})

async def _discard_fresh_firebase_user(uid: str) -> None:
    """Remove a Firebase login made moments ago that we're refusing, so the
    email isn't left half-registered. Older logins are left alone: an
    account that has existed a while isn't ours to delete here."""
    try:
        fb_user = await asyncio.to_thread(fb_auth.get_user, uid)
        created = dt.datetime.fromtimestamp(fb_user.user_metadata.creation_timestamp / 1000, dt.timezone.utc)
        if dt.datetime.now(dt.timezone.utc) - created < dt.timedelta(minutes=15):
            await asyncio.to_thread(fb_auth.delete_user, uid)
    except Exception:
        log.warning("auth: couldn't discard refused sign-up %s", uid)


@router.post("/auth/firebase", include_in_schema=False)
async def auth_firebase(request: Request, id_token: str = Form(...), username: str = Form(None)):
    """
    Unified endpoint to handle Firebase login/signup.
    Receives an ID Token from the client.
    Verifies it with Firebase Admin.
    Finds or creates a User in Firestore.
    Sets a session cookie.
    """
    try:
        decoded_token = fb_auth.verify_id_token(id_token)
        firebase_uid = decoded_token['uid']
        email = decoded_token.get('email', '')
        email_verified = bool(decoded_token.get('email_verified'))
        
        # Check if user exists by firebase_uid
        # We need to query because we don't know the internal ID yet (unless we use firebase_uid as internal ID)
        # Using firebase_uid as Document ID is simpler and cleaner.
        
        doc_ref = db_module.db.collection("users").document(firebase_uid)
        doc = await doc_ref.get()
        
        user = None

        if doc.exists and (doc.to_dict() or {}).get("merged_into"):
            main_uid, main_data = await resolve_account(firebase_uid)
            user = User(id=main_uid, **(main_data or {}))
        elif doc.exists:
            data = doc.to_dict()
            user = User(id=doc.id, **data)
            # Accounts created before the recruiter directory have no email
            # stored; backfill it from the verified token on next sign-in.
            if email and not data.get("email"):
                await doc_ref.update({"email": email})
                user.email = email
        else:
            # First time logic (Signup). An email that's already the Oxford
            # email on someone's application is that person's: send them to
            # the account they applied with rather than starting a second.
            taken_by = await application_using_email(email) if email else None
            if taken_by and taken_by != firebase_uid:
                await _discard_fresh_firebase_user(firebase_uid)
                return JSONResponse({"status": "linked", "message": already_linked_message(
                    await account_hint(taken_by))}, status_code=409)

            if not username:
                # Fallback: use email part or random
                username = email.split('@')[0] if email else f"user_{firebase_uid[:6]}"
            
            # Check username uniqueness
            # Firestore query for username
            q = db_module.db.collection("users").where("username", "==", username).limit(1)
            existing_docs = await q.get()
            if existing_docs:
                 return JSONResponse({"status": "error", "message": "Username already taken"}, status_code=400)

            user = User(
                id=firebase_uid, # Use firebase_uid as Firestore ID
                username=username,
                firebase_uid=firebase_uid, # explicit field too
                email=email or None,
                balance=10000.0,
                is_admin=False,
                is_blacklisted=False
            )
            # Create user
            # exclude id from dump if we rely on doc id, but pydantic model includes it.
            # to_dict helper?
            user_dict = user.model_dump(exclude={"id"})
            # convert datetime to simple timestamp/server timestamp if needed, but firestore handles datetime ok-ish
            # Explicitly set document ID to firebase_uid
            await db_module.db.collection("users").document(firebase_uid).set(user_dict)
            log.info(f"Created new user: {username} ({firebase_uid})")
            # User created successfully

            # Only for accounts that actually need it — a Google sign-in
            # arrives with email_verified already true, so this only fires
            # for a fresh email/password signup.
            if email and not email_verified:
                await _send_branded_verification_email(email, username)

        # The account exists (and, for a fresh signup, its verification email
        # has just gone out) — but nobody gets a session until Firebase
        # reports the address as verified. Signing in again later with a
        # freshly-issued token (post-verification) picks up email_verified
        # true and sails through this check normally.
        if not email or not email_verified:
            return JSONResponse({
                "status": "unverified",
                "message": "Check your inbox (and your spam/junk folder) to verify your email before continuing.",
            }, status_code=403)

        # Create session
        token = create_token(user.id) # user.id is firebase_uid
        
        response = JSONResponse({"status": "ok", "redirect": "/"})
        secure = _is_https(request)
        response.set_cookie(
            COOKIE_NAME,
            token,
            httponly=True,
            secure=secure,
            samesite="lax",
            max_age=COOKIE_MAX_AGE,
            path="/",
        )
        return response

    except Exception as e:
        log.exception("Firebase auth failed")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=401)


@router.post("/auth/resolve-username", include_in_schema=False)
async def resolve_username(username: str = Form(...)):
    """
    Firebase signs in by email, not username — this looks up the email
    behind a username so the login page can run a username-typed login
    through the exact same Firebase flow as an email-typed one, instead of
    the separate (admin-only) direct-login path.

    Not found and found-but-no-email are reported the same way, so this
    doesn't become a way to probe which usernames exist.
    """
    q = db_module.db.collection("users").where("username", "==", username).limit(1)
    docs = await q.get()
    email = (docs[0].to_dict() or {}).get("email") if docs else None
    if not email:
        return JSONResponse({"status": "error", "message": "Invalid credentials"}, status_code=404)
    return JSONResponse({"status": "ok", "email": email})


@router.post("/auth/resend-verification", include_in_schema=False)
async def resend_verification(id_token: str = Form(...)):
    """Re-sends the branded verification email — the account itself proves
    who's asking, via the same Firebase ID token used everywhere else,
    since a stuck-in-junk email is exactly the situation this exists for."""
    try:
        decoded_token = fb_auth.verify_id_token(id_token)
    except Exception:
        return JSONResponse({"status": "error", "message": "Invalid session — please sign in again"}, status_code=401)

    email = decoded_token.get("email")
    if not email:
        return JSONResponse({"status": "error", "message": "This account has no email on file"}, status_code=400)
    if decoded_token.get("email_verified"):
        return JSONResponse({"status": "ok", "message": "Already verified"})

    doc = await db_module.db.collection("users").document(decoded_token["uid"]).get()
    username = ((doc.to_dict() or {}).get("username") if doc.exists else "") or email.split("@")[0]
    await _send_branded_verification_email(email, username)
    return JSONResponse({"status": "ok", "message": "Verification email sent"})


# ----- Direct admin login (no Firebase) -----
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
# No default: this repo is public, so any fallback here is a password anyone
# can read. Unset means direct admin login is simply off.
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
ADMIN_UID = "admin_user_id"  # Must match the UID used in main.py startup

@router.post("/auth/direct", include_in_schema=False)
async def direct_login(request: Request, username: str = Form(...), password: str = Form(...)):
    """Direct username/password login – used for admin access from the normal login page."""
    if not ADMIN_PASSWORD or username != ADMIN_USERNAME or password != ADMIN_PASSWORD:
        return JSONResponse({"status": "error", "message": "Invalid credentials"}, status_code=401)

    # Ensure admin user doc exists in Firestore
    doc_ref = db_module.db.collection("users").document(ADMIN_UID)
    doc = await doc_ref.get()
    if not doc.exists:
        from app.models import User as UserModel
        admin_user = UserModel(
            id=ADMIN_UID,
            username="admin",
            balance=10000.0,
            is_admin=True,
            is_blacklisted=False,
            firebase_uid=ADMIN_UID,
        )
        await doc_ref.set(admin_user.model_dump(exclude={"id"}))

    token = create_token(ADMIN_UID)
    response = JSONResponse({"status": "ok", "redirect": "/"})
    secure = _is_https(request)
    response.set_cookie(
        COOKIE_NAME,
        token,
        httponly=True,
        secure=secure,
        samesite="lax",
        max_age=COOKIE_MAX_AGE,
        path="/",
    )
    return response


@router.get("/login", include_in_schema=False)
def login_form(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


# ----- emailed sign-in links -----
# A way in that doesn't depend on how the account was made or which browser
# it's opened in: Google sign-in fails inside some mail apps' browsers, and
# people often try the Oxford address their emails arrive at when the account
# itself is a Gmail. Typing either address emails a one-time link to it, and
# being able to open that inbox is the proof. Only addresses we already trust
# get one: an account's own sign-in email, or an Oxford email on an
# application that wasn't left unconfirmed (confirmed with an emailed code,
# or from before that check existed).
#
# It is a way back in, never a way past the checks: only accounts that have
# already passed verification (Google, or a verified password sign-up) get a
# link, and signing in by link verifies nothing. A fresh or throwaway account
# still has to verify its email the normal way, and an application still can't
# move past the CV step until its Oxford email is confirmed by code.
LOGIN_LINKS = "login_links"
LOGIN_LINK_REQUESTS = "login_link_requests"
LOGIN_LINK_TTL = dt.timedelta(minutes=30)
LOGIN_LINK_COOLDOWN = dt.timedelta(seconds=60)
LOGIN_LINKS_PER_DAY = 5         # per address
LOGIN_LINK_REQUESTS_PER_HOUR = 20   # per connection (IP), across all addresses
LOGIN_LINK_SENT = ("If that email belongs to an AlphaBook account, a sign-in link is on its way. "
                   "Check your inbox (and junk folder); the link works once, for 30 minutes.")


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _as_utc(value) -> Optional[dt.datetime]:
    if not isinstance(value, dt.datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)


def safe_next(path: Optional[str]) -> str:
    """A same-site path to land on after signing in, or "/". Anything that
    could point off-site ("//evil.com", "https://...", backslashes) is refused."""
    path = (path or "").strip()
    if not path.startswith("/") or path.startswith("//") or "\\" in path:
        return "/"
    return path


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


async def _verified_firebase_user(uid: str) -> bool:
    """Whether this account has passed verification: a Firebase user that
    isn't disabled and whose sign-in email is verified (always so for Google)."""
    try:
        fb_user = await asyncio.to_thread(fb_auth.get_user, uid)
    except Exception:
        return False
    return bool(fb_user.email_verified) and not fb_user.disabled


async def _accounts_for_email(email: str) -> Dict[str, Dict]:
    """Every account this address may sign in to, by user id: accounts whose
    sign-in email it is, and accounts whose application has it as a
    code-confirmed Oxford email. Suspended accounts, disabled ones and any
    that haven't verified their sign-in email are left out."""
    variants = {email, email.lower()}
    found: Dict[str, Dict] = {}
    for v in variants:
        for d in await db_module.db.collection("users").where("email", "==", v).get():
            found[d.id] = {**(d.to_dict() or {}), "_via": "account"}
    for v in variants:
        for d in await db_module.db.collection("applications").where("oxford_email", "==", v).get():
            a = d.to_dict() or {}
            # Applications from before the code check have no flag either way and
            # were reviewed as they are; only one explicitly unconfirmed is out.
            if a.get("oxford_email_verified") is False or d.id in found:
                continue
            udoc = await db_module.db.collection("users").document(d.id).get()
            if udoc.exists:
                found[d.id] = {**(udoc.to_dict() or {}), "_via": "oxford"}
    out: Dict[str, Dict] = {}
    for uid, u in found.items():
        main_uid, main = await resolve_account(uid)
        if main_uid in out or main is None or main.get("is_blacklisted"):
            continue
        # The session it leads to is the main account's, so that's the one
        # that has to have passed verification.
        if await _verified_firebase_user(main_uid):
            out[main_uid] = {**main, "_via": u["_via"]}
    return out


def _client_ip(request: Request) -> str:
    """The visitor's address: Firebase Hosting and Cloud Run put the original
    client first in X-Forwarded-For."""
    forwarded = request.headers.get("x-forwarded-for", "")
    return forwarded.split(",")[0].strip() or (request.client.host if request.client else "") or "unknown"


async def _within_limit(doc_id: str, window: dt.timedelta, limit: int, min_gap: Optional[dt.timedelta] = None) -> bool:
    """Record one use against ``doc_id`` if it's under ``limit`` uses in the
    last ``window`` (and at least ``min_gap`` after the previous one)."""
    ref = db_module.db.collection(LOGIN_LINK_REQUESTS).document(doc_id)
    doc = await ref.get()
    now = _now()
    times = [t for t in ((doc.to_dict() or {}).get("times") or []) if _as_utc(t) and now - _as_utc(t) < window] \
        if doc.exists else []
    if len(times) >= limit or (min_gap and times and now - max(_as_utc(t) for t in times) < min_gap):
        return False
    await ref.set({"times": times + [now]})
    return True


@router.post("/auth/email-link", include_in_schema=False)
async def request_email_link(request: Request, email: str = Form(...), next: str = Form("/")):
    """Email a one-time sign-in link to ``email``, if it's an address on file.

    The reply is the same whether or not anything was sent, so this can't be
    used to find out who has an account. Limits: one send per address per
    minute and LOGIN_LINKS_PER_DAY a day, and LOGIN_LINK_REQUESTS_PER_HOUR
    requests an hour from one connection, so it can't be used to flood inboxes."""
    email = (email or "").strip()
    if "@" not in email or len(email) > 254:
        return JSONResponse({"status": "error", "message": "Enter the email address you use with AlphaBook."},
                            status_code=400)
    if not await _within_limit("ip_" + _hash(_client_ip(request)), dt.timedelta(hours=1),
                               LOGIN_LINK_REQUESTS_PER_HOUR):
        return JSONResponse({"status": "error", "message": "Too many sign-in link requests. Try again later."},
                            status_code=429)

    accounts = await _accounts_for_email(email)
    if not accounts:
        return JSONResponse({"status": "ok", "message": LOGIN_LINK_SENT})
    if not await _within_limit("email_" + _hash(email.lower()), dt.timedelta(days=1),
                               LOGIN_LINKS_PER_DAY, min_gap=LOGIN_LINK_COOLDOWN):
        return JSONResponse({"status": "ok", "message": LOGIN_LINK_SENT})

    nxt = safe_next(next)
    links: List[tuple] = []
    for uid, u in accounts.items():
        url = await _store_link(uid, email, nxt, LOGIN_LINK_TTL, u["_via"])
        label = u.get("username") or u.get("email") or "your account"
        links.append((url, label, u.get("email") or ""))

    if len(links) == 1:
        body = ("<p>Use the button below to sign in to AlphaBook. The link works once and expires in "
                "30 minutes.</p><p>If you didn't ask for this, you can ignore this email.</p>")
        cta_label, cta_url = "Sign in to AlphaBook", links[0][0]
    else:
        items = "".join(
            f'<li><a href="{html.escape(url)}">Sign in as {html.escape(label)}</a>'
            f'{f" ({html.escape(acct)})" if acct else ""}</li>' for url, label, acct in links)
        body = ("<p>This email is linked to more than one AlphaBook account. Choose the one to sign in "
                f"to:</p><ul>{items}</ul><p>Each link works once and expires in 30 minutes. If you "
                "didn't ask for this, you can ignore this email.</p>")
        cta_label = cta_url = None
    await mailer.send_email(to=email, subject="Your AlphaBook sign-in link", title="Sign in to AlphaBook",
                            body_html=body, cta_label=cta_label, cta_url=cta_url)
    return JSONResponse({"status": "ok", "message": LOGIN_LINK_SENT})


# Buttons in our emails ("Enter your availability") carry their own sign-in:
# they sign straight into the account the application belongs to, whichever
# inbox (Oxford or personal) they're clicked from and whichever account the
# browser was signed into before. They only ever sign into that existing
# account; nothing here creates one. A week to use, once; clicking an old one
# offers a fresh link by email instead.
ACCOUNT_LINK_TTL = dt.timedelta(days=7)
RENEWABLE_FOR = dt.timedelta(days=60)


async def _store_link(uid: str, address: str, nxt: str, ttl: dt.timedelta, via: str) -> str:
    token = secrets.token_urlsafe(32)
    await db_module.db.collection(LOGIN_LINKS).document(_hash(token)).set({
        "uid": uid, "email": (address or "").lower(), "via": via, "next": safe_next(nxt),
        "created_at": _now(), "expires_at": _now() + ttl, "used_at": None,
    })
    return f"{BASE_URL}/auth/email-link?t={token}"


async def account_link(uid: str, address: str, next: str = "/apply") -> Optional[str]:
    """A button URL that signs into ``uid``'s own account and opens ``next``,
    or None if that account can't use one (unverified, disabled, suspended),
    in which case the caller falls back to the plain page."""
    udoc = await db_module.db.collection("users").document(uid).get()
    if not udoc.exists or (udoc.to_dict() or {}).get("is_blacklisted") or not await _verified_firebase_user(uid):
        return None
    return await _store_link(uid, address, next, ACCOUNT_LINK_TTL, "email-button")


async def _live_link(token: str):
    """The stored link for ``token`` if it's unused and unexpired, else None."""
    if not token:
        return None, None
    ref = db_module.db.collection(LOGIN_LINKS).document(_hash(token))
    doc = await ref.get()
    if not doc.exists:
        return None, None
    link = doc.to_dict() or {}
    expires = _as_utc(link.get("expires_at"))
    if link.get("used_at") or not expires or expires < _now():
        return None, None
    return ref, link


@router.get("/auth/email-link", include_in_schema=False)
async def email_link_page(request: Request, t: str = ""):
    """The page the emailed link opens: a button that signs in, rather than
    signing in on arrival. Mail scanners (Microsoft's Safe Links on Oxford
    mail) open every link before the person does, and a link used up on
    arrival would be spent by the scanner."""
    _, link = await _live_link(t)
    account = None
    if link:
        udoc = await db_module.db.collection("users").document(link["uid"]).get()
        account = (udoc.to_dict() or {}) if udoc.exists else None
    return templates.TemplateResponse("login_link.html", {
        "request": request, "token": t if account else "",
        "who": (account or {}).get("username") or (account or {}).get("email") or "",
        "renew_token": t if not account and await _renewable(t) else "",
    })


async def _renewable(token: str) -> Optional[dict]:
    """A spent or expired link that can be swapped for a fresh one: recent
    enough, and for an account that may still use links."""
    if not token:
        return None
    doc = await db_module.db.collection(LOGIN_LINKS).document(_hash(token)).get()
    if not doc.exists:
        return None
    link = doc.to_dict() or {}
    created = _as_utc(link.get("created_at"))
    if not created or _now() - created > RENEWABLE_FOR or not link.get("email"):
        return None
    udoc = await db_module.db.collection("users").document(link["uid"]).get()
    if not udoc.exists or (udoc.to_dict() or {}).get("is_blacklisted") or not await _verified_firebase_user(link["uid"]):
        return None
    return link


@router.post("/auth/email-link/renew", include_in_schema=False)
async def renew_email_link(request: Request, t: str = Form("")):
    """Email a fresh 30-minute link to the same address the old one went to,
    for the same account. Never to an address the visitor chooses."""
    link = await _renewable(t)
    # Within the daily limit a fresh link goes out; past it, one went out
    # recently anyway, so either way the inbox is where to look.
    sent_to = mask_email(link["email"]) if link else ""
    if link and await _within_limit("email_" + _hash(link["email"]), dt.timedelta(days=1),
                                    LOGIN_LINKS_PER_DAY, min_gap=LOGIN_LINK_COOLDOWN):
        url = await _store_link(link["uid"], link["email"], link.get("next") or "/", LOGIN_LINK_TTL, "renewed")
        await mailer.send_email(
            to=link["email"], subject="Your AlphaBook sign-in link", title="Sign in to AlphaBook",
            body_html=("<p>Here's a fresh link to sign in to AlphaBook. It works once and expires in "
                       "30 minutes.</p><p>If you didn't ask for this, you can ignore this email.</p>"),
            cta_label="Sign in to AlphaBook", cta_url=url)
    return templates.TemplateResponse("login_link.html", {
        "request": request, "token": "", "who": "", "renew_token": "", "renewed_to": sent_to,
    }, status_code=200 if sent_to else 400)


@router.post("/auth/email-link/confirm", include_in_schema=False)
async def confirm_email_link(request: Request, t: str = Form("")):
    origin = request.headers.get("origin")
    if origin and origin.rstrip("/") not in (BASE_URL, f"{request.url.scheme}://{request.url.netloc}"):
        raise HTTPException(403, "Sign in from the link in your email")
    ref, link = await _live_link(t)
    if not link:
        return templates.TemplateResponse("login_link.html", {"request": request, "token": "", "who": ""},
                                          status_code=400)
    # Re-checked at sign-in, not just when the link was sent: an account
    # suspended or disabled in the meantime doesn't get in.
    udoc = await db_module.db.collection("users").document(link["uid"]).get()
    if (not udoc.exists or (udoc.to_dict() or {}).get("is_blacklisted")
            or not await _verified_firebase_user(link["uid"])):
        return templates.TemplateResponse("login_link.html", {"request": request, "token": "", "who": ""},
                                          status_code=400)
    await ref.update({"used_at": _now()})
    main_uid, _ = await resolve_account(link["uid"])
    return _make_redirect_with_cookie(request, create_token(main_uid), safe_next(link.get("next")))



@router.post("/logout", include_in_schema=False)
def logout_post():
    resp = RedirectResponse(url="/", status_code=303)
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp

# Convenience GET for logout
@router.get("/logout", include_in_schema=False)
def logout_get():
    resp = RedirectResponse(url="/", status_code=303)
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp

# ----- JSON helpers -----
@router.get("/session/token")
async def session_token(user: User = Depends(current_user)):
    return {"access_token": create_token(str(user.id)), "token_type": "bearer"}

@router.get("/whoami", include_in_schema=False)
async def whoami(user: User = Depends(current_user)):
    return JSONResponse({"id": str(user.id), "username": user.username})

@router.get("/me", include_in_schema=False)
async def me(user: User = Depends(current_user)):
    return JSONResponse({
        "username": user.username,
        "is_admin": user.is_admin
    })