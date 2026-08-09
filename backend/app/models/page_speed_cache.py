import uuid
from datetime import datetime, timezone
from sqlalchemy import (
    CheckConstraint, Column, DateTime, ForeignKey, Integer, Numeric,
    String, UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from app.database import Base


class PageSpeedCache(Base):
    """Monthly-refreshed cache of Google PageSpeed Insights Speed Index per branch.

    PSI is a live synthetic test with no history API, so unlike the GA4
    purchase_cvr KPI (re-queried live for any past month) a Speed Index
    reading only exists for the month it was actually fetched in. This table
    is that persisted snapshot — one row per (branch, year, month), written
    by the monthly page-speed sync job.

    Grain: one row per (branch, year, month).
    """
    __tablename__ = "page_speed_cache"
    __table_args__ = (
        UniqueConstraint(
            "branch_id", "year", "month",
            name="ux_psc_branch_year_month",
        ),
        CheckConstraint("month >= 1 AND month <= 12", name="ck_psc_month_range"),
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
    speed_index_seconds = Column(Numeric(6, 2), nullable=True)
    strategy = Column(String(16), nullable=False, default="mobile")
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
