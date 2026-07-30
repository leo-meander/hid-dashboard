import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, String
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.database import Base


class WebhookEvent(Base):
    """One reservation fan-out outcome — GHL, Meta, Google Ads, TikTok.

    Replaces an in-memory ring buffer that was wiped on every Zeabur deploy,
    which repeatedly cost us the history right when we needed it to confirm a
    fix. Rows are pruned after WEBHOOK_EVENT_RETENTION_DAYS by a nightly job.

    Per-service results are JSONB rather than columns: the shape differs per
    service and changes whenever a target is added, and this is a debugging
    log, not a table anything reports off. `has_failure` is denormalised so
    "show me only the failures" doesn't have to scan the JSON.

    `branch` is the branch key ("saigon", "1948"), not a FK to branches — the
    log has to be writable even for a propertyID with no branch mapping, which
    is itself a failure worth recording.
    """
    __tablename__ = "webhook_events"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
        index=True,
    )
    reservation_id = Column(String(64), nullable=False, index=True)
    branch = Column(String(32), nullable=True, index=True)
    guest_email = Column(String(255), nullable=True)
    source = Column(String(128), nullable=True)

    ghl = Column(JSONB, nullable=True)
    meta = Column(JSONB, nullable=True)
    google_ads = Column(JSONB, nullable=True)
    tiktok = Column(JSONB, nullable=True)

    has_failure = Column(Boolean, nullable=False, default=False, index=True)
