"""Re-anchor weekly_report_archives + comments to the DATA week's Monday.

Revision ID: 042
Revises: 041

Why
----
Before this migration, `weekly_report_archives.week_start` and
`weekly_report_comments.week_start` were set to the Monday of the week
the cron RAN in (i.e. the calendar week containing the click). But the
report data covers the PRIOR Mon-Sun (`last_week_range(today)`), so a
click on Sun and a click on Mon could land on different keys even
though one of them was reporting on the same data week. That produced
"duplicate" archive rows in the UI selector.

Fix: shift every existing `week_start` back by 7 days so the key matches
the data window the snapshot actually covers. Going forward,
report.py uses `_archive_week_start(today) = last_week_range(today)[0]`
which matches this convention.

This is a pure key shift — no payload/comment content changes, so it's
reversible.
"""
from alembic import op


revision = "042"
down_revision = "041"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("UPDATE weekly_report_archives SET week_start = week_start - INTERVAL '7 days'")
    op.execute("UPDATE weekly_report_comments SET week_start = week_start - INTERVAL '7 days'")


def downgrade():
    op.execute("UPDATE weekly_report_archives SET week_start = week_start + INTERVAL '7 days'")
    op.execute("UPDATE weekly_report_comments SET week_start = week_start + INTERVAL '7 days'")
