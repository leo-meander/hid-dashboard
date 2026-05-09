"""Rate plan quota — per-branch caps on bookings per CRM/event rate plan.

Created for the "CRM_June 2026 Events" use case but generic: any
rate_plan_name substring can be tracked. Each in-scope branch carries its
own cap and its own alert-bucket history, so one branch hitting 95% emails
just for that branch without re-arming the others. Status row holds the
latest live snapshot, populated every 30 min by the cron in
app.services.rate_plan_quota_engine.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean, Column, DateTime, ForeignKey, Integer, JSON, Numeric, String,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.database import Base


class RatePlanQuota(Base):
    __tablename__ = "rate_plan_quotas"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Match pattern — used as ILIKE %{rate_plan_name}% against
    # reservations.rate_plan_name AND reservations.room_type (Cloudbeds packs
    # the rate plan inside roomTypeName parens for some properties).
    rate_plan_name = Column(String(200), nullable=False, unique=True)
    display_name = Column(String(200), nullable=True)
    # Per-branch caps: {"<branch_id_str>": <int_cap>, …}
    # Keys define the in-scope branches (absence = excluded). Values are the
    # max active bookings allowed for that single branch before alerts fire.
    branch_limits = Column(JSON, nullable=False)
    alert_threshold_pct = Column(Numeric(5, 2), nullable=False, default=90)
    notify_email = Column(Boolean, nullable=False, default=True)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True),
                        default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True),
                        default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))

    status = relationship(
        "RatePlanQuotaStatus", uselist=False, back_populates="quota",
        cascade="all, delete-orphan",
    )


class RatePlanQuotaStatus(Base):
    __tablename__ = "rate_plan_quota_status"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    quota_id = Column(UUID(as_uuid=True),
                      ForeignKey("rate_plan_quotas.id", ondelete="CASCADE"),
                      nullable=False, unique=True)
    # Sums across all in-scope branches — convenient totals for the card
    # headline. Per-branch breakdown lives in by_branch.
    active_count = Column(Integer, nullable=False, default=0)
    canceled_count = Column(Integer, nullable=False, default=0)
    consumed_pct = Column(Numeric(6, 2), nullable=False, default=0)
    # [{branch_id, branch_name, active, canceled, limit, consumed_pct,
    #   last_alert_bucket}, …] — fully self-contained for the dashboard.
    by_branch = Column(JSON, nullable=True)
    # Per-branch bucket history: {"<branch_id>": 0|90|95|100}.
    # Re-alert only when a branch's count crosses INTO a higher bucket so the
    # inbox doesn't get spammed every 30 min while a branch sits inside one
    # bucket. State persists across cron ticks.
    last_alert_buckets = Column(JSON, nullable=False, default=dict)
    last_alerted_at = Column(DateTime(timezone=True), nullable=True)
    evaluated_at = Column(DateTime(timezone=True),
                          default=lambda: datetime.now(timezone.utc),
                          onupdate=lambda: datetime.now(timezone.utc))

    quota = relationship("RatePlanQuota", back_populates="status")
