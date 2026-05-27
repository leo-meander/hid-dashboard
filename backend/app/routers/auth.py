"""
Auth router — login, token refresh, user management (admin only).
JWT-based authentication. Token stored client-side (localStorage).
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import List, Optional
from uuid import UUID

import bcrypt
import jwt
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.user import User

router = APIRouter()

# ── Config ────────────────────────────────────────────────────────────────────

SECRET_KEY = os.getenv("JWT_SECRET", "hid-dev-secret-change-in-production")
ALGORITHM  = "HS256"
TOKEN_EXPIRE_HOURS = 24 * 7  # 7 days

bearer = HTTPBearer(auto_error=False)

# ── Helpers ───────────────────────────────────────────────────────────────────

def _hash(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()

def _verify(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode(), hashed.encode())
    except Exception:
        return False

def _create_token(user: User) -> str:
    payload = {
        "sub":   str(user.id),
        "email": user.email,
        "role":  user.role,
        "exp":   datetime.now(timezone.utc) + timedelta(hours=TOKEN_EXPIRE_HOURS),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)

def _decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")


# ── Auth dependency ───────────────────────────────────────────────────────────

def get_current_user(
    creds: Optional[HTTPAuthorizationCredentials] = Depends(bearer),
    db: Session = Depends(get_db),
) -> User:
    if not creds:
        raise HTTPException(status_code=401, detail="Not authenticated")
    payload = _decode_token(creds.credentials)
    user = db.query(User).filter_by(id=payload["sub"], is_active=True).first()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    return user

def require_admin(current: User = Depends(get_current_user)) -> User:
    if current.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    return current


# ── Schemas ───────────────────────────────────────────────────────────────────

class LoginIn(BaseModel):
    email: str
    password: str

class CreateUserIn(BaseModel):
    email: str
    name: Optional[str] = None
    password: str
    role: str = "editor"   # admin | editor | viewer
    # Access scope — empty/None means "all". Ignored for admins.
    allowed_branches: Optional[List[str]] = None  # branch UUIDs (as text)
    allowed_pages: Optional[List[str]] = None      # sidebar group keys

class UpdateUserIn(BaseModel):
    name: Optional[str] = None
    role: Optional[str] = None
    password: Optional[str] = None
    is_active: Optional[bool] = None
    allowed_branches: Optional[List[str]] = None
    allowed_pages: Optional[List[str]] = None

class ChangePasswordIn(BaseModel):
    old_password: str
    new_password: str

def _user_out(u: User) -> dict:
    return {
        "id":               str(u.id),
        "email":            u.email,
        "name":             u.name,
        "role":             u.role,
        "is_active":        u.is_active,
        # Always arrays for the frontend; [] means "all".
        "allowed_branches": list(u.allowed_branches or []),
        "allowed_pages":    list(u.allowed_pages or []),
        "created_at":       u.created_at.isoformat() if u.created_at else None,
    }


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/login")
def login(body: LoginIn, db: Session = Depends(get_db)):
    user = db.query(User).filter_by(email=body.email.lower().strip(), is_active=True).first()
    if not user or not user.password_hash or not _verify(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    return {
        "success": True,
        "data": {
            "token": _create_token(user),
            "user":  _user_out(user),
        },
    }


@router.get("/me")
def me(current: User = Depends(get_current_user)):
    return {"success": True, "data": _user_out(current)}


@router.post("/change-password")
def change_password(
    body: ChangePasswordIn,
    current: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not current.password_hash or not _verify(body.old_password, current.password_hash):
        raise HTTPException(status_code=400, detail="Old password is incorrect")
    if not body.new_password or len(body.new_password) < 6:
        raise HTTPException(status_code=400, detail="New password must be at least 6 characters")
    if _verify(body.new_password, current.password_hash):
        raise HTTPException(status_code=400, detail="New password must differ from old password")
    current.password_hash = _hash(body.new_password)
    db.commit()
    return {"success": True, "data": {"changed": True}}


@router.get("/users")
def list_users(
    _current: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    users = db.query(User).order_by(User.created_at).all()
    return {"success": True, "data": [_user_out(u) for u in users]}


@router.post("/users", status_code=201)
def create_user(
    body: CreateUserIn,
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if body.role not in ("admin", "editor", "viewer"):
        raise HTTPException(400, "Role must be admin, editor, or viewer")
    if db.query(User).filter_by(email=body.email.lower().strip()).first():
        raise HTTPException(400, "Email already exists")
    # Admins always have full access — scope only applies to editor/viewer.
    is_admin = body.role == "admin"
    user = User(
        email=body.email.lower().strip(),
        name=body.name,
        role=body.role,
        password_hash=_hash(body.password),
        allowed_branches=None if is_admin else (body.allowed_branches or None),
        allowed_pages=None if is_admin else (body.allowed_pages or None),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return {"success": True, "data": _user_out(user)}


@router.put("/users/{user_id}")
def update_user(
    user_id: UUID,
    body: UpdateUserIn,
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter_by(id=user_id).first()
    if not user:
        raise HTTPException(404, "User not found")
    if body.name      is not None: user.name       = body.name
    if body.role      is not None:
        if body.role not in ("admin", "editor", "viewer"):
            raise HTTPException(400, "Invalid role")
        user.role = body.role
    if body.is_active is not None: user.is_active  = body.is_active
    if body.password  is not None: user.password_hash = _hash(body.password)
    # Empty list normalizes to NULL = "all". Field omitted (None) = leave unchanged.
    if body.allowed_branches is not None: user.allowed_branches = body.allowed_branches or None
    if body.allowed_pages    is not None: user.allowed_pages    = body.allowed_pages or None
    # Admins always have full access — clear any scope when promoting to admin.
    if user.role == "admin":
        user.allowed_branches = None
        user.allowed_pages = None
    db.commit()
    db.refresh(user)
    return {"success": True, "data": _user_out(user)}


@router.delete("/users/{user_id}")
def deactivate_user(
    user_id: UUID,
    current: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if str(user_id) == str(current.id):
        raise HTTPException(400, "Cannot deactivate yourself")
    user = db.query(User).filter_by(id=user_id).first()
    if not user:
        raise HTTPException(404, "User not found")
    user.is_active = False
    db.commit()
    return {"success": True, "data": {"deactivated": str(user_id)}}


# ── First-time setup (only works when no users exist) ─────────────────────────

class SetupIn(BaseModel):
    email: str
    name: Optional[str] = "Admin"
    password: str

@router.post("/setup")
def setup(body: SetupIn, db: Session = Depends(get_db)):
    """Create the first admin user. Fails if any user already has a password."""
    has_password = db.query(User).filter(User.password_hash.isnot(None)).count() > 0
    if has_password:
        raise HTTPException(403, "Setup already completed")
    # Upsert: update existing record with this email, or create new
    user = db.query(User).filter_by(email=body.email.lower().strip()).first()
    if user:
        user.name          = body.name or user.name
        user.role          = "admin"
        user.password_hash = _hash(body.password)
        user.is_active     = True
    else:
        user = User(
            email=body.email.lower().strip(),
            name=body.name,
            role="admin",
            password_hash=_hash(body.password),
        )
        db.add(user)
    db.commit()
    db.refresh(user)
    return {
        "success": True,
        "data": {"token": _create_token(user), "user": _user_out(user)},
    }

@router.get("/needs-setup")
def needs_setup(db: Session = Depends(get_db)):
    has_password = db.query(User).filter(User.password_hash.isnot(None)).count() > 0
    return {"success": True, "data": {"needs_setup": not has_password}}
