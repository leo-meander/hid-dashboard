"""MCP per-key scoping + audit log table.

Revision ID: 044
Revises: 043

Adds per-API-key scoping for the MCP server mounted at /mcp:
  - api_keys.allowed_branches JSONB  (null/[] = no access; ["*"] = all; ["uuid",...] = whitelist)
  - api_keys.allowed_tools    JSONB  (same convention, but with tool names)

Default is DENY — existing keys cannot access MCP until an admin adds scopes
via PATCH /api/api-keys/{id}. This keeps the existing /api/public clients
unaffected while letting us share narrowly-scoped keys to teammates who
will plug them into their own Claude Desktop/Code instances.

Also creates mcp_audit_log: every tool call (allowed, denied, or errored)
writes one row so we can investigate misuse or quota overruns later.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB

revision = "044"
down_revision = "043"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "api_keys",
        sa.Column("allowed_branches", JSONB, nullable=True),
    )
    op.add_column(
        "api_keys",
        sa.Column("allowed_tools", JSONB, nullable=True),
    )

    op.create_table(
        "mcp_audit_log",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("api_key_id", UUID(as_uuid=True), sa.ForeignKey("api_keys.id", ondelete="SET NULL"), nullable=True),
        sa.Column("tool_name", sa.String(64), nullable=False),
        sa.Column("arguments", JSONB, nullable=True),
        sa.Column("status", sa.String(16), nullable=False),  # 'ok' | 'denied' | 'error'
        sa.Column("response_size_bytes", sa.Integer, nullable=True),
        sa.Column("duration_ms", sa.Integer, nullable=True),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("idx_mcp_audit_key_time", "mcp_audit_log", ["api_key_id", "created_at"])
    op.create_index("idx_mcp_audit_tool_time", "mcp_audit_log", ["tool_name", "created_at"])


def downgrade():
    op.drop_index("idx_mcp_audit_tool_time")
    op.drop_index("idx_mcp_audit_key_time")
    op.drop_table("mcp_audit_log")
    op.drop_column("api_keys", "allowed_tools")
    op.drop_column("api_keys", "allowed_branches")
