"""page_speed_cache table

Revision ID: 058
Revises: 057
Create Date: 2026-08-09

Avg Website Load Speed KPI (Paid Ads). PageSpeed Insights is a live
synthetic test with no history API, so unlike GA4 purchase_cvr (re-queried
live for any past month) a Speed Index reading only exists for the month it
was fetched in — this table is that persisted monthly snapshot per branch,
written by the monthly PageSpeed sync job (POST /api/sync/page-speed).
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "058"
down_revision = "057"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "page_speed_cache",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "branch_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("branches.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("month", sa.Integer(), nullable=False),
        sa.Column("speed_index_seconds", sa.Numeric(6, 2), nullable=True),
        sa.Column("strategy", sa.String(16), nullable=False, server_default="mobile"),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "branch_id", "year", "month", name="ux_psc_branch_year_month",
        ),
        sa.CheckConstraint("month >= 1 AND month <= 12", name="ck_psc_month_range"),
    )
    op.create_index(
        "ix_psc_branch_id", "page_speed_cache", ["branch_id"]
    )


def downgrade():
    op.drop_index("ix_psc_branch_id", table_name="page_speed_cache")
    op.drop_table("page_speed_cache")
