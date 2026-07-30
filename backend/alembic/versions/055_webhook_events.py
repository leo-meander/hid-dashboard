"""webhook_events table — persist reservation fan-out history

Revision ID: 055
Revises: 054
Create Date: 2026-07-30
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "055"
down_revision = "054"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "webhook_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("reservation_id", sa.String(64), nullable=False),
        sa.Column("branch", sa.String(32), nullable=True),
        sa.Column("guest_email", sa.String(255), nullable=True),
        sa.Column("source", sa.String(128), nullable=True),
        sa.Column("ghl", postgresql.JSONB(), nullable=True),
        sa.Column("meta", postgresql.JSONB(), nullable=True),
        sa.Column("google_ads", postgresql.JSONB(), nullable=True),
        sa.Column("tiktok", postgresql.JSONB(), nullable=True),
        sa.Column(
            "has_failure", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    # created_at drives both the monitor ordering and the nightly purge.
    op.create_index("ix_webhook_events_created_at", "webhook_events", ["created_at"])
    # reservation_id backs the dedup lookup, which runs once per polled
    # reservation — it has to be an index seek, not a scan.
    op.create_index(
        "ix_webhook_events_reservation_id", "webhook_events", ["reservation_id"]
    )
    op.create_index("ix_webhook_events_branch", "webhook_events", ["branch"])
    op.create_index("ix_webhook_events_has_failure", "webhook_events", ["has_failure"])


def downgrade():
    op.drop_table("webhook_events")
