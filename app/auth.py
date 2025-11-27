from __future__ import annotations
import os, datetime as dt, logging
from typing import Optional

from fastapi import APIRouter, Depends, Request, Form, HTTPException, status
from fastapi.responses import RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import jwt, JWTError
from passlib.context import CryptContext
from sqlmodel import select, Session

from app.db import get_session
from app.models import User

# ----- config -----
log = logging.getLogger("auth")
SECRET_KEY = os.getenv("SECRET_KEY", "devsecret_change_me")
ALGORITHM = "HS256"
COOKIE_NAME = "session"
COOKIE_MAX_AGE = 60 * 60 * 24 * 7  # 7 days

# Use PBKDF2-SHA256 to avoid bcrypt platform issues & 72-byte limit
pwd = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")

# Cookie + Bearer support
http_bearer = HTTPBearer(auto_error=False)

# ----- exports for main.py -----
router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

# ----- helpers -----
def hash_pw(p: str) -> str:
    return pwd.hash(p)

def verify_pw(p: str, h: str) -> bool:
    return pwd.verify(p, h)

def create_token(user_id: int, max_age: int = COOKIE_MAX_AGE) -> str:
    exp = dt.datetime.utcnow() + dt.timedelta(seconds=max_age)
    # python-jose accepts datetime for "exp"
    return jwt.encode({"sub": str(user_id), "exp": exp}, SECRET_KEY, algorithm=ALGORITHM)

def get_user_from_token(token: str, session: Session) -> Optional[User]:
    try:
        data = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        uid = int(data.get("sub", "0"))
    except JWTError as e:
        log.warning("JWT decode failed: %s", e)
        return None
    return session.get(User, uid)

def _make_redirect_with_cookie(request: Request, token: str, url: str = "/") -> RedirectResponse:
    # Use 303 for POST -> GET redirect to avoid form resubmission
    resp = RedirectResponse(url=url, status_code=303)
    # Only mark Secure over HTTPS; SameSite=Lax is perfect for same-origin app
    secure = (request.url.scheme == "https")
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
    session: Session = Depends(get_session),
) -> User:
    # 1) Cookie
    token = request.cookies.get(COOKIE_NAME)
    # 2) Bearer
    if not token and creds and creds.scheme.lower() == "bearer":
        token = creds.credentials
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    user = get_user_from_token(token, session)
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

@router.post("/signup", include_in_schema=False)
def signup_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    session: Session = Depends(get_session),
):
    try:
        existing = session.exec(select(User).where(User.username == username)).first()
        if existing:
            return templates.TemplateResponse(
                "signup.html", {"request": request, "error": "Username already exists"}
            )
        user = User(
            username=username,
            password_hash=hash_pw(password),
            balance=10000.0,
            is_admin=False,
            is_blacklisted=False
        )
        session.add(user); session.commit(); session.refresh(user)
        token = create_token(user.id)
        return _make_redirect_with_cookie(request, token, url="/")
    except Exception as e:
        log.exception("Signup failed")
        return templates.TemplateResponse(
            "signup.html", {"request": request, "error": f"Signup failed: {e}"}
        )

@router.get("/login", include_in_schema=False)
def login_form(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@router.post("/login", include_in_schema=False)
def login_submit(
        request: Request,
        username: str = Form(...),
        password: str = Form(...),
        session: Session = Depends(get_session),
):
    try:
        user = session.exec(select(User).where(User.username == username)).first()
        if not user or not verify_pw(password, user.password_hash):
            return templates.TemplateResponse(
                "login.html", {"request": request, "error": "Invalid credentials"}
            )
        token = create_token(user.id)

        # Redirect admins to admin panel
        redirect_url = "/admin" if user.is_admin else "/"
        return _make_redirect_with_cookie(request, token, url=redirect_url)
    except Exception as e:
        log.exception("Login failed")
        return templates.TemplateResponse(
            "login.html", {"request": request, "error": f"Login failed: {e}"}
        )

@router.post("/logout", include_in_schema=False)
def logout_post():
    resp = RedirectResponse(url="/", status_code=303)
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp

# Convenience GET for logout (e.g., simple <a href="/logout">)
@router.get("/logout", include_in_schema=False)
def logout_get():
    resp = RedirectResponse(url="/", status_code=303)
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp

# ----- JSON helpers (great for /docs) -----
@router.post("/login/json")
def login_json(
    username: str = Form(...),
    password: str = Form(...),
    session: Session = Depends(get_session),
):
    user = session.exec(select(User).where(User.username == username)).first()
    if not user or not verify_pw(password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = create_token(user.id)
    return {"access_token": token, "token_type": "bearer"}

@router.get("/session/token")
def session_token(user: User = Depends(current_user)):
    return {"access_token": create_token(user.id), "token_type": "bearer"}

# ----- small JSON identity endpoints used by the frontend -----
@router.get("/whoami", include_in_schema=False)
def whoami(user: User = Depends(current_user)):
    return JSONResponse({"id": user.id, "username": user.username})

# Alias the simple /me endpoint the UI polls to flip header buttons
@router.get("/me", include_in_schema=False)
def me(user: User = Depends(current_user)):
    return JSONResponse({"username": user.username})
