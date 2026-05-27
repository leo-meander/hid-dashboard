"""Per-user branch + page-group access control.

Revision ID: 047
Revises: 046

Adds two nullable TEXT[] columns to ``users``:

  - ``allowed_branches`` — branch UUIDs (as text) the user may see.
  - ``allowed_pages``    — sidebar group keys the user may open
                           (overview / performance / strategy / marketing / reports).

NULL or empty array means "all" — so existing users keep full access and
nothing breaks on deploy. Enforcement is UI-level (sidebar, branch tabs,
route guard); these columns are the source of truth the frontend reads via
``/api/auth/me``.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "047"
down_revision = "046"
branch_labels = None
depends_on = None


def _has_column(conn, table: str, column: str) -> bool:
    row = conn.execute(sa.text(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name=:t AND column_name=:c"
    ), {"t": table, "c": column}).fetchone()
    return row is not None


def upgrade():
    conn = op.get_bind()
    if not _has_column(conn, "users", "allowed_branches"):
        op.add_column("users", sa.Column("allowed_branches", postgresql.ARRAY(sa.Text()), nullable=True))
    if not _has_column(conn, "users", "allowed_pages"):
        op.add_column("users", sa.Column("allowed_pages", postgresql.ARRAY(sa.Text()), nullable=True))


def downgrade():
    conn = op.get_bind()
    if _has_column(conn, "users", "allowed_pages"):
        op.drop_column("users", "allowed_pages")
    if _has_column(conn, "users", "allowed_branches"):
        op.drop_column("users", "allowed_branches")
