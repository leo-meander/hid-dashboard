"""
Bi-Weekly Branch Manager Report router
- GET  /biweekly/periods       → selectable half-month periods
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
from app.models.biweekly_flag_override import BiweeklyFlagOverride
from app.models.biweekly_report_cache import BiweeklyReportCache
from app.models.user import User
from app.models.weekly_report_comment import WeeklyReportComment
from app.routers.auth import get_current_user
from app.routers.sync import verify_sync_token
from app.services.biweekly_period import (
    Period,
    current_period,
    is_complete,
    list_periods,
    parse_period_key,
)
from app.services.biweekly_render import _build_html
from app.services.biweekly_report_builder import build_biweekly_report
from app.services.report_common import envelope, ict_today

router = APIRouter()
logger = logging.getLogger(__name__)

REPORT_TYPE = "biweekly"


def _apply_flag_overrides(db: Session, p: Period, payload: list) -> list:
    """Fold operator corrections into a cached payload's flag lines.

    Applied here, per request, for the same reason `_visible_branches` is: the
    cache holds one payload per period shared by every reader, and an override
    is a later edit on top of it. Baking it in would mean the next rebuild
    silently dropped every correction.

    Keyed on the rule (`flag.revenue`, `act.kol_posts`), never on the text, so
    a rebuild that rewrites the sentence with new numbers still matches. An
    edited line is marked `edited` and shown exactly as typed — the same rule
    the rest of HiD follows for a hand-entered number. `is_hidden` drops the
    line: the rule fired and the operator judged it wrong.

    An override whose rule did not fire this period simply matches nothing,
    which is the honest outcome — there is no line left to correct.

    Reading the table is wrapped: Zeabur does not run Alembic on deploy (see
    POST /api/sync/run-migrations), so between the code landing and the
    migration being applied this query hits a table that does not exist. That
    must cost the reader their corrections, not the whole report.
    """
    if not payload:
        return payload
    try:
        rows = db.query(BiweeklyFlagOverride).filter(
            BiweeklyFlagOverride.period_key == p.key,
        ).all()
    except Exception:
        logger.warning(
            "biweekly flag overrides unavailable for %s — serving the generated "
            "lines. Has migration 059 been applied?", p.key, exc_info=True,
        )
        db.rollback()
        return payload
    if not rows:
        return payload
    by_branch: dict = {}
    for r in rows:
        by_branch.setdefault(str(r.branch_id), {})[r.flag_key] = r

    def _fold(items: list, ov: dict, text_field: str) -> list:
        out = []
        for it in items:
            if not isinstance(it, dict) or not it.get("key"):
                out.append(it)          # legacy payload, nothing to key on
                continue
            o = ov.get(it["key"])
            if o is None:
                out.append(it)
                continue
            if o.is_hidden:
                continue
            if o.body:
                out.append({**it, text_field: o.body, "edited": True})
            else:
                out.append(it)
        return out

    result = []
    for b in payload:
        ov = by_branch.get(str(b.get("branch_id")))
        if not ov:
            result.append(b)
            continue
        result.append({
            **b,
            "highlights": _fold(b.get("highlights") or [], ov, "text"),
            "watchouts": _fold(b.get("watchouts") or [], ov, "text"),
            # Actions carry title/when/body; an override replaces the whole
            # rendered sentence, so it lands in `text` and the renderer uses
            # that instead of reassembling the three parts.
            "actions": _fold(b.get("actions") or [], ov, "text"),
        })
    return result


def _visible_branches(payload: list, current: User) -> list:
    """The branches of a cached report this user is allowed to see.

    The cache holds one payload per period covering every branch, shared by
    every reader — so the access check belongs here, on the way out, not in
    the builder. Filtering at build time would write a payload shaped by
    whoever happened to trigger the build and then serve it to everyone else.

    An admin, or a user with no `allowed_branches` set, sees all of them —
    the same "empty means all" rule the rest of the app uses (see
    `BranchProvider` on the frontend and `CreateUserIn.allowed_branches`).
    """
    if current.role == "admin" or not current.allowed_branches:
        return payload
    allowed = {str(b) for b in current.allowed_branches}
    return [b for b in payload if str(b.get("branch_id")) in allowed]


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
    _current: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Selectable periods, newest first, flagged with whether they're cached.

    The newest entry is the period a report sent today would cover, which on
    the 14th or the last day of the month is the period still running — see
    `current_period`. `is_complete` is how the page says so.
    """
    today = ict_today()
    periods = list_periods(today, back)
    cached = {
        r.period_key for r in
        db.query(BiweeklyReportCache.period_key).filter(
            BiweeklyReportCache.period_key.in_([p.key for p in periods])
        ).all()
    }
    return envelope([
        {**p.to_dict(), "has_cache": p.key in cached,
         "is_complete": is_complete(p, today)}
        for p in periods
    ])


@router.get("/report")
def biweekly_report(
    period: Optional[str] = None,
    fresh: int = 0,
    current: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Bi-weekly report payload for a period (defaults to the one a report
    sent today would cover).

    Requires a login. This and `/preview` shipped with no auth dependency at
    all, which made every branch's revenue readable by anyone holding the URL.
    """
    p = _resolve_period(period)
    payload, computed_at = _get_report(db, p, force_fresh=bool(fresh))
    payload = _apply_flag_overrides(db, p, payload)
    return envelope({
        "period": {**p.to_dict(), "is_complete": is_complete(p, ict_today())},
        "computed_at": computed_at.isoformat() if computed_at else None,
        "from_cache": not bool(fresh),
        "branches": _visible_branches(payload, current),
    })


@router.get("/preview", response_class=HTMLResponse)
def biweekly_preview(
    period: Optional[str] = None,
    fresh: int = 0,
    current: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Rendered HTML for a period — what the dashboard page displays.

    Requires a login, and renders only the branches this user may see: the
    page slices its branch tabs straight out of this markup, so a branch left
    in here is a branch they can open.
    """
    p = _resolve_period(period)
    payload, computed_at = _get_report(db, p, force_fresh=bool(fresh))
    payload = _apply_flag_overrides(db, p, payload)
    visible = _visible_branches(payload, current)
    return HTMLResponse(_build_html(visible, p, computed_at))


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


# ── Flag overrides ───────────────────────────────────────────────────────────
#
# Corrections to the auto-generated Highlights / Watch-outs / Recommended
# Action lines. See `_apply_flag_overrides` for how they are folded in, and
# app/models/biweekly_flag_override.py for why they are not comments.


class FlagOverrideIn(BaseModel):
    period: str
    branch_id: UUID
    flag_key: str
    # Either replacement text, or hide the line. Sending neither clears the
    # override — the DELETE route does the same thing more explicitly.
    body: Optional[str] = None
    is_hidden: bool = False


def _flag_override_out(o: BiweeklyFlagOverride) -> dict:
    return {
        "period": o.period_key,
        "branch_id": str(o.branch_id),
        "flag_key": o.flag_key,
        "body": o.body,
        "is_hidden": o.is_hidden,
        "edited_by": str(o.edited_by) if o.edited_by else None,
        "updated_at": o.updated_at.isoformat() if o.updated_at else None,
    }


@router.get("/flag-overrides")
def list_flag_overrides(
    period: str,
    branch_id: Optional[UUID] = None,
    _current: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Every correction for a period, optionally one branch.

    The page needs these separately from the rendered HTML: the HTML shows the
    corrected text, but the editor has to offer "revert to the generated line",
    which means knowing that a line IS overridden.
    """
    q = db.query(BiweeklyFlagOverride).filter(
        BiweeklyFlagOverride.period_key == period,
    )
    if branch_id:
        q = q.filter(BiweeklyFlagOverride.branch_id == branch_id)
    return envelope([_flag_override_out(o) for o in q.all()])


@router.put("/flag-overrides")
def upsert_flag_override(
    body: FlagOverrideIn,
    current: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Correct or hide one generated line.

    Idempotent on (period, branch, flag_key) — the table's unique constraint —
    so the editor can save repeatedly without piling up rows. A viewer is not
    allowed: this changes what every other reader of the report sees.
    """
    if (current.role or "") not in ("admin", "editor"):
        raise HTTPException(403, "Editor or admin only")

    text = (body.body or "").strip() or None
    if not text and not body.is_hidden:
        raise HTTPException(400, "Provide replacement text, or set is_hidden")

    # Validate the period key rather than storing whatever arrives — a typo
    # here would write an override that can never match a rendered report.
    p = parse_period_key(body.period)

    row = db.query(BiweeklyFlagOverride).filter_by(
        period_key=p.key, branch_id=body.branch_id, flag_key=body.flag_key,
    ).first()
    if row is None:
        row = BiweeklyFlagOverride(
            period_key=p.key, branch_id=body.branch_id, flag_key=body.flag_key,
        )
        db.add(row)
    row.body = text
    row.is_hidden = body.is_hidden
    row.edited_by = current.id
    db.commit()
    db.refresh(row)
    return envelope(_flag_override_out(row))


@router.delete("/flag-overrides")
def delete_flag_override(
    period: str,
    branch_id: UUID,
    flag_key: str,
    current: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Revert a line to whatever the rules generate for it."""
    if (current.role or "") not in ("admin", "editor"):
        raise HTTPException(403, "Editor or admin only")
    row = db.query(BiweeklyFlagOverride).filter_by(
        period_key=period, branch_id=branch_id, flag_key=flag_key,
    ).first()
    if row:
        db.delete(row)
        db.commit()
    return envelope({"flag_key": flag_key, "reverted": True})
