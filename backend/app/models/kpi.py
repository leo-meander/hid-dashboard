import uuid
from datetime import datetime, timezone
from sqlalchemy import Column, Integer, Numeric, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from app.database import Base


class KPITarget(Base):
    __tablename__ = "kpi_targets"
    __table_args__ = (UniqueConstraint("branch_id", "year", "month", name="uq_kpi_branch_year_month"),)

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    branch_id = Column(UUID(as_uuid=True), ForeignKey("branches.id", ondelete="CASCADE"), nullable=False)
    year = Column(Integer, nullable=False)
    month = Column(Integer, nullable=False)
    target_revenue_native = Column(Numeric(15, 2), nullable=False)
    target_revenue_vnd = Column(Numeric(18, 2), nullable=False)
    predicted_occ_pct = Column(Numeric(5, 4), nullable=True)
    predicted_room_occ_pct = Column(Numeric(5, 4), nullable=True)
    predicted_dorm_occ_pct = Column(Numeric(5, 4), nullable=True)
    deduction_pct = Column(Numeric(5, 2), nullable=True, default=0)
    other_revenue_native = Column(Numeric(15, 2), nullable=True, default=0)  # Manual add-on (e.g. F&B, ancillary)
    actual_revenue_override = Column(Numeric(15, 2), nullable=True)  # Manual override from accounting
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))

    branch = relationship("Branch", back_populates="kpi_targets")
