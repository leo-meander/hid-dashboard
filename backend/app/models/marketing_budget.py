import uuid
from datetime import datetime, timezone
from sqlalchemy import (
    CheckConstraint, Column, DateTime, ForeignKey, Integer, Numeric,
    String, Text, UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from app.database import Base


class MarketingBudget(Base):
    """Monthly marketing budget allocation per branch and channel.

    Master amount is stored in VND. Native-currency display is derived at
    read time using the current FX rate. One row per (branch, year, month,
    channel) — channel ∈ {paid_ads, kol, crm}; the set is open-ended for
    future expansion.
    """
    __tablename__ = "marketing_budgets"
    __table_args__ = (
        UniqueConstraint(
            "branch_id", "year", "month", "channel",
            name="ux_marketing_budgets_branch_year_month_channel",
        ),
        CheckConstraint("month >= 1 AND month <= 12",
                        name="ck_marketing_budgets_month_range"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    branch_id = Column(UUID(as_uuid=True),
                       ForeignKey("branches.id", ondelete="CASCADE"),
                       nullable=False)
    year = Column(Integer, nullable=False)
    month = Column(Integer, nullable=False)            # 1..12
    channel = Column(String(32), nullable=False)       # paid_ads | kol | crm
    allocated_vnd = Column(Numeric(15, 2), nullable=False, default=0)
    # Manual override for channels with no upstream actuals feed (e.g. CRM).
    # NULL ⇒ defer to upstream / 0; non-NULL ⇒ wins over the upstream value.
    manual_actual_vnd = Column(Numeric(15, 2), nullable=True)
    # Cached upstream actual written by the nightly sync. Read path:
    # manual_actual_vnd > cached_actual_vnd > live upstream fetch > 0.
    cached_actual_vnd = Column(Numeric(15, 2), nullable=True)
    actuals_synced_at = Column(DateTime(timezone=True), nullable=True)
    note = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True),
                        default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True),
                        default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))

    branch = relationship("Branch")
