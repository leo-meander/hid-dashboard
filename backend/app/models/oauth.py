"""OAuth 2.1 models — minimal authorization server for MCP clients (claude.ai).

OAuthClient — one row per registered MCP client (claude.ai registers itself
via Dynamic Client Registration on first connect).

OAuthAuthCode — short-lived (10 min) single-use authorization codes that
hold the PKCE challenge between /oauth/authorize and /oauth/token. Refresh
tokens are intentionally skipped in v1: claude.ai will just redo the OAuth
dance silently when access tokens expire (24h)."""
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
