"""Minimal OAuth 2.1 + PKCE authorization server for the MCP at /mcp.

Why this exists
---------------
claude.ai's Custom Connector UI ONLY accepts OAuth — not pre-shared Bearer
tokens. To let HiD users connect their claude.ai chat to our MCP, we need
an OAuth flow. We don't run a real IdP here; the "identity" is just the
existing HiD User row (email + password) and the access token is a JWT
signed with the same JWT_SECRET as the web session — but with `aud: "mcp"`
so web session tokens can't be repurposed against /mcp and vice versa.

Flow
----
1. claude.ai → POST /oauth/register   (RFC 7591 Dynamic Client Registration)
   ↳ we mint a client_id, store it, return it
2. claude.ai → opens browser to GET /oauth/authorize?…
   ↳ we render an HTML page: email + password + Allow / Deny buttons
3. User submits → POST /oauth/authorize  (same path, form-encoded)
   ↳ we verify HiD credentials, validate PKCE state, issue a one-shot
     auth code, redirect browser to the registered redirect_uri?code=…&state=…
4. claude.ai → POST /oauth/token  (authorization_code grant)
   ↳ we verify PKCE (SHA256(verifier) == challenge), mark code used,
     return a 24h access JWT + a 30-day refresh token
5. claude.ai → GET /mcp/mcp/ with Authorization: Bearer <jwt>
   ↳ McpAuthMiddleware (in app/mcp_server/auth.py) decodes, loads User
6. When the access JWT expires, claude.ai → POST /oauth/token
   (refresh_token grant)
   ↳ we verify the refresh token, rotate it, return a new access JWT +
     a new refresh token — no user interaction

Why refresh tokens (added after v1)
-----------------------------------
v1 issued only a 24h access JWT, assuming claude.ai would silently redo
steps 3-4 when it expired. That was wrong: step 3 (/oauth/authorize) is an
interactive email+password consent page, so claude.ai cannot renew silently.
The connector dropped every ~24h with "Connection has expired" and forced a
manual re-login. Rotating refresh tokens (step 6) fix that — claude.ai renews
in the background and the connection stays live for the 30-day refresh window.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlencode

import bcrypt
import jwt
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.oauth import OAuthAuthCode, OAuthClient, OAuthRefreshToken
from app.models.user import User
from app.routers.auth import ALGORITHM, SECRET_KEY

router = APIRouter()
logger = logging.getLogger(__name__)


AUTH_CODE_TTL_SECONDS = 600              # 10 min
ACCESS_TOKEN_TTL_SECONDS = 24 * 3600     # 24 hours
REFRESH_TOKEN_TTL_SECONDS = 30 * 24 * 3600  # 30 days


# ── Helpers ──────────────────────────────────────────────────────────────────

def _issuer(request: Request) -> str:
    """Public origin (scheme + host) — used as `iss` and as the base for
    well-known URLs. Trust X-Forwarded-Proto/Host because Zeabur terminates TLS."""
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.url.netloc
    return f"{proto}://{host}"


def _verify_pkce(code_verifier: str, code_challenge: str, method: str) -> bool:
    if method == "plain":
        return secrets.compare_digest(code_verifier, code_challenge)
    if method == "S256":
        digest = hashlib.sha256(code_verifier.encode()).digest()
        derived = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
        return secrets.compare_digest(derived, code_challenge)
    return False


def _issue_access_token(user: User, audience: str) -> tuple[str, int]:
    """Return (jwt, expires_in_seconds).

    `audience` is the canonical resource URI per RFC 8707 — claude.ai
    requires that the JWT `aud` claim equals the URL of the MCP server it
    is using the token against. Web session tokens (issued by /api/auth/login)
    have no `aud` claim, so the MCP middleware's `require: ["aud"]` check
    cleanly separates the two token families."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user.id),
        "email": user.email,
        "role": user.role,
        "aud": audience,
        "iat": now,
        "exp": now + timedelta(seconds=ACCESS_TOKEN_TTL_SECONDS),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM), ACCESS_TOKEN_TTL_SECONDS


def _hash_refresh_token(raw: str) -> str:
    """SHA-256 hex — what we persist. The plaintext only ever leaves in the
    token response; the DB stores this so a DB leak can't be replayed."""
    return hashlib.sha256(raw.encode()).hexdigest()


def _issue_refresh_token(
    db: Session, *, user: User, client_id: str, audience: str, scope: Optional[str]
) -> str:
    """Mint an opaque refresh token, store its hash, return the plaintext."""
    raw = secrets.token_urlsafe(40)
    db.add(OAuthRefreshToken(
        token_hash=_hash_refresh_token(raw),
        client_id=client_id,
        user_id=user.id,
        audience=audience,
        scope=scope,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=REFRESH_TOKEN_TTL_SECONDS),
    ))
    return raw


def _token_error(error: str, description: str, status_code: int = 400) -> JSONResponse:
    """RFC 6749 §5.2 error body. The token endpoint must return this shape
    (not FastAPI's {"detail": ...}) so MCP clients can tell, e.g., a one-off
    invalid_grant (re-auth) apart from a hard failure."""
    return JSONResponse(
        {"error": error, "error_description": description}, status_code=status_code
    )


def _validate_resource(request: Request, resource: Optional[str]) -> str:
    """If client supplied a `resource` parameter (RFC 8707), require it to
    be on our origin and return it. Otherwise default to the canonical MCP
    URL on our origin (used as the JWT `aud`)."""
    own_origin = _issuer(request)
    if resource:
        if not resource.startswith(own_origin):
            raise HTTPException(400, f"resource must be on origin {own_origin}")
        return resource
    return f"{own_origin}/mcp"


def _consent_page(
    *,
    client_name: str,
    error: Optional[str],
    form_fields: dict,
) -> str:
    """Inline HTML — keeps backend the only thing that needs to change for OAuth.
    No frontend route or template engine required."""
    hidden = "".join(
        f'<input type="hidden" name="{k}" value="{_html_escape(v)}">'
        for k, v in form_fields.items() if v is not None
    )
    err_block = f'<div class="err">{_html_escape(error)}</div>' if error else ""
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Authorize HiD access</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  body {{ font-family: -apple-system, system-ui, sans-serif; background:#0f172a; color:#e2e8f0;
         display:flex; align-items:center; justify-content:center; min-height:100vh; margin:0; padding:1rem; }}
  .card {{ background:#1e293b; padding:2rem; border-radius:12px; max-width:380px; width:100%;
          box-shadow:0 10px 40px rgba(0,0,0,.5); }}
  h1 {{ font-size:1.25rem; margin:0 0 .5rem; }}
  p.sub {{ color:#94a3b8; margin:0 0 1.5rem; font-size:.9rem; }}
  .client {{ color:#60a5fa; font-weight:600; }}
  label {{ display:block; margin:.75rem 0 .25rem; font-size:.85rem; color:#cbd5e1; }}
  input[type=email], input[type=password] {{ width:100%; padding:.6rem .75rem; border-radius:6px;
         border:1px solid #334155; background:#0f172a; color:#e2e8f0; font-size:.95rem; box-sizing:border-box; }}
  input:focus {{ outline:none; border-color:#60a5fa; }}
  .row {{ display:flex; gap:.5rem; margin-top:1.25rem; }}
  button {{ flex:1; padding:.65rem; border-radius:6px; border:0; font-weight:600; cursor:pointer; font-size:.95rem; }}
  button.allow {{ background:#3b82f6; color:white; }}
  button.allow:hover {{ background:#2563eb; }}
  button.deny  {{ background:#334155; color:#cbd5e1; }}
  button.deny:hover {{ background:#475569; }}
  .err {{ background:#7f1d1d; color:#fecaca; padding:.5rem .75rem; border-radius:6px; margin-bottom:1rem; font-size:.85rem; }}
  .footer {{ margin-top:1rem; text-align:center; font-size:.75rem; color:#64748b; }}
</style></head>
<body><div class="card">
  <h1>Authorize HiD access</h1>
  <p class="sub"><span class="client">{_html_escape(client_name)}</span> is requesting permission
     to read your hotel data through the HiD MCP server.</p>
  {err_block}
  <form method="POST" action="/oauth/authorize">
    {hidden}
    <label>HiD email</label>
    <input type="email" name="email" autocomplete="username" required autofocus>
    <label>HiD password</label>
    <input type="password" name="password" autocomplete="current-password" required>
    <div class="row">
      <button type="submit" name="decision" value="deny" class="deny">Deny</button>
      <button type="submit" name="decision" value="allow" class="allow">Allow</button>
    </div>
  </form>
  <div class="footer">Token expires in 24 hours · You can revoke access any time from HiD Settings</div>
</div></body></html>"""


def _html_escape(s) -> str:
    if s is None:
        return ""
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                  .replace('"', "&quot;").replace("'", "&#39;"))


def _redirect_with(redirect_uri: str, params: dict) -> RedirectResponse:
    sep = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(redirect_uri + sep + urlencode(params), status_code=302)


# ── Discovery endpoints (mounted at root via include_router with no prefix) ──

@router.get("/.well-known/oauth-authorization-server")
def discovery_authz_server(request: Request):
    iss = _issuer(request)
    return JSONResponse({
        "issuer": iss,
        "authorization_endpoint": f"{iss}/oauth/authorize",
        "token_endpoint": f"{iss}/oauth/token",
        "registration_endpoint": f"{iss}/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256", "plain"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": ["mcp"],
    })


@router.get("/.well-known/oauth-protected-resource")
def discovery_protected_resource(request: Request):
    iss = _issuer(request)
    return JSONResponse({
        # Use the canonical /mcp URL (no trailing slash, no double-/mcp/) —
        # claude.ai canonicalizes the connector URL the user typed and sends
        # this exact value in /authorize and /token `resource` params.
        "resource": f"{iss}/mcp",
        "authorization_servers": [iss],
        "bearer_methods_supported": ["header"],
        "scopes_supported": ["mcp"],
    })


# ── Dynamic Client Registration (RFC 7591) ───────────────────────────────────

class RegisterIn(BaseModel):
    redirect_uris: list[str] = Field(..., min_length=1)
    client_name: Optional[str] = Field(default="MCP Client")
    grant_types: Optional[list[str]] = None
    response_types: Optional[list[str]] = None
    token_endpoint_auth_method: Optional[str] = "none"
    # Accept any other claude.ai-sent metadata silently; we just don't store it.

    class Config:
        extra = "ignore"


@router.post("/oauth/register", status_code=201)
def register_client(body: RegisterIn, request: Request, db: Session = Depends(get_db)):
    logger.info("OAuth REGISTER from %s: redirect_uris=%s client_name=%s",
                request.client.host if request.client else "?",
                body.redirect_uris, body.client_name)
    client_id = "mcp_" + secrets.token_urlsafe(24)
    client = OAuthClient(
        client_id=client_id,
        client_name=body.client_name or "MCP Client",
        redirect_uris=body.redirect_uris,
        grant_types=body.grant_types or ["authorization_code"],
        response_types=body.response_types or ["code"],
        token_endpoint_auth_method=body.token_endpoint_auth_method or "none",
    )
    db.add(client)
    db.commit()
    db.refresh(client)
    return JSONResponse({
        "client_id": client.client_id,
        "client_id_issued_at": int(client.created_at.timestamp()) if client.created_at else 0,
        "client_name": client.client_name,
        "redirect_uris": client.redirect_uris,
        "grant_types": client.grant_types,
        "response_types": client.response_types,
        "token_endpoint_auth_method": client.token_endpoint_auth_method,
    }, status_code=201)


# ── Authorization endpoint (login + consent in one page) ─────────────────────

@router.get("/oauth/authorize", response_class=HTMLResponse)
def authorize_get(
    request: Request,
    client_id: str,
    redirect_uri: str,
    response_type: str,
    code_challenge: str,
    code_challenge_method: str = "S256",
    state: Optional[str] = None,
    scope: Optional[str] = None,
    resource: Optional[str] = None,
    db: Session = Depends(get_db),
):
    logger.info("OAuth AUTHORIZE GET client_id=%s redirect_uri=%s resource=%s scope=%s",
                client_id, redirect_uri, resource, scope)
    client = _validate_authz_request(
        db, client_id, redirect_uri, response_type, code_challenge_method,
    )
    # Validate resource if provided (RFC 8707). Errors here surface in the
    # browser before the user logs in, which is the right place to surface
    # misconfiguration.
    _validate_resource(request, resource)
    return HTMLResponse(_consent_page(
        client_name=client.client_name,
        error=None,
        form_fields={
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": response_type,
            "code_challenge": code_challenge,
            "code_challenge_method": code_challenge_method,
            "state": state,
            "scope": scope,
            "resource": resource,
        },
    ))


@router.post("/oauth/authorize", response_class=HTMLResponse)
def authorize_post(
    request: Request,
    client_id: str = Form(...),
    redirect_uri: str = Form(...),
    response_type: str = Form(...),
    code_challenge: str = Form(...),
    code_challenge_method: str = Form("S256"),
    state: Optional[str] = Form(None),
    scope: Optional[str] = Form(None),
    resource: Optional[str] = Form(None),
    email: str = Form(...),
    password: str = Form(...),
    decision: str = Form(...),
    db: Session = Depends(get_db),
):
    logger.info("OAuth AUTHORIZE POST client_id=%s email=%s decision=%s resource=%s",
                client_id, email, decision, resource)
    client = _validate_authz_request(
        db, client_id, redirect_uri, response_type, code_challenge_method,
    )
    _validate_resource(request, resource)

    if decision != "allow":
        return _redirect_with(redirect_uri, {
            "error": "access_denied",
            "error_description": "User denied access",
            **({"state": state} if state else {}),
        })

    # Verify HiD credentials
    user = db.query(User).filter_by(email=email.lower().strip(), is_active=True).first()
    if not user or not user.password_hash or not _check_pw(password, user.password_hash):
        return HTMLResponse(_consent_page(
            client_name=client.client_name,
            error="Invalid HiD email or password — please try again.",
            form_fields={
                "client_id": client_id, "redirect_uri": redirect_uri,
                "response_type": response_type, "code_challenge": code_challenge,
                "code_challenge_method": code_challenge_method,
                "state": state, "scope": scope, "resource": resource,
            },
        ), status_code=401)

    # Mint single-use authorization code
    code = secrets.token_urlsafe(32)
    db.add(OAuthAuthCode(
        code=code,
        client_id=client_id,
        user_id=user.id,
        code_challenge=code_challenge,
        code_challenge_method=code_challenge_method,
        redirect_uri=redirect_uri,
        scope=scope,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=AUTH_CODE_TTL_SECONDS),
    ))
    db.commit()
    logger.info("OAuth code issued user=%s client=%s", user.email, client_id)

    return _redirect_with(redirect_uri, {
        "code": code,
        **({"state": state} if state else {}),
    })


def _validate_authz_request(
    db: Session, client_id: str, redirect_uri: str,
    response_type: str, code_challenge_method: str,
) -> OAuthClient:
    if response_type != "code":
        raise HTTPException(400, "Only response_type=code is supported")
    if code_challenge_method not in ("S256", "plain"):
        raise HTTPException(400, "code_challenge_method must be S256 or plain")
    client = db.query(OAuthClient).filter_by(client_id=client_id).first()
    if not client:
        raise HTTPException(400, "Unknown client_id — re-register via /oauth/register")
    if redirect_uri not in (client.redirect_uris or []):
        raise HTTPException(400, "redirect_uri does not match any registered URI")
    return client


def _check_pw(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode(), hashed.encode())
    except Exception:
        return False


# ── Token endpoint ───────────────────────────────────────────────────────────

@router.post("/oauth/token")
def token(
    request: Request,
    grant_type: str = Form(...),
    client_id: str = Form(...),
    # authorization_code grant fields
    code: Optional[str] = Form(None),
    redirect_uri: Optional[str] = Form(None),
    code_verifier: Optional[str] = Form(None),
    # refresh_token grant field
    refresh_token: Optional[str] = Form(None),
    resource: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    logger.info("OAuth TOKEN grant=%s client_id=%s redirect_uri=%s resource=%s",
                grant_type, client_id, redirect_uri, resource)
    if grant_type == "authorization_code":
        return _grant_authorization_code(
            request, db, client_id=client_id, code=code,
            redirect_uri=redirect_uri, code_verifier=code_verifier, resource=resource,
        )
    if grant_type == "refresh_token":
        return _grant_refresh_token(db, client_id=client_id, refresh_token=refresh_token)
    return _token_error(
        "unsupported_grant_type",
        "Only authorization_code and refresh_token are supported",
    )


def _grant_authorization_code(
    request: Request, db: Session, *, client_id: str, code: Optional[str],
    redirect_uri: Optional[str], code_verifier: Optional[str], resource: Optional[str],
) -> JSONResponse:
    if not code or not redirect_uri or not code_verifier:
        return _token_error(
            "invalid_request",
            "code, redirect_uri and code_verifier are required for authorization_code",
        )

    row = db.query(OAuthAuthCode).filter_by(code=code).first()
    if row is None:
        return _token_error("invalid_grant", "Invalid or unknown code")
    if row.used_at is not None:
        return _token_error("invalid_grant", "Code already used")
    if row.expires_at < datetime.now(timezone.utc):
        return _token_error("invalid_grant", "Code expired")
    if row.client_id != client_id:
        return _token_error("invalid_grant", "client_id mismatch")
    if row.redirect_uri != redirect_uri:
        return _token_error("invalid_grant", "redirect_uri mismatch")
    if not _verify_pkce(code_verifier, row.code_challenge, row.code_challenge_method):
        return _token_error("invalid_grant", "PKCE verification failed")

    user = db.query(User).filter_by(id=row.user_id, is_active=True).first()
    if not user:
        return _token_error("invalid_grant", "User no longer active")

    row.used_at = datetime.now(timezone.utc)

    # Audience binding (RFC 8707). Use the resource parameter the client
    # sent (claude.ai always sends one). If absent, fall back to our
    # canonical URL — this happens for clients that don't implement
    # Resource Indicators, who then accept whatever aud we issue.
    audience = _validate_resource(request, resource)
    access_token, expires_in = _issue_access_token(user, audience)
    refresh = _issue_refresh_token(
        db, user=user, client_id=client_id, audience=audience, scope=row.scope,
    )
    db.commit()
    logger.info("OAuth token issued (auth_code) user=%s client=%s aud=%s", user.email, client_id, audience)
    return JSONResponse({
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": expires_in,
        "refresh_token": refresh,
        "scope": row.scope or "mcp",
    })


def _grant_refresh_token(
    db: Session, *, client_id: str, refresh_token: Optional[str],
) -> JSONResponse:
    """Rotating refresh (RFC 6749 §6 + OAuth 2.1 §4.3.1). Verify the presented
    token, revoke it, and issue a fresh access JWT + a fresh refresh token. A
    presented token that is missing/expired/already-rotated yields
    invalid_grant, which tells claude.ai to fall back to the full login flow."""
    if not refresh_token:
        return _token_error("invalid_request", "refresh_token is required")

    row = db.query(OAuthRefreshToken).filter_by(
        token_hash=_hash_refresh_token(refresh_token)
    ).first()
    if row is None:
        return _token_error("invalid_grant", "Unknown refresh token")
    if row.revoked_at is not None:
        # Already rotated away (or revoked). claude.ai should be holding the
        # successor token; an old one here means a stale/duplicate request.
        return _token_error("invalid_grant", "Refresh token no longer valid")
    if row.expires_at < datetime.now(timezone.utc):
        return _token_error("invalid_grant", "Refresh token expired")
    if row.client_id != client_id:
        return _token_error("invalid_grant", "client_id mismatch")

    user = db.query(User).filter_by(id=row.user_id, is_active=True).first()
    if not user:
        return _token_error("invalid_grant", "User no longer active")

    # Rotate: this token is single-use. Mark it revoked and mint a successor.
    row.revoked_at = datetime.now(timezone.utc)
    access_token, expires_in = _issue_access_token(user, row.audience)
    new_refresh = _issue_refresh_token(
        db, user=user, client_id=client_id, audience=row.audience, scope=row.scope,
    )
    db.commit()
    logger.info("OAuth token issued (refresh) user=%s client=%s aud=%s", user.email, client_id, row.audience)
    return JSONResponse({
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": expires_in,
        "refresh_token": new_refresh,
        "scope": row.scope or "mcp",
    })
