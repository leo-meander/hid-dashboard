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

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import SessionLocal, get_db
from app.models.ads import AdsPerformance
from app.models.ads_booking_match import AdsBookingMatch
from app.models.branch import Branch
from app.models.reservation import Reservation
from app.routers.marketing_budget import ActualsCache, _get_rate_to_vnd, _vnd_to_native
from app.services.ads_platform import branch_slug_for, get_client as _get_ads_client
from app.services.crm_filters import crm_rate_plan_label_expr, crm_reservation_filter
from app.services.kol_engine import fetch_kol_revenue, resolve_hotel_id_from_branch_name
from app.config import settings

router = APIRouter()
log = logging.getLogger(__name__)

# Status exclusion: cancelled / no-show (lowercase canonical, matches Cloudbeds sync normalization)
_EXCLUDED_STATUSES = {"cancelled", "canceled", "no_show", "noshow", "no show", "no-show", "cancelled_by_guest"}

# Revenue exclusion: non-paying guests
_EXCLUDED_SOURCES = {"blogger", "house use", "houseuse", "special case", "work exchange"}


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

    # Run the three independent blocks in parallel — each opens its own
    # DB session so SQLAlchemy stays thread-safe. _build_overview is
    # HTTP-heavy (3 Ads Platform + 1 KOL Engine call each), so wallclock
    # collapses to roughly the slowest task instead of summing.
    def _with_session(fn):
        session = SessionLocal()
        try:
            return fn(session)
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=3) as ex:
        f_cur = ex.submit(_with_session, lambda s: _build_overview(s, branch_id, d_from, d_to, use_native))
        f_prev = ex.submit(_with_session, lambda s: _build_overview(s, branch_id, p_from, p_to, use_native))
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
    })


# ── KOL totals: KOL Engine API → Cloudbeds fallback ──────────────────────────


def _fetch_kol_totals_cloudbeds(db, branch_id, d_from, d_to, use_native):
    """Local Cloudbeds aggregation — used as fallback when KOL Engine API
    is unreachable. NOT de-duped against Ads Platform attribution, so the
    card may inflate for May 2026+ data when the API returns None."""
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


def _fetch_kol_totals(db, branch_id, d_from, d_to, use_native):
    """Pull KOL bookings/revenue from KOL Engine public API.

    Single-branch view returns native currency from the matching branches[]
    row; all-branches view sums revenue_vnd across the response. The API
    pre-excludes Blogger/House Use/Special Case + bookings the Ads Platform
    already attributes to ads (cutoff 2026-05-01) — single source of truth.

    Falls back to local Cloudbeds aggregation on any API failure.
    """
    branch_obj = None
    hotel_id = None
    if branch_id:
        branch_obj = db.query(Branch).filter(Branch.id == branch_id).first()
        if branch_obj:
            hotel_id = resolve_hotel_id_from_branch_name(branch_obj.name)

    data = fetch_kol_revenue(
        base_url=settings.KOL_ENGINE_URL,
        org_slug=settings.KOL_TARGETS_ORG_SLUG,
        api_key=settings.KOL_REVENUE_API_SECRET,
        year=d_from.year,
        month=d_from.month,
        hotel_id=hotel_id,
    )
    if data is None:
        log.warning(
            "KOL revenue API unavailable for %s-%s — falling back to Cloudbeds",
            d_from.year, d_from.month,
        )
        return _fetch_kol_totals_cloudbeds(db, branch_id, d_from, d_to, use_native)

    branches = data.get("branches") or []

    if branch_id:
        # Single-branch — match by hotel_id, return native revenue.
        if not hotel_id:
            log.warning(
                "No KOL Engine hotel_id mapping for branch %s — falling back",
                branch_obj.name if branch_obj else branch_id,
            )
            return _fetch_kol_totals_cloudbeds(db, branch_id, d_from, d_to, use_native)
        match = next((b for b in branches if b.get("hotel_id") == hotel_id), None)
        if not match:
            return 0, 0.0
        return int(match.get("bookings") or 0), float(match.get("revenue") or 0)

    # All branches — sum VND-equivalent.
    totals = data.get("totals") or {}
    bookings = int(totals.get("bookings") or 0)
    revenue = sum(float(b.get("revenue_vnd") or 0) for b in branches)
    return bookings, revenue


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
