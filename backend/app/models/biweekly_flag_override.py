"""Operator corrections to the Bi-Weekly report's auto-generated flags.

`highlights_block` and `recommendations_block` write the Highlights /
Watch-outs / Recommended Actions lines from rules over the period's numbers.
The rules are right most of the time and wrong some of the time — a market
"growing fast" off five bookings, an action that no longer applies. This table
is how an operator corrects one without the correction being wiped by the next
rebuild.

One row per (period_key, branch_id, flag_key). `flag_key` names the RULE that
produced the line (`flag.revenue`, `flag.ads.Meta`, `act.kol_posts` — see
`highlights_block`), never the text it produced, so a rebuild that changes the
numbers in the sentence still finds the override.

Two states, both in this one row:
  - `body` set        → show this text instead of the generated one
  - `is_hidden` True  → drop the line entirely

Overrides are applied on the way OUT of the cache, per request, exactly like
`_visible_branches` — never baked into the cached payload, which is shared by
every reader.

Deliberately NOT stored in `weekly_report_comments`: that table is a thread
(many rows per key, soft-delete, resolve state) and its rows drive the comment
badges on the report. An override is a single authoritative value per key, and
putting it there would have made every correction show up as a discussion.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean, Column, DateTime, ForeignKey, String, Text, UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID

from app.database import Base


class BiweeklyFlagOverride(Base):
    __tablename__ = "biweekly_flag_overrides"
    __table_args__ = (
        UniqueConstraint("period_key", "branch_id", "flag_key",
                         name="uq_biweekly_flag_override"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # e.g. '2026-W31' — Period.key, the same identifier the cache is keyed by.
    period_key = Column(String(16), nullable=False)
    branch_id = Column(UUID(as_uuid=True),
                       ForeignKey("branches.id", ondelete="CASCADE"),
                       nullable=False)
    flag_key = Column(String(64), nullable=False)
    # NULL with is_hidden=False would be a no-op row; the router deletes
    # instead of writing one.
    body = Column(Text, nullable=True)
    is_hidden = Column(Boolean, nullable=False, default=False,
                       server_default="false")
    edited_by = Column(UUID(as_uuid=True),
                       ForeignKey("users.id", ondelete="SET NULL"),
                       nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False,
                        default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), nullable=False,
                        default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))
