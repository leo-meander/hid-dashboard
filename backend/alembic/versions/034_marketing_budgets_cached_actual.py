"""Add cached_actual_vnd + actuals_synced_at to marketing_budgets.

Revision ID: 034
Revises: 033

Daily sync writes the upstream paid_ads / kol actuals here so Budget
Planner doesn't have to call the upstream APIs on every page load.
manual_actual_vnd still wins over the cached value (user override).
"""
from alembic import op
import sqlalchemy as sa


revision = "034"
down_revision = "033"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "marketing_budgets",
        sa.Column("cached_actual_vnd", sa.Numeric(15, 2), nullable=True),
    )
    op.add_column(
        "marketing_budgets",
        sa.Column("actuals_synced_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade():
    op.drop_column("marketing_budgets", "actuals_synced_at")
    op.drop_column("marketing_budgets", "cached_actual_vnd")
