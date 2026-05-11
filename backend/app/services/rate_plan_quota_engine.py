"""Rate plan quota engine — per-branch fresh count + threshold alerts.

Cron runs every 30 min and calls `evaluate_quotas`:

1. Trigger an incremental Cloudbeds sync (modified-only, 2-day lookback) for
   each in-scope branch so the local DB reflects bookings made in the last
   ~30 min before we count. Daily full sync still runs at 02:00; this just
   keeps the count fresh between syncs.

2. For each active quota, count active vs canceled bookings whose
   rate_plan_name OR room_type match the quota pattern, broken down per
   in-scope branch. Each branch is compared against ITS OWN cap from
   quota.branch_limits — caps are no longer shared across branches.

3. For each branch independently, if consumed_pct >= alert_threshold_pct
   AND we just crossed into a higher bucket (90 / 95 / 100), include it in
   one digest email per quota and bump its bucket in last_alert_buckets.
   Branches sitting inside the same bucket they were already alerted on do
   NOT re-trigger — keeps the inbox quiet between transitions.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Iterable

from sqlalchemy import or_, func
from sqlalchemy.orm import Session

from app.config import settings
from app.models.branch import Branch
from app.models.rate_plan_quota import RatePlanQuota, RatePlanQuotaStatus
from app.models.reservation import Reservation
from app.services.cloudbeds import backfill_room_type_and_rate_plan, sync_branch
from app.services.email_sender import send_email_html

logger = logging.getLogger(__name__)

# Bucket ladder — only re-send email when count crosses INTO a new bucket.
ALERT_BUCKETS = (90, 95, 100)

# Cancellation status values (case-insensitive). Cloudbeds can return either.
CANCELLED_STATUSES = ("canceled", "cancelled")


# ── Branch scoping ───────────────────────────────────────────────────────────

def _scope_branches(db: Session, quota: RatePlanQuota) -> list[Branch]:
    """Resolve the in-scope branches for a quota.

    Keys of branch_limits define scope. Inactive branches are excluded so a
    deactivated branch stops counting without needing to edit each quota.
    """
    limits = quota.branch_limits or {}
    if not limits:
        return []
    ids = list(limits.keys())
    return (
        db.query(Branch)
        .filter(Branch.id.in_(ids), Branch.is_active.is_(True))
        .all()
    )


# ── Counting ─────────────────────────────────────────────────────────────────

def _count_for_quota(
    db: Session, quota: RatePlanQuota, branches: list[Branch]
) -> tuple[int, int, list[dict]]:
    """Return (total_active, total_canceled, per_branch_breakdown).

    Match rule mirrors marketing_activity._crm_filter — rate_plan_name OR
    room_type ILIKE %pattern%. Each breakdown row carries its own limit
    and consumed_pct so the dashboard and email don't have to re-derive.
    """
    pattern = f"%{quota.rate_plan_name}%"
    branch_ids = [b.id for b in branches]
    if not branch_ids:
        return 0, 0, []

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
    counts: dict[str, dict] = {}
    for branch_id, canceled, n in rows:
        key = str(branch_id)
        bucket = counts.setdefault(
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
        else:
            bucket["active"] = int(n)

    # Include zero-count branches so the dashboard table doesn't drop them
    # silently (a branch contributing 0 is still a meaningful data point).
    limits = quota.branch_limits or {}
    breakdown: list[dict] = []
    total_active = total_canceled = 0
    for b in branches:
        key = str(b.id)
        row = counts.get(
            key,
            {"branch_id": key, "branch_name": b.name, "active": 0, "canceled": 0},
        )
        limit = int(limits.get(key, 0) or 0)
        active = int(row["active"])
        canceled = int(row["canceled"])
        consumed_pct = (active / limit * 100) if limit > 0 else 0
        breakdown.append({
            "branch_id": key,
            "branch_name": row["branch_name"],
            "active": active,
            "canceled": canceled,
            "limit": limit,
            "consumed_pct": round(consumed_pct, 2),
        })
        total_active += active
        total_canceled += canceled

    breakdown.sort(key=lambda r: r["consumed_pct"], reverse=True)
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
    crossed: list[dict],
    breakdown: list[dict],
) -> bool:
    """One digest email per quota per cron tick.

    `crossed` holds the branches that just transitioned into a higher
    bucket (these drive the subject + headline). `breakdown` is the full
    per-branch table so the recipient can see context — including branches
    that didn't cross.
    """
    recipients = [
        e.strip()
        for e in (settings.EMAIL_RECIPIENTS or "").split(",")
        if e.strip()
    ]
    if not recipients or not quota.notify_email or not crossed:
        return False

    label = quota.display_name or quota.rate_plan_name
    # Worst bucket among the crossed branches drives color + emoji.
    top_bucket = max(int(c["new_bucket"]) for c in crossed)
    color = "#DC2626" if top_bucket >= 100 else "#D97706"
    title_emoji = "🚨" if top_bucket >= 100 else "⚠️"

    if len(crossed) == 1:
        c = crossed[0]
        headline = (
            f"{c['branch_name']} hit {c['new_bucket']}% of {label} cap"
            f" ({c['active']}/{c['limit']})"
        )
    else:
        names = ", ".join(c["branch_name"] for c in crossed)
        headline = f"{len(crossed)} branches crossed {label} thresholds: {names}"

    crossed_ids = {c["branch_id"] for c in crossed}

    rows_html = ""
    for r in breakdown:
        is_alert = r["branch_id"] in crossed_ids
        row_bg = "#FEF2F2" if is_alert and top_bucket >= 100 else (
            "#FFFBEB" if is_alert else "transparent"
        )
        pct = float(r["consumed_pct"] or 0)
        pct_color = (
            "#DC2626" if pct >= 100 else
            "#D97706" if pct >= 90 else
            "#374151"
        )
        rows_html += f"""
        <tr style="background: {row_bg};">
          <td style="padding: 8px 12px; border: 1px solid #E5E7EB; font-size: 13px;">
            {r['branch_name']}
            {"<strong style='color:" + color + ";'> ●</strong>" if is_alert else ""}
          </td>
          <td style="padding: 8px 12px; border: 1px solid #E5E7EB; font-size: 13px; text-align: right;">
            {r['active']} / {r['limit']}
          </td>
          <td style="padding: 8px 12px; border: 1px solid #E5E7EB; font-size: 13px; text-align: right; color: {pct_color}; font-weight: 600;">
            {pct:.2f}%
          </td>
          <td style="padding: 8px 12px; border: 1px solid #E5E7EB; font-size: 13px; text-align: right; color: #9CA3AF;">
            {r['canceled']}
          </td>
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
        <p style="margin: 0 0 16px; font-size: 13px; color: #374151;">
          Per-branch counts for <strong>{label}</strong>. Branches marked
          <strong style="color:{color};">●</strong> just crossed a new
          alert threshold.
        </p>

        <table style="width: 100%; border-collapse: collapse; margin-bottom: 20px;">
          <thead>
            <tr style="background: #F9FAFB;">
              <th style="padding: 8px 12px; border: 1px solid #E5E7EB; text-align: left; font-size: 12px; color: #6B7280;">Branch</th>
              <th style="padding: 8px 12px; border: 1px solid #E5E7EB; text-align: right; font-size: 12px; color: #6B7280;">Active / Cap</th>
              <th style="padding: 8px 12px; border: 1px solid #E5E7EB; text-align: right; font-size: 12px; color: #6B7280;">Consumed</th>
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
          Automated alert from HiD. Each branch fires at most one email per
          threshold bucket (90% / 95% / 100%) until that branch's count
          crosses into the next bucket.
        </p>
      </div>
    </div>
    """

    if len(crossed) == 1:
        c = crossed[0]
        subject = (
            f"[HiD] {label} — {c['branch_name']} {c['new_bucket']}% of cap "
            f"({c['active']}/{c['limit']})"
        )
    else:
        subject = (
            f"[HiD] {label} — {len(crossed)} branches crossed thresholds"
        )

    ok = send_email_html(subject, html, recipients)
    if ok:
        logger.info(
            "Quota alert sent: quota=%s branches=%s",
            quota.rate_plan_name,
            ",".join(f"{c['branch_name']}@{c['new_bucket']}" for c in crossed),
        )
    else:
        logger.error("Quota alert email send failed for %s", quota.rate_plan_name)
    return ok


# ── Sync helper ──────────────────────────────────────────────────────────────

def _refresh_branches_from_cloudbeds(branches: Iterable[Branch]) -> None:
    """Pull modified-only reservations + backfill per-reservation fields.

    Two-step refresh per branch:

    1. `sync_branch(incremental=True, lookback_days=2)` — bulk
       /getReservations for rows modified in the last 2 days. Catches new
       bookings, cancellations, modifications.

    2. `backfill_room_type_and_rate_plan` — bulk /getReservations does NOT
       return ratePlanNamePublic/Private, roomTypeName, OR balanceDetailed,
       so step 1 leaves brand-new rows with NULL on those fields plus NULL
       grand_total_native. The quota engine matches `rate_plan_name OR
       room_type ILIKE %pattern%` — without this backfill the count silently
       undercounts. The CRM Reservations view also relies on grand_total
       being populated; this is the only cron that fills it for new bookings.
       Bounded by a -14d→+365d check-in window and a per-branch row limit
       so a backlog of historical NULLs doesn't blow past the cron's 5-min
       curl timeout. The backfill prioritises rows touched in the last 2
       hours (uncapped) so booking thứ N+1 just synced in this tick still
       gets filled even when older NULL rows would fill the 150-slot budget.

    Errors per branch are logged but don't abort the run; we still want
    other branches' counts to refresh.
    """
    today = date.today()
    bf_from = today - timedelta(days=14)
    bf_to = today + timedelta(days=365)

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
            continue

        try:
            backfill_room_type_and_rate_plan(
                str(b.id), pid, api_key=api_key,
                currency=b.currency or "VND",
                checkin_from=bf_from, checkin_to=bf_to,
                limit=150,
            )
        except Exception:
            logger.exception(
                "Quota room_type/rate_plan backfill — branch %s failed", b.name,
            )


# ── Main entry point ─────────────────────────────────────────────────────────

def evaluate_quotas(session_factory, *, refresh: bool = True) -> dict:
    """Refresh counts for all active quotas and dispatch per-branch alerts.

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

        # Resolve scope per quota first; sync each unique branch only once
        # even if multiple quotas share the same branch list.
        scope_per_quota = {q.id: _scope_branches(db, q) for q in quotas}
        if refresh:
            unique = {b.id: b for q in quotas for b in scope_per_quota[q.id]}
            _refresh_branches_from_cloudbeds(unique.values())

        for quota in quotas:
            branches = scope_per_quota[quota.id]
            total_active, total_canceled, breakdown = _count_for_quota(
                db, quota, branches
            )
            limits = quota.branch_limits or {}
            total_cap = sum(int(v or 0) for v in limits.values())
            overall_pct = (
                (total_active / total_cap) * 100 if total_cap > 0 else 0
            )

            status = quota.status or RatePlanQuotaStatus(quota_id=quota.id)
            status.active_count = total_active
            status.canceled_count = total_canceled
            status.consumed_pct = round(overall_pct, 2)
            status.evaluated_at = datetime.now(timezone.utc)

            # Per-branch bucket transitions.
            threshold = float(quota.alert_threshold_pct or 90)
            # Copy so we only persist if the email actually sent.
            buckets = dict(status.last_alert_buckets or {})
            crossed: list[dict] = []
            for row in breakdown:
                bid = row["branch_id"]
                new_bucket = _current_bucket(row["consumed_pct"], threshold)
                prev_bucket = int(buckets.get(bid, 0) or 0)
                if new_bucket > prev_bucket:
                    crossed.append({**row, "new_bucket": new_bucket,
                                    "prev_bucket": prev_bucket})
                elif (
                    new_bucket == 0
                    and prev_bucket > 0
                    and row["consumed_pct"] < threshold
                ):
                    # Branch fell back below threshold (cancellations) —
                    # reset its bucket so a future climb re-fires.
                    buckets[bid] = 0
                # Always merge the live bucket onto by_branch so the
                # dashboard can show "currently at 95% bucket".
                row["last_alert_bucket"] = buckets.get(bid, prev_bucket)

            status.by_branch = breakdown

            if crossed:
                if _send_alert_email(quota, crossed, breakdown):
                    for c in crossed:
                        buckets[c["branch_id"]] = c["new_bucket"]
                    status.last_alerted_at = datetime.now(timezone.utc)
                    sent_emails += 1
                    # Refresh by_branch with the now-promoted buckets so the
                    # dashboard reflects them on the next read.
                    for row in status.by_branch:
                        row["last_alert_bucket"] = buckets.get(
                            row["branch_id"], row.get("last_alert_bucket", 0)
                        )

            status.last_alert_buckets = buckets

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
