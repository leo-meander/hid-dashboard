import uuid
from datetime import datetime, timezone
from sqlalchemy import Boolean, Column, Integer, String, DateTime
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from app.database import Base


class Branch(Base):
    __tablename__ = "branches"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(100), nullable=False)
    city = Column(String(100), nullable=False)
    country = Column(String(100), nullable=False)
    currency = Column(String(10), nullable=False)
    total_rooms = Column(Integer, nullable=False)
    total_room_count = Column(Integer, nullable=True)
    total_dorm_count = Column(Integer, nullable=True)
    timezone = Column(String(50), nullable=False)
    cloudbeds_property_id = Column(String(100), nullable=True)
    ads_platform_slug = Column(String(32), nullable=True)   # maps to Ads Platform branch key
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))

    # Relationships
    reservations = relationship("Reservation", back_populates="branch")
    kpi_targets = relationship("KPITarget", back_populates="branch")
    daily_metrics = relationship("DailyMetrics", back_populates="branch")
    ads_performance = relationship("AdsPerformance", back_populates="branch")
    kol_records = relationship("KOLRecord", back_populates="branch")
    marketing_activities = relationship("MarketingActivity", back_populates="branch")
    keypoints = relationship("BranchKeypoint", back_populates="branch")
    alerts = relationship("AlertHistory", back_populates="branch")
