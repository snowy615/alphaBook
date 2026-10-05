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

async def get_user_from_token(token: str) -> Optional[User]:
    try:
        data = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        uid = data.get("sub")
        if not uid: return None
    except JWTError as e:
        log.warning("JWT decode failed: %s", e)
        return None
    
    # Firestore get
    doc_ref = db_module.db.collection("users").document(uid)
    doc = await doc_ref.get()

    if doc.exists:
        u_data = doc.to_dict()
        return User(id=doc.id, **u_data)
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

# ----- HTML forms -----
@router.get("/signup", include_in_schema=False)
def signup_form(request: Request):
    return templates.TemplateResponse("signup.html", {"request": request, "error": None})

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

        if doc.exists:
            data = doc.to_dict()
            user = User(id=doc.id, **data)
            # Accounts created before the recruiter directory have no email
            # stored; backfill it from the verified token on next sign-in.
            if email and not data.get("email"):
                await doc_ref.update({"email": email})
                user.email = email
        else:
            # First time logic (Signup)
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
# application that was confirmed with an emailed code.
LOGIN_LINKS = "login_links"
LOGIN_LINK_REQUESTS = "login_link_requests"
LOGIN_LINK_TTL = dt.timedelta(minutes=30)
LOGIN_LINK_COOLDOWN = dt.timedelta(seconds=60)
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


async def _accounts_for_email(email: str) -> Dict[str, Dict]:
    """Every account this address may sign in to, by user id: accounts whose
    sign-in email it is, and accounts whose application has it as a
    code-confirmed Oxford email. Suspended accounts are left out."""
    variants = {email, email.lower()}
    found: Dict[str, Dict] = {}
    for v in variants:
        for d in await db_module.db.collection("users").where("email", "==", v).get():
            found[d.id] = {**(d.to_dict() or {}), "_via": "account"}
    for v in variants:
        for d in await db_module.db.collection("applications").where("oxford_email", "==", v).get():
            a = d.to_dict() or {}
            if a.get("oxford_email_verified") is not True or d.id in found:
                continue
            udoc = await db_module.db.collection("users").document(d.id).get()
            if udoc.exists:
                found[d.id] = {**(udoc.to_dict() or {}), "_via": "oxford"}
    return {uid: u for uid, u in found.items() if not u.get("is_blacklisted")}


@router.post("/auth/email-link", include_in_schema=False)
async def request_email_link(email: str = Form(...), next: str = Form("/")):
    """Email a one-time sign-in link to ``email``, if it's an address on file.

    The reply is the same whether or not anything was sent, so this can't be
    used to find out who has an account. One send per address per minute."""
    email = (email or "").strip()
    if "@" not in email or len(email) > 254:
        return JSONResponse({"status": "error", "message": "Enter the email address you use with AlphaBook."},
                            status_code=400)
    key = _hash(email.lower())
    req_ref = db_module.db.collection(LOGIN_LINK_REQUESTS).document(key)
    last = await req_ref.get()
    last_sent = _as_utc((last.to_dict() or {}).get("sent_at")) if last.exists else None
    if last_sent and _now() - last_sent < LOGIN_LINK_COOLDOWN:
        return JSONResponse({"status": "ok", "message": LOGIN_LINK_SENT})

    accounts = await _accounts_for_email(email)
    if not accounts:
        return JSONResponse({"status": "ok", "message": LOGIN_LINK_SENT})

    nxt = safe_next(next)
    links: List[tuple] = []
    for uid, u in accounts.items():
        token = secrets.token_urlsafe(32)
        await db_module.db.collection(LOGIN_LINKS).document(_hash(token)).set({
            "uid": uid, "email": email.lower(), "via": u["_via"], "next": nxt,
            "created_at": _now(), "expires_at": _now() + LOGIN_LINK_TTL, "used_at": None,
        })
        label = u.get("username") or u.get("email") or "your account"
        links.append((f"{BASE_URL}/auth/email-link?t={token}", label, u.get("email") or ""))
    await req_ref.set({"sent_at": _now()})

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
    })


@router.post("/auth/email-link/confirm", include_in_schema=False)
async def confirm_email_link(request: Request, t: str = Form("")):
    origin = request.headers.get("origin")
    if origin and origin.rstrip("/") not in (BASE_URL, f"{request.url.scheme}://{request.url.netloc}"):
        raise HTTPException(403, "Sign in from the link in your email")
    ref, link = await _live_link(t)
    if not link:
        return templates.TemplateResponse("login_link.html", {"request": request, "token": "", "who": ""},
                                          status_code=400)
    udoc = await db_module.db.collection("users").document(link["uid"]).get()
    if not udoc.exists or (udoc.to_dict() or {}).get("is_blacklisted"):
        return templates.TemplateResponse("login_link.html", {"request": request, "token": "", "who": ""},
                                          status_code=400)
    await ref.update({"used_at": _now()})

    # The link was opened from this address's inbox, so when it's the
    # account's own sign-in email that still shows unverified, it's verified now.
    if link.get("via") == "account":
        try:
            fb_user = await asyncio.to_thread(fb_auth.get_user, link["uid"])
            if not fb_user.email_verified and (fb_user.email or "").lower() == link.get("email"):
                await asyncio.to_thread(fb_auth.update_user, link["uid"], email_verified=True)
        except Exception:
            log.warning("auth: couldn't mark %s's email verified after an emailed sign-in", link["uid"])
    return _make_redirect_with_cookie(request, create_token(link["uid"]), safe_next(link.get("next")))


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