import uuid
from datetime import datetime, timezone
from sqlalchemy import Column, Integer, Numeric, String, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from app.database import Base


class TeamKPITarget(Base):
    __tablename__ = "team_kpi_targets"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    role_key = Column(String(20), nullable=False)   # kol | paid_ads | designer | crm | pm
    branch_id = Column(UUID(as_uuid=True), ForeignKey("branches.id", ondelete="CASCADE"), nullable=True)
    year = Column(Integer, nullable=False)
    month = Column(Integer, nullable=False)
    kpi_key = Column(String(50), nullable=False)
    target_value = Column(Numeric(18, 4), nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))

    branch = relationship("Branch", foreign_keys=[branch_id])

    __table_args__ = (
        UniqueConstraint("role_key", "branch_id", "year", "month", "kpi_key",
                         name="uq_team_kpi_targets"),
    )
