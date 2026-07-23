"""marketing_activity_cache table

Revision ID: 052
Revises: 051
Create Date: 2026-07-15
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "052"
down_revision = "051"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "marketing_activity_cache",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "branch_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("branches.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("month", sa.Integer(), nullable=False),
        sa.Column("channel", sa.String(32), nullable=False),
        sa.Column("bookings", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("revenue_vnd", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "branch_id", "year", "month", "channel",
            name="ux_mac_branch_year_month_channel",
        ),
        sa.CheckConstraint("month >= 1 AND month <= 12", name="ck_mac_month_range"),
    )
    op.create_index(
        "ix_mac_branch_id", "marketing_activity_cache", ["branch_id"]
    )
    op.create_index(
        "ix_mac_year_month", "marketing_activity_cache", ["year", "month"]
    )


def downgrade():
    op.drop_table("marketing_activity_cache")
