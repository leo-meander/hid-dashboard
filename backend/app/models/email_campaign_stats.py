import uuid
from datetime import datetime, timezone
from sqlalchemy import Column, String, Date, Integer, Numeric, DateTime, UniqueConstraint, Index
from sqlalchemy.dialects.postgresql import UUID
from app.database import Base


class EmailCampaignStats(Base):
    __tablename__ = "email_campaign_stats"
    __table_args__ = (
        UniqueConstraint("workflow_id", "stat_date", "campaign_type", "branch_name", name="uq_email_stats_workflow_date"),
        Index("idx_email_stats_workflow_date", "workflow_id", "stat_date"),
        Index("idx_email_stats_date", "stat_date"),
        Index("idx_email_stats_campaign_type", "campaign_type"),
        Index("idx_email_stats_branch", "branch_name"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workflow_id = Column(String(100), nullable=False)
    workflow_name = Column(String(200), nullable=True)
    campaign_type = Column(String(20), nullable=False, default="workflow")  # 'workflow' or 'bulk'
    branch_name = Column(String(50), nullable=False, default="Saigon")
    stat_date = Column(Date, nullable=False)
    total_sent = Column(Integer, default=0)
    total_delivered = Column(Integer, default=0)
    total_opened = Column(Integer, default=0)
    unique_opened = Column(Integer, default=0)
    total_clicked = Column(Integer, default=0)
    unique_clicked = Column(Integer, default=0)
    total_bounced = Column(Integer, default=0)
    total_unsubscribed = Column(Integer, default=0)
    total_complained = Column(Integer, default=0)
    open_rate = Column(Numeric(5, 4), default=0)
    click_rate = Column(Numeric(5, 4), default=0)
    bounce_rate = Column(Numeric(5, 4), default=0)
    unsubscribe_rate = Column(Numeric(5, 4), default=0)

    # CRM revenue attribution (matched on rate_plan_name / room_type containing "CRM_{workflow_name} Events")
    attributed_bookings = Column(Integer, default=0, nullable=False)
    attributed_canceled = Column(Integer, default=0, nullable=False)
    attributed_nights = Column(Integer, default=0, nullable=False)
    attributed_revenue_native = Column(Numeric(12, 2), default=0, nullable=False)
    attributed_revenue_vnd = Column(Numeric(15, 2), default=0, nullable=False)
    attributed_currency = Column(String(8), nullable=True)
    attributed_rate_plan = Column(String(200), nullable=True)

    computed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
