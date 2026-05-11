"""Weekly report JSON cache table.

Revision ID: 040
Revises: 039

Why
----
GET /api/report/preview rebuilds the full multi-branch weekly report on
every hit — ~30-90s because _build_report runs ~65 country GROUP BYs and
re-pulls Cloudbeds Insights. Users hit "Generating weekly report…" every
time they open the page. We cache the JSON output of _build_report and
refresh it weekly (Mon 03:00 ICT via GitHub Actions cron) so the page
loads instantly.

Schema
------
weekly_report_cache (single-row table, id is a fixed sentinel)
  id            VARCHAR PK = 'singleton'
  payload       JSONB    NOT NULL   -- _build_report() return value
  computed_at   TIMESTAMPTZ NOT NULL
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "040"
down_revision = "039"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "weekly_report_cache",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    op.drop_table("weekly_report_cache")
