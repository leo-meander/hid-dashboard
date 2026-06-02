"""Seed the OTA commission opportunity alert rule.

Forward-looking alert: flags upcoming weeks with low on-the-books occupancy
where raising OTA commission could drive incremental demand while there is
still lead time to fill rooms.

Revision ID: 049
Revises: 048
"""
from alembic import op
import sqlalchemy as sa

revision = "049"
down_revision = "048"
branch_labels = None
depends_on = None


def upgrade():
    alert_rules = sa.table(
        "alert_rules",
        sa.column("metric_key", sa.String),
        sa.column("display_name", sa.String),
        sa.column("category", sa.String),
        sa.column("severity", sa.String),
        sa.column("threshold_type", sa.String),
        sa.column("threshold_value", sa.Numeric),
        sa.column("comparison_op", sa.String),
        sa.column("lookback_days", sa.Integer),
    )
    op.bulk_insert(alert_rules, [
        dict(metric_key="ota_commission_opportunity",
             display_name="OTA Commission Opportunity (Soft Week)",
             category="channel", severity="WARNING", threshold_type="forecast",
             threshold_value=0.40, comparison_op="lt", lookback_days=0),
    ])


def downgrade():
    op.execute(
        "DELETE FROM alert_rules WHERE metric_key = 'ota_commission_opportunity'"
    )
