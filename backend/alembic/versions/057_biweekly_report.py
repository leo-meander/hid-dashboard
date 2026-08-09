"""biweekly_report_cache table + report_type on weekly_report_comments

Revision ID: 057
Revises: 056
Create Date: 2026-08-07

The Bi-Weekly Branch Manager Report reuses `weekly_report_comments` for its
manager's-notes threads, keyed by the period's start Monday. Without a
discriminator those rows would surface inside the Weekly Report's comment
panel for the same week, so `report_type` separates them. Existing rows are
all weekly by definition.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "057"
down_revision = "056"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "biweekly_report_cache",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("period_key", sa.String(16), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "computed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("source", sa.String(20), server_default="manual", nullable=False),
        sa.UniqueConstraint("period_key", name="ux_brc_period_key"),
    )
    op.create_index(
        "ix_brc_period_start", "biweekly_report_cache", ["period_start"]
    )

    # NOT NULL with a server_default so the backfill is implicit — existing
    # threads all belong to the weekly report.
    op.add_column(
        "weekly_report_comments",
        sa.Column(
            "report_type",
            sa.String(16),
            nullable=False,
            server_default="weekly",
        ),
    )
    op.create_index(
        "ix_wrc_report_type_week",
        "weekly_report_comments",
        ["report_type", "week_start"],
    )


def downgrade():
    op.drop_index("ix_wrc_report_type_week", table_name="weekly_report_comments")
    op.drop_column("weekly_report_comments", "report_type")
    op.drop_index("ix_brc_period_start", table_name="biweekly_report_cache")
    op.drop_table("biweekly_report_cache")
