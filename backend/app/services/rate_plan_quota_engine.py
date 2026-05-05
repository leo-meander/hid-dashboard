"""Rate plan quota engine — fresh count + threshold alerts.

Cron runs every 30 min and calls `evaluate_quotas`:

1. Trigger an incremental Cloudbeds sync (modified-only, 2-day lookback) for
   each in-scope branch so the local DB reflects bookings made in the last
   ~30 min before we count. Daily full sync still runs at 02:00; this just
   keeps the count fresh between syncs.

2. For each active quota, count active vs canceled bookings whose
   rate_plan_name OR room_type match the quota pattern. Status row holds
   per-branch breakdown so the dashboard can show which branch is driving
   the consumption.

3. If consumed_pct >= alert_threshold_pct AND we just crossed into a higher
   alert bucket (90 / 95 / 100), send a single email and bump
   last_alert_bucket. Holding inside a bucket does NOT re-email — keeps the
   inbox quiet between threshold transitions.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Iterable

from sqlalchemy import or_, func
from sqlalchemy.orm import Session

from app.config import settings
from app.models.branch import Branch
from app.models.rate_plan_quota import RatePlanQuota, RatePlanQuotaStatus
from app.models.reservation import Reservation
from app.services.cloudbeds import sync_branch
from app.services.email_sender import send_email_html

logger = logging.getLogger(__name__)

# Bucket ladder — only re-send email when count crosses INTO a new bucket.
ALERT_BUCKETS = (90, 95, 100)

# Cancellation status values (case-insensitive). Cloudbeds can return either.
CANCELLED_STATUSES = ("canceled", "cancelled")


# ── Branch scoping ───────────────────────────────────────────────────────────

def _scope_branches(db: Session, quota: RatePlanQuota) -> list[Branch]:
    """Resolve the branch list for one quota.

    'all_excl_oani' covers the user's primary requirement (Saigon, Taipei,
    1948, Osaka — Oani is excluded per product decision). 'specific' lets a
    future quota target a single branch.
    """
    q = db.query(Branch).filter_by(is_active=True)
    if quota.branch_scope == "specific" and quota.branch_ids:
        ids = [str(b) for b in quota.branch_ids]
        return q.filter(Branch.id.in_(ids)).all()
    # default: all_excl_oani
    return [b for b in q.all() if (b.name or "").strip().lower() != "oani"]


# ── Counting ─────────────────────────────────────────────────────────────────

def _count_for_quota(
    db: Session, quota: RatePlanQuota, branches: list[Branch]
) -> tuple[int, int, list[dict]]:
    """Return (active, canceled, by_branch_breakdown).

    Match rule mirrors marketing_activity._crm_filter — rate_plan_name OR
    room_type ILIKE %pattern%. The pattern is the quota's rate_plan_name
    field treated as a substring (so 'CRM_June 2026 Events' picks up both
    rows with rate_plan_name set and rows where Cloudbeds packed the rate
    plan into the room_type parens).
    """
    pattern = f"%{quota.rate_plan_name}%"
    branch_ids = [b.id for b in branches]
    if not branch_ids:
        return 0, 0, []

    # One grouped query — branch_id + canceled flag → count. Avoids issuing
    # 2*N queries for the breakdown.
    is_canceled = func.lower(func.coalesce(Reservation.status, "")).in_(
        CANCELLED_STATUSES
    )
    rows = (
        db.query(
            Reservation.branch_id,
            is_canceled.label("canceled"),
            func.count(Reservation.id).label("n"),
        )
        .filter(
            Reservation.branch_id.in_(branch_ids),
            or_(
                Reservation.rate_plan_name.ilike(pattern),
                Reservation.room_type.ilike(pattern),
            ),
        )
        .group_by(Reservation.branch_id, is_canceled)
        .all()
    )

    name_by_id = {b.id: b.name for b in branches}
    per_branch: dict[str, dict] = {}
    total_active = total_canceled = 0
    for branch_id, canceled, n in rows:
        key = str(branch_id)
        bucket = per_branch.setdefault(
            key,
            {
                "branch_id": key,
                "branch_name": name_by_id.get(branch_id, "?"),
                "active": 0,
                "canceled": 0,
            },
        )
        if canceled:
            bucket["canceled"] = int(n)
            total_canceled += int(n)
        else:
            bucket["active"] = int(n)
            total_active += int(n)

    # Include zero-count branches so the dashboard table doesn't drop them
    # silently (a branch contributing 0 is still a meaningful data point).
    for b in branches:
        per_branch.setdefault(
            str(b.id),
            {"branch_id": str(b.id), "branch_name": b.name, "active": 0, "canceled": 0},
        )

    breakdown = sorted(
        per_branch.values(), key=lambda r: r["active"], reverse=True
    )
    return total_active, total_canceled, breakdown


# ── Alert bucket logic ───────────────────────────────────────────────────────

def _current_bucket(consumed_pct: float, threshold_pct: float) -> int:
    """Return the highest reached bucket, or 0 if below threshold.

    Buckets are 90/95/100. The threshold is the floor — buckets below the
    user's configured threshold are ignored. So a quota with threshold=95
    will only ever fire 95 or 100, never 90.
    """
    reached = 0
    for b in ALERT_BUCKETS:
        if consumed_pct >= b and b >= threshold_pct:
            reached = b
    return reached


# ── Email ────────────────────────────────────────────────────────────────────

def _send_alert_email(
    quota: RatePlanQuota,
    active: int,
    canceled: int,
    consumed_pct: float,
    bucket: int,
    breakdown: list[dict],
) -> bool:
    recipients = [
        e.strip()
        for e in (settings.EMAIL_RECIPIENTS or "").split(",")
        if e.strip()
    ]
    if not recipients or not quota.notify_email:
        return False

    label = quota.display_name or quota.rate_plan_name
    color = "#DC2626" if bucket >= 100 else "#D97706"
    title_emoji = "🚨" if bucket >= 100 else "⚠️"
    headline = (
        f"{label} reached {bucket}% of cap"
        if bucket < 100
        else f"{label} hit 100% of cap"
    )

    rows_html = ""
    for r in breakdown:
        rows_html += f"""
        <tr>
          <td style="padding: 8px 12px; border: 1px solid #E5E7EB; font-size: 13px;">{r['branch_name']}</td>
          <td style="padding: 8px 12px; border: 1px solid #E5E7EB; font-size: 13px; text-align: right;">{r['active']}</td>
          <td style="padding: 8px 12px; border: 1px solid #E5E7EB; font-size: 13px; text-align: right; color: #9CA3AF;">{r['canceled']}</td>
        </tr>"""

    dashboard_url = (
        f"{settings.FRONTEND_URL}/rate-plan-quotas"
        if settings.FRONTEND_URL
        else "#"
    )

    html = f"""
    <div style="font-family: Arial, sans-serif; max-width: 720px; margin: 0 auto; padding: 20px;">
      <div style="background: {color}; color: white; padding: 16px 24px; border-radius: 8px 8px 0 0;">
        <h2 style="margin: 0; font-size: 18px;">{title_emoji} HiD — Rate Plan Quota Alert</h2>
        <p style="margin: 4px 0 0; font-size: 13px; opacity: 0.9;">{headline}</p>
      </div>
      <div style="border: 1px solid #E5E7EB; border-top: none; padding: 24px; border-radius: 0 0 8px 8px;">
        <table style="width: 100%; border-collapse: collapse; margin-bottom: 16px;">
          <tr>
            <td style="padding: 6px 0; font-size: 13px; color: #6B7280;">Rate Plan</td>
            <td style="padding: 6px 0; font-size: 13px; font-weight: 600; text-align: right;">{label}</td>
          </tr>
          <tr>
            <td style="padding: 6px 0; font-size: 13px; color: #6B7280;">Active Bookings</td>
            <td style="padding: 6px 0; font-size: 18px; font-weight: 700; text-align: right; color: {color};">
              {active} / {quota.limit_count}
            </td>
          </tr>
          <tr>
            <td style="padding: 6px 0; font-size: 13px; color: #6B7280;">Consumed</td>
            <td style="padding: 6px 0; font-size: 13px; font-weight: 600; text-align: right;">{consumed_pct:.1f}%</td>
          </tr>
          <tr>
            <td style="padding: 6px 0; font-size: 13px; color: #6B7280;">Canceled (reference only)</td>
            <td style="padding: 6px 0; font-size: 13px; text-align: right; color: #9CA3AF;">{canceled}</td>
          </tr>
        </table>

        <table style="width: 100%; border-collapse: collapse; margin-bottom: 20px;">
          <thead>
            <tr style="background: #F9FAFB;">
              <th style="padding: 8px 12px; border: 1px solid #E5E7EB; text-align: left; font-size: 12px; color: #6B7280;">Branch</th>
              <th style="padding: 8px 12px; border: 1px solid #E5E7EB; text-align: right; font-size: 12px; color: #6B7280;">Active</th>
              <th style="padding: 8px 12px; border: 1px solid #E5E7EB; text-align: right; font-size: 12px; color: #6B7280;">Canceled</th>
            </tr>
          </thead>
          <tbody>{rows_html}</tbody>
        </table>

        <div style="text-align: center; margin: 24px 0;">
          <a href="{dashboard_url}"
             style="display: inline-block; background: #4F46E5; color: white; padding: 12px 32px;
                    border-radius: 6px; text-decoration: none; font-weight: 600; font-size: 14px;">
            Open Quota Dashboard
          </a>
        </div>

        <p style="margin: 16px 0 0; font-size: 11px; color: #9CA3AF; text-align: center;">
          Automated alert from HiD. You will receive at most one email per threshold bucket
          (90% / 95% / 100%) until the count crosses into the next bucket.
        </p>
      </div>
    </div>
    """

    subject = f"[HiD] {label} — {bucket}% of cap reached ({active}/{quota.limit_count})"
    ok = send_email_html(subject, html, recipients)
    if ok:
        logger.info(
            "Quota alert email sent: quota=%s bucket=%d active=%d/%d",
            quota.rate_plan_name, bucket, active, quota.limit_count,
        )
    else:
        logger.error("Quota alert email send failed for %s", quota.rate_plan_name)
    return ok


# ── Sync helper ──────────────────────────────────────────────────────────────

def _refresh_branches_from_cloudbeds(branches: Iterable[Branch]) -> None:
    """Pull modified-only reservations (last 2 days) for each branch.

    Reuses sync_branch with incremental=True — same pattern the daily
    GitHub Actions cron uses, just with a tighter lookback window for speed.
    Errors per branch are logged but don't abort the run; we still want
    other branches' counts to refresh.
    """
    for b in branches:
        pid = b.cloudbeds_property_id
        if not pid:
            continue
        api_key = settings.get_api_key_for_property(str(pid))
        if not api_key:
            continue
        try:
            sync_branch(
                str(b.id), pid, b.currency or "VND",
                api_key=api_key, incremental=True, lookback_days=2,
            )
        except Exception:
            logger.exception("Quota sync — branch %s failed", b.name)


# ── Main entry point ─────────────────────────────────────────────────────────

def evaluate_quotas(session_factory, *, refresh: bool = True) -> dict:
    """Refresh counts for all active quotas and dispatch alerts.

    refresh=False skips the Cloudbeds sync — used by the manual "Refresh
    now" button in the dashboard when the user just wants a re-count from
    DB without paying the API roundtrip again.
    """
    db: Session = session_factory()
    sent_emails = updated = 0
    try:
        quotas = (
            db.query(RatePlanQuota).filter_by(is_active=True).all()
        )
        if not quotas:
            return {"quotas": 0, "updated": 0, "emails_sent": 0}

        # Collect all in-scope branches across quotas, then sync each once
        # even if multiple quotas share the same branch list.
        scope_per_quota = {q.id: _scope_branches(db, q) for q in quotas}
        if refresh:
            unique = {b.id: b for q in quotas for b in scope_per_quota[q.id]}
            _refresh_branches_from_cloudbeds(unique.values())

        # Recount after sync — counters were updated above.
        for quota in quotas:
            branches = scope_per_quota[quota.id]
            active, canceled, breakdown = _count_for_quota(db, quota, branches)
            consumed_pct = (
                (active / quota.limit_count) * 100
                if quota.limit_count > 0
                else 0
            )

            status = quota.status or RatePlanQuotaStatus(quota_id=quota.id)
            status.active_count = active
            status.canceled_count = canceled
            status.consumed_pct = round(consumed_pct, 2)
            status.by_branch = breakdown
            status.evaluated_at = datetime.now(timezone.utc)

            threshold = float(quota.alert_threshold_pct or 90)
            new_bucket = _current_bucket(consumed_pct, threshold)
            prev_bucket = int(status.last_alert_bucket or 0)

            if new_bucket > prev_bucket:
                if _send_alert_email(
                    quota, active, canceled, consumed_pct, new_bucket, breakdown
                ):
                    status.last_alert_bucket = new_bucket
                    status.last_alerted_at = datetime.now(timezone.utc)
                    sent_emails += 1
            elif new_bucket == 0 and prev_bucket > 0 and consumed_pct < threshold:
                # Count fell back below threshold (cancellations) — reset
                # bucket so a future climb re-fires alerts.
                status.last_alert_bucket = 0

            if quota.status is None:
                db.add(status)
            updated += 1

        db.commit()
        logger.info(
            "Quota evaluation complete — %d quotas, %d emails sent",
            len(quotas), sent_emails,
        )
        return {"quotas": len(quotas), "updated": updated, "emails_sent": sent_emails}
    except Exception:
        db.rollback()
        logger.exception("Quota evaluation failed")
        raise
    finally:
        db.close()
