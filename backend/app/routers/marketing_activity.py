"""
Marketing Activity router — consolidated view of Paid Ads, KOL, and CRM performance.

Data sources (post migration 028):
  - Paid Ads: local ``ads_performance`` table, populated by the Ads Platform
              sync service (grain='daily' for totals, grain='ad' for country
              breakdown).
  - KOL:      Cloudbeds reservations (room_type ILIKE '%KOL_%')
  - CRM:      Cloudbeds reservations (CRM/MEANDER'S FRIEND/Travel guide/Grand Open)

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
from app.models.branch import Branch
from app.models.reservation import Reservation

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


def _fetch_daily_ads_rows(
    db: Session,
    branch_id: Optional[UUID],
    d_from: date,
    d_to: date,
) -> list[dict]:
    """Daily-grain ads spend rows, one per (branch, channel, date, account).

    The ``grain='daily'`` rows carry authoritative spend / revenue / bookings
    aggregates — they are what KPI totals SUM on. Country is not present at
    this grain (see ``_fetch_ad_country_rows``).
    """
    q = (
        db.query(
            AdsPerformance.branch_id.label("branch_id"),
            Branch.name.label("branch_name"),
            Branch.currency.label("currency"),
            AdsPerformance.channel,
            AdsPerformance.funnel_stage.label("funnel"),
            AdsPerformance.date_from.label("date"),
            func.coalesce(AdsPerformance.cost_native, 0).label("cost_native"),
            func.coalesce(AdsPerformance.cost_vnd, 0).label("cost_vnd"),
            func.coalesce(AdsPerformance.revenue_native, 0).label("revenue_native"),
            func.coalesce(AdsPerformance.revenue_vnd, 0).label("revenue_vnd"),
            func.coalesce(AdsPerformance.bookings, 0).label("bookings"),
        )
        .join(Branch, Branch.id == AdsPerformance.branch_id)
        .filter(
            AdsPerformance.grain == "daily",
            AdsPerformance.date_from >= d_from,
            AdsPerformance.date_from <= d_to,
        )
    )
    if branch_id is not None:
        q = q.filter(AdsPerformance.branch_id == branch_id)

    return [
        {
            "branch_id": str(r.branch_id),
            "branch_name": r.branch_name,
            "currency": r.currency or "VND",
            "channel": r.channel or "",
            "funnel": r.funnel or "",
            "date": r.date,
            "cost_native": float(r.cost_native or 0),
            "cost_vnd": float(r.cost_vnd or 0),
            "revenue_native": float(r.revenue_native or 0),
            "revenue_vnd": float(r.revenue_vnd or 0),
            "bookings": int(r.bookings or 0),
            "mkt_activity": "Paid Ads",
        }
        for r in q.all()
    ]


def _fetch_ad_country_rows(
    db: Session,
    branch_id: Optional[UUID],
    d_from: date,
    d_to: date,
) -> list[dict]:
    """Per-ad rows with ``target_country``. Used to build the By-Country tab
    (daily-grain rows don't carry country). Spend/revenue here are per-ad
    cumulative values across the ad's full lifetime — see
    ``_build_monthly_by_country`` for how we re-weight against daily totals.
    """
    q = (
        db.query(
            AdsPerformance.branch_id.label("branch_id"),
            Branch.name.label("branch_name"),
            Branch.currency.label("currency"),
            AdsPerformance.channel,
            AdsPerformance.target_country.label("country"),
            func.coalesce(AdsPerformance.cost_native, 0).label("cost_native"),
            func.coalesce(AdsPerformance.cost_vnd, 0).label("cost_vnd"),
            func.coalesce(AdsPerformance.revenue_native, 0).label("revenue_native"),
            func.coalesce(AdsPerformance.revenue_vnd, 0).label("revenue_vnd"),
            func.coalesce(AdsPerformance.bookings, 0).label("bookings"),
        )
        .join(Branch, Branch.id == AdsPerformance.branch_id)
        .filter(
            AdsPerformance.grain == "ad",
            # Fall back to all-time ad rows when date_from is null (metadata-only).
            or_(
                AdsPerformance.date_from.is_(None),
                AdsPerformance.date_from <= d_to,
            ),
        )
    )
    if branch_id is not None:
        q = q.filter(AdsPerformance.branch_id == branch_id)

    return [
        {
            "branch_id": str(r.branch_id),
            "currency": r.currency or "VND",
            "channel": r.channel or "",
            "country": r.country or "",
            "cost_native": float(r.cost_native or 0),
            "cost_vnd": float(r.cost_vnd or 0),
            "revenue_native": float(r.revenue_native or 0),
            "revenue_vnd": float(r.revenue_vnd or 0),
            "bookings": int(r.bookings or 0),
        }
        for r in q.all()
    ]


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

    # Ads data now lives in local ads_performance (populated by Ads Platform sync).
    ads_cur = _fetch_daily_ads_rows(db, branch_id, d_from, d_to)
    ads_prev = _fetch_daily_ads_rows(db, branch_id, p_from, p_to)
    ads_country = _fetch_ad_country_rows(db, branch_id, d_from, d_to)

    use_native = branch_id is not None

    overview_cur = _build_overview(db, branch_id, d_from, d_to, ads_cur, use_native)
    overview_prev = _build_overview(db, branch_id, p_from, p_to, ads_prev, use_native)
    monthly = _build_monthly_by_country(db, branch_id, d_from, d_to, ads_country, use_native)
    suggestions = _build_kol_suggestions(db, branch_id, d_from, d_to)
    crm_rate_plans = _build_crm_by_rate_plan(db, branch_id, d_from, d_to, use_native)

    currency = "VND"
    if use_native and ads_cur:
        currency = ads_cur[0].get("currency", "VND")

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

def _build_overview(db, branch_id, d_from, d_to, ads_rows, use_native):
    cost_key = "cost_native" if use_native else "cost_vnd"
    rev_key = "revenue_native" if use_native else "revenue_vnd"

    # Paid Ads (from Google Sheet)
    ads_bookings = sum(r["bookings"] for r in ads_rows)
    ads_revenue = sum(r[rev_key] for r in ads_rows)
    ads_cost = sum(r[cost_key] for r in ads_rows)
    ads_roas = round(ads_revenue / ads_cost, 2) if ads_cost > 0 else 0

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

    # KOL cost
    cost_col = KOLRecord.cost_native if use_native else KOLRecord.cost_vnd
    kol_cost_q = db.query(func.coalesce(func.sum(cost_col), 0))
    if branch_id:
        kol_cost_q = kol_cost_q.filter(KOLRecord.branch_id == branch_id)
    kol_cost = float(kol_cost_q.scalar() or 0)

    # CRM (from Cloudbeds) — excludes Blogger/House Use/Special Case
    crm_q = db.query(
        func.count(Reservation.id).label("bookings"),
        func.coalesce(func.sum(rev_col), 0).label("revenue"),
    ).filter(
        _crm_filter(),
        Reservation.check_in_date >= d_from,
        Reservation.check_in_date <= d_to,
        _status_filter(),
        _revenue_source_filter(),
    )
    if branch_id:
        crm_q = crm_q.filter(Reservation.branch_id == branch_id)
    crm_row = crm_q.one()
    crm_bookings = int(crm_row.bookings)
    crm_revenue = float(crm_row.revenue)

    total_bookings = ads_bookings + kol_bookings + crm_bookings
    total_revenue = ads_revenue + kol_revenue + crm_revenue
    total_cost = ads_cost + kol_cost
    total_roas = round(total_revenue / total_cost, 2) if total_cost > 0 else 0

    return {
        "paid_ads": {"bookings": ads_bookings, "revenue": ads_revenue, "cost": ads_cost, "roas": ads_roas},
        "kol": {"bookings": kol_bookings, "revenue": kol_revenue, "cost": kol_cost},
        "crm": {"bookings": crm_bookings, "revenue": crm_revenue},
        "total": {"bookings": total_bookings, "revenue": total_revenue, "cost": total_cost, "roas": total_roas},
    }


# ── Monthly by Country ───────────────────────────────────────────────────────

def _build_monthly_by_country(db, branch_id, d_from, d_to, ads_rows, use_native):
    grid = defaultdict(lambda: {
        "paid_ads": {"bookings": 0, "revenue": 0, "cost": 0},
        "kol": {"bookings": 0, "revenue": 0},
        "crm": {"bookings": 0, "revenue": 0},
    })

    cost_key = "cost_native" if use_native else "cost_vnd"
    rev_key = "revenue_native" if use_native else "revenue_vnd"

    for r in ads_rows:
        country = r["country"] or "Unknown"
        grid[country]["paid_ads"]["bookings"] += r["bookings"]
        grid[country]["paid_ads"]["revenue"] += r[rev_key]
        grid[country]["paid_ads"]["cost"] += r[cost_key]

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

    # CRM — excludes non-paying
    crm_q = db.query(
        Reservation.guest_country_code,
        func.count(Reservation.id),
        func.coalesce(func.sum(rev_col), 0),
    ).filter(
        _crm_filter(),
        Reservation.check_in_date >= d_from,
        Reservation.check_in_date <= d_to,
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
        if data["paid_ads"]["bookings"] > 0 or data["paid_ads"]["cost"] > 0:
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
        Reservation.check_in_date >= d_from,
        Reservation.check_in_date <= d_to,
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
