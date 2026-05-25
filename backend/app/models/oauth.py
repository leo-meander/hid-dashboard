"""OAuth 2.1 models — minimal authorization server for MCP clients (claude.ai).

OAuthClient — one row per registered MCP client (claude.ai registers itself
via Dynamic Client Registration on first connect).

OAuthAuthCode — short-lived (10 min) single-use authorization codes that
hold the PKCE challenge between /oauth/authorize and /oauth/token.

OAuthRefreshToken — long-lived (30 day) rotating refresh tokens. The access
JWT lives only 24h; without a refresh token claude.ai cannot silently renew
it (the /oauth/authorize step is an interactive email+password form), so the
connection would drop and force a manual re-login every day. Refresh tokens
let claude.ai renew in the background via grant_type=refresh_token. We store
only the SHA-256 hash of the token (never the plaintext), and rotate on every
use — each refresh marks the old row revoked and issues a fresh one."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.database import Base


class OAuthClient(Base):
    __tablename__ = "oauth_clients"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    client_id = Column(String(64), nullable=False, unique=True)
    client_name = Column(String(200), nullable=False)
    redirect_uris = Column(JSONB, nullable=False)
    grant_types = Column(JSONB, nullable=True)
    response_types = Column(JSONB, nullable=True)
    token_endpoint_auth_method = Column(String(32), nullable=False, default="none")
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class OAuthAuthCode(Base):
    __tablename__ = "oauth_auth_codes"

    code = Column(String(64), primary_key=True)
    client_id = Column(String(64), nullable=False)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    code_challenge = Column(String(128), nullable=False)
    code_challenge_method = Column(String(16), nullable=False, default="S256")
    redirect_uri = Column(Text, nullable=False)
    scope = Column(Text, nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    used_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class OAuthRefreshToken(Base):
    __tablename__ = "oauth_refresh_tokens"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # SHA-256 hex of the opaque refresh token. We never store the plaintext —
    # /oauth/token looks the token up by hashing the presented value.
    token_hash = Column(String(64), nullable=False, unique=True)
    client_id = Column(String(64), nullable=False)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    # The RFC 8707 resource URI this token's access JWTs are bound to (`aud`),
    # captured at issue time so refresh re-issues with the same audience.
    audience = Column(Text, nullable=False)
    scope = Column(Text, nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    # Set when this token is rotated away (used) or explicitly revoked. A
    # non-null value means the token is no longer accepted.
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
