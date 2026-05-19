"""MCP auth — Starlette middleware that validates the API key on every
incoming /mcp request and stashes the matched ApiKey in a ContextVar so
tool handlers can read it.

The official mcp Python SDK does not yet expose HTTP request headers to tool
handlers (modelcontextprotocol/python-sdk#750), so ContextVar is the cleanest
way to pass per-request auth state from middleware into tools. ContextVars
propagate correctly across asyncio task boundaries within the same request.
"""
from __future__ import annotations

import contextvars
import logging
from datetime import datetime, timezone
from typing import Optional

import bcrypt
from starlette.types import ASGIApp, Receive, Scope, Send

from app.database import SessionLocal
from app.models.api_key import ApiKey

logger = logging.getLogger(__name__)


_current_api_key: contextvars.ContextVar[Optional[ApiKey]] = contextvars.ContextVar(
    "_mcp_current_api_key", default=None
)


def get_current_api_key() -> Optional[ApiKey]:
    """Return the ApiKey row matched by the current request, or None."""
    return _current_api_key.get()


class McpAuthMiddleware:
    """ASGI middleware: extract `Authorization: Bearer <key>` or `X-API-Key`,
    bcrypt-verify against api_keys table, then set ContextVar.

    Rejects with 401 if the header is missing, malformed, or the key is
    revoked / unknown. Touches `last_used_at` on success (best-effort)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        plain_key = self._extract_key(scope)
        if not plain_key:
            await self._send_401(send, "Missing API key (use Authorization: Bearer hid_... or X-API-Key)")
            return
        if not plain_key.startswith("hid_"):
            await self._send_401(send, "Invalid API key format")
            return

        matched = self._verify_key(plain_key)
        if matched is None:
            await self._send_401(send, "Invalid or revoked API key")
            return

        token = _current_api_key.set(matched)
        try:
            await self.app(scope, receive, send)
        finally:
            _current_api_key.reset(token)

    @staticmethod
    def _extract_key(scope: Scope) -> Optional[str]:
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        auth = headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        x_api = headers.get("x-api-key", "")
        if x_api:
            return x_api.strip()
        return None

    @staticmethod
    def _verify_key(plain_key: str) -> Optional[ApiKey]:
        """Bcrypt-verify plain_key against api_keys table. Returns a detached
        ApiKey instance on match, or None. Best-effort touches last_used_at."""
        db = SessionLocal()
        try:
            prefix = plain_key[:12]
            candidates = db.query(ApiKey).filter_by(key_prefix=prefix, is_active=True).all()
            for c in candidates:
                try:
                    if bcrypt.checkpw(plain_key.encode(), c.key_hash.encode()):
                        try:
                            c.last_used_at = datetime.now(timezone.utc)
                            db.commit()
                            db.refresh(c)
                        except Exception:
                            db.rollback()
                        db.expunge(c)
                        return c
                except Exception:
                    continue
            return None
        finally:
            db.close()

    @staticmethod
    async def _send_401(send: Send, message: str) -> None:
        body = ('{"error":"' + message.replace('"', "'") + '"}').encode()
        await send({
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"www-authenticate", b'Bearer realm="hid-mcp"'),
            ],
        })
        await send({"type": "http.response.body", "body": body})


# ── Scope helpers (used by tool wrappers) ────────────────────────────────────

def _normalize_scope(value) -> list[str]:
    """Coerce JSONB column to list[str]. None / missing → []."""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(x) for x in value]
    return []


def tool_allowed(api_key: ApiKey, tool_name: str) -> bool:
    scopes = _normalize_scope(api_key.allowed_tools)
    return "*" in scopes or tool_name in scopes


def branch_allowed(api_key: ApiKey, branch_id: Optional[str]) -> bool:
    """Return True if this key may read data scoped to branch_id.
    `branch_id` may be None — meaning the caller did not specify a branch,
    in which case we permit it (tool will filter response by allowed_branches).
    """
    if branch_id is None:
        return True
    scopes = _normalize_scope(api_key.allowed_branches)
    return "*" in scopes or str(branch_id) in scopes


def allowed_branches(api_key: ApiKey) -> Optional[set[str]]:
    """Return the set of branch IDs this key may see, or None for 'all'.
    Tools use this to filter response rows when the caller did not pre-narrow."""
    scopes = _normalize_scope(api_key.allowed_branches)
    if "*" in scopes:
        return None
    return set(scopes)
