import uuid
from datetime import datetime, timezone
from sqlalchemy import Column, String, Date, Numeric, DateTime, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from app.database import Base


class AdsBookingMatch(Base):
    __tablename__ = "ads_booking_matches"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    external_match_id = Column(String(64), unique=True, nullable=False)
    branch_id = Column(UUID(as_uuid=True),
                       ForeignKey("branches.id", ondelete="CASCADE"),
                       nullable=False)
    channel = Column(String(32), nullable=True)
    match_result = Column(String(32), nullable=True)         # matched, unmatched, partial
    purchase_kind = Column(String(32), nullable=True)
    booking_date = Column(Date, nullable=True)
    match_date = Column(Date, nullable=True)
    revenue_native = Column(Numeric(12, 2), nullable=True)
    revenue_vnd = Column(Numeric(15, 2), nullable=True)
    currency = Column(String(10), nullable=True)
    reservation_ref = Column(String(100), nullable=True)
    external_ad_id = Column(String(64), nullable=True)
    external_campaign_id = Column(String(64), nullable=True)
    synced_at = Column(DateTime(timezone=True),
                       default=lambda: datetime.now(timezone.utc))

    branch = relationship("Branch")
