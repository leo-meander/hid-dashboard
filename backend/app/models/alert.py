import uuid
from datetime import datetime, timezone
from sqlalchemy import (
    Boolean, Column, Date, DateTime, ForeignKey, Index, Integer, Numeric,
    String, Text, UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from app.database import Base


class AlertRule(Base):
    """Configurable alert thresholds — one rule per metric (optionally per branch)."""
    __tablename__ = "alert_rules"
    __table_args__ = (
        UniqueConstraint("metric_key", "branch_id", name="uq_alert_rule_metric_branch"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    metric_key = Column(String(100), nullable=False)
    display_name = Column(String(200), nullable=False)
    category = Column(String(50), nullable=False)          # revenue, occupancy, bookings, channel, market
    severity = Column(String(20), nullable=False)           # CRITICAL, WARNING, INFO
    threshold_type = Column(String(20), nullable=False)     # static, rolling_avg, yoy, forecast
    threshold_value = Column(Numeric(10, 4), nullable=False)
    comparison_op = Column(String(10), nullable=False)      # lt, gt, lte, gte, pct_drop
    lookback_days = Column(Integer, default=0)
    branch_id = Column(UUID(as_uuid=True), ForeignKey("branches.id", ondelete="CASCADE"), nullable=True)
    is_active = Column(Boolean, default=True)
    notify_email = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))

    branch = relationship("Branch", foreign_keys=[branch_id])


class AlertHistory(Base):
    """One alert instance per branch/metric/day — idempotent via unique constraint."""
    __tablename__ = "alert_history"
    __table_args__ = (
        UniqueConstraint("branch_id", "metric_key", "alert_date",
                         name="uq_alert_history_branch_metric_date"),
        Index("idx_alert_history_branch_date", "branch_id", "alert_date"),
        Index("idx_alert_history_status_date", "status", "alert_date"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    branch_id = Column(UUID(as_uuid=True), ForeignKey("branches.id", ondelete="CASCADE"), nullable=False)
    metric_key = Column(String(100), nullable=False)
    alert_date = Column(Date, nullable=False)
    severity = Column(String(20), nullable=False)
    category = Column(String(50), nullable=False)
    current_value = Column(Numeric(15, 4), nullable=True)
    threshold_value = Column(Numeric(15, 4), nullable=True)
    baseline_value = Column(Numeric(15, 4), nullable=True)
    deviation_pct = Column(Numeric(8, 4), nullable=True)
    message = Column(Text, nullable=False)
    recommendation = Column(Text, nullable=False)
    status = Column(String(20), default="active")          # active, acknowledged, resolved
    acknowledged_by = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    acknowledged_at = Column(DateTime(timezone=True), nullable=True)
    email_sent = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    branch = relationship("Branch", back_populates="alerts")
    acknowledged_user = relationship("User", foreign_keys=[acknowledged_by])


class AlertNotificationLog(Base):
    """Audit trail for alert emails sent."""
    __tablename__ = "alert_notification_log"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    alert_id = Column(UUID(as_uuid=True), ForeignKey("alert_history.id", ondelete="CASCADE"), nullable=False)
    recipient = Column(String(200), nullable=False)
    sent_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    delivery_status = Column(String(20), default="sent")   # sent, failed

    alert = relationship("AlertHistory")
