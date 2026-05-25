"""OAuth refresh tokens for MCP — keep claude.ai connected past 24h.

Revision ID: 046
Revises: 045

Why
----
045 issued a 24h access JWT but no refresh token, on the assumption that
claude.ai would silently redo the OAuth dance when it expired. That
assumption was wrong: /oauth/authorize is an interactive email+password
consent page, so claude.ai cannot renew silently — every ~24h the connector
dropped with "Connection has expired. You can reconnect to re-authenticate"
and the user had to log in by hand.

This adds rotating refresh tokens (RFC 6749 §6). The token endpoint now also
accepts grant_type=refresh_token, so claude.ai renews the access JWT in the
background. Tokens live 30 days and rotate on every use (each refresh revokes
the old row and issues a new one). We store only the SHA-256 hash.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "046"
down_revision = "045"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "oauth_refresh_tokens",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("client_id", sa.String(64), nullable=False),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("audience", sa.Text, nullable=False),
        sa.Column("scope", sa.Text, nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("idx_oauth_refresh_token_hash", "oauth_refresh_tokens", ["token_hash"])
    op.create_index("idx_oauth_refresh_expires", "oauth_refresh_tokens", ["expires_at"])


def downgrade():
    op.drop_index("idx_oauth_refresh_expires")
    op.drop_index("idx_oauth_refresh_token_hash")
    op.drop_table("oauth_refresh_tokens")
