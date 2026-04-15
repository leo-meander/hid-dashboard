"""Add other_revenue_native column to kpi_targets for manual revenue add-on.

Revision ID: 026
Revises: 025
"""

from alembic import op
import sqlalchemy as sa


revision = "026"
down_revision = "025"
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    result = conn.execute(sa.text(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name='kpi_targets' AND column_name='other_revenue_native'"
    ))
    if not result.fetchone():
        op.add_column(
            "kpi_targets",
            sa.Column("other_revenue_native", sa.Numeric(15, 2), nullable=True, server_default="0"),
        )


def downgrade():
    op.drop_column("kpi_targets", "other_revenue_native")
