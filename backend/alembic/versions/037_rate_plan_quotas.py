"""Rate plan quota tracking — count bookings per rate plan against a manual cap.

Revision ID: 037
Revises: 036

Two tables:

1. rate_plan_quotas — config row per tracked rate plan (name pattern, cap,
   alert threshold %, branch scope). Manually edited from the dashboard.

2. rate_plan_quota_status — latest live count snapshot per quota (active +
   canceled bookings, % consumed, evaluated_at). Refreshed by the cron every
   30 min and consumed by the dashboard widget. Also tracks the highest
   threshold bucket already alerted on so we don't spam (90/95/100).
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


revision = "037"
down_revision = "036"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "rate_plan_quotas",
        sa.Column("id", UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("rate_plan_name", sa.String(200), nullable=False, unique=True),
        sa.Column("display_name", sa.String(200), nullable=True),
        sa.Column("limit_count", sa.Integer, nullable=False),
        sa.Column("alert_threshold_pct", sa.Numeric(5, 2),
                  nullable=False, server_default="90"),
        # 'all' = every active branch except Oani; 'specific' = filter by branch_ids list.
        sa.Column("branch_scope", sa.String(20),
                  nullable=False, server_default="all_excl_oani"),
        sa.Column("branch_ids", sa.JSON, nullable=True),
        sa.Column("notify_email", sa.Boolean, nullable=False,
                  server_default=sa.text("true")),
        sa.Column("is_active", sa.Boolean, nullable=False,
                  server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now()),
    )

    op.create_table(
        "rate_plan_quota_status",
        sa.Column("id", UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("quota_id", UUID(as_uuid=True),
                  sa.ForeignKey("rate_plan_quotas.id", ondelete="CASCADE"),
                  nullable=False, unique=True),
        sa.Column("active_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("canceled_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("consumed_pct", sa.Numeric(6, 2), nullable=False, server_default="0"),
        # JSON list: [{"branch_id":..., "branch_name":..., "active":N, "canceled":N}, ...]
        sa.Column("by_branch", sa.JSON, nullable=True),
        # Last threshold bucket we sent an email for: 0=none, 90, 95, or 100.
        # Allows re-alerting only when count crosses a higher bucket.
        sa.Column("last_alert_bucket", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_alerted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("evaluated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now()),
    )


def downgrade():
    op.drop_table("rate_plan_quota_status")
    op.drop_table("rate_plan_quotas")
