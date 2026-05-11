"""Weekly Report — per-metric discussions + weekly archive snapshots.

Revision ID: 041
Revises: 040

Why
----
Two collaboration features layered on top of the Weekly Report page:

1. Per-metric discussions — every cell in the Executive Summary table and
   per-branch KPI table is now clickable. Any logged-in user (admin /
   editor / viewer) can leave comments, mark them as action items, or
   resolve them. Threads are scoped by (week_start, branch_id, metric_key)
   so the discussion stays attached to the data point it was about, even
   when the user later jumps to a past week.

   `branch_id` is nullable — the Executive Summary table mixes per-branch
   rows but lives in the "all branches" view, so we still want comments
   scoped per (week, branch, metric) row. Per-row comments use branch_id
   = that row's branch. metric_key examples: 'revenue_mtd', 'target',
   'pacing', 'forecast', 'occ', 'adr', 'revpar', 'wow_revenue',
   'yoy_revenue', plus per-branch detail metrics.

2. Weekly archives — `weekly_report_cache` is a single-row singleton that
   gets overwritten every Monday 03:00 ICT by the GitHub Actions cron.
   To keep history we snapshot the payload into `weekly_report_archives`
   keyed by `week_start` (the Monday of the week the cron ran). The UI
   week selector reads this table.

   payload is the full _build_report(db) JSONB (same shape as the cache),
   so the front-end can render any past week with the same components.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB


revision = "041"
down_revision = "040"
branch_labels = None
depends_on = None


def upgrade():
    # ── weekly_report_comments ───────────────────────────────────────────────
    op.create_table(
        "weekly_report_comments",
        sa.Column("id", UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("week_start", sa.Date(), nullable=False),
        sa.Column("branch_id", UUID(as_uuid=True),
                  sa.ForeignKey("branches.id", ondelete="CASCADE"), nullable=True),
        sa.Column("metric_key", sa.String(64), nullable=False),
        sa.Column("parent_comment_id", UUID(as_uuid=True),
                  sa.ForeignKey("weekly_report_comments.id", ondelete="CASCADE"),
                  nullable=True),
        sa.Column("author_id", UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("is_action_item", sa.Boolean(),
                  server_default=sa.text("false"), nullable=False),
        sa.Column("is_resolved", sa.Boolean(),
                  server_default=sa.text("false"), nullable=False),
        sa.Column("resolved_by", UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_deleted", sa.Boolean(),
                  server_default=sa.text("false"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )
    op.create_index(
        "idx_weekly_comments_week_branch_metric",
        "weekly_report_comments",
        ["week_start", "branch_id", "metric_key"],
    )
    op.create_index(
        "idx_weekly_comments_week",
        "weekly_report_comments",
        ["week_start"],
    )
    op.create_index(
        "idx_weekly_comments_parent",
        "weekly_report_comments",
        ["parent_comment_id"],
    )

    # ── weekly_report_archives ───────────────────────────────────────────────
    op.create_table(
        "weekly_report_archives",
        sa.Column("id", UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("week_start", sa.Date(), nullable=False, unique=True),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("archived_by", UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("source", sa.String(20),
                  server_default=sa.text("'cron'"), nullable=False),
    )
    op.create_index(
        "idx_weekly_archives_week",
        "weekly_report_archives",
        ["week_start"],
    )


def downgrade():
    op.drop_index("idx_weekly_archives_week", table_name="weekly_report_archives")
    op.drop_table("weekly_report_archives")
    op.drop_index("idx_weekly_comments_parent", table_name="weekly_report_comments")
    op.drop_index("idx_weekly_comments_week", table_name="weekly_report_comments")
    op.drop_index("idx_weekly_comments_week_branch_metric", table_name="weekly_report_comments")
    op.drop_table("weekly_report_comments")
