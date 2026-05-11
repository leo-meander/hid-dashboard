"""Per-week snapshot of the Weekly Report payload.

`weekly_report_cache` is a singleton — every Monday 03:00 ICT cron
overwrites it. This table preserves the snapshot keyed by `week_start`
(Monday of the week the snapshot was taken) so the UI can let users
filter to any past week.

Comments are NOT duplicated into the archive — they're queried live
against `weekly_report_comments` using the same `week_start`, so a
discussion stays editable even when viewing an old week.
"""
import uuid
from datetime import datetime, timezone
from sqlalchemy import Column, String, DateTime, ForeignKey, Date
from sqlalchemy.dialects.postgresql import UUID, JSONB
from app.database import Base


class WeeklyReportArchive(Base):
    __tablename__ = "weekly_report_archives"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    week_start = Column(Date, nullable=False, unique=True)
    payload = Column(JSONB, nullable=False)
    archived_at = Column(DateTime(timezone=True),
                         default=lambda: datetime.now(timezone.utc),
                         nullable=False)
    archived_by = Column(UUID(as_uuid=True),
                         ForeignKey("users.id", ondelete="SET NULL"),
                         nullable=True)
    source = Column(String(20), default="cron", nullable=False)
