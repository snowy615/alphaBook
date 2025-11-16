# app/auth.py
from __future__ import annotations
import os, datetime as dt, logging
from typing import Optional

from fastapi import APIRouter, Depends, Request, Form, HTTPException, status
from fastapi.responses import RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from jose import jwt, JWTError
from passlib.context import CryptContext
from sqlmodel import select, Session
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from app.db import get_session
from app.models import User

log = logging.getLogger("auth")

SECRET_KEY = os.getenv("SECRET_KEY", "devsecret_change_me")
ALGORITHM = "HS256"
COOKIE_NAME = "session"
COOKIE_MAX_AGE = 60 * 60 * 24 * 7  # 7 days

# Use PBKDF2-SHA256 to avoid bcrypt platform issues & 72-byte limit
pwd = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")

# Bearer auth support for Swagger (/docs)
http_bearer = HTTPBearer(auto_error=False)

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

def hash_pw(p: str) -> str:
    return pwd.hash(p)

def verify_pw(p: str, h: str) -> bool:
    return pwd.verify(p, h)

def create_token(user_id: int, max_age: int = COOKIE_MAX_AGE) -> str:
    exp = dt.datetime.utcnow() + dt.timedelta(seconds=max_age)
    return jwt.encode({"sub": str(user_id), "exp": exp}, SECRET_KEY, algorithm=ALGORITHM)

def get_user_from_token(token: str, session: Session) -> Optional[User]:
    try:
        data = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        uid = int(data.get("sub", "0"))
    except JWTError as e:
        log.warning("JWT decode failed: %s", e)
        return None
    return session.get(User, uid)

async def current_user(
    request: Request,
    creds: HTTPAuthorizationCredentials = Depends(http_bearer),
    session: Session = Depends(get_session),
) -> User:
    # 1) Try cookie
    token = request.cookies.get(COOKIE_NAME)
    # 2) Fallback to Authorization: Bearer <token>
    if not token and creds and creds.scheme.lower() == "bearer":
        token = creds.credentials
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    user = get_user_from_token(token, session)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    return user

# ---------------------- HTML forms ----------------------

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
            return templates.TemplateResponse("signup.html", {"request": request, "error": "Username already exists"})
        user = User(username=username, password_hash=hash_pw(password))
        session.add(user); session.commit(); session.refresh(user)

        token = create_token(user.id)
        resp = RedirectResponse(url="/", status_code=302)
        resp.set_cookie(COOKIE_NAME, token, httponly=True, samesite="lax", max_age=COOKIE_MAX_AGE, path="/")
        return resp
    except Exception as e:
        log.exception("Signup failed")
        return templates.TemplateResponse("signup.html", {"request": request, "error": f"Signup failed: {e}"})

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
            return templates.TemplateResponse("login.html", {"request": request, "error": "Invalid credentials"})
        token = create_token(user.id)
        resp = RedirectResponse(url="/", status_code=302)
        resp.set_cookie(COOKIE_NAME, token, httponly=True, samesite="lax", max_age=COOKIE_MAX_AGE, path="/")
        return resp
    except Exception as e:
        log.exception("Login failed")
        return templates.TemplateResponse("login.html", {"request": request, "error": f"Login failed: {e}"})

@router.post("/logout", include_in_schema=False)
def logout():
    resp = RedirectResponse(url="/", status_code=302)
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp

# ---------------------- JSON helpers for Swagger ----------------------

@router.post("/login/json")
def login_json(
    username: str = Form(...),
    password: str = Form(...),
    session: Session = Depends(get_session),
):
    """Login via form fields and get a bearer token (for /docs Authorize)."""
    user = session.exec(select(User).where(User.username == username)).first()
    if not user or not verify_pw(password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = create_token(user.id)
    return {"access_token": token, "token_type": "bearer"}

@router.get("/session/token")
def session_token(user: User = Depends(current_user)):
    """Get a fresh token using your current cookie session."""
    return {"access_token": create_token(user.id), "token_type": "bearer"}

@router.get("/whoami", include_in_schema=False)
def whoami(user: User = Depends(current_user)):
    return JSONResponse({"id": user.id, "username": user.username})
