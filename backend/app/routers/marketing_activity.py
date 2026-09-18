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
                               Travel guide/Grand Open/Extension Promotion)

Revenue exclusion: Blogger, House Use, Special Case, Work Exchange (non-paying guests)
"""
import calendar
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import extract, func
from sqlalchemy.orm import Session

from app.database import SessionLocal, get_db
from app.models.ads import AdsPerformance
from app.models.ads_booking_match import AdsBookingMatch
from app.models.branch import Branch
from app.models.marketing_activity_cache import MarketingActivityCache
from app.models.rate_plan_campaign import RatePlanCampaign
from app.models.reservation import Reservation
from app.models.seasonal_campaign import SeasonalCampaign
from app.routers.marketing_budget import ActualsCache, _get_rate_to_vnd, _vnd_to_native
from app.services.ads_platform import branch_slug_for, get_client as _get_ads_client
from app.services.crm_filters import (
    crm_rate_plan_label_expr,
    crm_rate_plan_value_expr,
    crm_reservation_filter,
)
from app.services.kol_engine import fetch_kol_revenue, resolve_hotel_id_from_branch_name
from app.services.rate_plan_campaigns import campaign_map, label_rows
from app.services.seasonal_campaigns import (
    build_rows as build_seasonal_rows,
    clean_patterns as clean_campaign_patterns,
    fetch_ad_metrics as fetch_seasonal_ad_metrics,
    list_campaigns as list_seasonal_campaigns,
    serialize as serialize_seasonal,
)
from app.config import settings

router = APIRouter()
log = logging.getLogger(__name__)

# Status exclusion: cancelled / no-show (lowercase canonical, matches Cloudbeds sync normalization)
_EXCLUDED_STATUSES = {"cancelled", "canceled", "no_show", "noshow", "no show", "no-show", "cancelled_by_guest"}

# Revenue exclusion: non-paying guests
_EXCLUDED_SOURCES = {"blogger", "house use", "houseuse", "special case", "work exchange"}

# Branch display order — mirrors the Weekly Report (report.py). Requested by
# growth team 2026-05-26: Taipei → 1948 → Oani → Osaka → Saigon.
_BRANCH_DISPLAY_ORDER = ("taipei", "1948", "oani", "osaka", "saigon")


def _branch_display_sort_key(name: str):
    n = (name or "").lower()
    for i, kw in enumerate(_BRANCH_DISPLAY_ORDER):
        if kw in n:
            return (i, n)
    return (len(_BRANCH_DISPLAY_ORDER), n)


def _envelope(data):
    return {
        "success": True, "data": data, "error": None,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _revenue_source_filter():
    """Exclude Blogger/House Use/Special Case/Work Exchange from revenue queries."""
    return ~func.lower(func.coalesce(Reservation.source, "")).in_(list(_EXCLUDED_SOURCES))


def _status_filter():
    # Cloudbeds sync stores status lowercased + trimmed (services/cloudbeds.py),
    # so compare case-insensitively against the canonical excluded set.
    return ~func.lower(func.coalesce(Reservation.status, "")).in_(list(_EXCLUDED_STATUSES))


# ── Ads data readers ─────────────────────────────────────────────────────────


def _fetch_paid_ads_totals(
    db: Session,
    branch_id: Optional[UUID],
    d_from: date,
    d_to: date,
    use_native: bool,
) -> tuple[int, float]:
    """Paid Ads (bookings, revenue) for a month — Path B, dashboard parity.

    Live call to ``/export/spend/daily?valid_country_only=true``. Server
    applies the same ``_apply_common_filters`` as the ADS Performance
    dashboard: drops ad-level rows (kept adset, plus campaign-level only
    for PMax to avoid grain inflation), drops invalid country, dedups
    conversions to ``omni_purchase``. Result matches dashboard's KPI
    cards exactly.

    Replaces interim Path A (``/spend/daily-by-country``) which read the
    wrong cache (``ad_country_metrics``, built for booking-from-ads
    matcher) and inflated bookings ~8x via raw additive event counts
    (fb_pixel_purchase + offline_purchase) instead of pre-deduped
    omni_purchase.
    """
    client = _get_ads_client()
    branch_obj = None
    slug = None
    if branch_id is not None:
        branch_obj = db.query(Branch).filter(Branch.id == branch_id).first()
        if branch_obj:
            slug = branch_slug_for(branch_obj)

    branches_q = db.query(Branch).filter(Branch.is_active.is_(True))
    if branch_id is not None:
        branches_q = branches_q.filter(Branch.id == branch_id)
    slug_to_currency = {
        branch_slug_for(b).lower(): (b.currency or "VND").upper()
        for b in branches_q.all()
    }

    bookings_total = 0
    revenue_vnd_total = 0.0

    def _fetch_one(platform: str):
        return client.get_spend_daily(
            d_from.isoformat(), d_to.isoformat(),
            platform=platform, branch=slug,
            valid_country_only=True,
        )

    with ThreadPoolExecutor(max_workers=3) as executor:
        future_to_platform = {
            executor.submit(_fetch_one, p): p
            for p in ("meta", "google", "tiktok")
        }
        for future in as_completed(future_to_platform):
            platform = future_to_platform[future]
            try:
                rows = future.result()
            except Exception as exc:
                log.warning(
                    "spend/daily failed (platform=%s, branch=%s): %s",
                    platform, slug, exc,
                )
                continue

            for r in rows or []:
                row_branch = (r.get("branch") or "").lower().strip()
                # Skip rows from non-HiD branches (e.g. Bread leaks via
                # shared Meta accounts when querying without branch filter).
                if row_branch not in slug_to_currency:
                    continue
                bookings_total += int(r.get("conversions") or 0)
                revenue_native = float(r.get("revenue") or 0)
                if revenue_native == 0:
                    continue
                row_currency = (
                    r.get("currency")
                    or slug_to_currency.get(row_branch)
                    or "VND"
                ).upper()
                rate = _get_rate_to_vnd(row_currency)
                if rate:
                    revenue_vnd_total += revenue_native * rate

    if not use_native:
        return bookings_total, revenue_vnd_total

    branch_currency = (branch_obj.currency or "VND").upper() if branch_obj else "VND"
    if branch_currency == "VND":
        return bookings_total, revenue_vnd_total
    branch_rate = _get_rate_to_vnd(branch_currency)
    revenue_total = _vnd_to_native(revenue_vnd_total, branch_currency, branch_rate)
    return bookings_total, revenue_total


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

def _ytd_range(year: int):
    """Jan 1 → min(today, Dec 31) for the given year."""
    today = date.today()
    d_from = date(year, 1, 1)
    d_to = date(year, 12, 31) if year < today.year else today
    return d_from, d_to


def _safe_replace_year(d: date, year: int) -> date:
    """Replace year, clamping Feb 29 → Feb 28 for non-leap years."""
    try:
        return d.replace(year=year)
    except ValueError:
        return d.replace(year=year, day=28)


@router.get("/summary")
def get_marketing_activity_summary(
    branch_id: Optional[UUID] = Query(None),
    month: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    today = date.today()

    if date_from and date_to:
        # YTD / custom range mode
        from datetime import datetime as _dt
        d_from = _dt.fromisoformat(date_from).date()
        d_to = _dt.fromisoformat(date_to).date()
        p_from = _safe_replace_year(d_from, d_from.year - 1)
        p_to = _safe_replace_year(d_to, d_to.year - 1)
        current_month = date_from[:4]   # year string used as label key
        prev_month = str(d_from.year - 1)
        is_range = True
    else:
        current_month = month or f"{today.year}-{today.month:02d}"
        prev_month = _prev_month_str(current_month)
        d_from, d_to = _month_range(current_month)
        p_from, p_to = _month_range(prev_month)
        is_range = False

    use_native = branch_id is not None

    def _with_session(fn):
        session = SessionLocal()
        try:
            return fn(session)
        finally:
            session.close()

    # Try cache first for current + prev period (avoids slow external API calls).
    # Fall back to live APIs if cache hasn't been populated yet.
    cached_cur = _cache_read(db, branch_id, d_from, d_to)
    cached_prev = _cache_read(db, branch_id, p_from, p_to)

    def _build_cur(s):
        if cached_cur is not None:
            return _build_overview_from_cache(s, branch_id, d_from, d_to, use_native, cached_cur)
        return _build_overview(s, branch_id, d_from, d_to, use_native)

    def _build_prev(s):
        if cached_prev is not None:
            return _build_overview_from_cache(s, branch_id, p_from, p_to, use_native, cached_prev)
        return _build_overview(s, branch_id, p_from, p_to, use_native)

    with ThreadPoolExecutor(max_workers=3) as ex:
        f_cur = ex.submit(_with_session, _build_cur)
        f_prev = ex.submit(_with_session, _build_prev)
        f_crm = ex.submit(_with_session, lambda s: _build_crm_by_rate_plan(s, branch_id, d_from, d_to, use_native))
        overview_cur = f_cur.result()
        overview_prev = f_prev.result()
        crm_rate_plans = f_crm.result()

    currency = "VND"
    if use_native and branch_id is not None:
        b = db.query(Branch).filter(Branch.id == branch_id).first()
        if b and b.currency:
            currency = b.currency

    return _envelope({
        "overview": overview_cur,
        "prev_overview": overview_prev,
        "crm_by_rate_plan": crm_rate_plans,
        "currency": currency,
        "month": current_month,
        "prev_month": prev_month,
        "is_range": is_range,
    })


# ── ROAS trend: one point per month, for the YTD chart ───────────────────────


@router.get("/roas-trend")
def get_roas_trend(
    branch_id: Optional[UUID] = Query(None),
    year: Optional[int] = Query(None),
    db: Session = Depends(get_db),
):
    """ROAS per month, January → today, for the YTD view's trend chart.

    Every point is built by the same overview builder ``/summary`` uses for a
    single month, so a month on this line reads exactly what the Monthly view
    shows when you pick that month — no second, differently-shaped ledger.

    A month whose upstream call fails comes back with ``unavailable: true`` and
    no numbers, so the chart draws a gap instead of a fabricated zero.
    """
    today = date.today()
    yr = year or today.year
    if yr > today.year:
        return _envelope({"year": yr, "currency": "VND", "months": []})
    last_month = today.month if yr == today.year else 12

    use_native = branch_id is not None

    def _build_month(mo: int):
        session = SessionLocal()
        try:
            d_from, d_to = _month_range(f"{yr}-{mo:02d}")
            cached = _cache_read(session, branch_id, d_from, d_to)
            if cached is not None:
                return _build_overview_from_cache(
                    session, branch_id, d_from, d_to, use_native, cached,
                )
            return _build_overview(session, branch_id, d_from, d_to, use_native)
        finally:
            session.close()

    overviews: dict[int, dict] = {}
    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = {ex.submit(_build_month, mo): mo for mo in range(1, last_month + 1)}
        for future in as_completed(futures):
            mo = futures[future]
            try:
                overviews[mo] = future.result()
            except Exception as exc:
                log.warning("roas-trend: %s-%02d failed: %s", yr, mo, exc)

    currency = "VND"
    if use_native and branch_id is not None:
        b = db.query(Branch).filter(Branch.id == branch_id).first()
        if b and b.currency:
            currency = b.currency

    months = []
    for mo in range(1, last_month + 1):
        point = {"month": f"{yr}-{mo:02d}", "label": calendar.month_abbr[mo]}
        overview = overviews.get(mo)
        if overview is None:
            point["unavailable"] = True
        else:
            point.update(overview)
        months.append(point)

    return _envelope({"year": yr, "currency": currency, "months": months})


# ── KOL totals: KOL Engine API → Cloudbeds fallback ──────────────────────────


def _months_in_range(d_from: date, d_to: date) -> list[tuple[int, int]]:
    """Every (year, month) touched by [d_from, d_to], in order."""
    months = []
    cur = date(d_from.year, d_from.month, 1)
    end = date(d_to.year, d_to.month, 1)
    while cur <= end:
        months.append((cur.year, cur.month))
        cur = date(cur.year + 1, 1, 1) if cur.month == 12 else date(cur.year, cur.month + 1, 1)
    return months


def _fetch_kol_totals_cloudbeds(db, branch_id, d_from, d_to, use_native):
    """Fallback: Cloudbeds KOL revenue by room_type when KOL Engine API fails."""
    rev_col = Reservation.grand_total_native if use_native else Reservation.grand_total_vnd
    q = db.query(
        func.count(Reservation.id).label("bookings"),
        func.coalesce(func.sum(rev_col), 0).label("revenue"),
    ).filter(
        Reservation.room_type.ilike("%KOL_%"),
        Reservation.reservation_date >= d_from,
        Reservation.reservation_date <= d_to,
        _status_filter(),
        _revenue_source_filter(),
    )
    if branch_id:
        q = q.filter(Reservation.branch_id == branch_id)
    row = q.one()
    return int(row.bookings or 0), float(row.revenue or 0)


def _fetch_kol_totals_one_month(db, branch_id, branch_obj, hotel_id, year, month, use_native):
    """Fetch KOL bookings/revenue for one month from the KOL Engine public API.

    Uses fetch_kol_revenue which applies the same logic as the Insights page:
    same kol_reservations data, same ads exclusion, same currency conversion.
    Falls back to room_type filter if the API is unavailable.
    """
    d_from = date(year, month, 1)
    d_to = date(year, month, calendar.monthrange(year, month)[1])

    data = fetch_kol_revenue(
        base_url=settings.KOL_ENGINE_URL,
        org_slug=settings.KOL_TARGETS_ORG_SLUG,
        api_key=settings.KOL_REVENUE_API_SECRET,
        year=year,
        month=month,
        hotel_id=hotel_id,
    )

    if data is None:
        log.warning("KOL Engine API unavailable for %s-%s — falling back to room_type filter", year, month)
        return _fetch_kol_totals_cloudbeds(db, branch_id, d_from, d_to, use_native)

    if hotel_id:
        # Single branch: find the matching branch entry
        for b in (data.get("branches") or []):
            if b.get("hotel_id") == hotel_id:
                bookings = int(b.get("bookings") or 0)
                rev = float(b.get("revenue") if use_native else b.get("revenue_vnd") or 0)
                return bookings, rev
        return 0, 0.0
    else:
        # All branches: use org-wide totals (includes bookings with null hotel_id
        # that would be missed if summing per-branch values from branches[]).
        totals = data.get("totals") or {}
        return int(totals.get("bookings") or 0), float(totals.get("revenue") or 0)


def _fetch_kol_totals(db, branch_id, d_from, d_to, use_native):
    """Pull KOL bookings/revenue from KOL Engine public API.

    Single-branch view returns native currency from the matching branches[]
    row; all-branches view sums revenue_vnd across the response. The API
    pre-excludes Blogger/House Use/Special Case + bookings the Ads Platform
    already attributes to ads (cutoff 2026-05-01) — single source of truth.

    Falls back to local Cloudbeds aggregation on any API failure.
    Supports multi-month ranges (YTD) by fetching each month in parallel.
    """
    branch_obj = None
    hotel_id = None
    if branch_id:
        branch_obj = db.query(Branch).filter(Branch.id == branch_id).first()
        if branch_obj:
            hotel_id = resolve_hotel_id_from_branch_name(branch_obj.name)

    months = _months_in_range(d_from, d_to)

    if len(months) == 1:
        return _fetch_kol_totals_one_month(
            db, branch_id, branch_obj, hotel_id, months[0][0], months[0][1], use_native,
        )

    # Multi-month: fetch in parallel and sum
    total_bookings = 0
    total_revenue = 0.0
    with ThreadPoolExecutor(max_workers=min(len(months), 6)) as ex:
        futures = {
            ex.submit(
                _fetch_kol_totals_one_month,
                db, branch_id, branch_obj, hotel_id, yr, mo, use_native,
            ): (yr, mo)
            for yr, mo in months
        }
        for future in as_completed(futures):
            try:
                b, r = future.result()
                total_bookings += b
                total_revenue += r
            except Exception as exc:
                yr, mo = futures[future]
                log.warning("KOL month %s-%s failed: %s", yr, mo, exc)
    return total_bookings, total_revenue


# ── KOL cost: KOL Engine public revenue API ──────────────────────────────────
#
# Deliberately NOT Budget Planner's ActualsCache, which routes through the KOL
# Engine's Budget module (GET /api/sync/budgets → monthly_breakdown[].actual).
# The two agree exactly for Feb–Aug 2026 but not for January, where the Budget
# module reports 72.22M org-wide against this endpoint's 47.4M — and revenue on
# this row already comes from here, so taking cost from the other source would
# let the numerator and denominator of one ROAS sit on two different ledgers.
#
# cost_vnd also needs no currency handling: the Engine converts it back at the
# same rates it converted with. Budget Planner has to invert that by hand in
# upstream_actuals._KOL_ENGINE_NATIVE_TO_VND, which is a standing hazard.


def _fetch_kol_cost_one_month(hotel_id: Optional[str], year: int, month: int) -> Optional[float]:
    """KOL cost in VND for one month. None when the Engine is unreachable."""
    data = fetch_kol_revenue(
        base_url=settings.KOL_ENGINE_URL,
        org_slug=settings.KOL_TARGETS_ORG_SLUG,
        api_key=settings.KOL_REVENUE_API_SECRET,
        year=year,
        month=month,
        hotel_id=hotel_id,
    )
    if data is None:
        return None
    if hotel_id:
        for b in (data.get("branches") or []):
            if b.get("hotel_id") == hotel_id:
                return float(b.get("cost_vnd") or 0)
        return 0.0
    # totals.cost is org-wide and always VND (totals.currency == "VND").
    return float((data.get("totals") or {}).get("cost") or 0)


def _fetch_kol_cost_vnd(db, branch_id, d_from: date, d_to: date) -> Optional[float]:
    """KOL cost in VND over [d_from, d_to], or None if the Engine can't answer.

    None (not 0) on failure so the caller keeps Budget Planner's actuals as a
    fallback — a 0 here would silently turn the KOL ROAS into a dash.
    """
    hotel_id = None
    if branch_id:
        branch_obj = db.query(Branch).filter(Branch.id == branch_id).first()
        hotel_id = resolve_hotel_id_from_branch_name(branch_obj.name) if branch_obj else None
        if not hotel_id:
            return None

    months = _months_in_range(d_from, d_to)
    if len(months) == 1:
        return _fetch_kol_cost_one_month(hotel_id, months[0][0], months[0][1])

    total = 0.0
    with ThreadPoolExecutor(max_workers=min(len(months), 6)) as ex:
        futures = {
            ex.submit(_fetch_kol_cost_one_month, hotel_id, yr, mo): (yr, mo)
            for yr, mo in months
        }
        for future in as_completed(futures):
            yr, mo = futures[future]
            try:
                value = future.result()
            except Exception as exc:
                log.warning("KOL cost %s-%s failed: %s", yr, mo, exc)
                return None
            if value is None:
                return None
            total += value
    return total


def _kol_cost_for_view(db, branch_id, d_from: date, d_to: date, use_native: bool,
                       fallback: float) -> float:
    """KOL cost in the view's currency, falling back to Budget Planner actuals."""
    cost_vnd = _fetch_kol_cost_vnd(db, branch_id, d_from, d_to)
    if cost_vnd is None:
        log.warning("KOL cost unavailable from KOL Engine — using Budget Planner actuals")
        return fallback
    if not (use_native and branch_id):
        return cost_vnd
    branch_obj = db.query(Branch).filter(Branch.id == branch_id).first()
    cur = (branch_obj.currency or "VND").upper() if branch_obj else "VND"
    return _vnd_to_native(cost_vnd, cur, _get_rate_to_vnd(cur))


# ── Overview KPIs ────────────────────────────────────────────────────────────

def _build_overview(db, branch_id, d_from, d_to, use_native):
    # Paid Ads bookings/revenue: ads_booking_matches (de-duped, matches the
    # Ads Platform dashboard). Cost: Budget Planner's ActualsCache.
    ads_bookings, ads_revenue = _fetch_paid_ads_totals(
        db, branch_id, d_from, d_to, use_native,
    )

    # KOL bookings/revenue: KOL Engine /api/public/kol-revenue (de-duped vs
    # Ads Platform from 2026-05-01 cutoff). Falls back to Cloudbeds query
    # if the API is unreachable so the card never shows 0.
    kol_bookings, kol_revenue = _fetch_kol_totals(
        db, branch_id, d_from, d_to, use_native,
    )

    # CRM (from Cloudbeds) — excludes Blogger/House Use/Special Case/Work Exchange
    # Filter on reservation_date (Date Booked), not check_in_date (Stay Date) —
    # CRM activity is measured by when the booking landed, not when the guest stays.
    rev_col = Reservation.grand_total_native if use_native else Reservation.grand_total_vnd
    crm_q = db.query(
        func.count(Reservation.id).label("bookings"),
        func.coalesce(func.sum(rev_col), 0).label("revenue"),
    ).filter(
        crm_reservation_filter(),
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

    # Costs — Budget Planner's ActualsCache for paid_ads and crm.
    # paid_ads → Ads Platform yearly-plan API (or cached_actual_vnd)
    # crm      → manual_actual_vnd entered via Budget Planner UI
    ads_cost, kol_cost_budget, crm_cost = _budget_actuals_costs(
        db, branch_id, d_from.year, d_from.month, use_native,
        month_to=d_to.month if d_to.year == d_from.year else 12,
    )
    # kol → KOL Engine /api/public/kol-revenue cost_vnd, the same payload the
    # KOL revenue above comes from. Budget Planner's actuals stay as fallback.
    kol_cost = _kol_cost_for_view(db, branch_id, d_from, d_to, use_native, kol_cost_budget)

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


def _budget_actuals_costs(
    db, branch_id, year: int, month: int, use_native: bool,
    month_to: Optional[int] = None,
):
    """Sum (paid_ads, kol, crm) cost from Budget Planner's ActualsCache.

    Aggregates across all active branches when ``branch_id`` is None;
    otherwise scoped to the single branch. Returns native currency totals
    when ``use_native`` is True (single-branch view), VND otherwise.

    Pass ``month_to`` to sum across a range (e.g. YTD Jan–Jul: month=1, month_to=7).
    """
    q = db.query(Branch).filter(Branch.is_active.is_(True))
    if branch_id is not None:
        q = q.filter(Branch.id == branch_id)
    branches = q.all()

    months = list(range(month, (month_to or month) + 1))
    cache = ActualsCache(db)
    ads_total = 0.0
    kol_total = 0.0
    crm_total = 0.0
    for b in branches:
        for mo in months:
            ads_vnd = cache.get(b, year, mo, "paid_ads")
            kol_vnd = cache.get(b, year, mo, "kol")
            crm_vnd = cache.get(b, year, mo, "crm")
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


# ── CRM Reservations by Rate Plan ────────────────────────────────────────────

def _build_crm_by_rate_plan(db: Session, branch_id: Optional[UUID], d_from: date, d_to: date, use_native: bool):
    """CRM reservations grouped by rate plan tag (extracted from room_type when rate_plan_name blank)."""
    rev_col = Reservation.grand_total_native if use_native else Reservation.grand_total_vnd

    # Group by rate plan tag via the shared crm_filters expression (kept in
    # one place so the Weekly Report and this page never drift).
    rate_plan_expr = crm_rate_plan_label_expr()

    q = db.query(
        rate_plan_expr,
        func.count(Reservation.id).label("bookings"),
        func.coalesce(func.sum(Reservation.nights), 0).label("nights"),
        func.coalesce(func.sum(rev_col), 0).label("revenue"),
    ).filter(
        crm_reservation_filter(),
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


# ── CRM rate plan drill-down ─────────────────────────────────────────────────
#
# Opens one row of the CRM-by-rate-plan table. The headline numbers are built
# from exactly the rows that table counted (same window, same status and source
# exclusions) so the two never disagree; the status mix is deliberately built
# from the UNfiltered set, because "how many of these cancelled" is the whole
# reason to look at status and the table row can never show it.

_AGE_BUCKETS = ("<25", "25-34", "35-44", "45-54", "55+")


def _crm_row_exclusion(status: Optional[str], source: Optional[str]) -> Optional[str]:
    """Why the table would drop this row, or None when it counts it.

    The Python twin of ``_status_filter()`` and ``_revenue_source_filter()``,
    reading the same two constant sets. It exists so the drill-down can report
    the dropped rows instead of merely not selecting them — and so a change to
    either exclusion rule cannot move the table without moving this too.
    """
    if (status or "").strip().lower() in _EXCLUDED_STATUSES:
        return "cancelled"
    if (source or "").strip().lower() in _EXCLUDED_SOURCES:
        return "non_paying_source"
    return None


def _age_on(birth_year: Optional[int], on_date: Optional[date]) -> Optional[int]:
    """Age in whole years at `on_date` (check-in), or None when unknowable.

    Only the birth year is stored, so this is accurate to within a year — a
    guest who has not yet had their birthday reads one year older. At band
    edges that moves a small number of rows one bucket over, which is well
    inside the noise these bands are read against.
    """
    if not birth_year or not on_date:
        return None
    years = on_date.year - birth_year
    # Cloudbeds carries placeholder birthdates (year 1900, or a date after the
    # stay). Treat an implausible age as missing rather than charting it.
    if years < 0 or years > 120:
        return None
    return years


def _age_bucket(age: Optional[int]) -> Optional[str]:
    if age is None:
        return None
    if age < 25:
        return "<25"
    if age < 35:
        return "25-34"
    if age < 45:
        return "35-44"
    if age < 55:
        return "45-54"
    return "55+"


def _num_stats(vals: list) -> dict:
    if not vals:
        return {"count": 0, "avg": 0, "median": 0, "min": 0, "max": 0}
    ordered = sorted(vals)
    n = len(ordered)
    mid = n // 2
    median = ordered[mid] if n % 2 else (ordered[mid - 1] + ordered[mid]) / 2
    return {
        "count": n,
        "avg": round(sum(ordered) / n, 2),
        "median": round(median, 2),
        "min": ordered[0],
        "max": ordered[-1],
    }


def _counter_rows(counter: dict, key_name: str, top: Optional[int] = None) -> list:
    rows = sorted(counter.values(), key=lambda x: (-x["bookings"], -x["revenue"]))
    if top:
        rows = rows[:top]
    return rows


def _bump_counter(counter: dict, key, key_name: str, revenue: float, **extra) -> dict:
    row = counter.get(key)
    if row is None:
        row = {key_name: key, "bookings": 0, "revenue": 0.0, **extra}
        counter[key] = row
    row["bookings"] += 1
    row["revenue"] += revenue
    return row


@router.get("/crm-rate-plan-detail")
def crm_rate_plan_detail(
    rate_plan: str = Query(..., min_length=1, description="Exact rate plan label from the table"),
    branch_id: Optional[UUID] = Query(None),
    month: Optional[str] = Query(None, description="YYYY-MM; ignored when date_from/date_to given"),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    """Who books one CRM rate plan: status, country, demographics, stay shape."""
    try:
        if date_from and date_to:
            d_from = datetime.fromisoformat(date_from).date()
            d_to = datetime.fromisoformat(date_to).date()
        else:
            d_from, d_to = _month_range(month or f"{date.today().year}-{date.today().month:02d}")

        use_native = branch_id is not None
        rev_col = Reservation.grand_total_native if use_native else Reservation.grand_total_vnd

        q = (
            db.query(
                Reservation.status,
                Reservation.source,
                Reservation.source_category,
                Reservation.guest_country,
                Reservation.guest_country_code,
                Reservation.gender,
                Reservation.birth_year,
                Reservation.room_type,
                Reservation.room_type_category,
                Reservation.nights,
                Reservation.adults,
                Reservation.check_in_date,
                Reservation.reservation_date,
                rev_col.label("revenue"),
                Branch.name.label("branch_name"),
            )
            .outerjoin(Branch, Branch.id == Reservation.branch_id)
            .filter(
                crm_reservation_filter(),
                crm_rate_plan_value_expr() == rate_plan,
                Reservation.reservation_date >= d_from,
                Reservation.reservation_date <= d_to,
            )
        )
        if branch_id:
            q = q.filter(Reservation.branch_id == branch_id)

        rows = q.all()

        status_counter: dict = {}
        country: dict = {}
        gender: dict = {}
        age: dict = {}
        source: dict = {}
        branch: dict = {}
        room: dict = {}
        nights_vals: list = []
        adults_vals: list = []
        lead_vals: list = []
        adr_vals: list = []

        bookings = 0
        revenue_total = 0.0
        nights_total = 0
        guests_total = 0
        excluded_cancelled = 0
        excluded_source = 0
        gender_known = 0
        age_known = 0
        first_stay = last_stay = None

        for r in rows:
            rev = float(r.revenue or 0)
            dropped = _crm_row_exclusion(r.status, r.source)
            is_cancelled = dropped == "cancelled"
            is_nonpaying = dropped == "non_paying_source"

            # Status mix covers every row, including the ones the table drops —
            # that is the only place the cancellations are visible at all.
            _bump_counter(status_counter, (r.status or "Unknown").strip() or "Unknown",
                          "status", rev, cancelled=is_cancelled)

            if is_cancelled:
                excluded_cancelled += 1
                continue
            if is_nonpaying:
                excluded_source += 1
                continue

            bookings += 1
            revenue_total += rev
            n = int(r.nights or 0)
            nights_total += n
            guests_total += int(r.adults or 0)

            if r.check_in_date:
                if first_stay is None or r.check_in_date < first_stay:
                    first_stay = r.check_in_date
                if last_stay is None or r.check_in_date > last_stay:
                    last_stay = r.check_in_date

            country_name = (r.guest_country or "").strip() or "Unknown"
            crow = _bump_counter(country, country_name, "country", rev, country_code=None)
            if not crow.get("country_code") and r.guest_country_code:
                crow["country_code"] = r.guest_country_code

            g = (r.gender or "").strip().upper()
            if g in ("M", "F"):
                gender_known += 1
                label = "Male" if g == "M" else "Female"
            else:
                label = "Unknown"
            _bump_counter(gender, label, "gender", rev)

            bucket = _age_bucket(_age_on(r.birth_year, r.check_in_date))
            if bucket:
                age_known += 1
            _bump_counter(age, bucket or "Unknown", "bucket", rev)

            _bump_counter(source, (r.source or "Unknown").strip() or "Unknown", "source", rev,
                          category=r.source_category)
            _bump_counter(branch, r.branch_name or "Unknown", "branch", rev)
            _bump_counter(room, (r.room_type or "Unknown").strip() or "Unknown", "room_type", rev,
                          category=r.room_type_category)

            if n > 0:
                nights_vals.append(n)
                if rev > 0:
                    adr_vals.append(rev / n)
            if r.adults:
                adults_vals.append(int(r.adults))
            if r.reservation_date and r.check_in_date:
                delta = (r.check_in_date - r.reservation_date).days
                if delta >= 0:
                    lead_vals.append(delta)

        total_rows = len(rows)
        return _envelope({
            "rate_plan_name": rate_plan,
            "currency": "VND" if not use_native else None,
            "period": {"from": d_from.isoformat(), "to": d_to.isoformat()},
            # Reconciles with the table row it opened.
            "headline": {
                "bookings": bookings,
                "nights": nights_total,
                "revenue": revenue_total,
                "adr": round(revenue_total / nights_total, 2) if nights_total else 0,
                "guests": guests_total,
            },
            "excluded": {
                "cancelled": excluded_cancelled,
                "non_paying_source": excluded_source,
                "total_rows": total_rows,
                # Of every booking ever made on this plan in the window.
                "cancel_rate": round(excluded_cancelled / total_rows * 100, 1) if total_rows else 0,
            },
            "by_status": sorted(status_counter.values(), key=lambda x: -x["bookings"]),
            "by_country": _counter_rows(country, "country", top=15),
            "by_gender": _counter_rows(gender, "gender"),
            "by_age": sorted(
                age.values(),
                key=lambda x: _AGE_BUCKETS.index(x["bucket"]) if x["bucket"] in _AGE_BUCKETS else len(_AGE_BUCKETS),
            ),
            "by_source": _counter_rows(source, "source", top=12),
            "by_branch": _counter_rows(branch, "branch"),
            "by_room_type": _counter_rows(room, "room_type", top=12),
            # Demographics are backfilled per guest and are far from complete,
            # so the UI must be able to say how much of the split is real.
            "coverage": {
                "gender_known": gender_known,
                "age_known": age_known,
                "of_bookings": bookings,
            },
            "nights_stats": _num_stats(nights_vals),
            "adults_stats": _num_stats(adults_vals),
            "lead_time_stats": _num_stats(lead_vals),
            "adr_stats": _num_stats(adr_vals),
            "first_stay": first_stay.isoformat() if first_stay else None,
            "last_stay": last_stay.isoformat() if last_stay else None,
        })
    except Exception as e:
        log.exception("crm_rate_plan_detail failed")
        return {"success": False, "data": None, "error": str(e),
                "timestamp": datetime.now(timezone.utc).isoformat()}


# ── Rate plan → campaign labels (hand-typed) ─────────────────────────────────

class RatePlanCampaignIn(BaseModel):
    rate_plan_name: str = Field(..., min_length=1, max_length=300)
    # Empty string is the "clear it" signal — the row is deleted rather than
    # kept as a blank label.
    campaign_name: str = Field("", max_length=200)


@router.get("/rate-plan-campaigns")
def get_rate_plan_campaigns(db: Session = Depends(get_db)):
    """All rate plan → campaign labels, as a flat map."""
    return _envelope(campaign_map(db))


@router.put("/rate-plan-campaigns")
def upsert_rate_plan_campaign(payload: RatePlanCampaignIn, db: Session = Depends(get_db)):
    """Set (or clear) the campaign label for one rate plan name."""
    rate_plan_name = payload.rate_plan_name.strip()
    campaign_name = payload.campaign_name.strip()
    if not rate_plan_name:
        raise HTTPException(status_code=400, detail="rate_plan_name is required")

    row = (
        db.query(RatePlanCampaign)
        .filter(RatePlanCampaign.rate_plan_name == rate_plan_name)
        .first()
    )

    if not campaign_name:
        if row:
            db.delete(row)
            db.commit()
        return _envelope({"rate_plan_name": rate_plan_name, "campaign_name": None})

    if row:
        row.campaign_name = campaign_name
    else:
        db.add(RatePlanCampaign(rate_plan_name=rate_plan_name,
                                campaign_name=campaign_name))
    db.commit()
    return _envelope({"rate_plan_name": rate_plan_name, "campaign_name": campaign_name})


# ── Seasonal campaigns ───────────────────────────────────────────────────────
#
# A seasonal push is bought through ads and sold through a rate plan, so the
# team types both keys once here and the performance endpoint joins them. See
# app/services/seasonal_campaigns.py for where every column comes from.


class SeasonalCampaignIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    ads_campaign_names: list[str] = Field(default_factory=list)
    rate_plan_names: list[str] = Field(default_factory=list)
    cost_pct: float = Field(0, ge=0, le=100)
    notes: Optional[str] = Field(None, max_length=500)
    is_active: bool = True


class SeasonalCampaignUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=200)
    ads_campaign_names: Optional[list[str]] = None
    rate_plan_names: Optional[list[str]] = None
    cost_pct: Optional[float] = Field(None, ge=0, le=100)
    notes: Optional[str] = Field(None, max_length=500)
    is_active: Optional[bool] = None


def _resolve_range(month: Optional[str], date_from: Optional[str],
                   date_to: Optional[str]) -> tuple[date, date, str]:
    """(from, to, label) for the page's Monthly / YTD picker."""
    if date_from and date_to:
        from datetime import datetime as _dt
        d_from = _dt.fromisoformat(date_from).date()
        d_to = _dt.fromisoformat(date_to).date()
        return d_from, d_to, date_from[:4]
    today = date.today()
    current_month = month or f"{today.year}-{today.month:02d}"
    d_from, d_to = _month_range(current_month)
    return d_from, d_to, current_month


@router.get("/seasonal-campaigns/performance")
def get_seasonal_campaign_performance(
    branch_id: Optional[UUID] = Query(None),
    month: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    """Ads spend vs. real rate-plan bookings, one row per seasonal campaign.

    Declared above the ``/{campaign_id}`` routes so "performance" is never
    parsed as a campaign id.
    """
    d_from, d_to, label = _resolve_range(month, date_from, date_to)
    use_native = branch_id is not None

    currency = "VND"
    if use_native:
        b = db.query(Branch).filter(Branch.id == branch_id).first()
        if b and b.currency:
            currency = b.currency.upper()

    rate = _get_rate_to_vnd(currency) if currency != "VND" else 1.0
    if use_native and currency != "VND":
        def to_view_currency(vnd):
            return _vnd_to_native(vnd, currency, rate)
    else:
        def to_view_currency(vnd):
            return vnd

    rev_col = (Reservation.grand_total_native if use_native
               else Reservation.grand_total_vnd)

    rows = build_seasonal_rows(
        db,
        list_seasonal_campaigns(db),
        d_from, d_to, branch_id,
        status_filter=_status_filter(),
        source_filter=_revenue_source_filter(),
        rev_col=rev_col,
        rate_for=_get_rate_to_vnd,
        to_view_currency=to_view_currency,
    )

    return _envelope({
        "rows": rows,
        "currency": currency,
        "period": label,
        "date_from": d_from.isoformat(),
        "date_to": d_to.isoformat(),
    })


@router.get("/seasonal-campaigns/branch-comparison")
def get_seasonal_branch_comparison(
    month: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    """Campaign x branch matrix — which branch a seasonal push actually landed in.

    Everything is VND regardless of the branch's own currency, the same choice
    /crm-branch-comparison makes: five branches on TWD/JPY/VND are only
    comparable once they are on one scale.

    Ad metrics are fetched ONCE here and handed to every per-branch pass. The
    obvious loop would call the Ads Platform five times for one page load.
    """
    d_from, d_to, label = _resolve_range(month, date_from, date_to)
    campaigns = list_seasonal_campaigns(db)

    branches = sorted(
        db.query(Branch).filter(Branch.is_active.is_(True)).all(),
        key=lambda b: _branch_display_sort_key(b.name),
    )
    branch_list = [{"branch_id": str(b.id), "name": b.name} for b in branches]

    if not campaigns:
        return _envelope({
            "branches": branch_list, "rows": [], "currency": "VND",
            "period": label,
        })

    metrics = (
        fetch_seasonal_ad_metrics(d_from, d_to)
        if any(c.ads_campaign_names for c in campaigns) else {}
    )

    # Additive across branches; ROAS is recomputed from the totals rather than
    # averaged, because a mean of ratios is not the ratio of the sums.
    _SUMMED = ("spend", "ads_bookings", "ads_revenue", "actual_bookings",
               "actual_revenue", "campaign_cost", "total_cost")

    by_campaign: dict = {}
    for b in branches:
        rows = build_seasonal_rows(
            db, campaigns, d_from, d_to, b.id,
            status_filter=_status_filter(),
            source_filter=_revenue_source_filter(),
            rev_col=Reservation.grand_total_vnd,
            rate_for=_get_rate_to_vnd,
            to_view_currency=lambda vnd: vnd,
            metrics=metrics,
        )
        for r in rows:
            entry = by_campaign.setdefault(r["id"], {
                "id": r["id"],
                "name": r["name"],
                "cost_pct": r["cost_pct"],
                # Carried so the tab's search box matches the same fields in
                # both views — people look a campaign up by the rate plan or
                # ad name behind it as often as by what they named it.
                "ads_campaign_names": r["ads_campaign_names"],
                "rate_plan_names": r["rate_plan_names"],
                "notes": r["notes"],
                "cells": {},
                "total": {k: 0.0 for k in _SUMMED},
            })
            cell = {k: (r[k] or 0) for k in _SUMMED}
            cell["roas_ads"] = r["roas_ads"]
            cell["roas_actual"] = r["roas_actual"]
            entry["cells"][str(b.id)] = cell
            for k in _SUMMED:
                entry["total"][k] += cell[k]

    result = list(by_campaign.values())
    for entry in result:
        t = entry["total"]
        t["roas_ads"] = (round(t["ads_revenue"] / t["spend"], 2)
                         if t["spend"] > 0 else None)
        t["roas_actual"] = (round(t["actual_revenue"] / t["total_cost"], 2)
                            if t["total_cost"] > 0 else None)
    result.sort(key=lambda e: -e["total"]["actual_revenue"])

    return _envelope({
        "branches": branch_list,
        "rows": result,
        "currency": "VND",
        "period": label,
        "date_from": d_from.isoformat(),
        "date_to": d_to.isoformat(),
    })


@router.get("/seasonal-campaigns")
def get_seasonal_campaigns(db: Session = Depends(get_db)):
    """The campaign definitions themselves — what the set-up dialog edits."""
    return _envelope([serialize_seasonal(c) for c in list_seasonal_campaigns(db)])


@router.post("/seasonal-campaigns")
def create_seasonal_campaign(payload: SeasonalCampaignIn,
                             db: Session = Depends(get_db)):
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    existing = (
        db.query(SeasonalCampaign)
        .filter(func.lower(SeasonalCampaign.name) == name.lower())
        .first()
    )
    if existing:
        raise HTTPException(status_code=409,
                            detail=f'A campaign named "{name}" already exists')

    row = SeasonalCampaign(
        name=name,
        ads_campaign_names=clean_campaign_patterns(payload.ads_campaign_names),
        rate_plan_names=clean_campaign_patterns(payload.rate_plan_names),
        cost_pct=payload.cost_pct,
        notes=(payload.notes or "").strip() or None,
        is_active=payload.is_active,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _envelope(serialize_seasonal(row))


@router.patch("/seasonal-campaigns/{campaign_id}")
def update_seasonal_campaign(campaign_id: UUID, payload: SeasonalCampaignUpdate,
                             db: Session = Depends(get_db)):
    row = (
        db.query(SeasonalCampaign)
        .filter(SeasonalCampaign.id == campaign_id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Campaign not found")

    if payload.name is not None:
        name = payload.name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="name cannot be blank")
        clash = (
            db.query(SeasonalCampaign)
            .filter(func.lower(SeasonalCampaign.name) == name.lower(),
                    SeasonalCampaign.id != campaign_id)
            .first()
        )
        if clash:
            raise HTTPException(
                status_code=409, detail=f'A campaign named "{name}" already exists')
        row.name = name
    if payload.ads_campaign_names is not None:
        row.ads_campaign_names = clean_campaign_patterns(payload.ads_campaign_names)
    if payload.rate_plan_names is not None:
        row.rate_plan_names = clean_campaign_patterns(payload.rate_plan_names)
    if payload.cost_pct is not None:
        row.cost_pct = payload.cost_pct
    if payload.notes is not None:
        row.notes = payload.notes.strip() or None
    if payload.is_active is not None:
        row.is_active = payload.is_active

    db.commit()
    db.refresh(row)
    return _envelope(serialize_seasonal(row))


@router.delete("/seasonal-campaigns/{campaign_id}")
def delete_seasonal_campaign(campaign_id: UUID, db: Session = Depends(get_db)):
    row = (
        db.query(SeasonalCampaign)
        .filter(SeasonalCampaign.id == campaign_id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Campaign not found")
    db.delete(row)
    db.commit()
    return _envelope({"id": str(campaign_id), "deleted": True})


# ── Activity cache helpers ────────────────────────────────────────────────────

def _cache_read(
    db: Session,
    branch_id: Optional[UUID],
    d_from: date,
    d_to: date,
) -> Optional[dict]:
    """Return {channel: {bookings, revenue_vnd}} from cache for the given range.

    Returns None if any (branch, year, month, channel) row is missing —
    meaning the cache hasn't been populated yet for this period.
    """
    # Enumerate all (year, month) pairs in range
    months = []
    cur = date(d_from.year, d_from.month, 1)
    end = date(d_to.year, d_to.month, 1)
    while cur <= end:
        months.append((cur.year, cur.month))
        cur = date(cur.year + (cur.month // 12), cur.month % 12 + 1, 1)

    branches_q = db.query(Branch).filter(Branch.is_active.is_(True))
    if branch_id:
        branches_q = branches_q.filter(Branch.id == branch_id)
    branch_ids = [b.id for b in branches_q.all()]

    q = db.query(
        MarketingActivityCache.channel,
        func.sum(MarketingActivityCache.bookings).label("bookings"),
        func.sum(MarketingActivityCache.revenue_vnd).label("revenue_vnd"),
        func.count(MarketingActivityCache.id).label("row_count"),
    ).filter(
        MarketingActivityCache.branch_id.in_(branch_ids),
        MarketingActivityCache.year.in_([y for y, _ in months]),
        MarketingActivityCache.month.in_([m for _, m in months]),
    ).group_by(MarketingActivityCache.channel).all()

    if not q:
        return None

    expected_rows = len(branch_ids) * len(months) * 2  # 2 channels
    actual_rows = sum(r.row_count for r in q)
    if actual_rows < expected_rows:
        return None

    return {
        r.channel: {
            "bookings": int(r.bookings or 0),
            "revenue_vnd": float(r.revenue_vnd or 0),
        }
        for r in q
    }


def _cache_upsert(
    db: Session,
    branch_id: UUID,
    year: int,
    month: int,
    channel: str,
    bookings: int,
    revenue_vnd: float,
):
    now = datetime.now(timezone.utc)
    existing = db.query(MarketingActivityCache).filter(
        MarketingActivityCache.branch_id == branch_id,
        MarketingActivityCache.year == year,
        MarketingActivityCache.month == month,
        MarketingActivityCache.channel == channel,
    ).first()
    if existing:
        existing.bookings = bookings
        existing.revenue_vnd = revenue_vnd
        existing.synced_at = now
        existing.updated_at = now
    else:
        db.add(MarketingActivityCache(
            branch_id=branch_id,
            year=year,
            month=month,
            channel=channel,
            bookings=bookings,
            revenue_vnd=revenue_vnd,
            synced_at=now,
        ))


def _build_overview_from_cache(
    db: Session,
    branch_id: Optional[UUID],
    d_from: date,
    d_to: date,
    use_native: bool,
    cached: dict,
):
    """Build overview dict using cached paid_ads/kol + live CRM from DB."""
    # paid_ads from cache (always VND) — convert to native if needed
    ads_c = cached.get("paid_ads", {})
    ads_bookings = ads_c.get("bookings", 0)
    ads_revenue_vnd = ads_c.get("revenue_vnd", 0.0)

    # For all-branches view, the cached KOL values are a per-branch sum that
    # misses kol_reservations rows with null hotel_id. Fetch the org total live.
    if branch_id is None:
        kol_bookings, kol_revenue_vnd = _fetch_kol_totals(db, None, d_from, d_to, False)
    else:
        kol_c = cached.get("kol", {})
        kol_bookings = kol_c.get("bookings", 0)
        kol_revenue_vnd = kol_c.get("revenue_vnd", 0.0)

    if use_native and branch_id:
        branch_obj = db.query(Branch).filter(Branch.id == branch_id).first()
        cur = (branch_obj.currency or "VND").upper() if branch_obj else "VND"
        rate = _get_rate_to_vnd(cur)
        ads_revenue = _vnd_to_native(ads_revenue_vnd, cur, rate)
        kol_revenue = _vnd_to_native(kol_revenue_vnd, cur, rate)
    else:
        ads_revenue = ads_revenue_vnd
        kol_revenue = kol_revenue_vnd

    # CRM always from DB (fast)
    rev_col = Reservation.grand_total_native if use_native else Reservation.grand_total_vnd
    crm_q = db.query(
        func.count(Reservation.id).label("bookings"),
        func.coalesce(func.sum(rev_col), 0).label("revenue"),
    ).filter(
        crm_reservation_filter(),
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

    ads_cost, kol_cost_budget, crm_cost = _budget_actuals_costs(
        db, branch_id, d_from.year, d_from.month, use_native,
        month_to=d_to.month if d_to.year == d_from.year else 12,
    )
    kol_cost = _kol_cost_for_view(db, branch_id, d_from, d_to, use_native, kol_cost_budget)

    ads_roas = round(ads_revenue / ads_cost, 2) if ads_cost > 0 else 0
    kol_roas = round(kol_revenue / kol_cost, 2) if kol_cost > 0 else 0
    crm_roas = round(crm_revenue / crm_cost, 2) if crm_cost > 0 else 0
    total_revenue = ads_revenue + kol_revenue + crm_revenue
    total_cost = ads_cost + kol_cost + crm_cost

    return {
        "paid_ads": {"bookings": ads_bookings, "revenue": ads_revenue, "cost": ads_cost, "roas": ads_roas},
        "kol": {"bookings": kol_bookings, "revenue": kol_revenue, "cost": kol_cost, "roas": kol_roas},
        "crm": {"bookings": crm_bookings, "revenue": crm_revenue, "cost": crm_cost, "roas": crm_roas},
        "total": {
            "bookings": ads_bookings + kol_bookings + crm_bookings,
            "revenue": total_revenue,
            "cost": total_cost,
            "roas": round(total_revenue / total_cost, 2) if total_cost > 0 else 0,
        },
    }


# ── Sync endpoint (called by GitHub Actions nightly cron) ─────────────────────

@router.post("/sync-cache")
def sync_marketing_activity_cache(
    months_back: int = Query(2, ge=1, le=13),
    db: Session = Depends(get_db),
):
    """Refresh marketing_activity_cache for recent months.

    Fetches Paid Ads (Ads Platform API) and KOL (KOL Engine API) per branch
    and upserts into the cache table. CRM and costs are read live from DB so
    they are not cached here.

    Called nightly by GitHub Actions — same pattern as /api/sync/run-migrations.
    ``months_back`` controls how many months to backfill (default 2 = current + prev).
    """
    today = date.today()
    months_to_sync = []
    m = f"{today.year}-{today.month:02d}"
    for _ in range(months_back):
        yr, mo = int(m[:4]), int(m[5:])
        months_to_sync.append((yr, mo))
        m = _prev_month_str(m)

    branches = db.query(Branch).filter(Branch.is_active.is_(True)).all()
    synced = []
    errors = []

    for branch in branches:
        for year, month in months_to_sync:
            d_from, d_to = _month_range(f"{year}-{month:02d}")
            label = f"{branch.name} {year}-{month:02d}"
            try:
                # Paid Ads — always store VND
                ads_b, ads_r = _fetch_paid_ads_totals(
                    db, branch.id, d_from, d_to, use_native=False,
                )
                _cache_upsert(db, branch.id, year, month, "paid_ads", ads_b, ads_r)

                # KOL — always store VND (use_native=False)
                kol_b, kol_r = _fetch_kol_totals(
                    db, branch.id, d_from, d_to, use_native=False,
                )
                _cache_upsert(db, branch.id, year, month, "kol", kol_b, kol_r)

                synced.append(label)
            except Exception as exc:
                log.error("sync_cache failed for %s: %s", label, exc)
                errors.append({"label": label, "error": str(exc)})

    db.commit()
    return _envelope({
        "synced": synced,
        "errors": errors,
        "months": [f"{y}-{m:02d}" for y, m in months_to_sync],
        "branches": len(branches),
    })


# ── CRM Branch Comparison (campaign × branch and month × branch) ─────────────

@router.get("/crm-branch-comparison")
def get_crm_branch_comparison(
    month: Optional[str] = Query(None),
    months_back: int = Query(6, ge=1, le=24),
    db: Session = Depends(get_db),
):
    """Cross-branch CRM comparison for the CRM Reservations tab.

    Returns two matrices, both in VND so the five branches (VND/TWD/JPY/THB)
    are directly comparable:
      - ``by_campaign``: rate plan × branch for the selected month
      - ``by_month``:    month × branch over a trailing ``months_back`` window

    Filtered by Date Booked (reservation_date), excluding cancelled bookings
    and non-paying sources — same rules as the rest of this page.
    """
    today = date.today()
    current_month = month or f"{today.year}-{today.month:02d}"

    d_from, d_to = _month_range(current_month)

    # Trailing window ending at (and including) current_month.
    win_start_str = current_month
    for _ in range(months_back - 1):
        win_start_str = _prev_month_str(win_start_str)
    w_from, _w0 = _month_range(win_start_str)
    _w1, w_to = _month_range(current_month)

    branches = sorted(
        db.query(Branch).filter(Branch.is_active.is_(True)).all(),
        key=lambda b: _branch_display_sort_key(b.name),
    )
    branch_list = [{"branch_id": str(b.id), "name": b.name} for b in branches]

    return _envelope({
        "branches": branch_list,
        "by_campaign": _crm_comparison_by_campaign(db, d_from, d_to),
        "by_month": _crm_comparison_by_month(db, w_from, w_to),
        "currency": "VND",
        "month": current_month,
        "months_back": months_back,
    })


def _crm_comparison_by_campaign(db: Session, d_from: date, d_to: date):
    """Rate plan × branch matrix for one month (revenue in VND)."""
    rate_plan_expr = crm_rate_plan_label_expr()
    rows = db.query(
        Reservation.branch_id,
        rate_plan_expr,
        func.count(Reservation.id).label("bookings"),
        func.coalesce(func.sum(Reservation.nights), 0).label("nights"),
        func.coalesce(func.sum(Reservation.grand_total_vnd), 0).label("revenue"),
    ).filter(
        crm_reservation_filter(),
        Reservation.reservation_date >= d_from,
        Reservation.reservation_date <= d_to,
        _status_filter(),
        _revenue_source_filter(),
    ).group_by(Reservation.branch_id, "rate_plan").all()

    by_plan: dict = {}
    for branch_id, rate_plan, bookings, nights, revenue in rows:
        entry = by_plan.setdefault(rate_plan, {
            "rate_plan_name": rate_plan,
            "cells": {},
            "total": {"bookings": 0, "nights": 0, "revenue": 0.0},
        })
        cell = {
            "bookings": int(bookings or 0),
            "nights": int(nights or 0),
            "revenue": float(revenue or 0),
        }
        entry["cells"][str(branch_id)] = cell
        entry["total"]["bookings"] += cell["bookings"]
        entry["total"]["nights"] += cell["nights"]
        entry["total"]["revenue"] += cell["revenue"]

    result = list(by_plan.values())
    # Show the campaign the team typed for each rate plan; rows nobody has
    # labelled keep their rate plan name.
    label_rows(result, campaign_map(db))
    result.sort(key=lambda x: -x["total"]["revenue"])
    return result


def _crm_comparison_by_month(db: Session, d_from: date, d_to: date):
    """Month × branch matrix over a date window (revenue in VND)."""
    rows = db.query(
        Reservation.branch_id,
        extract("year", Reservation.reservation_date).label("year"),
        extract("month", Reservation.reservation_date).label("month"),
        func.count(Reservation.id).label("bookings"),
        func.coalesce(func.sum(Reservation.nights), 0).label("nights"),
        func.coalesce(func.sum(Reservation.grand_total_vnd), 0).label("revenue"),
    ).filter(
        crm_reservation_filter(),
        Reservation.reservation_date >= d_from,
        Reservation.reservation_date <= d_to,
        _status_filter(),
        _revenue_source_filter(),
    ).group_by(Reservation.branch_id, "year", "month").all()

    by_month: dict = {}
    for branch_id, year, mon, bookings, nights, revenue in rows:
        key = f"{int(year)}-{int(mon):02d}"
        entry = by_month.setdefault(key, {
            "month": key,
            "cells": {},
            "total": {"bookings": 0, "nights": 0, "revenue": 0.0},
        })
        cell = {
            "bookings": int(bookings or 0),
            "nights": int(nights or 0),
            "revenue": float(revenue or 0),
        }
        entry["cells"][str(branch_id)] = cell
        entry["total"]["bookings"] += cell["bookings"]
        entry["total"]["nights"] += cell["nights"]
        entry["total"]["revenue"] += cell["revenue"]

    return sorted(by_month.values(), key=lambda x: x["month"])


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

        # 3. spend/daily summed across platforms — both raw and with Path B flag
        # (Path B applies dashboard's _apply_common_filters server-side)
        for label, kwargs in (
            ("spend_daily_summed_raw", {}),
            ("spend_daily_summed_path_b", {"valid_country_only": True}),
        ):
            try:
                totals = {"conversions": 0, "revenue": 0.0, "spend": 0.0}
                per_platform: dict = {}
                per_branch: dict = {}
                fields_seen2: set = set()
                first_rows = []
                for platform in ("meta", "google", "tiktok"):
                    rows = client.get_spend_daily(
                        date_from, date_to,
                        platform=platform, branch=branch, **kwargs,
                    ) or []
                    p_total = {"conversions": 0, "revenue": 0.0, "spend": 0.0, "row_count": len(rows)}
                    for r in rows:
                        fields_seen2.update(r.keys())
                        c = int(r.get("conversions") or 0)
                        rv = float(r.get("revenue") or 0)
                        sp = float(r.get("spend") or 0)
                        p_total["conversions"] += c
                        p_total["revenue"] += rv
                        p_total["spend"] += sp
                        totals["conversions"] += c
                        totals["revenue"] += rv
                        totals["spend"] += sp
                        b_key = r.get("branch") or "(unknown)"
                        b_total = per_branch.setdefault(
                            b_key,
                            {"conversions": 0, "revenue": 0.0, "spend": 0.0, "row_count": 0},
                        )
                        b_total["conversions"] += c
                        b_total["revenue"] += rv
                        b_total["spend"] += sp
                        b_total["row_count"] += 1
                    per_platform[platform] = p_total
                    if rows and len(first_rows) < 3:
                        first_rows.extend(rows[: max(0, 3 - len(first_rows))])
                api_results[label] = {
                    "request_kwargs": kwargs,
                    "branch_param": branch,
                    "totals": totals,
                    "per_platform": per_platform,
                    "per_branch": per_branch,
                    "fields_seen": sorted(fields_seen2),
                    "first_3_rows": first_rows,
                }
            except Exception as e:
                api_results[label] = {"error": repr(e), "request_kwargs": kwargs}
    except Exception as e:
        api_results["client_init_error"] = repr(e)

    return _envelope({
        "params": {"date_from": date_from, "date_to": date_to, "branch": branch},
        "local_db": local_summary,
        "ads_platform_api": api_results,
    })


@router.get("/debug/seasonal-spend")
def debug_seasonal_spend(
    ads_campaign_name: str = Query(..., description="One name pattern to resolve"),
    date_from: str = Query(...),
    date_to: str = Query(...),
    branch_id: Optional[UUID] = Query(None),
    db: Session = Depends(get_db),
):
    """Show the working behind one campaign's Spend column.

    ``/export/ads/metrics`` has no response schema published, so the reader in
    services/seasonal_campaigns.py accepts several plausible spellings of the
    spend field. Hit this to see which one upstream actually sends, which ads
    the name pattern resolved to, and what they summed to — before anyone
    argues with the number on the tab.
    """
    from datetime import datetime as _dt

    from app.services.seasonal_campaigns import (
        _ad_scope, _ads_bookings, _ads_from_metrics, _spend_vnd, fetch_ad_metrics,
    )

    df = _dt.fromisoformat(date_from).date()
    dt = _dt.fromisoformat(date_to).date()

    scope = _ad_scope(db, [ads_campaign_name], branch_id)
    keys_seen: set = set()
    metrics = fetch_ad_metrics(df, dt, keys_seen)
    matcher_bookings, matcher_revenue_vnd = _ads_bookings(
        db, scope, df, dt, branch_id,
    )

    matched = {
        ad_id: metrics.get(ad_id)
        for ad_id in sorted(scope["ad_ids"])
        if metrics and ad_id in metrics
    }
    if metrics is None:
        from_metrics = (0, 0.0, False)
    else:
        from_metrics = _ads_from_metrics(scope, metrics, _get_rate_to_vnd)

    return _envelope({
        "pattern": ads_campaign_name,
        "resolved": {
            "ads": len(scope["ad_ids"]),
            "ad_campaigns": sorted(scope["campaign_ids"]),
            "sample_ad_ids": sorted(scope["ad_ids"])[:10],
        },
        "metrics_available": metrics is not None,
        # What the export actually sends — the endpoint publishes no response
        # schema, so this is the only way to see which spellings arrived.
        "upstream_fields": sorted(keys_seen),
        "ads_with_metrics_rows": len(matched),
        "sample_metric_rows": dict(list(matched.items())[:10]),
        "spend_vnd": (
            _spend_vnd(scope, metrics, _get_rate_to_vnd)
            if metrics is not None else None
        ),
        # The two candidate sources side by side, and which one the tab took.
        "from_ad_metrics": {
            "bookings": from_metrics[0],
            "revenue_vnd": from_metrics[1],
            "usable": from_metrics[2],
        },
        "from_booking_matcher": {
            "bookings": matcher_bookings,
            "revenue_vnd": matcher_revenue_vnd,
        },
        "tab_uses": "ads_metrics" if from_metrics[2] else "booking_matches",
    })


@router.get("/debug/kol-revenue-raw")
def debug_kol_revenue_raw(
    year: int = Query(...),
    month: int = Query(...),
    hotel_id: Optional[str] = Query(None),
):
    """Return the raw KOL Engine public kol-revenue API response for debugging.

    Shows totals.revenue vs excluded.revenue so we can diagnose discrepancies
    with the Insights page.
    """
    data = fetch_kol_revenue(
        base_url=settings.KOL_ENGINE_URL,
        org_slug=settings.KOL_TARGETS_ORG_SLUG,
        api_key=settings.KOL_REVENUE_API_SECRET,
        year=year,
        month=month,
        hotel_id=hotel_id or None,
    )
    return _envelope(data)
