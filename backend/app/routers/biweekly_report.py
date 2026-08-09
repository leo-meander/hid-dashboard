"""
Bi-Weekly Branch Manager Report router
- GET  /biweekly/periods       → selectable ISO-week-pair periods
- GET  /biweekly/report        → report payload (JSON)
- GET  /biweekly/preview       → rendered HTML (what the dashboard shows)
- POST /biweekly/refresh-cache → rebuild a period's snapshot (X-Sync-Token)
- CRUD /biweekly/comments      → manager's-notes threads

Kept out of `report.py`, which is already ~3k lines for the weekly report.

The HTML is inline-styled on purpose. It is rendered into the dashboard
today, but the same string has to survive an email client when the delivery
step lands — email clients drop <style> blocks, so every rule is on the
element. That constraint is why this reads more verbosely than page CSS.
"""
import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.biweekly_report_cache import BiweeklyReportCache
from app.models.user import User
from app.models.weekly_report_comment import WeeklyReportComment
from app.routers.auth import get_current_user
from app.routers.sync import verify_sync_token
from app.services.biweekly_period import (
    Period,
    current_period,
    list_periods,
    parse_period_key,
)
from app.services.biweekly_render import _build_html
from app.services.biweekly_report_builder import build_biweekly_report
from app.services.report_common import envelope, ict_today

router = APIRouter()
logger = logging.getLogger(__name__)

REPORT_TYPE = "biweekly"
GENERAL_METRIC_KEY = "bw._general"

# ── Cache ────────────────────────────────────────────────────────────────────


def _load_cached(db: Session, key: str):
    row = db.query(BiweeklyReportCache).filter_by(period_key=key).first()
    return (row.payload, row.computed_at) if row else None


def _save_cached(db: Session, p: Period, payload: list, source: str = "manual"):
    now = datetime.now(timezone.utc)
    row = db.query(BiweeklyReportCache).filter_by(period_key=p.key).first()
    if row:
        row.payload = payload
        row.computed_at = now
        row.source = source
    else:
        db.add(BiweeklyReportCache(
            period_key=p.key, period_start=p.start, period_end=p.end,
            payload=payload, computed_at=now, source=source,
        ))
    db.commit()
    return now


def _get_report(db: Session, p: Period, force_fresh: bool = False):
    """Cached payload for a period, building it if absent.

    A completed period's numbers do not change, so unlike the weekly
    report's singleton cache this never needs a scheduled refresh — the
    first read of a new period computes it, every later read is free.
    `?fresh=1` exists for the case where upstream data was backfilled after
    the fact.
    """
    if not force_fresh:
        cached = _load_cached(db, p.key)
        if cached is not None:
            return cached
    payload = build_biweekly_report(db, p)
    return payload, _save_cached(db, p, payload)


def _resolve_period(period: Optional[str]) -> Period:
    if not period:
        return current_period(ict_today())
    try:
        return parse_period_key(period)
    except ValueError as e:
        raise HTTPException(400, str(e))


# ── Endpoints ────────────────────────────────────────────────────────────────


@router.get("/periods")
def list_available_periods(
    back: int = Query(12, ge=1, le=52),
    db: Session = Depends(get_db),
):
    """Selectable periods, newest first, flagged with whether they're cached."""
    periods = list_periods(ict_today(), back)
    cached = {
        r.period_key for r in
        db.query(BiweeklyReportCache.period_key).filter(
            BiweeklyReportCache.period_key.in_([p.key for p in periods])
        ).all()
    }
    return envelope([{**p.to_dict(), "has_cache": p.key in cached} for p in periods])


@router.get("/report")
def biweekly_report(
    period: Optional[str] = None,
    fresh: int = 0,
    db: Session = Depends(get_db),
):
    """Bi-weekly report payload for a period (defaults to the latest completed)."""
    p = _resolve_period(period)
    payload, computed_at = _get_report(db, p, force_fresh=bool(fresh))
    return envelope({
        "period": p.to_dict(),
        "computed_at": computed_at.isoformat() if computed_at else None,
        "from_cache": not bool(fresh),
        "branches": payload,
    })


@router.get("/preview", response_class=HTMLResponse)
def biweekly_preview(
    period: Optional[str] = None,
    fresh: int = 0,
    db: Session = Depends(get_db),
):
    """Rendered HTML for a period — what the dashboard page displays."""
    p = _resolve_period(period)
    payload, computed_at = _get_report(db, p, force_fresh=bool(fresh))
    return HTMLResponse(_build_html(payload, p, computed_at))


@router.post("/refresh-cache", dependencies=[Depends(verify_sync_token)])
def refresh_cache(period: Optional[str] = None, db: Session = Depends(get_db)):
    """Rebuild a period's snapshot. Token-gated — it runs the full build."""
    p = _resolve_period(period)
    payload = build_biweekly_report(db, p)
    computed_at = _save_cached(db, p, payload, source="cron")
    return envelope({
        "period": p.to_dict(),
        "branches_included": len(payload),
        "computed_at": computed_at.isoformat(),
    })


# ── Manager's notes (reuses weekly_report_comments, report_type='biweekly') ──


class BiweeklyNoteIn(BaseModel):
    period: str
    branch_id: Optional[UUID] = None
    body: str
    metric_key: str = GENERAL_METRIC_KEY
    parent_comment_id: Optional[UUID] = None


class BiweeklyNotePatchIn(BaseModel):
    body: Optional[str] = None
    is_action_item: Optional[bool] = None
    is_resolved: Optional[bool] = None


def _note_out(c: WeeklyReportComment, author: Optional[User]) -> dict:
    return {
        "id": str(c.id),
        "branch_id": str(c.branch_id) if c.branch_id else None,
        "metric_key": c.metric_key,
        "parent_comment_id": str(c.parent_comment_id) if c.parent_comment_id else None,
        "body": c.body,
        "is_action_item": c.is_action_item,
        "is_resolved": c.is_resolved,
        "author_id": str(c.author_id) if c.author_id else None,
        "author_name": (author.name or author.email) if author else None,
        "created_at": c.created_at.isoformat() if c.created_at else None,
        "updated_at": c.updated_at.isoformat() if c.updated_at else None,
    }


def _hydrate(db: Session, rows: list) -> list[dict]:
    ids = {c.author_id for c in rows if c.author_id}
    authors = (
        {u.id: u for u in db.query(User).filter(User.id.in_(ids)).all()} if ids else {}
    )
    return [_note_out(c, authors.get(c.author_id)) for c in rows]


@router.get("/comments")
def list_notes(
    period: str,
    branch_id: Optional[UUID] = None,
    metric_key: Optional[str] = None,
    _current: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    p = _resolve_period(period)
    q = db.query(WeeklyReportComment).filter(
        WeeklyReportComment.report_type == REPORT_TYPE,
        WeeklyReportComment.week_start == p.start,
        WeeklyReportComment.is_deleted == False,  # noqa: E712
    )
    if branch_id is not None:
        q = q.filter(WeeklyReportComment.branch_id == branch_id)
    if metric_key is not None:
        q = q.filter(WeeklyReportComment.metric_key == metric_key)
    rows = q.order_by(WeeklyReportComment.created_at.asc()).all()
    return envelope(_hydrate(db, rows))


@router.post("/comments", status_code=201)
def create_note(
    body: BiweeklyNoteIn,
    current: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    text = (body.body or "").strip()
    if not text:
        raise HTTPException(400, "body is required")
    if len(text) > 5000:
        raise HTTPException(400, "body too long (max 5000 chars)")
    p = _resolve_period(body.period)
    if body.parent_comment_id is not None:
        # Scope the parent lookup to this report type AND this period, so a
        # reply can't be grafted onto a weekly comment or onto a thread from
        # a different period — either would orphan it in the drawer.
        parent = db.query(WeeklyReportComment).filter_by(
            id=body.parent_comment_id,
            report_type=REPORT_TYPE,
            week_start=p.start,
            is_deleted=False,
        ).first()
        if not parent:
            raise HTTPException(404, "Parent note not found")
    c = WeeklyReportComment(
        report_type=REPORT_TYPE,
        week_start=p.start,
        branch_id=body.branch_id,
        metric_key=body.metric_key or GENERAL_METRIC_KEY,
        parent_comment_id=body.parent_comment_id,
        author_id=current.id,
        body=text,
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    return envelope(_note_out(c, current))


@router.patch("/comments/{comment_id}")
def update_note(
    comment_id: UUID,
    body: BiweeklyNotePatchIn,
    current: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    c = db.query(WeeklyReportComment).filter_by(
        id=comment_id, report_type=REPORT_TYPE, is_deleted=False,
    ).first()
    if not c:
        raise HTTPException(404, "Note not found")
    # Rewriting a note is the author's (or an admin's) call, but resolving one
    # is not: the Support Needed board exists so Growth can ask the branch team
    # for something, and it is the branch team — never the author — who marks
    # it handled. Gating resolve on authorship would make that board unusable.
    # Same split the weekly report uses.
    if body.body is not None:
        if c.author_id != current.id and (current.role or "") != "admin":
            raise HTTPException(403, "Only the author or an admin can edit the body")
        text = body.body.strip()
        if not text:
            raise HTTPException(400, "body cannot be empty")
        if len(text) > 5000:
            raise HTTPException(400, "body too long (max 5000 chars)")
        c.body = text
    if body.is_action_item is not None:
        c.is_action_item = body.is_action_item
    if body.is_resolved is not None:
        c.is_resolved = body.is_resolved
        c.resolved_by = current.id if body.is_resolved else None
        c.resolved_at = datetime.now(timezone.utc) if body.is_resolved else None
    db.commit()
    db.refresh(c)
    # Re-read the author: an admin may be editing someone else's note, and
    # echoing `current` back would relabel the note as theirs in the drawer.
    author = db.query(User).filter_by(id=c.author_id).first() if c.author_id else None
    return envelope(_note_out(c, author))


@router.delete("/comments/{comment_id}")
def delete_note(
    comment_id: UUID,
    current: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    c = db.query(WeeklyReportComment).filter_by(
        id=comment_id, report_type=REPORT_TYPE, is_deleted=False,
    ).first()
    if not c:
        raise HTTPException(404, "Note not found")
    if c.author_id != current.id and (current.role or "") != "admin":
        raise HTTPException(403, "You can only delete your own notes")
    c.is_deleted = True
    db.commit()
    return envelope({"id": str(comment_id), "deleted": True})
