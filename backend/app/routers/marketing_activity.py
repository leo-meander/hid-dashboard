"""
Marketing Activity router — consolidated view of Paid Ads, KOL, and CRM performance.

Data sources (post migration 028):
  - Paid Ads bookings/revenue: ``ads_booking_matches`` (de-duped by
    ``external_match_id`` — same source the Ads Platform dashboard uses).
    For By-Country breakdown, joined to ``ads_performance(grain='ad')`` via
    ``external_ad_id`` to pick up ``target_country``.
  - Paid Ads cost:             Budget Planner's ActualsCache (Ads Platform
    yearly-plan feed). Not split per-country — daily grain has no country.
  - KOL:                       Cloudbeds reservations (room_type ILIKE '%KOL_%')
  - CRM:                       Cloudbeds reservations (CRM/MEANDER'S FRIEND/
                               Travel guide/Grand Open)

Revenue exclusion: Blogger, House Use, Special Case (non-paying guests)
"""
import calendar
import logging
import re
from datetime import date, datetime, timezone
from typing import Optional
from uuid import UUID
from collections import defaultdict

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, or_, literal_column
from sqlalchemy.orm import Session
from sqlalchemy import text

from app.database import get_db
from app.models.ads import AdsPerformance
from app.models.ads_booking_match import AdsBookingMatch
from app.models.branch import Branch
from app.models.reservation import Reservation
from app.routers.marketing_budget import ActualsCache, _get_rate_to_vnd, _vnd_to_native
from app.services.ads_platform import branch_slug_for, get_client as _get_ads_client

router = APIRouter()
log = logging.getLogger(__name__)

_KOL_RE = re.compile(r"\(KOL_([^)]+)\)")
_CANCELLED = {"canceled", "cancelled", "no_show", "no-show", "cancelled_by_guest"}

# Revenue exclusion: non-paying guests
_EXCLUDED_SOURCES = {"blogger", "house use", "houseuse", "special case"}


def _envelope(data):
    return {
        "success": True, "data": data, "error": None,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _crm_filter():
    return or_(
        Reservation.room_type.ilike("%CRM%"),
        Reservation.rate_plan_name.ilike("%CRM%"),
        Reservation.room_type.ilike("%MEANDER'S FRIEND%"),
        Reservation.rate_plan_name.ilike("%MEANDER'S FRIEND%"),
        Reservation.room_type.ilike("%Travel guide%"),
        Reservation.rate_plan_name.ilike("%Travel guide%"),
        Reservation.room_type.ilike("%Grand Open%"),
        Reservation.rate_plan_name.ilike("%Grand Open%"),
    )


def _revenue_source_filter():
    """Exclude Blogger/House Use/Special Case from revenue queries."""
    return ~func.lower(func.coalesce(Reservation.source, "")).in_(list(_EXCLUDED_SOURCES))


def _status_filter():
    return ~Reservation.status.in_(["Cancelled", "Canceled", "No-Show", "No_Show"])


# ── Ads data readers ─────────────────────────────────────────────────────────


def _fetch_paid_ads_totals(
    db: Session,
    branch_id: Optional[UUID],
    d_from: date,
    d_to: date,
    use_native: bool,
) -> tuple[int, float]:
    """Paid Ads (bookings, revenue) for a month.

    Sums local ``ads_performance(grain='daily')`` rows. Known caveat: this
    aggregate runs ~1.57x higher than the Ads Platform dashboard's totals
    on April 2026 (423 vs 269 conversions, 2.55B vs 1.63B revenue). The
    inflation source is still being investigated — see the
    /api/marketing-activity/debug/paid-ads endpoint for raw API responses.

    Earlier attempts (ads_booking_matches table, live booking-matches API)
    surfaced a different problem: matches sparse / revenue-null in upstream
    data, leaving the page showing 0 revenue. Sticking with the daily
    aggregate keeps numbers visible until the right authoritative source
    is identified.
    """
    rev_col = AdsPerformance.revenue_native if use_native else AdsPerformance.revenue_vnd
    q = db.query(
        func.coalesce(func.sum(AdsPerformance.bookings), 0).label("bookings"),
        func.coalesce(func.sum(rev_col), 0).label("revenue"),
    ).filter(
        AdsPerformance.grain == "daily",
        AdsPerformance.date_from >= d_from,
        AdsPerformance.date_from <= d_to,
    )
    if branch_id is not None:
        q = q.filter(AdsPerformance.branch_id == branch_id)
    row = q.one()
    return int(row.bookings or 0), float(row.revenue or 0)


def _fetch_paid_ads_by_country(
    db: Session,
    branch_id: Optional[UUID],
    d_from: date,
    d_to: date,
    use_native: bool,
) -> dict[str, dict]:
    """Per-country Paid Ads bookings + revenue.

    Joins ``ads_booking_matches`` (booking_date in window) to
    ``ads_performance(grain='ad')`` on ``external_ad_id`` to pick up
    ``target_country``. Bookings without a matching ad row land under
    'Unknown'.
    """
    rev_col = AdsBookingMatch.revenue_native if use_native else AdsBookingMatch.revenue_vnd
    country_expr = func.coalesce(
        func.nullif(func.trim(AdsPerformance.target_country), ""),
        "Unknown",
    ).label("country")

    q = (
        db.query(
            country_expr,
            func.count(AdsBookingMatch.id).label("bookings"),
            func.coalesce(func.sum(rev_col), 0).label("revenue"),
        )
        .outerjoin(
            AdsPerformance,
            (AdsPerformance.external_ad_id == AdsBookingMatch.external_ad_id)
            & (AdsPerformance.grain == "ad"),
        )
        .filter(
            AdsBookingMatch.booking_date >= d_from,
            AdsBookingMatch.booking_date <= d_to,
        )
        .group_by("country")
    )
    if branch_id is not None:
        q = q.filter(AdsBookingMatch.branch_id == branch_id)

    return {
        r.country: {"bookings": int(r.bookings or 0), "revenue": float(r.revenue or 0)}
        for r in q.all()
    }


def _month_range(month_str: str):
    """Given 'YYYY-MM', return (first_day, last_day) as date objects."""
    yr, mo = int(month_str[:4]), int(month_str[5:7])
    first = date(yr, mo, 1)
    last = date(yr, mo, calendar.monthrange(yr, mo)[1])
    return first, last


def _prev_month_str(month_str: str) -> str:
    """Given 'YYYY-MM', return the previous month string."""
    yr, mo = int(month_str[:4]), int(month_str[5:7])
    if mo == 1:
        return f"{yr - 1}-12"
    return f"{yr}-{mo - 1:02d}"


# ── Main endpoint ────────────────────────────────────────────────────────────

@router.get("/summary")
def get_marketing_activity_summary(
    branch_id: Optional[UUID] = Query(None),
    month: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    today = date.today()
    current_month = month or f"{today.year}-{today.month:02d}"
    prev_month = _prev_month_str(current_month)

    d_from, d_to = _month_range(current_month)
    p_from, p_to = _month_range(prev_month)

    use_native = branch_id is not None

    overview_cur = _build_overview(db, branch_id, d_from, d_to, use_native)
    overview_prev = _build_overview(db, branch_id, p_from, p_to, use_native)
    monthly = _build_monthly_by_country(db, branch_id, d_from, d_to, use_native)
    suggestions = _build_kol_suggestions(db, branch_id, d_from, d_to)
    crm_rate_plans = _build_crm_by_rate_plan(db, branch_id, d_from, d_to, use_native)

    currency = "VND"
    if use_native and branch_id is not None:
        b = db.query(Branch).filter(Branch.id == branch_id).first()
        if b and b.currency:
            currency = b.currency

    return _envelope({
        "overview": overview_cur,
        "prev_overview": overview_prev,
        "monthly_by_country": monthly,
        "kol_suggestions": suggestions,
        "crm_by_rate_plan": crm_rate_plans,
        "currency": currency,
        "month": current_month,
        "prev_month": prev_month,
    })


# ── Overview KPIs ────────────────────────────────────────────────────────────

def _build_overview(db, branch_id, d_from, d_to, use_native):
    # Paid Ads bookings/revenue: ads_booking_matches (de-duped, matches the
    # Ads Platform dashboard). Cost: Budget Planner's ActualsCache.
    ads_bookings, ads_revenue = _fetch_paid_ads_totals(
        db, branch_id, d_from, d_to, use_native,
    )

    # KOL (from Cloudbeds) — excludes Blogger/House Use/Special Case
    rev_col = Reservation.grand_total_native if use_native else Reservation.grand_total_vnd
    kol_q = db.query(
        func.count(Reservation.id).label("bookings"),
        func.coalesce(func.sum(rev_col), 0).label("revenue"),
    ).filter(
        Reservation.room_type.ilike("%KOL_%"),
        Reservation.check_in_date >= d_from,
        Reservation.check_in_date <= d_to,
        _status_filter(),
        _revenue_source_filter(),
    )
    if branch_id:
        kol_q = kol_q.filter(Reservation.branch_id == branch_id)
    kol_row = kol_q.one()
    kol_bookings = int(kol_row.bookings)
    kol_revenue = float(kol_row.revenue)

    # CRM (from Cloudbeds) — excludes Blogger/House Use/Special Case
    # Filter on reservation_date (Date Booked), not check_in_date (Stay Date) —
    # CRM activity is measured by when the booking landed, not when the guest stays.
    crm_q = db.query(
        func.count(Reservation.id).label("bookings"),
        func.coalesce(func.sum(rev_col), 0).label("revenue"),
    ).filter(
        _crm_filter(),
        Reservation.reservation_date >= d_from,
        Reservation.reservation_date <= d_to,
        _status_filter(),
        _revenue_source_filter(),
    )
    if branch_id:
        crm_q = crm_q.filter(Reservation.branch_id == branch_id)
    crm_row = crm_q.one()
    crm_bookings = int(crm_row.bookings)
    crm_revenue = float(crm_row.revenue)

    # Costs — pulled from the same source Budget Planner uses (ActualsCache).
    # paid_ads → Ads Platform yearly-plan API (or cached_actual_vnd)
    # kol      → KOL Engine /api/sync/budgets (or cached_actual_vnd)
    # crm      → manual_actual_vnd entered via Budget Planner UI
    ads_cost, kol_cost, crm_cost = _budget_actuals_costs(
        db, branch_id, d_from.year, d_from.month, use_native,
    )

    ads_roas = round(ads_revenue / ads_cost, 2) if ads_cost > 0 else 0
    kol_roas = round(kol_revenue / kol_cost, 2) if kol_cost > 0 else 0
    crm_roas = round(crm_revenue / crm_cost, 2) if crm_cost > 0 else 0

    total_bookings = ads_bookings + kol_bookings + crm_bookings
    total_revenue = ads_revenue + kol_revenue + crm_revenue
    total_cost = ads_cost + kol_cost + crm_cost
    total_roas = round(total_revenue / total_cost, 2) if total_cost > 0 else 0

    return {
        "paid_ads": {"bookings": ads_bookings, "revenue": ads_revenue, "cost": ads_cost, "roas": ads_roas},
        "kol": {"bookings": kol_bookings, "revenue": kol_revenue, "cost": kol_cost, "roas": kol_roas},
        "crm": {"bookings": crm_bookings, "revenue": crm_revenue, "cost": crm_cost, "roas": crm_roas},
        "total": {"bookings": total_bookings, "revenue": total_revenue, "cost": total_cost, "roas": total_roas},
    }


def _budget_actuals_costs(db, branch_id, year: int, month: int, use_native: bool):
    """Sum (paid_ads, kol, crm) cost from Budget Planner's ActualsCache.

    Aggregates across all active branches when ``branch_id`` is None;
    otherwise scoped to the single branch. Returns native currency totals
    when ``use_native`` is True (single-branch view), VND otherwise."""
    q = db.query(Branch).filter(Branch.is_active.is_(True))
    if branch_id is not None:
        q = q.filter(Branch.id == branch_id)
    branches = q.all()

    cache = ActualsCache(db)
    ads_total = 0.0
    kol_total = 0.0
    crm_total = 0.0
    for b in branches:
        ads_vnd = cache.get(b, year, month, "paid_ads")
        kol_vnd = cache.get(b, year, month, "kol")
        crm_vnd = cache.get(b, year, month, "crm")
        if use_native:
            cur = (b.currency or "VND").upper()
            rate = _get_rate_to_vnd(cur)
            ads_total += _vnd_to_native(ads_vnd, cur, rate)
            kol_total += _vnd_to_native(kol_vnd, cur, rate)
            crm_total += _vnd_to_native(crm_vnd, cur, rate)
        else:
            ads_total += ads_vnd
            kol_total += kol_vnd
            crm_total += crm_vnd
    return ads_total, kol_total, crm_total


# ── Monthly by Country ───────────────────────────────────────────────────────

def _build_monthly_by_country(db, branch_id, d_from, d_to, use_native):
    grid = defaultdict(lambda: {
        "paid_ads": {"bookings": 0, "revenue": 0, "cost": 0},
        "kol": {"bookings": 0, "revenue": 0},
        "crm": {"bookings": 0, "revenue": 0},
    })

    # Paid Ads from ads_booking_matches × ads_performance(grain='ad').
    # Cost stays 0 in this grid — daily-grain spend has no country, and
    # allocating it would be a guess; the frontend renders 0 as "—".
    paid_by_country = _fetch_paid_ads_by_country(
        db, branch_id, d_from, d_to, use_native,
    )
    for country, data in paid_by_country.items():
        grid[country]["paid_ads"]["bookings"] += data["bookings"]
        grid[country]["paid_ads"]["revenue"] += data["revenue"]

    rev_col = Reservation.grand_total_native if use_native else Reservation.grand_total_vnd

    # KOL — excludes non-paying
    kol_q = db.query(
        Reservation.guest_country_code,
        func.count(Reservation.id),
        func.coalesce(func.sum(rev_col), 0),
    ).filter(
        Reservation.room_type.ilike("%KOL_%"),
        Reservation.check_in_date >= d_from,
        Reservation.check_in_date <= d_to,
        _status_filter(),
        _revenue_source_filter(),
    ).group_by(Reservation.guest_country_code)
    if branch_id:
        kol_q = kol_q.filter(Reservation.branch_id == branch_id)

    for country, bookings, rev in kol_q.all():
        c = country or "Unknown"
        grid[c]["kol"]["bookings"] += int(bookings)
        grid[c]["kol"]["revenue"] += float(rev)

    # CRM — excludes non-paying. Filter on reservation_date (Date Booked).
    crm_q = db.query(
        Reservation.guest_country_code,
        func.count(Reservation.id),
        func.coalesce(func.sum(rev_col), 0),
    ).filter(
        _crm_filter(),
        Reservation.reservation_date >= d_from,
        Reservation.reservation_date <= d_to,
        _status_filter(),
        _revenue_source_filter(),
    ).group_by(Reservation.guest_country_code)
    if branch_id:
        crm_q = crm_q.filter(Reservation.branch_id == branch_id)

    for country, bookings, rev in crm_q.all():
        c = country or "Unknown"
        grid[c]["crm"]["bookings"] += int(bookings)
        grid[c]["crm"]["revenue"] += float(rev)

    # Flatten — now grouped by country only (single month)
    result = []
    for country, data in sorted(grid.items()):
        activities = []
        if data["paid_ads"]["bookings"] > 0:
            activities.append("Paid Ads")
        if data["kol"]["bookings"] > 0:
            activities.append("KOL")
        if data["crm"]["bookings"] > 0:
            activities.append("CRM")

        total_rev = data["paid_ads"]["revenue"] + data["kol"]["revenue"] + data["crm"]["revenue"]
        total_cost = data["paid_ads"]["cost"]
        total_bookings = data["paid_ads"]["bookings"] + data["kol"]["bookings"] + data["crm"]["bookings"]

        result.append({
            "country": country,
            "paid_ads": data["paid_ads"],
            "kol": data["kol"],
            "crm": data["crm"],
            "activities": activities,
            "total_bookings": total_bookings,
            "total_revenue": total_rev,
            "total_cost": total_cost,
            "roas": round(total_rev / total_cost, 2) if total_cost > 0 else None,
        })

    result.sort(key=lambda x: -x["total_revenue"])
    return result


# ── CRM Reservations by Rate Plan ────────────────────────────────────────────

def _build_crm_by_rate_plan(db: Session, branch_id: Optional[UUID], d_from: date, d_to: date, use_native: bool):
    """CRM reservations grouped by rate plan tag (extracted from room_type when rate_plan_name blank)."""
    rev_col = Reservation.grand_total_native if use_native else Reservation.grand_total_vnd

    # Cloudbeds packs the rate plan name inside the roomTypeName parentheses,
    # e.g. 'Female Dorm* (CRM_May 2026 Event)'. When rate_plan_name is null we
    # extract just the parenthesised tag so each CRM event gets its own row
    # instead of collapsing into one row per base room type.
    # Fallback order: rate_plan_name → substring inside first (…) in room_type
    #                 → full room_type → '(unknown)'.
    # PostgreSQL-specific: SUBSTRING(col FROM 'pattern') returns the first
    # capture group or NULL. Wrap in literal_column because SQLAlchemy's
    # func.substring emits the positional (int) form instead of the FROM form.
    crm_tag = literal_column(r"substring(reservations.room_type from E'\\(([^)]+)\\)')")
    rate_plan_expr = func.coalesce(
        func.nullif(func.trim(Reservation.rate_plan_name), ""),
        func.nullif(func.trim(crm_tag), ""),
        func.nullif(func.trim(Reservation.room_type), ""),
        "(unknown)",
    ).label("rate_plan")

    q = db.query(
        rate_plan_expr,
        func.count(Reservation.id).label("bookings"),
        func.coalesce(func.sum(Reservation.nights), 0).label("nights"),
        func.coalesce(func.sum(rev_col), 0).label("revenue"),
    ).filter(
        _crm_filter(),
        Reservation.reservation_date >= d_from,
        Reservation.reservation_date <= d_to,
        _status_filter(),
        _revenue_source_filter(),
    ).group_by("rate_plan")

    if branch_id:
        q = q.filter(Reservation.branch_id == branch_id)

    rows = q.all()
    result = []
    for rate_plan, bookings, nights, revenue in rows:
        b = int(bookings or 0)
        n = int(nights or 0)
        r = float(revenue or 0)
        result.append({
            "rate_plan_name": rate_plan,
            "bookings": b,
            "nights": n,
            "revenue": r,
            "adr": round(r / n, 2) if n > 0 else 0,
        })

    result.sort(key=lambda x: -x["revenue"])
    return result


# ── KOL Suggestions for Paid Ads ─────────────────────────────────────────────

def _build_kol_suggestions(db: Session, branch_id: Optional[UUID], d_from: date, d_to: date):
    bid_filter = "AND r.branch_id = :bid" if branch_id else ""

    rows = db.execute(text(f"""
        SELECT r.room_type,
               r.guest_country_code,
               r.grand_total_vnd,
               r.status,
               r.source,
               b.id   AS branch_id,
               b.name AS branch_name
        FROM   reservations r
        JOIN   branches b ON r.branch_id = b.id
        WHERE  r.room_type ILIKE '%KOL_%%'
          AND  r.reservation_date >= :d_from
          AND  r.reservation_date <= :d_to
          {bid_filter}
    """), {
        "d_from": d_from,
        "d_to": d_to,
        **({"bid": str(branch_id)} if branch_id else {}),
    }).fetchall()

    agg = defaultdict(lambda: {"organic_bookings": 0, "organic_revenue_vnd": 0.0})

    for room_type, country, total_vnd, status, source, bid, branch_name in rows:
        m = _KOL_RE.search(room_type or "")
        if not m:
            continue
        kol_name = "KOL_" + m.group(1).strip()
        if (status or "").lower() in _CANCELLED:
            continue
        # Exclude non-paying sources from revenue
        if (source or "").lower().strip() in _EXCLUDED_SOURCES:
            continue
        country = country or "Unknown"
        key = (kol_name, country, str(bid), branch_name)
        agg[key]["organic_bookings"] += 1
        agg[key]["organic_revenue_vnd"] += float(total_vnd or 0)

    if not agg:
        return []

    kol_rows = db.execute(text("""
        SELECT kol_name, kol_nationality, usage_rights_expiry_date,
               paid_ads_eligible, paid_ads_channel, ads_usage_status
        FROM   kol_records
    """)).fetchall()

    kol_map = {}
    for kr in kol_rows:
        kol_map[kr[0]] = {
            "kol_nationality": kr[1],
            "usage_rights_until": kr[2].isoformat() if kr[2] else None,
            "paid_ads_eligible": kr[3],
            "paid_ads_channel": kr[4],
            "ads_usage_status": kr[5],
        }

    result = []
    for (kol_name, country, bid, branch_name), data in agg.items():
        if data["organic_bookings"] <= 0:
            continue
        mgmt = kol_map.get(kol_name, {})
        if mgmt.get("paid_ads_channel") or mgmt.get("ads_usage_status") == "In Use":
            continue
        result.append({
            "kol_name": kol_name,
            "country": country,
            "organic_bookings": data["organic_bookings"],
            "organic_revenue_vnd": data["organic_revenue_vnd"],
            "branch_id": bid,
            "branch": branch_name,
            "kol_nationality": mgmt.get("kol_nationality"),
            "usage_rights_until": mgmt.get("usage_rights_until"),
            "paid_ads_eligible": mgmt.get("paid_ads_eligible", False),
        })

    result.sort(key=lambda x: (x["country"], -x["organic_revenue_vnd"]))
    return result


# ── Debug: probe Ads Platform endpoints to find authoritative source ────────

@router.get("/debug/paid-ads")
def debug_paid_ads_sources(
    date_from: str = Query(...),
    date_to: str = Query(...),
    branch: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    """Side-by-side compare every plausible source for Paid Ads totals.

    Hit this in browser to see which endpoint matches the Ads Platform
    dashboard's KPI cards. Helps figure out whether spend/daily,
    booking-matches, or booking-matches/summary is the right source.
    """
    from datetime import datetime as _dt
    df = _dt.fromisoformat(date_from).date()
    dt = _dt.fromisoformat(date_to).date()

    # ── Local DB sums ──────────────────────────────────────────────────────
    daily_q = db.query(
        func.coalesce(func.sum(AdsPerformance.bookings), 0).label("bookings"),
        func.coalesce(func.sum(AdsPerformance.revenue_vnd), 0).label("revenue_vnd"),
        func.coalesce(func.sum(AdsPerformance.cost_vnd), 0).label("cost_vnd"),
        func.count(AdsPerformance.id).label("row_count"),
    ).filter(
        AdsPerformance.grain == "daily",
        AdsPerformance.date_from >= df,
        AdsPerformance.date_from <= dt,
    )
    matches_q = db.query(
        func.count(AdsBookingMatch.id).label("count"),
        func.coalesce(func.sum(AdsBookingMatch.revenue_vnd), 0).label("revenue_vnd"),
    ).filter(
        AdsBookingMatch.booking_date >= df,
        AdsBookingMatch.booking_date <= dt,
    )
    daily_row = daily_q.one()
    matches_row = matches_q.one()
    local_summary = {
        "ads_performance_grain_daily": {
            "row_count": int(daily_row.row_count or 0),
            "bookings_sum": int(daily_row.bookings or 0),
            "revenue_vnd_sum": float(daily_row.revenue_vnd or 0),
            "cost_vnd_sum": float(daily_row.cost_vnd or 0),
        },
        "ads_booking_matches_table": {
            "row_count": int(matches_row.count or 0),
            "revenue_vnd_sum": float(matches_row.revenue_vnd or 0),
        },
    }

    # ── Live Ads Platform API ──────────────────────────────────────────────
    api_results: dict = {}
    try:
        client = _get_ads_client()
        # 1. summary endpoint (likely the dashboard's KPI source)
        try:
            api_results["booking_matches_summary"] = client.get_booking_matches_summary(
                date_from, date_to, branch=branch,
            )
        except Exception as e:
            api_results["booking_matches_summary"] = {"error": repr(e)}

        # 2. paginated booking-matches — count + revenue + sample
        try:
            matches = list(client.get_booking_matches(date_from, date_to, branch=branch))
            fields_seen: set = set()
            for m in matches:
                fields_seen.update(m.keys())
            rev_total = 0.0
            null_rev_count = 0
            for m in matches:
                r = m.get("revenue")
                if r is None:
                    r = m.get("revenue_native")
                if r is None:
                    null_rev_count += 1
                else:
                    try:
                        rev_total += float(r)
                    except (TypeError, ValueError):
                        null_rev_count += 1
            api_results["booking_matches_paginated"] = {
                "count": len(matches),
                "raw_revenue_sum": rev_total,
                "matches_with_null_revenue": null_rev_count,
                "fields_seen": sorted(fields_seen),
                "first_3": matches[:3],
            }
        except Exception as e:
            api_results["booking_matches_paginated"] = {"error": repr(e)}

        # 3. spend/daily summed across platforms (if branch supplied)
        if branch:
            try:
                totals = {"conversions": 0, "revenue": 0.0, "spend": 0.0}
                fields_seen2: set = set()
                first_rows = []
                for platform in ("meta", "google", "tiktok"):
                    rows = client.get_spend_daily(
                        date_from, date_to, platform=platform, branch=branch,
                    ) or []
                    for r in rows:
                        fields_seen2.update(r.keys())
                        totals["conversions"] += int(r.get("conversions") or 0)
                        totals["revenue"] += float(r.get("revenue") or 0)
                        totals["spend"] += float(r.get("spend") or 0)
                    if rows and len(first_rows) < 3:
                        first_rows.extend(rows[: max(0, 3 - len(first_rows))])
                api_results["spend_daily_summed"] = {
                    "totals": totals,
                    "fields_seen": sorted(fields_seen2),
                    "first_3_rows": first_rows,
                }
            except Exception as e:
                api_results["spend_daily_summed"] = {"error": repr(e)}
        else:
            api_results["spend_daily_summed"] = {
                "skipped": "spend/daily requires branch slug; pass ?branch=<slug>",
            }
    except Exception as e:
        api_results["client_init_error"] = repr(e)

    return _envelope({
        "params": {"date_from": date_from, "date_to": date_to, "branch": branch},
        "local_db": local_summary,
        "ads_platform_api": api_results,
    })
