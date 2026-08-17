"""
Bi-Weekly Branch Manager Report router
- GET  /biweekly/periods       → selectable half-month periods
- GET  /biweekly/report        → report payload (JSON)
- GET  /biweekly/preview       → rendered HTML (what the dashboard shows)
- POST /biweekly/refresh-cache → rebuild a period's snapshot (X-Sync-Token)
- CRUD /biweekly/comments      → manager's-notes threads
- GET  /biweekly/recipients    → who may be emailed a given branch
- POST /biweekly/send          → email one branch's summary + share link
- GET  /biweekly/shares        → the live share link for (period, branch)
- DEL  /biweekly/shares        → revoke it
- GET  /biweekly/shared/{token} → the full branch report, NO LOGIN

Every endpoint here requires a session except `/shared/{token}`, which is
opened from an emailed link by a branch manager who has no HiD account. See
the note on that handler for what bounds it.

Kept out of `report.py`, which is already ~3k lines for the weekly report.

The HTML is inline-styled on purpose. It is rendered into the dashboard
today, but the same string has to survive an email client when the delivery
step lands — email clients drop <style> blocks, so every rule is on the
element. That constraint is why this reads more verbosely than page CSS.
"""
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models.biweekly_flag_override import BiweeklyFlagOverride
from app.models.biweekly_report_cache import BiweeklyReportCache
from app.models.biweekly_report_share import BiweeklyReportShare
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
from app.services.biweekly_share import (
    build_share_page_html,
    build_summary_email_html,
)
from app.services.email_sender import send_email_html
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


# ── Emailing a branch's report, and the no-login page it opens ───────────────
#
# The recipients are branch managers, most of whom have no HiD account. The
# email therefore carries a summary and a link that IS the credential — see
# app/models/biweekly_report_share.py for the four things that bound the risk
# (one branch, one period, expiring, revocable) and app/services/
# biweekly_share.py for what each of the two documents contains.

#: How long a share link stays live. Long enough to survive a manager coming
#: back from leave, short enough that a forwarded link does not outlive the
#: fortnight it describes by a year.
SHARE_TTL_DAYS = 120

#: Applied to every response from `/shared/{token}`. `noindex` keeps the URL
#: out of search results; `no-store` keeps it out of shared-proxy caches; the
#: referrer policy stops the token leaking in the `Referer` of any link the
#: reader clicks from the page.
_NO_INDEX = {
    "X-Robots-Tag": "noindex, nofollow, noarchive",
    "Cache-Control": "private, no-store, max-age=0",
    "Referrer-Policy": "no-referrer",
}


def _may_see_branch(current: User, branch_id) -> bool:
    """Same "empty means all" rule as `_visible_branches`, for one branch.

    Sending a report is a stronger act than reading one — it puts the figures
    in somebody else's inbox behind a link that needs no login — so the sender
    has to be someone who could already open that branch themselves.
    """
    if current.role == "admin" or not current.allowed_branches:
        return True
    return str(branch_id) in {str(b) for b in current.allowed_branches}


def _user_may_see_branch(u: User, branch_id) -> bool:
    """The same test for a prospective RECIPIENT rather than the sender."""
    if u.role == "admin" or not u.allowed_branches:
        return True
    return str(branch_id) in {str(b) for b in u.allowed_branches}


def _share_url(token: str) -> str:
    """The absolute link that goes in the email.

    Points at THIS service, not the dashboard: the shared page is a complete
    rendered HTML document served by `/shared/{token}`, and on Zeabur the
    frontend is a separate deployment that would 404 on the path. Falls back
    to a relative URL when `PUBLIC_API_URL` is unset — which works in local
    dev and is obviously wrong in a real inbox, rather than silently pointing
    somewhere plausible and broken.
    """
    base = (getattr(settings, "PUBLIC_API_URL", "") or "").rstrip("/")
    return f"{base}/api/biweekly/shared/{token}"


def _get_or_create_share(db: Session, p: Period, branch_id: UUID,
                         current: User) -> BiweeklyReportShare:
    """The live link for this (period, branch), minted if there isn't one.

    Re-sending reuses the existing token, so the link already sitting in
    somebody's inbox keeps working instead of being quietly replaced by a
    second one that also works. An expired or revoked row is rotated in place
    rather than left to collide with the unique constraint — and rotating
    kills the old token, which is the point of having revoked it.
    """
    now = datetime.now(timezone.utc)
    row = db.query(BiweeklyReportShare).filter_by(
        period_key=p.key, branch_id=branch_id,
    ).first()
    if row and row.is_live(now):
        return row
    if row:
        row.token = secrets.token_urlsafe(32)
        row.created_by = current.id
        row.created_at = now
        row.expires_at = now + timedelta(days=SHARE_TTL_DAYS)
        row.revoked_at = None
    else:
        row = BiweeklyReportShare(
            token=secrets.token_urlsafe(32),
            period_key=p.key,
            branch_id=branch_id,
            created_by=current.id,
            expires_at=now + timedelta(days=SHARE_TTL_DAYS),
        )
        db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _branch_comments(db: Session, p: Period, branch_id) -> list:
    """Every live note on this branch's report, oldest first."""
    rows = (
        db.query(WeeklyReportComment)
        .filter(
            WeeklyReportComment.report_type == REPORT_TYPE,
            WeeklyReportComment.week_start == p.start,
            WeeklyReportComment.branch_id == branch_id,
            WeeklyReportComment.is_deleted == False,  # noqa: E712
        )
        .order_by(WeeklyReportComment.created_at.asc())
        .all()
    )
    return _hydrate(db, rows)


@router.get("/recipients")
def list_recipients(
    branch_id: UUID,
    current: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Active users allowed to see `branch_id` — the send list.

    Deliberately scoped rather than "everyone": the picker is the last thing
    standing between a branch's revenue and the wrong inbox, so a user who
    could not open this branch in HiD is not offered as a recipient for it.
    """
    if not _may_see_branch(current, branch_id):
        raise HTTPException(403, "You do not have access to that branch")
    users = (
        db.query(User)
        .filter(User.is_active == True)  # noqa: E712
        .order_by(User.name.asc(), User.email.asc())
        .all()
    )
    return envelope([
        {"id": str(u.id), "name": u.name or u.email,
         "email": u.email, "role": u.role}
        for u in users
        if u.email and _user_may_see_branch(u, branch_id)
    ])


class BiweeklySendIn(BaseModel):
    period: str
    branch_id: UUID
    user_ids: list[UUID] = []
    to: list[str] = []


@router.post("/send")
def send_branch_report(
    body: BiweeklySendIn,
    current: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Email one branch's report summary, with a link to the full thing.

    One message per recipient, not one addressed to all of them: the greeting
    is personal, recipients cannot see each other's addresses, and a bounce on
    one does not take the rest down with it.

    The response names exactly who received it and who did not. A partial
    failure is reported as a partial failure — never as "sent", which is the
    one outcome the sender cannot verify for themselves.
    """
    if (current.role or "") not in ("admin", "editor"):
        raise HTTPException(403, "Editor or admin only")
    if not _may_see_branch(current, body.branch_id):
        raise HTTPException(403, "You do not have access to that branch")

    p = _resolve_period(body.period)

    recipients: list = []
    if body.user_ids:
        users = db.query(User).filter(User.id.in_(body.user_ids)).all()
        found = {u.id for u in users}
        missing = [str(i) for i in body.user_ids if i not in found]
        if missing:
            raise HTTPException(400, f"Unknown user(s): {', '.join(missing)}")
        for u in users:
            if not u.email:
                continue
            # Checked again on send, not only when the picker was drawn: the
            # list could have been fetched before someone's branch access was
            # narrowed, and this is the request that actually discloses.
            if not _user_may_see_branch(u, body.branch_id):
                raise HTTPException(
                    403,
                    f"{u.email} is not allowed to see this branch — grant "
                    "access on the Users page first",
                )
            recipients.append((u.name, u.email))
    for raw in body.to:
        addr = (raw or "").strip()
        if addr:
            recipients.append((None, addr))

    seen = set()
    recipients = [
        r for r in recipients
        if not (r[1].lower() in seen or seen.add(r[1].lower()))
    ]
    if not recipients:
        raise HTTPException(400, "No recipients — pick at least one")

    payload, computed_at = _get_report(db, p)
    payload = _apply_flag_overrides(db, p, payload)
    branch = next(
        (b for b in payload if str(b.get("branch_id")) == str(body.branch_id)),
        None,
    )
    if branch is None:
        raise HTTPException(404, "That branch has no data in this period's report")

    share = _get_or_create_share(db, p, body.branch_id, current)
    url = _share_url(share.token)
    expires_on = share.expires_at.date() if share.expires_at else None

    subject = (
        f"{branch.get('branch_name') or 'Branch'} — Bi-Weekly Report · "
        f"{p.date_label}"
    )
    sent, failed = [], []
    for name, addr in recipients:
        html = build_summary_email_html(
            branch, p, url, recipient_name=name, expires_on=expires_on,
        )
        (sent if send_email_html(subject, html, [addr]) else failed).append(addr)

    if not sent:
        raise HTTPException(
            502,
            "Email send failed for every recipient — check the Zeabur logs "
            "and GET /api/report/email-config",
        )
    return envelope({
        "period": p.key,
        "branch_id": str(body.branch_id),
        "subject": subject,
        "sent_to": sent,
        "failed": failed,
        "share_url": url,
        "expires_at": share.expires_at.isoformat() if share.expires_at else None,
        "computed_at": computed_at.isoformat() if computed_at else None,
    })


@router.get("/shares")
def get_share(
    period: str,
    branch_id: UUID,
    current: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """The live link for a (period, branch), if one has been issued."""
    if not _may_see_branch(current, branch_id):
        raise HTTPException(403, "You do not have access to that branch")
    p = _resolve_period(period)
    row = db.query(BiweeklyReportShare).filter_by(
        period_key=p.key, branch_id=branch_id,
    ).first()
    if row is None or not row.is_live(datetime.now(timezone.utc)):
        return envelope(None)
    return envelope({
        "url": _share_url(row.token),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
        "view_count": row.view_count,
        "last_viewed_at": (
            row.last_viewed_at.isoformat() if row.last_viewed_at else None
        ),
    })


@router.delete("/shares")
def revoke_share(
    period: str,
    branch_id: UUID,
    current: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Kill a link that went somewhere it shouldn't have."""
    if (current.role or "") not in ("admin", "editor"):
        raise HTTPException(403, "Editor or admin only")
    if not _may_see_branch(current, branch_id):
        raise HTTPException(403, "You do not have access to that branch")
    p = _resolve_period(period)
    row = db.query(BiweeklyReportShare).filter_by(
        period_key=p.key, branch_id=branch_id,
    ).first()
    if row and row.revoked_at is None:
        row.revoked_at = datetime.now(timezone.utc)
        db.commit()
    return envelope({"period": p.key, "branch_id": str(branch_id), "revoked": True})


def _share_gone_html() -> str:
    """One page for a wrong token, an expired one and a revoked one.

    Telling the reader which of the three it was would tell someone probing
    the endpoint the same thing.
    """
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Link no longer available</title></head>"
        "<body style='font-family:system-ui,-apple-system,Segoe UI,Roboto,"
        "sans-serif;background:#faf7f3;color:#3f3b3a;margin:0;padding:0;'>"
        "<div style='max-width:460px;margin:16vh auto;padding:0 24px;"
        "text-align:center;'>"
        "<div style='font-weight:600;letter-spacing:.14em;font-size:13px;"
        "opacity:.6;'>MEANDER</div>"
        "<h1 style='font-size:21px;font-weight:600;margin:14px 0 8px;'>"
        "This report link is no longer available</h1>"
        "<p style='font-size:14px;line-height:1.6;color:#6b6664;margin:0;'>"
        "It may have expired or been withdrawn. Ask the Growth team to send "
        "you a fresh link.</p></div></body></html>"
    )


@router.get("/shared/{token}", response_class=HTMLResponse)
def shared_report(token: str, db: Session = Depends(get_db)):
    """The full report for one branch, opened by link alone — no login.

    This is the ONE endpoint in this router with no auth dependency, and it is
    deliberate: the reader is a branch manager with no HiD account. What keeps
    it bounded is that the token names exactly one branch and one period,
    expires, and can be revoked — and that an unknown token is answered with
    the same page as an expired one, so the response never confirms that some
    other token would have worked.

    Every note on the report is rendered into the page, because the reader has
    no authenticated API to fetch them from.
    """
    now = datetime.now(timezone.utc)
    row = db.query(BiweeklyReportShare).filter_by(token=token).first()
    if row is None or not row.is_live(now):
        return HTMLResponse(_share_gone_html(), status_code=404, headers=_NO_INDEX)

    try:
        p = parse_period_key(row.period_key)
    except ValueError:
        logger.error("share %s carries an unparseable period key %r",
                     row.id, row.period_key)
        return HTMLResponse(_share_gone_html(), status_code=404, headers=_NO_INDEX)

    payload, computed_at = _get_report(db, p)
    payload = _apply_flag_overrides(db, p, payload)
    branch = next(
        (b for b in payload if str(b.get("branch_id")) == str(row.branch_id)), None
    )
    if branch is None:
        return HTMLResponse(_share_gone_html(), status_code=404, headers=_NO_INDEX)

    # Recorded before rendering: if the render raises, the view still happened
    # and the audit trail should say so.
    row.view_count = (row.view_count or 0) + 1
    row.last_viewed_at = now
    db.commit()

    comments = _branch_comments(db, p, row.branch_id)
    html = build_share_page_html(branch, p, computed_at, comments)
    return HTMLResponse(html, headers=_NO_INDEX)
