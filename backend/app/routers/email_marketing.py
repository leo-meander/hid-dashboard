"""
Email Marketing router — GHL email performance + CRM revenue attribution.

Workflow campaigns now write a single sentinel-dated row per workflow that
each sync OVERWRITES (lifetime cumulative — see ghl_email_sync.py rationale).
The summary and per-campaign endpoints therefore include workflow rows
regardless of the requested date range, while bulk-campaign rows still honor
the date filter because each bulk send carries its real schedule date.
"""
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy import and_, func, desc, or_
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models.email_campaign_stats import EmailCampaignStats

router = APIRouter()
logger = logging.getLogger(__name__)


def _envelope(data):
    return {
        "success": True,
        "data": data,
        "error": None,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _default_dates(date_from, date_to):
    today = datetime.now(timezone.utc).date()
    if date_to is None:
        date_to = today
    if date_from is None:
        date_from = date_to - timedelta(days=90)
    return date_from, date_to


def _latest_snapshot_query(db: Session, date_from, date_to, campaign_type=None,
                           workflow_id=None, branch_name=None):
    """Return a query yielding the latest snapshot row per (workflow_id, branch_name).

    Workflow rows live at a sentinel stat_date (lifetime cumulative — one row
    per workflow that gets overwritten each sync), so they're always included
    regardless of the date window. Bulk rows keep their per-send schedule
    date and stay date-filtered.
    """
    inner = db.query(
        EmailCampaignStats.workflow_id,
        EmailCampaignStats.branch_name,
        EmailCampaignStats.campaign_type,
        func.max(EmailCampaignStats.stat_date).label("max_date"),
    ).filter(or_(
        EmailCampaignStats.campaign_type == "workflow",
        EmailCampaignStats.stat_date.between(date_from, date_to),
    ))
    if campaign_type:
        inner = inner.filter(EmailCampaignStats.campaign_type == campaign_type)
    if workflow_id:
        inner = inner.filter(EmailCampaignStats.workflow_id == workflow_id)
    if branch_name:
        inner = inner.filter(EmailCampaignStats.branch_name == branch_name)
    inner = inner.group_by(
        EmailCampaignStats.workflow_id,
        EmailCampaignStats.branch_name,
        EmailCampaignStats.campaign_type,
    ).subquery()

    return db.query(EmailCampaignStats).join(
        inner,
        and_(
            EmailCampaignStats.workflow_id == inner.c.workflow_id,
            EmailCampaignStats.branch_name == inner.c.branch_name,
            EmailCampaignStats.campaign_type == inner.c.campaign_type,
            EmailCampaignStats.stat_date == inner.c.max_date,
        ),
    )


def _row_to_dict(r: EmailCampaignStats) -> dict:
    sent = int(r.total_sent or 0)
    opened = int(r.unique_opened or 0)
    clicked = int(r.unique_clicked or 0)
    bounced = int(r.total_bounced or 0)
    unsub = int(r.total_unsubscribed or 0)
    revenue_vnd = float(r.attributed_revenue_vnd or 0)
    bookings = int(r.attributed_bookings or 0)
    return {
        "workflow_id": r.workflow_id,
        "workflow_name": r.workflow_name or r.workflow_id,
        "campaign_type": r.campaign_type,
        "branch_name": r.branch_name,
        "stat_date": r.stat_date.isoformat(),
        "sent": sent,
        "delivered": int(r.total_delivered or 0),
        "opened": int(r.total_opened or 0),
        "unique_opened": opened,
        "clicked": int(r.total_clicked or 0),
        "unique_clicked": clicked,
        "bounced": bounced,
        "unsubscribed": unsub,
        "open_rate": round(opened / sent, 4) if sent > 0 else 0,
        "click_rate": round(clicked / sent, 4) if sent > 0 else 0,
        "bounce_rate": round(bounced / sent, 4) if sent > 0 else 0,
        "unsubscribe_rate": round(unsub / sent, 4) if sent > 0 else 0,
        "attributed_bookings": bookings,
        "attributed_canceled": int(r.attributed_canceled or 0),
        "attributed_nights": int(r.attributed_nights or 0),
        "attributed_revenue_native": float(r.attributed_revenue_native or 0),
        "attributed_revenue_vnd": revenue_vnd,
        "attributed_currency": r.attributed_currency,
        "attributed_rate_plan": r.attributed_rate_plan,
        # Cost-per-booking and revenue-per-email indicators
        "revenue_per_email": round(revenue_vnd / sent, 2) if sent > 0 else 0,
        "booking_rate": round(bookings / sent, 6) if sent > 0 else 0,
    }


# ── Summary KPIs ─────────────────────────────────────────────────────────────

@router.get("/summary")
def email_summary(
    date_from: Optional[date] = Query(None),
    date_to: Optional[date] = Query(None),
    campaign_type: Optional[str] = Query(None, description="'workflow' or 'bulk'"),
    branch_name: Optional[str] = Query(None, description="e.g. 'Saigon' or '1948'"),
    workflow_id: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    """Overall email marketing KPIs (latest-snapshot per workflow×branch)."""
    try:
        date_from, date_to = _default_dates(date_from, date_to)
        rows = _latest_snapshot_query(
            db, date_from, date_to, campaign_type, workflow_id, branch_name
        ).all()

        sent = sum(int(r.total_sent or 0) for r in rows)
        delivered = sum(int(r.total_delivered or 0) for r in rows)
        opened = sum(int(r.total_opened or 0) for r in rows)
        unique_opened = sum(int(r.unique_opened or 0) for r in rows)
        clicked = sum(int(r.total_clicked or 0) for r in rows)
        unique_clicked = sum(int(r.unique_clicked or 0) for r in rows)
        bounced = sum(int(r.total_bounced or 0) for r in rows)
        unsubscribed = sum(int(r.total_unsubscribed or 0) for r in rows)
        complained = sum(int(r.total_complained or 0) for r in rows)
        bookings = sum(int(r.attributed_bookings or 0) for r in rows)
        canceled = sum(int(r.attributed_canceled or 0) for r in rows)
        nights = sum(int(r.attributed_nights or 0) for r in rows)
        revenue_vnd = sum(float(r.attributed_revenue_vnd or 0) for r in rows)

        last_synced = max(
            (getattr(r, "updated_at", None) for r in rows
             if getattr(r, "updated_at", None)),
            default=None,
        )
        data = {
            "total_sent": sent,
            "total_delivered": delivered,
            "total_opened": opened,
            "unique_opened": unique_opened,
            "total_clicked": clicked,
            "unique_clicked": unique_clicked,
            "total_bounced": bounced,
            "total_unsubscribed": unsubscribed,
            "total_complained": complained,
            "open_rate": round(unique_opened / sent, 4) if sent > 0 else 0,
            "click_rate": round(unique_clicked / sent, 4) if sent > 0 else 0,
            "bounce_rate": round(bounced / sent, 4) if sent > 0 else 0,
            "unsubscribe_rate": round(unsubscribed / sent, 4) if sent > 0 else 0,
            "attributed_bookings": bookings,
            "attributed_canceled": canceled,
            "attributed_nights": nights,
            "attributed_revenue_vnd": round(revenue_vnd, 2),
            "revenue_per_email": round(revenue_vnd / sent, 2) if sent > 0 else 0,
            "booking_rate": round(bookings / sent, 6) if sent > 0 else 0,
            "date_from": date_from.isoformat(),
            "date_to": date_to.isoformat(),
            "data_synced_at": last_synced.isoformat() if last_synced else None,
        }
        return _envelope(data)
    except Exception as e:
        logger.exception("email_summary failed")
        return {"success": False, "data": None, "error": str(e),
                "timestamp": datetime.now(timezone.utc).isoformat()}


# ── Daily Trend ──────────────────────────────────────────────────────────────

@router.get("/daily")
def email_daily(
    date_from: Optional[date] = Query(None),
    date_to: Optional[date] = Query(None),
    campaign_type: Optional[str] = Query(None),
    branch_name: Optional[str] = Query(None),
    workflow_id: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    """Daily email stats trend (raw per-day rows — useful for time-series only)."""
    try:
        date_from, date_to = _default_dates(date_from, date_to)

        q = db.query(
            EmailCampaignStats.stat_date,
            func.sum(EmailCampaignStats.total_sent).label("sent"),
            func.sum(EmailCampaignStats.total_delivered).label("delivered"),
            func.sum(EmailCampaignStats.total_opened).label("opened"),
            func.sum(EmailCampaignStats.total_clicked).label("clicked"),
            func.sum(EmailCampaignStats.total_bounced).label("bounced"),
            func.sum(EmailCampaignStats.total_unsubscribed).label("unsubscribed"),
        ).filter(EmailCampaignStats.stat_date.between(date_from, date_to))
        if campaign_type:
            q = q.filter(EmailCampaignStats.campaign_type == campaign_type)
        if workflow_id:
            q = q.filter(EmailCampaignStats.workflow_id == workflow_id)
        if branch_name:
            q = q.filter(EmailCampaignStats.branch_name == branch_name)
        q = q.group_by(EmailCampaignStats.stat_date).order_by(EmailCampaignStats.stat_date)
        rows = q.all()

        data = []
        for r in rows:
            sent = int(r.sent or 0)
            opened = int(r.opened or 0)
            clicked = int(r.clicked or 0)
            data.append({
                "date": r.stat_date.isoformat(),
                "sent": sent,
                "delivered": int(r.delivered or 0),
                "opened": opened,
                "clicked": clicked,
                "bounced": int(r.bounced or 0),
                "unsubscribed": int(r.unsubscribed or 0),
                "open_rate": round(opened / sent, 4) if sent > 0 else 0,
                "click_rate": round(clicked / sent, 4) if sent > 0 else 0,
            })

        return _envelope(data)
    except Exception as e:
        logger.exception("email_daily failed")
        return {"success": False, "data": None, "error": str(e),
                "timestamp": datetime.now(timezone.utc).isoformat()}


# ── By Campaign (per workflow_id × branch — latest snapshot) ─────────────────

@router.get("/by-campaign")
def email_by_campaign(
    date_from: Optional[date] = Query(None),
    date_to: Optional[date] = Query(None),
    campaign_type: Optional[str] = Query(None),
    branch_name: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    """One row per (workflow_id, branch_name) showing latest cumulative stats
    plus CRM revenue attribution."""
    try:
        date_from, date_to = _default_dates(date_from, date_to)
        rows = _latest_snapshot_query(
            db, date_from, date_to, campaign_type, branch_name=branch_name
        ).all()
        rows = sorted(rows, key=lambda r: int(r.total_sent or 0), reverse=True)
        return _envelope([_row_to_dict(r) for r in rows])
    except Exception as e:
        logger.exception("email_by_campaign failed")
        return {"success": False, "data": None, "error": str(e),
                "timestamp": datetime.now(timezone.utc).isoformat()}


@router.get("/by-workflow")
def email_by_workflow(
    date_from: Optional[date] = Query(None),
    date_to: Optional[date] = Query(None),
    campaign_type: Optional[str] = Query(None),
    branch_name: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    """Alias for /by-campaign (legacy)."""
    return email_by_campaign(date_from, date_to, campaign_type, branch_name, db)


# ── By Campaign Name (grouped — totals across branches + branch breakdown) ───

@router.get("/by-campaign-name")
def email_by_campaign_name(
    date_from: Optional[date] = Query(None),
    date_to: Optional[date] = Query(None),
    campaign_type: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    """Group by workflow_name. Each campaign returns aggregate totals + a
    per-branch breakdown array. Useful when the same campaign exists across
    multiple GHL locations (one workflow_id per branch)."""
    try:
        date_from, date_to = _default_dates(date_from, date_to)
        rows = _latest_snapshot_query(db, date_from, date_to, campaign_type).all()

        groups: dict[str, dict] = {}
        for r in rows:
            name = r.workflow_name or r.workflow_id
            if name not in groups:
                groups[name] = {
                    "workflow_name": name,
                    "campaign_type": r.campaign_type,
                    "branches": [],
                }
            groups[name]["branches"].append(_row_to_dict(r))

        result = []
        for name, g in groups.items():
            branches = g["branches"]
            sent = sum(b["sent"] for b in branches)
            delivered = sum(b["delivered"] for b in branches)
            opened = sum(b["unique_opened"] for b in branches)
            clicked = sum(b["unique_clicked"] for b in branches)
            bounced = sum(b["bounced"] for b in branches)
            unsub = sum(b["unsubscribed"] for b in branches)
            bookings = sum(b["attributed_bookings"] for b in branches)
            canceled = sum(b["attributed_canceled"] for b in branches)
            nights = sum(b["attributed_nights"] for b in branches)
            revenue_vnd = sum(b["attributed_revenue_vnd"] for b in branches)
            result.append({
                "workflow_name": name,
                "campaign_type": g["campaign_type"],
                "sent": sent,
                "delivered": delivered,
                "unique_opened": opened,
                "unique_clicked": clicked,
                "bounced": bounced,
                "unsubscribed": unsub,
                "open_rate": round(opened / sent, 4) if sent > 0 else 0,
                "click_rate": round(clicked / sent, 4) if sent > 0 else 0,
                "bounce_rate": round(bounced / sent, 4) if sent > 0 else 0,
                "unsubscribe_rate": round(unsub / sent, 4) if sent > 0 else 0,
                "attributed_bookings": bookings,
                "attributed_canceled": canceled,
                "attributed_nights": nights,
                "attributed_revenue_vnd": round(revenue_vnd, 2),
                "revenue_per_email": round(revenue_vnd / sent, 2) if sent > 0 else 0,
                "booking_rate": round(bookings / sent, 6) if sent > 0 else 0,
                "branches": branches,
            })

        result.sort(key=lambda x: x["sent"], reverse=True)
        return _envelope(result)
    except Exception as e:
        logger.exception("email_by_campaign_name failed")
        return {"success": False, "data": None, "error": str(e),
                "timestamp": datetime.now(timezone.utc).isoformat()}


# ── Campaign Detail (one workflow_name across branches) ──────────────────────

@router.get("/campaign-detail")
def email_campaign_detail(
    workflow_name: str = Query(..., description="Workflow name, e.g. 'April 2026'"),
    date_from: Optional[date] = Query(None),
    date_to: Optional[date] = Query(None),
    db: Session = Depends(get_db),
):
    """Per-branch breakdown of a single workflow_name (matches across
    branches' independent workflow_ids)."""
    try:
        date_from, date_to = _default_dates(date_from, date_to)
        # Pre-filter inner subquery by workflow_name via join
        q = _latest_snapshot_query(db, date_from, date_to).filter(
            EmailCampaignStats.workflow_name == workflow_name
        )
        rows = q.all()
        if not rows:
            return _envelope({"workflow_name": workflow_name, "branches": []})

        branches = [_row_to_dict(r) for r in rows]
        branches.sort(key=lambda b: b["attributed_revenue_vnd"], reverse=True)
        return _envelope({
            "workflow_name": workflow_name,
            "branches": branches,
        })
    except Exception as e:
        logger.exception("email_campaign_detail failed")
        return {"success": False, "data": None, "error": str(e),
                "timestamp": datetime.now(timezone.utc).isoformat()}


# ── GHL API Sync ─────────────────────────────────────────────────────────────

@router.post("/sync-ghl")
def sync_from_ghl(
    secret: str = Query(""),
    db: Session = Depends(get_db),
):
    """Manually trigger GHL email stats sync (workflows + bulk + CRM attribution)."""
    try:
        if settings.GHL_WEBHOOK_SECRET and secret != settings.GHL_WEBHOOK_SECRET:
            raise HTTPException(status_code=401, detail="Invalid secret")

        from app.services.ghl_email_sync import sync_ghl_email_stats
        count = sync_ghl_email_stats(db)
        return _envelope({"items_synced": count})
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("GHL sync failed")
        return {"success": False, "data": None, "error": str(e),
                "timestamp": datetime.now(timezone.utc).isoformat()}
