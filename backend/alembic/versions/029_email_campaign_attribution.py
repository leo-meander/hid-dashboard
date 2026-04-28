"""Add CRM revenue attribution fields to email_campaign_stats.

Revision ID: 029
Revises: 028
"""
from alembic import op
import sqlalchemy as sa

revision = "029"
down_revision = "028"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("email_campaign_stats", sa.Column("attributed_bookings", sa.Integer(), server_default="0", nullable=False))
    op.add_column("email_campaign_stats", sa.Column("attributed_canceled", sa.Integer(), server_default="0", nullable=False))
    op.add_column("email_campaign_stats", sa.Column("attributed_nights", sa.Integer(), server_default="0", nullable=False))
    op.add_column("email_campaign_stats", sa.Column("attributed_revenue_native", sa.Numeric(12, 2), server_default="0", nullable=False))
    op.add_column("email_campaign_stats", sa.Column("attributed_revenue_vnd", sa.Numeric(15, 2), server_default="0", nullable=False))
    op.add_column("email_campaign_stats", sa.Column("attributed_currency", sa.String(8), nullable=True))
    op.add_column("email_campaign_stats", sa.Column("attributed_rate_plan", sa.String(200), nullable=True))


def downgrade():
    for col in (
        "attributed_rate_plan",
        "attributed_currency",
        "attributed_revenue_vnd",
        "attributed_revenue_native",
        "attributed_nights",
        "attributed_canceled",
        "attributed_bookings",
    ):
        op.drop_column("email_campaign_stats", col)
