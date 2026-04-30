import uuid
from datetime import datetime, timezone
from sqlalchemy import (
    Column, DateTime, ForeignKey, Integer, Numeric, UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship
from app.database import Base


class YearlyPlan(Base):
    """Per (branch, year) yearly total + monthly percentage allocation.

    Source-of-truth for the Yearly Plan tab on Budget Planner. Monthly totals
    cascade to ``marketing_budgets`` rows when the user saves the plan, which
    keeps existing per-channel allocation logic working unchanged.
    """
    __tablename__ = "yearly_plans"
    __table_args__ = (
        UniqueConstraint("branch_id", "year", name="ux_yearly_plans_branch_year"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    branch_id = Column(UUID(as_uuid=True),
                       ForeignKey("branches.id", ondelete="CASCADE"),
                       nullable=False)
    year = Column(Integer, nullable=False)
    total_vnd = Column(Numeric(15, 2), nullable=False, default=0)
    # {"1": 8.33, "2": 8.33, ..., "12": 8.37}
    monthly_pcts = Column(JSONB, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True),
                        default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True),
                        default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))

    branch = relationship("Branch")
