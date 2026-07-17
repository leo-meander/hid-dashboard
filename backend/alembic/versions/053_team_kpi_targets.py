"""team_kpi_targets table

Revision ID: 053
Revises: 052
Create Date: 2026-07-17
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "053"
down_revision = "052"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "team_kpi_targets",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("role_key", sa.String(20), nullable=False),
        sa.Column("branch_id", UUID(as_uuid=True), sa.ForeignKey("branches.id", ondelete="CASCADE"), nullable=True),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("month", sa.Integer(), nullable=False),
        sa.Column("kpi_key", sa.String(50), nullable=False),
        sa.Column("target_value", sa.Numeric(18, 4), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), onupdate=sa.text("NOW()")),
        sa.UniqueConstraint("role_key", "branch_id", "year", "month", "kpi_key", name="uq_team_kpi_targets"),
    )
    op.create_index("ix_team_kpi_role_branch_year", "team_kpi_targets", ["role_key", "branch_id", "year"])


def downgrade():
    op.drop_index("ix_team_kpi_role_branch_year", table_name="team_kpi_targets")
    op.drop_table("team_kpi_targets")
