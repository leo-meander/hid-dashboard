"""Add marketing_budgets — per branch / month / channel allocation.

Revision ID: 030
Revises: 029

Stores Paid Ads / KOL / CRM monthly budget allocations in VND. Native amounts
are computed at read time from current FX rates. Distinct from ``ads_budgets``
which mirrors plans pulled from the upstream Ads Platform API.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


revision = "030"
down_revision = "029"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "marketing_budgets",
        sa.Column("id", UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("branch_id", UUID(as_uuid=True),
                  sa.ForeignKey("branches.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("month", sa.Integer(), nullable=False),     # 1..12
        sa.Column("channel", sa.String(32), nullable=False),  # paid_ads | kol | crm
        sa.Column("allocated_vnd", sa.Numeric(15, 2),
                  server_default="0", nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("NOW()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("NOW()"), nullable=False),
        sa.UniqueConstraint(
            "branch_id", "year", "month", "channel",
            name="ux_marketing_budgets_branch_year_month_channel",
        ),
        sa.CheckConstraint("month >= 1 AND month <= 12",
                           name="ck_marketing_budgets_month_range"),
    )
    op.create_index(
        "ix_marketing_budgets_branch_year",
        "marketing_budgets",
        ["branch_id", "year"],
    )


def downgrade():
    op.drop_index("ix_marketing_budgets_branch_year",
                  table_name="marketing_budgets")
    op.drop_table("marketing_budgets")
