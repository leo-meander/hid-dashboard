"""Add yearly_plans — yearly total + per-month % per branch.

Revision ID: 033
Revises: 032

Yearly Plan is the entry point for the Budget Planner: ops set one
total_vnd per branch+year, then a 12-element percentage list. The
monthly totals (= total_vnd * pct[m] / 100) cascade into Channel Splits
which still write to ``marketing_budgets`` per channel.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB


revision = "033"
down_revision = "032"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "yearly_plans",
        sa.Column("id", UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("branch_id", UUID(as_uuid=True),
                  sa.ForeignKey("branches.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("total_vnd", sa.Numeric(15, 2),
                  server_default="0", nullable=False),
        # 12-element JSON: {"1": 8.33, "2": 8.33, ..., "12": 8.37}
        sa.Column("monthly_pcts", JSONB,
                  server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("NOW()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("NOW()"), nullable=False),
        sa.UniqueConstraint("branch_id", "year",
                            name="ux_yearly_plans_branch_year"),
    )


def downgrade():
    op.drop_table("yearly_plans")
