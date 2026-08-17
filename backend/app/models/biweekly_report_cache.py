"""Per-period snapshot of the Bi-Weekly Branch Manager Report.

Deliberately NOT the Weekly Report's singleton-cache-plus-archive pair.
Because a bi-weekly period is a closed, immutable range of dates, one row
per period serves as both the cache and the history: once a period has been
computed it never needs recomputing, and the row IS the archive.

That also sidesteps `weekly_report_archives.week_start` being UNIQUE, which
a second report type keyed by a Monday date would have collided with.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, Date, DateTime, String
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.database import Base


class BiweeklyReportCache(Base):
    __tablename__ = "biweekly_report_cache"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Half-month key, e.g. "2026-08-H2" — see services/biweekly_period.py
    period_key = Column(String(16), nullable=False, unique=True)
    period_start = Column(Date, nullable=False)
    period_end = Column(Date, nullable=False)
    payload = Column(JSONB, nullable=False)
    computed_at = Column(DateTime(timezone=True),
                         default=lambda: datetime.now(timezone.utc),
                         nullable=False)
    source = Column(String(20), default="manual", nullable=False)
