import uuid
from datetime import datetime, timezone
from sqlalchemy import Column, String, Numeric, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from app.database import Base


class AdsBudget(Base):
    __tablename__ = "ads_budgets"
    __table_args__ = (
        UniqueConstraint(
            "branch_id", "month", "channel", "external_plan_id",
            name="ux_ads_budgets_branch_month_channel_plan",
        ),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    branch_id = Column(UUID(as_uuid=True),
                       ForeignKey("branches.id", ondelete="CASCADE"),
                       nullable=False)
    external_plan_id = Column(String(64), nullable=False)
    month = Column(String(7), nullable=False)                # "YYYY-MM"
    plan_name = Column(String(200), nullable=True)
    channel = Column(String(32), nullable=True)              # Meta, Google, TikTok
    total_budget_native = Column(Numeric(12, 2), nullable=True)
    total_budget_vnd = Column(Numeric(15, 2), nullable=True)
    currency = Column(String(10), nullable=True)
    created_at = Column(DateTime(timezone=True),
                        default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True),
                        default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))

    branch = relationship("Branch")
