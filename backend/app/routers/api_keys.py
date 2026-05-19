"""API Keys router — admin-only CRUD for managing external API keys.

Keys are shared with external Claude clients via the MCP server mounted at
/mcp. Each key has per-tool and per-branch scopes; default is DENY (a freshly
created key with no scopes can authenticate but cannot call any tool)."""
from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

import bcrypt
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.api_key import ApiKey
from app.routers.auth import require_admin
from app.models.user import User

router = APIRouter()

# ── Helpers ──────────────────────────────────────────────────────────────────

def _generate_api_key() -> str:
    """Generate a random API key with 'hid_' prefix."""
    return "hid_" + secrets.token_urlsafe(32)


def _hash_key(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def _normalize_scope(value) -> Optional[list[str]]:
    """Coerce empty list to None so the column stays NULL (= no access) by default."""
    if value is None:
        return None
    if isinstance(value, list):
        cleaned = [str(x).strip() for x in value if str(x).strip()]
        return cleaned if cleaned else None
    return None


def _key_out(k: ApiKey) -> dict:
    return {
        "id": str(k.id),
        "name": k.name,
        "key_prefix": k.key_prefix,
        "is_active": k.is_active,
        "last_used_at": k.last_used_at.isoformat() if k.last_used_at else None,
        "allowed_branches": k.allowed_branches,
        "allowed_tools": k.allowed_tools,
        "created_by": str(k.created_by) if k.created_by else None,
        "created_at": k.created_at.isoformat() if k.created_at else None,
    }


# ── Schemas ──────────────────────────────────────────────────────────────────

class CreateKeyIn(BaseModel):
    name: str
    # Both default to None (= no access). Use ["*"] to grant all.
    allowed_branches: Optional[list[str]] = Field(default=None)
    allowed_tools: Optional[list[str]] = Field(default=None)


class UpdateKeyIn(BaseModel):
    name: Optional[str] = None
    is_active: Optional[bool] = None
    allowed_branches: Optional[list[str]] = None
    allowed_tools: Optional[list[str]] = None


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.post("")
def create_api_key(
    body: CreateKeyIn,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Generate a new API key. The plaintext key is returned ONCE — store it safely.
    The key starts with no MCP access; pass allowed_branches / allowed_tools to
    grant scopes at creation, or PATCH later."""
    try:
        plain_key = _generate_api_key()
        key_prefix = plain_key[:12]

        api_key = ApiKey(
            name=body.name.strip(),
            key_hash=_hash_key(plain_key),
            key_prefix=key_prefix,
            created_by=admin.id,
            allowed_branches=_normalize_scope(body.allowed_branches),
            allowed_tools=_normalize_scope(body.allowed_tools),
        )
        db.add(api_key)
        db.commit()
        db.refresh(api_key)

        result = _key_out(api_key)
        result["key"] = plain_key  # Only time the full key is returned

        return {
            "success": True,
            "data": result,
            "error": None,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.get("")
def list_api_keys(
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """List all API keys (active and inactive)."""
    try:
        keys = db.query(ApiKey).order_by(ApiKey.created_at.desc()).all()
        return {
            "success": True,
            "data": [_key_out(k) for k in keys],
            "error": None,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.patch("/{key_id}")
def update_api_key(
    key_id: UUID,
    body: UpdateKeyIn,
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Update name, active flag, or MCP scopes. Pass `[]` to clear a scope
    (revoke all access for that dimension); omit the field to leave it unchanged."""
    try:
        api_key = db.query(ApiKey).filter_by(id=key_id).first()
        if not api_key:
            raise HTTPException(status_code=404, detail="API key not found")

        data = body.model_dump(exclude_unset=True)
        if "name" in data and data["name"] is not None:
            api_key.name = data["name"].strip()
        if "is_active" in data and data["is_active"] is not None:
            api_key.is_active = bool(data["is_active"])
        if "allowed_branches" in data:
            api_key.allowed_branches = _normalize_scope(data["allowed_branches"])
        if "allowed_tools" in data:
            api_key.allowed_tools = _normalize_scope(data["allowed_tools"])

        api_key.updated_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(api_key)

        return {
            "success": True,
            "data": _key_out(api_key),
            "error": None,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/{key_id}")
def revoke_api_key(
    key_id: UUID,
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Revoke (soft-delete) an API key."""
    try:
        api_key = db.query(ApiKey).filter_by(id=key_id).first()
        if not api_key:
            raise HTTPException(status_code=404, detail="API key not found")
        api_key.is_active = False
        api_key.updated_at = datetime.now(timezone.utc)
        db.commit()
        return {
            "success": True,
            "data": {"revoked": str(key_id)},
            "error": None,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
