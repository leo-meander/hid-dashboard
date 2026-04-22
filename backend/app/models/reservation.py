import uuid
from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, Date, Numeric, DateTime, ForeignKey, Index
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import relationship
from app.database import Base


class Reservation(Base):
    __tablename__ = "reservations"
    __table_args__ = (
        Index("idx_reservations_branch_checkin", "branch_id", "check_in_date"),
        Index("idx_reservations_status", "status"),
        Index("idx_reservations_source_category", "source_category"),
        Index("idx_reservations_country_code", "guest_country_code"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    branch_id = Column(UUID(as_uuid=True), ForeignKey("branches.id", ondelete="CASCADE"), nullable=False)
    cloudbeds_reservation_id = Column(String(50), unique=True, nullable=False)
    guest_country = Column(String(100), nullable=True)
    guest_country_code = Column(String(50), nullable=True)
    room_type = Column(String(100), nullable=True)
    room_type_category = Column(String(10), nullable=True)   # "Room" or "Dorm"
    rate_plan_name = Column(String(200), nullable=True)
    room_number = Column(String(50), nullable=True)
    source = Column(String(100), nullable=True)
    source_category = Column(String(32), nullable=True)      # "OTA", "Direct", or "Local travel agency"
    check_in_date = Column(Date, nullable=False)
    check_out_date = Column(Date, nullable=False)
    nights = Column(Integer, nullable=False)
    adults = Column(Integer, nullable=True)
    grand_total_native = Column(Numeric(12, 2), nullable=True)
    grand_total_vnd = Column(Numeric(15, 2), nullable=True)
    status = Column(String(50), nullable=True)
    cancellation_date = Column(Date, nullable=True)
    reservation_date = Column(Date, nullable=True)
    raw_data = Column(JSONB, nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))

    branch = relationship("Branch", back_populates="reservations")
    kol_bookings = relationship("KOLBooking", back_populates="reservation")

