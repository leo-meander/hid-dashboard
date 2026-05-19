"""OAuth 2.1 tables for MCP — claude.ai connector support.

Revision ID: 045
Revises: 044

Why
----
claude.ai web chat's Custom Connector UI only accepts OAuth-style auth,
not static Bearer tokens. To let HiD users connect to MCP from claude.ai
without installing Claude Code separately, we add a minimal OAuth 2.1 +
PKCE authorization server backed by the existing User table.

Two new tables:
  - oauth_clients: dynamically-registered clients (claude.ai posts to
    /oauth/register per RFC 7591 and gets a client_id back)
  - oauth_auth_codes: short-lived single-use authorization codes that hold
    the PKCE challenge until /oauth/token exchanges them for an access JWT

We also extend mcp_audit_log to record user_id (since callers are now Users
via OAuth, not ApiKeys). api_key_id stays nullable for backward compat — no
rows ever existed since 044 only landed days ago and the API-key MCP path
is being removed in this same change.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB

revision = "045"
down_revision = "044"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "oauth_clients",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("client_id", sa.String(64), nullable=False, unique=True),
        sa.Column("client_name", sa.String(200), nullable=False),
        sa.Column("redirect_uris", JSONB, nullable=False),
        sa.Column("grant_types", JSONB, nullable=True),
        sa.Column("response_types", JSONB, nullable=True),
        sa.Column("token_endpoint_auth_method", sa.String(32), nullable=False, server_default="none"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("idx_oauth_clients_client_id", "oauth_clients", ["client_id"])

    op.create_table(
        "oauth_auth_codes",
        sa.Column("code", sa.String(64), primary_key=True),
        sa.Column("client_id", sa.String(64), nullable=False),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("code_challenge", sa.String(128), nullable=False),
        sa.Column("code_challenge_method", sa.String(16), nullable=False, server_default="S256"),
        sa.Column("redirect_uri", sa.Text, nullable=False),
        sa.Column("scope", sa.Text, nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("idx_oauth_auth_codes_expires", "oauth_auth_codes", ["expires_at"])

    # Extend audit log to track which User called (in addition to legacy api_key_id).
    op.add_column(
        "mcp_audit_log",
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
    )
    op.create_index("idx_mcp_audit_user_time", "mcp_audit_log", ["user_id", "created_at"])


def downgrade():
    op.drop_index("idx_mcp_audit_user_time")
    op.drop_column("mcp_audit_log", "user_id")
    op.drop_index("idx_oauth_auth_codes_expires")
    op.drop_table("oauth_auth_codes")
    op.drop_index("idx_oauth_clients_client_id")
    op.drop_table("oauth_clients")
