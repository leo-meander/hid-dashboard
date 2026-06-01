"""Move deduction_pct + other_revenue_native to branches (fixed per-branch, no monthly reset).

Adds branch-level deduction_pct and other_revenue_native so the Group Summary
Deduct %% / Other Rev columns persist across all months instead of resetting
each month. Backfills from the most recent kpi_targets value per branch.

Revision ID: 048
Revises: 047
"""

from alembic import op
import sqlalchemy as sa


revision = "048"
down_revision = "047"
branch_labels = None
depends_on = None


def _has_column(conn, table, column):
    result = conn.execute(sa.text(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name=:t AND column_name=:c"
    ), {"t": table, "c": column})
    return result.fetchone() is not None


def upgrade():
    conn = op.get_bind()

    if not _has_column(conn, "branches", "deduction_pct"):
        op.add_column(
            "branches",
            sa.Column("deduction_pct", sa.Numeric(5, 2), nullable=True, server_default="0"),
        )

    if not _has_column(conn, "branches", "other_revenue_native"):
        op.add_column(
            "branches",
            sa.Column("other_revenue_native", sa.Numeric(15, 2), nullable=True, server_default="0"),
        )

    # Backfill from the most recent kpi_targets row per branch so existing
    # settings are not lost. DISTINCT ON picks the latest (year, month).
    conn.execute(sa.text("""
        UPDATE branches b
        SET deduction_pct = COALESCE(t.deduction_pct, 0),
            other_revenue_native = COALESCE(t.other_revenue_native, 0)
        FROM (
            SELECT DISTINCT ON (branch_id) branch_id, deduction_pct, other_revenue_native
            FROM kpi_targets
            ORDER BY branch_id, year DESC, month DESC
        ) t
        WHERE b.id = t.branch_id
    """))


def downgrade():
    op.drop_column("branches", "other_revenue_native")
    op.drop_column("branches", "deduction_pct")
