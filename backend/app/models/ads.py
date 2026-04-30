import uuid
from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, Text, Date, Numeric, DateTime, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from app.database import Base


class AdsPerformance(Base):
    __tablename__ = "ads_performance"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    branch_id = Column(UUID(as_uuid=True), ForeignKey("branches.id", ondelete="CASCADE"), nullable=False)
    # Legacy upstream identifiers (preserved for backward-compat joins by name/ID).
    meta_ad_id = Column(String(50), nullable=True)
    meta_campaign_id = Column(String(50), nullable=True)
    # Ads Platform upstream identifiers — preferred from 028 onwards.
    external_ad_id = Column(String(64), nullable=True)
    external_campaign_id = Column(String(64), nullable=True)
    account_id = Column(String(64), nullable=True)           # Ads Platform account UUID
    data_source = Column(String(32), nullable=True)          # "AdsPlatform"
    grain = Column(String(16), nullable=True)                # "daily" | "ad"
    campaign_name = Column(String(500), nullable=True)
    adset_name = Column(String(500), nullable=True)
    ad_name = Column(String(500), nullable=True)
    channel = Column(String(50), nullable=True)              # Meta, Google, TikTok
    target_country = Column(String(500), nullable=True)
    target_audience = Column(String(500), nullable=True)     # Solo, Couple, Friend, Family, Business, High Intent
    funnel_stage = Column(String(20), nullable=True)         # TOF, MOF, BOF
    pic = Column(String(50), nullable=True)                  # PIC name from campaign
    ad_body = Column(Text, nullable=True)                    # Primary text from creative
    ad_angle_id = Column(UUID(as_uuid=True), ForeignKey("ad_angles.id", ondelete="SET NULL"), nullable=True)
    campaign_category = Column(String(500), nullable=True)
    date_from = Column(Date, nullable=True)
    date_to = Column(Date, nullable=True)
    cost_native = Column(Numeric(12, 2), nullable=True)
    cost_vnd = Column(Numeric(15, 2), nullable=True)
    impressions = Column(Integer, nullable=True)
    clicks = Column(Integer, nullable=True)
    leads = Column(Integer, nullable=True)
    bookings = Column(Integer, nullable=True)
    lp_views = Column(Integer, nullable=True)
    add_to_cart = Column(Integer, nullable=True)
    initiate_checkout = Column(Integer, nullable=True)
    revenue_native = Column(Numeric(12, 2), nullable=True)
    revenue_vnd = Column(Numeric(15, 2), nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))

    branch = relationship("Branch", back_populates="ads_performance")
    angle = relationship("AdAngle", back_populates="ads_performance")
