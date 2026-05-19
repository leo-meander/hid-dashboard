"""MCP auth — Starlette middleware that validates an OAuth-issued JWT on
every incoming /mcp request and stashes the matched User in a ContextVar
so tool handlers can read it.

JWTs are issued by /oauth/token in routers/oauth.py, signed with the same
JWT_SECRET / HS256 as the rest of the app. The `aud` claim is the resource
URL the client requested (RFC 8707) — claude.ai canonicalises the connector
URL and sends it back. We don't strict-match the value because the mount
point (/mcp vs /mcp/mcp/) varies; we just require the claim is *present*,
which cleanly distinguishes OAuth tokens from web session tokens (the
latter have no `aud`).

The official mcp Python SDK does not yet expose HTTP request headers to
tool handlers (modelcontextprotocol/python-sdk#750), so ContextVar is the
cleanest way to pass per-request auth state into tools. ContextVars
propagate across asyncio task boundaries within the same request."""
from __future__ import annotations

import contextvars
import json
import logging
from typing import Optional

import jwt
from starlette.types import ASGIApp, Receive, Scope, Send

from app.database import SessionLocal
from app.models.user import User
from app.routers.auth import ALGORITHM, SECRET_KEY

logger = logging.getLogger(__name__)


_current_user: contextvars.ContextVar[Optional[User]] = contextvars.ContextVar(
    "_mcp_current_user", default=None
)


def get_current_user() -> Optional[User]:
    """Return the User row matched by the current request, or None."""
    return _current_user.get()


class McpAuthMiddleware:
    """ASGI middleware: extract `Authorization: Bearer <jwt>`, verify it's an
    MCP-audience token, load the User, set ContextVar.

    Rejects with 401 + WWW-Authenticate per RFC 6750 when the header is
    missing, malformed, or the token fails verification. The
    WWW-Authenticate header points to our protected-resource metadata so
    MCP clients can discover the authorization server."""

    def __init__(self, app: ASGIApp, *, resource_metadata_path: str = "/.well-known/oauth-protected-resource") -> None:
        self.app = app
        self.resource_metadata_path = resource_metadata_path

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        token = self._extract_bearer(scope)
        if not token:
            await self._send_401(scope, send, "invalid_request", "Missing Bearer token")
            return

        user = self._verify_jwt_and_load_user(token)
        if user is None:
            await self._send_401(scope, send, "invalid_token", "Token invalid, expired, or user disabled")
            return

        ctx_token = _current_user.set(user)
        try:
            await self.app(scope, receive, send)
        finally:
            _current_user.reset(ctx_token)

    @staticmethod
    def _extract_bearer(scope: Scope) -> Optional[str]:
        for k, v in scope.get("headers", []):
            if k.lower() == b"authorization":
                val = v.decode()
                if val.lower().startswith("bearer "):
                    return val[7:].strip()
        return None

    @staticmethod
    def _verify_jwt_and_load_user(token: str) -> Optional[User]:
        try:
            payload = jwt.decode(
                token, SECRET_KEY,
                algorithms=[ALGORITHM],
                # No strict `audience=` match — the JWT's aud is the
                # resource URL the client requested (e.g.
                # https://host/mcp), but the URL the client actually hits
                # may differ in mount-point segments. We `require` aud is
                # present (below), which is enough to separate OAuth
                # tokens from web session tokens (latter have no aud).
                options={"require": ["exp", "sub", "aud"], "verify_aud": False},
            )
        except jwt.PyJWTError as e:
            logger.info("MCP JWT verify failed: %s", e)
            return None

        user_id = payload.get("sub")
        if not user_id:
            return None

        db = SessionLocal()
        try:
            user = db.query(User).filter_by(id=user_id, is_active=True).first()
            if user is None:
                return None
            db.expunge(user)
            return user
        finally:
            db.close()

    async def _send_401(self, scope: Scope, send: Send, error: str, description: str) -> None:
        # Build absolute URL to the protected-resource metadata so MCP
        # clients can follow the WWW-Authenticate hint to discover OAuth.
        proto = b"https"
        host = None
        for k, v in scope.get("headers", []):
            if k.lower() == b"x-forwarded-proto":
                proto = v
            elif k.lower() == b"x-forwarded-host":
                host = v
            elif k.lower() == b"host" and host is None:
                host = v
        host_str = host.decode() if host else "localhost"
        resource_md = f"{proto.decode()}://{host_str}{self.resource_metadata_path}"

        challenge = (
            f'Bearer realm="hid-mcp", '
            f'error="{error}", '
            f'error_description="{description}", '
            f'resource_metadata="{resource_md}"'
        )
        body = json.dumps({"error": error, "error_description": description}).encode()
        await send({
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"www-authenticate", challenge.encode()),
            ],
        })
        await send({"type": "http.response.body", "body": body})
