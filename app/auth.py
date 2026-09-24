from __future__ import annotations
import asyncio, os, datetime as dt, logging
from typing import Optional

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
            f"<p>Hi {username},</p>"
            f"<p>One last step — confirm this is your email address so we know where to reach you.</p>"
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
        if email and not email_verified:
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