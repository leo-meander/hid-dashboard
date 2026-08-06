"""Delete pre-August-2026 Design Ideas targets/actuals

Revision ID: 056
Revises: 055
Create Date: 2026-08-06

Design Ideas only became a tracked KPI from 2026-08. The Jan–Jul rows were
entered while the KPI list was still being shaped and are meaningless; the
KPI now carries "starts": "2026-08" in KPI_DEFS so those months render blank
and locked. Clearing the stored rows keeps the DB agreeing with the grid —
otherwise a later change to the start month would resurrect stale numbers.

Only the designer role's design_ideas rows are touched. Hidden KPIs
(design_assets, videos_delivered) keep their history on purpose.
"""
from alembic import op


revision = "056"
down_revision = "055"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        """
        DELETE FROM team_kpi_targets
        WHERE role_key = 'designer'
          AND year = 2026
          AND month < 8
          AND kpi_key IN ('design_ideas', 'design_ideas__actual')
        """
    )


def downgrade():
    # Irreversible: the deleted values were hand-entered and are not
    # reconstructible from any other source.
    pass
