from datetime import datetime, timezone
from sqlalchemy import Column, String, DateTime
from sqlalchemy.dialects.postgresql import JSONB
from app.database import Base


class WeeklyReportCache(Base):
    """Single-row cache for the weekly report JSON payload.

    `id` is a fixed sentinel ('singleton') so the table only ever holds
    one row — refreshes upsert the same row in place. Refreshed by the
    GitHub Actions cron Mon 03:00 ICT; consumed by /api/report/weekly,
    /api/report/preview, and the Mon 07:00 email send.
    """
    __tablename__ = "weekly_report_cache"

    SINGLETON_ID = "singleton"

    id = Column(String(32), primary_key=True)
    payload = Column(JSONB, nullable=False)
    computed_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
