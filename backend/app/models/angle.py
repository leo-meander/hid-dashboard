import uuid
from datetime import datetime, timezone
from sqlalchemy import Column, String, Text, DateTime, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from app.database import Base


class AdAngle(Base):
    __tablename__ = "ad_angles"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    angle_code = Column(String(20), nullable=True)          # ANG-001 format
    external_angle_id = Column(String(64), nullable=True)   # Ads Platform sync key
    name = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    hook_type = Column(String(50), nullable=True)           # Question / Pain Point / Aspiration...
    keypoint_1 = Column(Text, nullable=True)
    keypoint_2 = Column(Text, nullable=True)
    keypoint_3 = Column(Text, nullable=True)
    keypoint_4 = Column(Text, nullable=True)
    keypoint_5 = Column(Text, nullable=True)
    status = Column(String(20), nullable=True)              # WIN, TEST, LOSE
    verdict = Column(String(50), nullable=True)             # Winner-Scale Up / Good-Test More / Neutral / Underperformer / Kill
    branch_id = Column(UUID(as_uuid=True), ForeignKey("branches.id", ondelete="SET NULL"), nullable=True)
    created_by = Column(String(100), nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))

    ads_performance = relationship("AdsPerformance", back_populates="angle")
    kol_records = relationship("KOLRecord", back_populates="angle")
    ad_copies = relationship("AdCopy", back_populates="angle")
    ad_materials = relationship("AdMaterial", back_populates="angle")
