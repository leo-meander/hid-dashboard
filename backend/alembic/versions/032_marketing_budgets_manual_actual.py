"""Add manual_actual_vnd to marketing_budgets.

Revision ID: 032
Revises: 031

For channels without an upstream actuals feed (currently CRM, but the
column also serves as a manual override for any channel — useful when
ops needs to correct an upstream miss).
"""
from alembic import op
import sqlalchemy as sa


revision = "032"
down_revision = "031"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "marketing_budgets",
        sa.Column("manual_actual_vnd", sa.Numeric(15, 2), nullable=True),
    )


def downgrade():
    op.drop_column("marketing_budgets", "manual_actual_vnd")
