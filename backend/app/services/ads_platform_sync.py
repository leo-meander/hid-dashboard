"""Ads Platform sync job — pulls everything from the unified API into local DB.

Flow (one invocation = one full snapshot of the requested window):

1. ``/api/export/accounts``  → map account_id → (branch, platform, currency)
2. ``/api/export/angles``    → mirror into local ``ad_angles`` via ``external_angle_id``
3. ``/api/export/campaigns`` (per platform) → in-memory campaign map
4. ``/api/export/ads``       → upsert ``ads_performance`` with ``grain='ad'``
5. ``/api/export/spend/daily`` (per branch × platform) → upsert ``ads_performance``
   with ``grain='daily'`` (this is the table SUMmed for KPI aggregates)
6. ``/api/export/booking-matches`` (paginated) → upsert ``ads_booking_matches``
7. ``/api/export/budget/monthly`` → upsert ``ads_budgets``

Creative-library endpoints (/combos /copies /materials /spy-ads) are pulled
separately by a future Phase 4 mirror job; they are not part of this flow.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.models.ads import AdsPerformance
from app.models.ads_booking_match import AdsBookingMatch
from app.models.ads_budget import AdsBudget
from app.models.angle import AdAngle
from app.models.branch import Branch
from app.services.ads_platform import (
    AdsPlatformClient,
    branch_slug_for,
    get_client,
    platform_to_channel,
)
from app.services.currency import fetch_rate

logger = logging.getLogger(__name__)

DEFAULT_LOOKBACK_DAYS = 14

# Hardcoded fallbacks used when the live FX API can't be reached.
# Same values as ghl_email_sync — keep in sync if either changes.
FX_FALLBACK_TO_VND = {"VND": 1.0, "TWD": 830.0, "JPY": 165.0}


# ── Helpers ──────────────────────────────────────────────────────────────────


def _iso(d: date | str) -> str:
    return d.isoformat() if isinstance(d, date) else str(d)


def _parse_date(val: Any) -> Optional[date]:
    if isinstance(val, date):
        return val
    if not val:
        return None
    s = str(val).strip()
    if not s:
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def _build_rate_map(branches: list[Branch]) -> dict[str, float]:
    """Pre-fetch native→VND rates for every currency used by active branches.

    Falls back to ``FX_FALLBACK_TO_VND`` when the live API can't be reached —
    a 0.0 fallback (the previous behaviour) zeroed every cost_vnd for TWD/JPY
    branches and broke Budget Planner / Marketing Activity totals.
    """
    rates: dict[str, float] = {"VND": 1.0}
    for b in branches:
        cur = (b.currency or "VND").upper()
        if cur in rates:
            continue
        rate = asyncio.run(fetch_rate(cur, "VND"))
        if rate is None:
            fallback = FX_FALLBACK_TO_VND.get(cur)
            if fallback is None:
                logger.warning(
                    "No exchange rate for %s → VND and no hardcoded fallback; "
                    "leaving *_vnd NULL", cur,
                )
                rates[cur] = 0.0
            else:
                logger.warning(
                    "FX API unavailable for %s → VND; using fallback rate %s",
                    cur, fallback,
                )
                rates[cur] = fallback
        else:
            rates[cur] = rate
    return rates


def _to_vnd(amount: Optional[float], currency: str, rate_map: dict[str, float]) -> Optional[float]:
    if amount is None:
        return None
    rate = rate_map.get((currency or "VND").upper())
    if not rate:
        return None
    return round(float(amount) * rate, 2)


def _from_vnd_to_native(
    amount_vnd: Optional[float], currency: str, rate_map: dict[str, float]
) -> Optional[float]:
    """Convert a VND amount back to the account's native currency.

    Upstream Ads Platform returns ``spend`` and ``revenue`` already in VND
    (master currency) for every account, regardless of the account's local
    currency. To populate ``cost_native`` we have to divide by the same FX
    rate we'd use to go the other way.
    """
    if amount_vnd is None:
        return None
    cur = (currency or "VND").upper()
    if cur == "VND":
        return float(amount_vnd)
    rate = rate_map.get(cur)
    if not rate:
        return None
    return round(float(amount_vnd) / rate, 2)


# ── Per-step syncers ─────────────────────────────────────────────────────────


def _sync_angles(
    db: Session, client: AdsPlatformClient, branch_by_id: dict[str, Branch]
) -> int:
    """Mirror Ads Platform angles into local ``ad_angles`` via ``external_angle_id``."""
    try:
        upstream = client.get_angles()
    except Exception as exc:
        logger.warning("get_angles failed: %s — skipping angle mirror", exc)
        return 0

    count = 0
    for row in upstream:
        ext_id = str(row.get("id") or row.get("angle_id") or "").strip()
        if not ext_id:
            continue
        angle = db.query(AdAngle).filter_by(external_angle_id=ext_id).first()
        branch_id = row.get("branch_id")
        branch = branch_by_id.get(str(branch_id)) if branch_id else None
        fields = {
            "name": row.get("name") or row.get("angle_name") or ext_id,
            "hook_type": row.get("hook_type"),
            "status": row.get("status"),
            "description": row.get("description"),
            "branch_id": branch.id if branch else None,
        }
        if angle:
            for k, v in fields.items():
                if v is not None:
                    setattr(angle, k, v)
        else:
            angle = AdAngle(external_angle_id=ext_id, **fields)
            db.add(angle)
        count += 1
    db.flush()
    return count


def _sync_ads(
    db: Session,
    client: AdsPlatformClient,
    account_map: dict[str, dict],
    angle_map: dict[str, str],
    campaign_map: dict[str, dict],
    rate_map: dict[str, float],
) -> int:
    """Upsert one ``grain='ad'`` row per Ads Platform ad (metadata only)."""
    count = 0
    try:
        ads = list(client.get_ads())
    except Exception as exc:
        logger.warning("get_ads failed: %s", exc)
        return 0

    # Upstream occasionally yields the same ad_id twice across pages; the
    # session has autoflush=False so a duplicate would race a pending INSERT
    # and trip the partial unique index ux_ads_performance_ad_external.
    # Track ids we've already handled in this run.
    seen: set[str] = set()
    for ad in ads:
        ext_ad_id = str(ad.get("id") or ad.get("ad_id") or "").strip()
        if not ext_ad_id or ext_ad_id in seen:
            continue
        seen.add(ext_ad_id)
        ext_campaign_id = str(ad.get("campaign_id") or "").strip() or None
        campaign_meta = campaign_map.get(ext_campaign_id, {}) if ext_campaign_id else {}
        account_id = ad.get("account_id") or campaign_meta.get("account_id")
        account = account_map.get(str(account_id)) if account_id else None
        if not account:
            continue  # unknown account → can't place into any branch

        branch = account["branch"]
        if not branch:
            continue  # account exists but slug doesn't match any local branch
        channel = platform_to_channel(account.get("platform")) or "Meta"

        ext_angle_id = ad.get("angle_id") or campaign_meta.get("angle_id")
        ad_angle_id = angle_map.get(str(ext_angle_id)) if ext_angle_id else None

        row = (
            db.query(AdsPerformance)
            .filter_by(grain="ad", external_ad_id=ext_ad_id)
            .first()
        )
        values = dict(
            branch_id=branch.id,
            external_ad_id=ext_ad_id,
            external_campaign_id=ext_campaign_id,
            account_id=str(account_id) if account_id else None,
            data_source="AdsPlatform",
            grain="ad",
            channel=channel,
            ad_name=ad.get("name") or ad.get("ad_name"),
            campaign_name=ad.get("campaign_name") or campaign_meta.get("name"),
            adset_name=ad.get("adset_name") or ad.get("ad_group_name"),
            ad_body=ad.get("body") or ad.get("primary_text"),
            target_country=ad.get("target_country") or campaign_meta.get("target_country"),
            target_audience=ad.get("target_audience") or campaign_meta.get("target_audience"),
            funnel_stage=ad.get("funnel_stage") or campaign_meta.get("funnel_stage"),
            pic=ad.get("pic") or campaign_meta.get("pic"),
            ad_angle_id=ad_angle_id,
            # Preserve legacy upsert key for verdict_sync lookups
            meta_ad_id=ext_ad_id if channel == "Meta" else None,
            meta_campaign_id=ext_campaign_id if channel == "Meta" else None,
        )
        if row:
            for k, v in values.items():
                if v is not None:
                    setattr(row, k, v)
        else:
            db.add(AdsPerformance(**values))
        count += 1
    db.flush()
    return count


def _sync_spend_daily(
    db: Session,
    client: AdsPlatformClient,
    branches: list[Branch],
    date_from: date,
    date_to: date,
    rate_map: dict[str, float],
) -> int:
    """Upsert one ``grain='daily'`` row per (branch, channel, date, account)."""
    count = 0
    df_iso, dt_iso = _iso(date_from), _iso(date_to)
    for branch in branches:
        currency = (branch.currency or "VND").upper()
        slug = branch_slug_for(branch)
        for platform in ("meta", "google", "tiktok"):
            try:
                rows = client.get_spend_daily(
                    df_iso, dt_iso, platform=platform, branch=slug
                )
            except Exception as exc:
                logger.warning(
                    "spend/daily FAIL branch=%s platform=%s: %s",
                    branch.name, platform, exc,
                )
                continue
            channel = platform_to_channel(platform)
            for r in rows or []:
                row_date = _parse_date(r.get("date"))
                if not row_date:
                    continue
                account_id = str(r.get("account_id") or "").strip() or None
                cost = r.get("spend")
                revenue = r.get("revenue")
                existing = (
                    db.query(AdsPerformance)
                    .filter_by(
                        grain="daily",
                        branch_id=branch.id,
                        channel=channel,
                        date_from=row_date,
                        account_id=account_id,
                    )
                    .first()
                )
                # Upstream returns spend/revenue already in VND (master) — store
                # that as cost_vnd directly and derive cost_native by dividing
                # by the branch's FX rate.
                values = dict(
                    branch_id=branch.id,
                    grain="daily",
                    data_source="AdsPlatform",
                    channel=channel,
                    account_id=account_id,
                    date_from=row_date,
                    date_to=row_date,
                    cost_native=_from_vnd_to_native(cost, currency, rate_map),
                    cost_vnd=cost,
                    impressions=r.get("impressions"),
                    clicks=r.get("clicks"),
                    bookings=r.get("conversions"),
                    revenue_native=_from_vnd_to_native(revenue, currency, rate_map),
                    revenue_vnd=revenue,
                )
                if existing:
                    for k, v in values.items():
                        setattr(existing, k, v)
                else:
                    db.add(AdsPerformance(**values))
                count += 1
    db.flush()
    return count


def _sync_booking_matches(
    db: Session,
    client: AdsPlatformClient,
    date_from: date,
    date_to: date,
    branch_slug_map: dict[str, Branch],
    rate_map: dict[str, float],
) -> int:
    count = 0
    try:
        it = client.get_booking_matches(_iso(date_from), _iso(date_to))
    except Exception as exc:
        logger.warning("booking-matches failed: %s", exc)
        return 0
    for m in it:
        ext_id = str(m.get("id") or m.get("match_id") or "").strip()
        if not ext_id:
            continue
        branch = branch_slug_map.get(str(m.get("branch") or "").lower().strip())
        if not branch:
            continue
        currency = (m.get("currency") or branch.currency or "VND").upper()
        revenue_native = m.get("revenue") or m.get("revenue_native")
        row = (
            db.query(AdsBookingMatch)
            .filter_by(external_match_id=ext_id)
            .first()
        )
        values = dict(
            external_match_id=ext_id,
            branch_id=branch.id,
            channel=platform_to_channel(m.get("channel") or m.get("platform")),
            match_result=m.get("match_result"),
            purchase_kind=m.get("purchase_kind"),
            booking_date=_parse_date(m.get("booking_date")),
            match_date=_parse_date(m.get("match_date")),
            revenue_native=revenue_native,
            revenue_vnd=_to_vnd(revenue_native, currency, rate_map),
            currency=currency,
            reservation_ref=m.get("reservation_ref") or m.get("reservation_id"),
            external_ad_id=m.get("ad_id"),
            external_campaign_id=m.get("campaign_id"),
        )
        if row:
            for k, v in values.items():
                setattr(row, k, v)
        else:
            db.add(AdsBookingMatch(**values))
        count += 1
    db.flush()
    return count


def _sync_budget(
    db: Session,
    client: AdsPlatformClient,
    month: str,
    branch_slug_map: dict[str, Branch],
    rate_map: dict[str, float],
) -> int:
    count = 0
    try:
        payload = client.get_budget_monthly(month)
    except Exception as exc:
        logger.warning("budget/monthly failed: %s", exc)
        return 0
    plans = (payload or {}).get("plans") or []
    for p in plans:
        ext_id = str(p.get("id") or "").strip()
        if not ext_id:
            continue
        branch = branch_slug_map.get(str(p.get("branch") or "").lower().strip())
        if not branch:
            continue
        currency = (p.get("currency") or branch.currency or "VND").upper()
        total_native = p.get("total_budget")
        channel = platform_to_channel(p.get("channel"))
        row = (
            db.query(AdsBudget)
            .filter_by(
                branch_id=branch.id,
                month=month,
                channel=channel,
                external_plan_id=ext_id,
            )
            .first()
        )
        values = dict(
            branch_id=branch.id,
            external_plan_id=ext_id,
            month=month,
            plan_name=p.get("name"),
            channel=channel,
            total_budget_native=total_native,
            total_budget_vnd=_to_vnd(total_native, currency, rate_map),
            currency=currency,
        )
        if row:
            for k, v in values.items():
                setattr(row, k, v)
        else:
            db.add(AdsBudget(**values))
        count += 1
    db.flush()
    return count


# ── Entry point ──────────────────────────────────────────────────────────────


def run_ads_platform_sync(
    db: Session,
    *,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    month: Optional[str] = None,
) -> dict:
    """Full snapshot sync. Caller owns ``db.commit()``."""
    started = datetime.utcnow()
    client = get_client()

    today = date.today()
    dt = date_to or today
    df = date_from or (dt - timedelta(days=DEFAULT_LOOKBACK_DAYS))
    billing_month = month or f"{dt.year}-{dt.month:02d}"

    branches = db.query(Branch).filter_by(is_active=True).all()
    rate_map = _build_rate_map(branches)
    branch_by_id = {str(b.id): b for b in branches}
    branch_slug_map = {branch_slug_for(b).lower(): b for b in branches}

    # 1. Accounts
    accounts = client.get_accounts()
    account_map: dict[str, dict] = {}
    for acc in accounts:
        acc_id = str(acc.get("id") or acc.get("account_id") or "").strip()
        if not acc_id:
            continue
        branch = branch_slug_map.get(
            str(acc.get("branch") or "").lower().strip()
        )
        account_map[acc_id] = {
            "branch": branch,
            "platform": acc.get("platform"),
            "currency": acc.get("currency"),
        }

    # 2. Angles
    angles_synced = _sync_angles(db, client, branch_by_id)
    angle_map: dict[str, str] = {
        a.external_angle_id: str(a.id)
        for a in db.query(AdAngle).filter(AdAngle.external_angle_id.isnot(None)).all()
    }

    # 3. Campaigns (in-memory map only — no DB write)
    campaign_map: dict[str, dict] = {}
    for platform in ("meta", "google", "tiktok"):
        try:
            for c in client.get_campaigns(platform=platform):
                cid = str(c.get("id") or c.get("campaign_id") or "").strip()
                if cid:
                    campaign_map[cid] = c
        except Exception as exc:
            logger.warning("campaigns platform=%s failed: %s", platform, exc)

    # 4. Ads
    ads_synced = _sync_ads(db, client, account_map, angle_map, campaign_map, rate_map)

    # 5. Daily spend (authoritative aggregate)
    daily_synced = _sync_spend_daily(db, client, branches, df, dt, rate_map)

    # 6. Booking matches
    matches_synced = _sync_booking_matches(
        db, client, df, dt, branch_slug_map, rate_map
    )

    # 7. Booking matches summary (read-only reconcile)
    try:
        summary = client.get_booking_matches_summary(_iso(df), _iso(dt))
    except Exception as exc:
        logger.warning("booking-matches/summary failed: %s", exc)
        summary = {}

    # 8. Budget
    budgets_synced = _sync_budget(
        db, client, billing_month, branch_slug_map, rate_map
    )

    duration_s = (datetime.utcnow() - started).total_seconds()
    return {
        "date_from": _iso(df),
        "date_to": _iso(dt),
        "month": billing_month,
        "synced_accounts": len(accounts),
        "synced_angles": angles_synced,
        "synced_campaigns": len(campaign_map),
        "synced_ads": ads_synced,
        "synced_daily_rows": daily_synced,
        "synced_booking_matches": matches_synced,
        "synced_budgets": budgets_synced,
        "match_summary": summary,
        "duration_s": round(duration_s, 2),
    }
