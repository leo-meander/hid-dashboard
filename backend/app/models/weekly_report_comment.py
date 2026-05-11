"""Per-metric discussion threads on the Weekly Report page.

Scoped by (week_start, branch_id, metric_key). All logged-in users can
post — admin/editor/viewer alike. Authors can edit/delete their own;
admins can edit/delete anyone's. Soft-delete via `is_deleted` so threads
keep their context if a parent is removed.

`metric_key` is a stable identifier emitted as a `data-metric-key`
attribute on the rendered HTML cell — see report.py renderers. Examples:
  - exec summary table: 'revenue_mtd', 'target', 'pacing', 'forecast',
    'occ', 'adr', 'revpar', 'wow_revenue', 'yoy_revenue'
  - per-branch table: 'branch.revenue', 'branch.target', 'branch.adr',
    'branch.occ_actual', 'branch.occ_forecast', 'branch.forecast',
    'branch.next_revenue', 'branch.next_target', 'branch.next_adr',
    'branch.next_occ_forecast', 'branch.next_forecast'
"""
import uuid
from datetime import datetime, timezone
from sqlalchemy import (
    Column, Boolean, String, Text, DateTime, ForeignKey, Date,
)
from sqlalchemy.dialects.postgresql import UUID
from app.database import Base


class WeeklyReportComment(Base):
    __tablename__ = "weekly_report_comments"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    week_start = Column(Date, nullable=False)
    branch_id = Column(UUID(as_uuid=True),
                       ForeignKey("branches.id", ondelete="CASCADE"),
                       nullable=True)
    metric_key = Column(String(64), nullable=False)
    parent_comment_id = Column(UUID(as_uuid=True),
                               ForeignKey("weekly_report_comments.id",
                                          ondelete="CASCADE"),
                               nullable=True)
    author_id = Column(UUID(as_uuid=True),
                       ForeignKey("users.id", ondelete="SET NULL"),
                       nullable=True)
    body = Column(Text, nullable=False)
    is_action_item = Column(Boolean, default=False, nullable=False)
    is_resolved = Column(Boolean, default=False, nullable=False)
    resolved_by = Column(UUID(as_uuid=True),
                         ForeignKey("users.id", ondelete="SET NULL"),
                         nullable=True)
    resolved_at = Column(DateTime(timezone=True), nullable=True)
    is_deleted = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime(timezone=True),
                        default=lambda: datetime.now(timezone.utc),
                        nullable=False)
    updated_at = Column(DateTime(timezone=True),
                        default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc),
                        nullable=False)
