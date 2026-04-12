"""Alert system — alert_rules, alert_history, alert_notification_log tables.

Revision ID: 025
Revises: 024
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "025"
down_revision = "024"
branch_labels = None
depends_on = None


def upgrade():
    # ── alert_rules ──────────────────────────────────────────────────────────
    op.create_table(
        "alert_rules",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("metric_key", sa.String(100), nullable=False),
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.Column("category", sa.String(50), nullable=False),
        sa.Column("severity", sa.String(20), nullable=False),
        sa.Column("threshold_type", sa.String(20), nullable=False),
        sa.Column("threshold_value", sa.Numeric(10, 4), nullable=False),
        sa.Column("comparison_op", sa.String(10), nullable=False),
        sa.Column("lookback_days", sa.Integer(), server_default=sa.text("0")),
        sa.Column("branch_id", UUID(as_uuid=True), sa.ForeignKey("branches.id", ondelete="CASCADE"), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true")),
        sa.Column("notify_email", sa.Boolean(), server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("metric_key", "branch_id", name="uq_alert_rule_metric_branch"),
    )

    # ── alert_history ────────────────────────────────────────────────────────
    op.create_table(
        "alert_history",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("branch_id", UUID(as_uuid=True), sa.ForeignKey("branches.id", ondelete="CASCADE"), nullable=False),
        sa.Column("metric_key", sa.String(100), nullable=False),
        sa.Column("alert_date", sa.Date(), nullable=False),
        sa.Column("severity", sa.String(20), nullable=False),
        sa.Column("category", sa.String(50), nullable=False),
        sa.Column("current_value", sa.Numeric(15, 4), nullable=True),
        sa.Column("threshold_value", sa.Numeric(15, 4), nullable=True),
        sa.Column("baseline_value", sa.Numeric(15, 4), nullable=True),
        sa.Column("deviation_pct", sa.Numeric(8, 4), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("recommendation", sa.Text(), nullable=False),
        sa.Column("status", sa.String(20), server_default=sa.text("'active'")),
        sa.Column("acknowledged_by", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("email_sent", sa.Boolean(), server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("branch_id", "metric_key", "alert_date", name="uq_alert_history_branch_metric_date"),
    )
    op.create_index("idx_alert_history_branch_date", "alert_history", ["branch_id", "alert_date"])
    op.create_index("idx_alert_history_status_date", "alert_history", ["status", "alert_date"])

    # ── alert_notification_log ───────────────────────────────────────────────
    op.create_table(
        "alert_notification_log",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("alert_id", UUID(as_uuid=True), sa.ForeignKey("alert_history.id", ondelete="CASCADE"), nullable=False),
        sa.Column("recipient", sa.String(200), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("delivery_status", sa.String(20), server_default=sa.text("'sent'")),
    )

    # ── Seed default alert rules (13 rules) ──────────────────────────────────
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
        # Revenue & Pricing
        dict(metric_key="revenue_pace", display_name="Revenue Pace vs Monthly Target",
             category="revenue", severity="CRITICAL", threshold_type="forecast",
             threshold_value=0.85, comparison_op="lt", lookback_days=0),
        dict(metric_key="adr_drop_7d", display_name="ADR Drop vs 7-Day Average",
             category="revenue", severity="WARNING", threshold_type="rolling_avg",
             threshold_value=0.10, comparison_op="pct_drop", lookback_days=7),
        dict(metric_key="revpar_below_forecast", display_name="RevPAR Below Forecast",
             category="revenue", severity="WARNING", threshold_type="forecast",
             threshold_value=0.90, comparison_op="lt", lookback_days=0),
        dict(metric_key="revenue_yoy_decline", display_name="Revenue Decline YoY",
             category="revenue", severity="WARNING", threshold_type="yoy",
             threshold_value=0.15, comparison_op="pct_drop", lookback_days=0),
        # Occupancy
        dict(metric_key="occ_below_target", display_name="OCC Below Predicted Target",
             category="occupancy", severity="CRITICAL", threshold_type="forecast",
             threshold_value=0.90, comparison_op="lt", lookback_days=0),
        dict(metric_key="occ_sudden_drop", display_name="Sudden OCC Drop (Day-over-Day)",
             category="occupancy", severity="WARNING", threshold_type="static",
             threshold_value=0.10, comparison_op="pct_drop", lookback_days=1),
        # Bookings & Cancellations
        dict(metric_key="cancellation_spike", display_name="Cancellation Rate Spike",
             category="bookings", severity="CRITICAL", threshold_type="rolling_avg",
             threshold_value=1.50, comparison_op="gt", lookback_days=30),
        dict(metric_key="booking_pace_decline", display_name="New Bookings Pace Decline",
             category="bookings", severity="WARNING", threshold_type="rolling_avg",
             threshold_value=0.75, comparison_op="lt", lookback_days=7),
        dict(metric_key="net_booking_decline", display_name="Net Booking Decline",
             category="bookings", severity="WARNING", threshold_type="rolling_avg",
             threshold_value=0.00, comparison_op="lt", lookback_days=7),
        # Channel
        dict(metric_key="ota_dependency", display_name="OTA Dependency Alert",
             category="channel", severity="WARNING", threshold_type="static",
             threshold_value=0.80, comparison_op="gt", lookback_days=30),
        # Guest Markets
        dict(metric_key="country_booking_drop", display_name="Top Country Booking Drop (YoY)",
             category="market", severity="WARNING", threshold_type="yoy",
             threshold_value=0.25, comparison_op="pct_drop", lookback_days=0),
        dict(metric_key="country_surge", display_name="Emerging Country Surge",
             category="market", severity="INFO", threshold_type="yoy",
             threshold_value=0.50, comparison_op="gt", lookback_days=0),
        dict(metric_key="country_concentration", display_name="Country Concentration Risk",
             category="market", severity="WARNING", threshold_type="static",
             threshold_value=0.40, comparison_op="gt", lookback_days=30),
    ])


def downgrade():
    op.drop_table("alert_notification_log")
    op.drop_index("idx_alert_history_status_date")
    op.drop_index("idx_alert_history_branch_date")
    op.drop_table("alert_history")
    op.drop_table("alert_rules")
