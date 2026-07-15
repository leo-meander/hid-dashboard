import uuid
from datetime import datetime, timezone
from sqlalchemy import (
    CheckConstraint, Column, DateTime, ForeignKey, Integer, Numeric,
    String, UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from app.database import Base


class MarketingActivityCache(Base):
    """Nightly-refreshed cache of Paid Ads and KOL bookings/revenue per branch/month.

    CRM comes from reservations (already in DB, fast).
    Costs come from marketing_budgets (already cached there).
    This table caches only the two slow external API calls:
      - Paid Ads: Ads Platform /export/spend/daily
      - KOL:      KOL Engine  /api/public/kol-revenue

    Grain: one row per (branch, year, month, channel).
    All revenue stored in VND for cross-branch consistency; converted to
    native currency at read time the same way the rest of the app does it.
    """
    __tablename__ = "marketing_activity_cache"
    __table_args__ = (
        UniqueConstraint(
            "branch_id", "year", "month", "channel",
            name="ux_mac_branch_year_month_channel",
        ),
        CheckConstraint("month >= 1 AND month <= 12", name="ck_mac_month_range"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    branch_id = Column(
        UUID(as_uuid=True),
        ForeignKey("branches.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    year = Column(Integer, nullable=False)
    month = Column(Integer, nullable=False)
    channel = Column(String(32), nullable=False)   # paid_ads | kol
    bookings = Column(Integer, nullable=False, default=0)
    revenue_vnd = Column(Numeric(18, 2), nullable=False, default=0)
    synced_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    branch = relationship("Branch")
