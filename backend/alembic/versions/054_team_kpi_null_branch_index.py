"""Fix team_kpi_targets unique constraint for NULL branch_id

PostgreSQL treats NULL != NULL so the standard UniqueConstraint does not
catch conflicts on org-wide rows (branch_id IS NULL).  Replace it with:
  - a partial unique index for the NULL case
  - a partial unique index for the non-NULL case

Revision ID: 054
Revises: 053
Create Date: 2026-07-17
"""
from alembic import op

revision = "054"
down_revision = "053"
branch_labels = None
depends_on = None


def upgrade():
    # Drop the broken constraint (may already be gone if applied manually)
    op.execute("ALTER TABLE team_kpi_targets DROP CONSTRAINT IF EXISTS uq_team_kpi_targets")

    # Partial index: org-wide rows (branch_id IS NULL)
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_team_kpi_org_wide
        ON team_kpi_targets (role_key, year, month, kpi_key)
        WHERE branch_id IS NULL
    """)

    # Partial index: branch-specific rows (branch_id IS NOT NULL)
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_team_kpi_branch
        ON team_kpi_targets (role_key, branch_id, year, month, kpi_key)
        WHERE branch_id IS NOT NULL
    """)

    # Remove duplicate org-wide rows created before this fix
    op.execute("""
        DELETE FROM team_kpi_targets a
        USING team_kpi_targets b
        WHERE a.branch_id IS NULL
          AND b.branch_id IS NULL
          AND a.role_key  = b.role_key
          AND a.year      = b.year
          AND a.month     = b.month
          AND a.kpi_key   = b.kpi_key
          AND a.created_at > b.created_at
    """)


def downgrade():
    op.execute("DROP INDEX IF EXISTS uq_team_kpi_org_wide")
    op.execute("DROP INDEX IF EXISTS uq_team_kpi_branch")
    op.create_unique_constraint(
        "uq_team_kpi_targets",
        "team_kpi_targets",
        ["role_key", "branch_id", "year", "month", "kpi_key"],
    )
