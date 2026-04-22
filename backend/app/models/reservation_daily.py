"""
ReservationDaily — v2.0
Stores per-night breakdown for each reservation.
One row per reservation × per night of stay.
nightly_rate = grand_total_native / nights (simple proration).
Used by metrics_engine for accurate daily Revenue, ADR, RevPAR.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

from sqlalchemy import (
    Column, Date, DateTime, ForeignKey, Index, Numeric, String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.database import Base


class ReservationDaily(Base):
    __tablename__ = "reservation_daily"
    __table_args__ = (
        UniqueConstraint("reservation_id", "date", name="uq_reservation_daily_res_date"),
        Index("idx_reservation_daily_branch_date", "branch_id", "date"),
        Index("idx_reservation_daily_reservation", "reservation_id"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    reservation_id = Column(
        UUID(as_uuid=True),
        ForeignKey("reservations.id", ondelete="CASCADE"),
        nullable=False,
    )
    branch_id = Column(
        UUID(as_uuid=True),
        ForeignKey("branches.id", ondelete="CASCADE"),
        nullable=False,
    )
    date = Column(Date, nullable=False)
    room_id = Column(String(50), nullable=True)
    nightly_rate = Column(Numeric(12, 2), nullable=True)
    nightly_rate_vnd = Column(Numeric(15, 2), nullable=True)
    status = Column(String(50), nullable=True)
    source = Column(String(100), nullable=True)
    source_category = Column(String(32), nullable=True)
    room_type_category = Column(String(10), nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    reservation = relationship("Reservation", backref="daily_rows")
