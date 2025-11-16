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

from app.db import get_session
from app.models import User

log = logging.getLogger("auth")

SECRET_KEY = os.getenv("SECRET_KEY", "devsecret_change_me")
ALGORITHM = "HS256"
COOKIE_NAME = "session"
COOKIE_MAX_AGE = 60 * 60 * 24 * 7  # 7 days

pwd = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

def hash_pw(p: str) -> str:
    return pwd.hash(p)

def verify_pw(p: str, h: str) -> bool:
    return pwd.verify(p, h)

def create_token(user_id: int) -> str:
    exp = dt.datetime.utcnow() + dt.timedelta(seconds=COOKIE_MAX_AGE)
    return jwt.encode({"sub": str(user_id), "exp": exp}, SECRET_KEY, algorithm=ALGORITHM)

def get_user_from_token(token: str, session: Session) -> Optional[User]:
    try:
        data = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        uid = int(data.get("sub", "0"))
    except JWTError as e:
        log.warning("JWT decode failed: %s", e)
        return None
    return session.get(User, uid)

async def current_user(request: Request, session: Session = Depends(get_session)) -> User:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    user = get_user_from_token(token, session)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    return user

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
        # hash password (requires passlib[bcrypt] + bcrypt installed)
        try:
            pwhash = hash_pw(password)
        except Exception as e:
            log.exception("Password hash failed")
            return templates.TemplateResponse("signup.html", {"request": request, "error": f"Password hashing failed: {e}"})
        user = User(username=username, password_hash=pwhash)
        session.add(user); session.commit(); session.refresh(user)

        token = create_token(user.id)
        resp = RedirectResponse(url="/", status_code=302)
        resp.set_cookie(
            COOKIE_NAME, token,
            httponly=True, samesite="lax", max_age=COOKIE_MAX_AGE, path="/"
        )
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
        resp.set_cookie(
            COOKIE_NAME, token,
            httponly=True, samesite="lax", max_age=COOKIE_MAX_AGE, path="/"
        )
        return resp
    except Exception as e:
        log.exception("Login failed")
        return templates.TemplateResponse("login.html", {"request": request, "error": f"Login failed: {e}"})

@router.post("/logout", include_in_schema=False)
def logout():
    resp = RedirectResponse(url="/", status_code=302)
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp

# --- tiny helpers for debugging ---

@router.get("/whoami", include_in_schema=False)
def whoami(user: User = Depends(current_user)):
    return JSONResponse({"id": user.id, "username": user.username})

# DEV ONLY: quick seeder to bypass forms if needed; remove in non-dev
@router.get("/dev/seed_user", include_in_schema=False)
def dev_seed_user(username: str, password: str, session: Session = Depends(get_session)):
    existing = session.exec(select(User).where(User.username == username)).first()
    if existing:
        return JSONResponse({"status": "exists"})
    user = User(username=username, password_hash=hash_pw(password))
    session.add(user); session.commit(); session.refresh(user)
    return JSONResponse({"status": "created", "id": user.id})
