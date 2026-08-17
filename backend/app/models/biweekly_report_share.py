"""Unlisted share links for one branch's Bi-Weekly report.

A branch manager is not a HiD user. The report is emailed to them, and the
email has to open into the full report without a login — so the link itself
is the credential.

That is a deliberate trade-off, and every column here exists to bound it:

  * **Scoped to one (period, branch).** A leaked link exposes one branch's
    fortnight, never the group, never another period. `/preview` requires a
    login precisely because it carries all five branches at once.
  * **Unguessable.** The token is `secrets.token_urlsafe(32)` — 256 bits.
    Long enough that enumeration is not the threat model; forwarding is.
  * **Expiring.** `expires_at` is set at creation. A link that outlives the
    reason it was sent is a liability with no upside.
  * **Revocable.** `revoked_at` kills a link that was forwarded somewhere it
    should not have been, without waiting for the expiry.
  * **Audited.** `view_count` / `last_viewed_at` are how anyone answers "was
    this opened, and how often" after the fact.

One row per (period_key, branch_id), so re-sending the same report reuses the
link that is already in someone's inbox instead of littering the table with
tokens that all still work.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Column, DateTime, ForeignKey, Integer, String, UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID

from app.database import Base


class BiweeklyReportShare(Base):
    __tablename__ = "biweekly_report_shares"
    __table_args__ = (
        UniqueConstraint("period_key", "branch_id",
                         name="uq_biweekly_share_period_branch"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # The URL secret. Indexed and unique — every public read is a lookup on it.
    token = Column(String(64), nullable=False, unique=True, index=True)
    # Half-month key, e.g. "2026-08-H2" — see services/biweekly_period.py
    period_key = Column(String(16), nullable=False)
    branch_id = Column(UUID(as_uuid=True),
                       ForeignKey("branches.id", ondelete="CASCADE"),
                       nullable=False)
    created_by = Column(UUID(as_uuid=True),
                        ForeignKey("users.id", ondelete="SET NULL"),
                        nullable=True)
    created_at = Column(DateTime(timezone=True),
                        default=lambda: datetime.now(timezone.utc),
                        nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    view_count = Column(Integer, nullable=False, default=0, server_default="0")
    last_viewed_at = Column(DateTime(timezone=True), nullable=True)

    def is_live(self, now: datetime) -> bool:
        """True when this link should still open the report."""
        return self.revoked_at is None and self.expires_at > now
