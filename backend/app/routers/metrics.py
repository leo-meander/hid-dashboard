"""
Metrics router — Phase 2
Daily / Weekly / Monthly performance metrics + Country YoY comparison.
"""
import logging
import calendar
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from uuid import UUID
from collections import defaultdict

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, extract
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.branch import Branch
from app.models.reservation import Reservation
from app.services.metrics_engine import (
    EXCLUDED_STATUSES,
    get_daily_metrics,
    get_ota_mix,
    get_channel_rates,
    get_ota_trend,
    get_rates_trend,
    get_country_yoy,
    get_country_yoy_insights_local,
)

logger = logging.getLogger(__name__)

# Re-export under the legacy name for backward compatibility — report.py
# imports _EXCLUDED_STATUSES from this module. The canonical lowercase set
# lives in metrics_engine.EXCLUDED_STATUSES (covers cancelled/canceled/
# no_show/noshow/no show/no-show). Comparisons MUST go through
# func.lower() because Cloudbeds stores statuses lowercase like "canceled"
# while older code paths assumed Title Case "Canceled" — that mismatch was
# silently letting cancelled rows leak into Country / OTA dashboards.
_EXCLUDED_STATUSES = EXCLUDED_STATUSES
_EXCLUDED_SOURCES_REV = {"blogger", "house use", "houseuse", "special case", "work exchange"}


def _status_active_filter():
    """Reservation.status NOT in excluded set, case-insensitive + NULL-safe."""
    return ~func.lower(func.coalesce(Reservation.status, "")).in_(list(EXCLUDED_STATUSES))

router = APIRouter()


def _envelope(data):
    return {
        "success": True,
        "data": data,
        "error": None,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _last_reservations_synced_at(db: Session, branch_id: Optional[UUID]) -> Optional[str]:
    """Return ISO timestamp of latest reservations.updated_at for the given branch
    (or all branches if branch_id is None). Used to surface a freshness badge on
    derived views (OTA mix, channel rates, country views) — those views aggregate
    reservations directly, not daily_metrics.
    """
    q = db.query(func.max(Reservation.updated_at))
    if branch_id:
        q = q.filter(Reservation.branch_id == branch_id)
    ts = q.scalar()
    return ts.isoformat() if ts else None


def _dm_to_dict(dm) -> dict:
    return {
        "id": str(dm.id),
        "branch_id": str(dm.branch_id),
        "date": dm.date.isoformat(),
        "rooms_sold": dm.rooms_sold,
        "dorms_sold": dm.dorms_sold,
        "total_sold": dm.total_sold,
        "occ_pct": float(dm.occ_pct or 0),
        "room_occ_pct": float(dm.room_occ_pct) if dm.room_occ_pct is not None else None,
        "dorm_occ_pct": float(dm.dorm_occ_pct) if dm.dorm_occ_pct is not None else None,
        "revenue_native": float(dm.revenue_native or 0),
        "revenue_vnd": float(dm.revenue_vnd or 0),
        "adr_native": float(dm.adr_native or 0),
        "revpar_native": float(dm.revpar_native or 0),
        "new_bookings": dm.new_bookings,
        "cancellations": dm.cancellations,
        "cancellation_pct": float(dm.cancellation_pct or 0),
        "computed_at": dm.computed_at.isoformat() if dm.computed_at else None,
    }


# ── Daily ──────────────────────────────────────────────────────────────────────

@router.get("/daily")
def get_daily(
    branch_id: Optional[UUID] = Query(None),
    date_from: Optional[date] = Query(None),
    date_to: Optional[date] = Query(None),
    db: Session = Depends(get_db),
):
    """Daily metrics. Defaults to last 30 days if no range given."""
    today = datetime.now(timezone.utc).date()
    if date_to is None:
        date_to = today
    if date_from is None:
        date_from = date_to - timedelta(days=29)

    rows = get_daily_metrics(db, branch_id, date_from, date_to)
    return _envelope([_dm_to_dict(r) for r in rows])


# ── Weekly ─────────────────────────────────────────────────────────────────────

@router.get("/weekly")
def get_weekly(
    branch_id: Optional[UUID] = Query(None),
    date_from: Optional[date] = Query(None),
    date_to: Optional[date] = Query(None),
    db: Session = Depends(get_db),
):
    """
    Weekly aggregation of daily_metrics.
    Groups by ISO week (Monday-Sunday).
    """
    today = datetime.now(timezone.utc).date()
    if date_to is None:
        date_to = today
    if date_from is None:
        date_from = date_to - timedelta(weeks=12)

    rows = get_daily_metrics(db, branch_id, date_from, date_to)

    # Latest computed_at across all daily_metrics that feed this aggregation —
    # surfaced to FE as data_synced_at so dashboards show "Last synced: …".
    last_synced = max(
        (dm.computed_at for dm in rows if dm.computed_at), default=None
    )
    last_synced_iso = last_synced.isoformat() if last_synced else None

    # Group by branch + ISO week
    from collections import defaultdict
    weekly: dict = defaultdict(lambda: {
        "rooms_sold": 0, "dorms_sold": 0, "total_sold": 0,
        "revenue_native": 0.0, "revenue_vnd": 0.0,
        "new_bookings": 0, "cancellations": 0,
        "occ_sum": 0.0, "cancel_pct_sum": 0.0, "day_count": 0,
    })

    for dm in rows:
        iso = dm.date.isocalendar()
        key = (str(dm.branch_id), iso.year, iso.week)
        w = weekly[key]
        w["branch_id"] = str(dm.branch_id)
        w["year"] = iso.year
        w["week"] = iso.week
        # Week start = Monday
        w["week_start"] = (dm.date - timedelta(days=dm.date.weekday())).isoformat()
        w["rooms_sold"] += dm.rooms_sold or 0
        w["dorms_sold"] += dm.dorms_sold or 0
        w["total_sold"] += dm.total_sold or 0
        w["revenue_native"] += float(dm.revenue_native or 0)
        w["revenue_vnd"] += float(dm.revenue_vnd or 0)
        w["new_bookings"] += dm.new_bookings or 0
        w["cancellations"] += dm.cancellations or 0
        w["occ_sum"] += float(dm.occ_pct or 0)
        w["cancel_pct_sum"] += float(dm.cancellation_pct or 0)
        w["day_count"] += 1

    result = []
    for w in weekly.values():
        n = w["day_count"]
        sold = w["total_sold"]
        # Weighted ADR = SUM(revenue) / SUM(rooms_sold) — industry standard
        avg_adr = round(w["revenue_native"] / sold, 2) if sold > 0 else 0
        # Weighted RevPAR = revenue / (days × total_rooms) — use OCC × ADR as proxy
        avg_occ = round(w["occ_sum"] / n, 4) if n > 0 else 0
        avg_revpar = round(avg_occ * avg_adr, 2)
        # Cancel rate = average of daily cancellation_pct (each daily pct is already
        # correctly computed as cancelled_checkins / total_checkins for that date)
        avg_cancel_pct = round(w["cancel_pct_sum"] / n, 4) if n > 0 else 0
        result.append({
            "branch_id": w.get("branch_id"),
            "year": w.get("year"),
            "week": w.get("week"),
            "week_start": w.get("week_start"),
            "rooms_sold": w["rooms_sold"],
            "dorms_sold": w["dorms_sold"],
            "total_sold": sold,
            "revenue_native": round(w["revenue_native"], 2),
            "revenue_vnd": round(w["revenue_vnd"], 2),
            "new_bookings": w["new_bookings"],
            "cancellations": w["cancellations"],
            "cancellation_pct": avg_cancel_pct,
            "avg_occ_pct": avg_occ,
            "avg_adr_native": avg_adr,
            "avg_revpar_native": avg_revpar,
            "data_synced_at": last_synced_iso,
        })

    result.sort(key=lambda x: (x.get("branch_id", ""), x.get("year", 0), x.get("week", 0)))
    return _envelope(result)


# ── Monthly ────────────────────────────────────────────────────────────────────

@router.get("/monthly")
def get_monthly(
    branch_id: Optional[UUID] = Query(None),
    year_from: Optional[int] = Query(None),
    year_to: Optional[int] = Query(None),
    db: Session = Depends(get_db),
):
    """
    Monthly aggregation. Defaults to current year + previous year.
    Also returns country breakdown per month.
    """
    from sqlalchemy import func
    from app.models.reservation import Reservation
    from app.models.daily_metrics import DailyMetrics
    from collections import defaultdict

    today = datetime.now(timezone.utc).date()
    if year_to is None:
        year_to = today.year
    if year_from is None:
        year_from = year_to - 1

    date_from = date(year_from, 1, 1)
    date_to = date(year_to, 12, 31)

    rows = get_daily_metrics(db, branch_id, date_from, date_to)

    last_synced = max(
        (dm.computed_at for dm in rows if dm.computed_at), default=None
    )
    last_synced_iso = last_synced.isoformat() if last_synced else None

    monthly: dict = defaultdict(lambda: {
        "rooms_sold": 0, "dorms_sold": 0, "total_sold": 0,
        "revenue_native": 0.0, "revenue_vnd": 0.0,
        "new_bookings": 0, "cancellations": 0,
        "occ_sum": 0.0, "cancel_pct_sum": 0.0, "day_count": 0,
    })

    for dm in rows:
        key = (str(dm.branch_id), dm.date.year, dm.date.month)
        m = monthly[key]
        m["branch_id"] = str(dm.branch_id)
        m["year"] = dm.date.year
        m["month"] = dm.date.month
        m["rooms_sold"] += dm.rooms_sold or 0
        m["dorms_sold"] += dm.dorms_sold or 0
        m["total_sold"] += dm.total_sold or 0
        m["revenue_native"] += float(dm.revenue_native or 0)
        m["revenue_vnd"] += float(dm.revenue_vnd or 0)
        m["new_bookings"] += dm.new_bookings or 0
        m["cancellations"] += dm.cancellations or 0
        m["occ_sum"] += float(dm.occ_pct or 0)
        m["cancel_pct_sum"] += float(dm.cancellation_pct or 0)
        m["day_count"] += 1

    # Country breakdown per month
    country_q = (
        db.query(
            func.extract("year", Reservation.check_in_date).label("year"),
            func.extract("month", Reservation.check_in_date).label("month"),
            Reservation.guest_country_code,
            Reservation.guest_country,
            func.count(Reservation.id).label("count"),
        )
        .filter(
            Reservation.check_in_date >= date_from,
            Reservation.check_in_date <= date_to,
            _status_active_filter(),
        )
    )
    if branch_id:
        country_q = country_q.filter(Reservation.branch_id == branch_id)

    country_rows = country_q.group_by(
        "year", "month",
        Reservation.guest_country_code,
        Reservation.guest_country,
    ).all()

    country_by_month: dict = defaultdict(list)
    for r in country_rows:
        country_by_month[(int(r.year), int(r.month))].append({
            "country_code": r.guest_country_code,
            "country": r.guest_country,
            "count": r.count,
        })

    result = []
    for m in monthly.values():
        n = m["day_count"]
        sold = m["total_sold"]
        ym = (m["year"], m["month"])
        # Weighted ADR = SUM(revenue) / SUM(rooms_sold) — industry standard
        avg_adr = round(m["revenue_native"] / sold, 2) if sold > 0 else 0
        avg_occ = round(m["occ_sum"] / n, 4) if n > 0 else 0
        avg_revpar = round(avg_occ * avg_adr, 2)
        # Cancel rate = average of daily cancellation_pct
        avg_cancel_pct = round(m["cancel_pct_sum"] / n, 4) if n > 0 else 0
        result.append({
            "branch_id": m.get("branch_id"),
            "year": m.get("year"),
            "month": m.get("month"),
            "rooms_sold": m["rooms_sold"],
            "dorms_sold": m["dorms_sold"],
            "total_sold": sold,
            "revenue_native": round(m["revenue_native"], 2),
            "revenue_vnd": round(m["revenue_vnd"], 2),
            "new_bookings": m["new_bookings"],
            "cancellations": m["cancellations"],
            "cancellation_pct": avg_cancel_pct,
            "avg_occ_pct": avg_occ,
            "avg_adr_native": avg_adr,
            "avg_revpar_native": avg_revpar,
            "country_breakdown": sorted(
                country_by_month.get(ym, []),
                key=lambda x: x["count"],
                reverse=True,
            )[:20],
            "data_synced_at": last_synced_iso,
        })

    result.sort(key=lambda x: (x.get("branch_id", ""), x.get("year", 0), x.get("month", 0)))
    return _envelope(result)


# ── OTA Mix ────────────────────────────────────────────────────────────────────

@router.get("/ota-mix")
def get_ota_mix_endpoint(
    branch_id: Optional[UUID] = Query(None),
    date_from: Optional[date] = Query(None),
    date_to: Optional[date] = Query(None),
    db: Session = Depends(get_db),
):
    """Channel mix — Direct aggregated, each OTA source shown individually."""
    today = datetime.now(timezone.utc).date()
    if date_to is None:
        date_to = today
    if date_from is None:
        date_from = date_to - timedelta(days=29)

    mix = get_ota_mix(db, branch_id, date_from, date_to)
    total_count = sum(v["count"] for v in mix.values())
    total_revenue = sum(v["revenue_native"] for v in mix.values())

    last_synced_iso = _last_reservations_synced_at(db, branch_id)
    result = []
    for channel, vals in sorted(mix.items(), key=lambda x: -x[1]["count"]):
        result.append({
            "category": vals["category"],
            "channel": channel,
            "count": vals["count"],
            "revenue_native": vals["revenue_native"],
            "revenue_vnd": vals["revenue_vnd"],
            "count_pct": round(vals["count"] / total_count, 4) if total_count > 0 else 0,
            "revenue_pct": round(vals["revenue_native"] / total_revenue, 4) if total_revenue > 0 else 0,
            "data_synced_at": last_synced_iso,
        })
    return _envelope(result)


@router.get("/channel-rates")
def get_channel_rates_endpoint(
    branch_id: Optional[UUID] = Query(None),
    date_from: Optional[date] = Query(None),
    date_to: Optional[date] = Query(None),
    db: Session = Depends(get_db),
):
    """Cancellation rate and check-in rate by channel (individual OTA or Direct)."""
    today = datetime.now(timezone.utc).date()
    if date_to is None:
        date_to = today
    if date_from is None:
        date_from = date_to - timedelta(days=29)

    result = get_channel_rates(db, branch_id, date_from, date_to)
    last_synced_iso = _last_reservations_synced_at(db, branch_id)
    if isinstance(result, list):
        for row in result:
            if isinstance(row, dict):
                row["data_synced_at"] = last_synced_iso
    elif isinstance(result, dict):
        result["data_synced_at"] = last_synced_iso
    return _envelope(result)


@router.get("/ota-trend")
def get_ota_trend_endpoint(
    mode: str = Query("daily", pattern="^(daily|weekly|monthly)$"),
    branch_id: Optional[UUID] = Query(None),
    db: Session = Depends(get_db),
):
    """OTA channel share pivot: % per period (daily/weekly/monthly)."""
    result = get_ota_trend(db, branch_id, mode)
    last_synced_iso = _last_reservations_synced_at(db, branch_id)
    if isinstance(result, dict):
        result["data_synced_at"] = last_synced_iso
    return _envelope(result)


@router.get("/rates-trend")
def get_rates_trend_endpoint(
    mode: str = Query("daily", pattern="^(daily|weekly|monthly)$"),
    branch_id: Optional[UUID] = Query(None),
    date_type: str = Query("check_in", pattern="^(check_in|booked)$"),
    db: Session = Depends(get_db),
):
    """Cancel rate & check-in rate pivot per channel × period."""
    result = get_rates_trend(db, branch_id, mode, date_type)
    last_synced_iso = _last_reservations_synced_at(db, branch_id)
    if isinstance(result, dict):
        result["data_synced_at"] = last_synced_iso
    return _envelope(result)


# ── Country YoY ────────────────────────────────────────────────────────────────

@router.get("/country-yoy")
def get_country_yoy_endpoint(
    year: int = Query(...),
    month: Optional[int] = Query(None),
    branch_id: Optional[UUID] = Query(None),
    db: Session = Depends(get_db),
):
    """Country YoY comparison: current year vs previous year."""
    rows = get_country_yoy(db, branch_id, year, month)
    last_synced_iso = _last_reservations_synced_at(db, branch_id)
    if isinstance(rows, list):
        for r in rows:
            if isinstance(r, dict):
                r["data_synced_at"] = last_synced_iso
    return _envelope(rows)


# ── Country YoY via Cloudbeds Insights API ─────────────────────────────────────

@router.get("/country-yoy-insights")
def get_country_yoy_insights(
    year: int = Query(None),
    month: int = Query(None, ge=1, le=12),
    branch_id: Optional[UUID] = Query(None),
    date_type: str = Query("check_in", regex="^(check_in|booked)$"),
    db: Session = Depends(get_db),
):
    """
    Country YoY comparison — local DB first, Cloudbeds Data Insights fallback.

    `date_type`: "check_in" buckets by check-in month (default);
                 "booked" buckets by reservation_date (when booking was made).
    Cloudbeds Insights fallback only supports check-in date — the booked-date
    view always reads from local DB.
    """
    today = date.today()
    if year is None:
        year = today.year
    if month is None:
        month = today.month

    # Currency: branch-scoped → native; All-Branches → VND.
    currency = "VND"
    if branch_id:
        b = db.query(Branch.currency).filter(Branch.id == branch_id).first()
        if b and b.currency:
            currency = b.currency

    try:
        # ── Primary: local DB ─────────────────────────────────────────────
        local = get_country_yoy_insights_local(db, branch_id, year, month, date_type)
        current_totals = local["current"]
        prev_totals = local["previous"]

        # ── Fallback: Cloudbeds Data Insights API (if local is empty) ─────
        # Cloudbeds Insights only supports check-in based grouping, so we
        # only fall back when date_type=check_in.
        if not current_totals and not prev_totals and date_type == "check_in":
            logger.info("Local DB empty for country YoY %d-%02d, trying Cloudbeds", year, month)
            current_totals, prev_totals = _fetch_country_yoy_cloudbeds(
                db, branch_id, year, month,
            )

        # ── Build response rows ───────────────────────────────────────────
        all_countries = set(current_totals.keys()) | set(prev_totals.keys())
        rows = []
        for country in all_countries:
            curr_d = current_totals.get(country, {"nights": 0, "revenue": 0, "guests": 0})
            prev_d = prev_totals.get(country, {"nights": 0, "revenue": 0, "guests": 0})

            def pct_change(curr_val, prev_val):
                if prev_val == 0:
                    return None if curr_val == 0 else 100.0
                return round(((curr_val - prev_val) / prev_val) * 100, 2)

            rows.append({
                "country": country,
                "current_nights": curr_d["nights"],
                "current_revenue": curr_d["revenue"],
                "current_guests": curr_d["guests"],
                "prev_nights": prev_d["nights"],
                "prev_revenue": prev_d["revenue"],
                "prev_guests": prev_d["guests"],
                "nights_change_pct": pct_change(curr_d["nights"], prev_d["nights"]),
                "revenue_change_pct": pct_change(curr_d["revenue"], prev_d["revenue"]),
                "guests_change_pct": pct_change(curr_d["guests"], prev_d["guests"]),
            })

        rows.sort(key=lambda r: r["current_nights"], reverse=True)

        return _envelope({
            "year": year,
            "month": month,
            "countries": rows,
            "currency": currency,
            "date_type": date_type,
            "data_synced_at": _last_reservations_synced_at(db, branch_id),
        })
    except Exception as exc:
        logger.exception("country-yoy-insights failed: %s", exc)
        return _envelope({
            "year": year,
            "month": month,
            "countries": [],
            "currency": currency,
            "date_type": date_type,
            "data_synced_at": _last_reservations_synced_at(db, branch_id),
        })


def _fetch_country_yoy_cloudbeds(
    db: Session,
    branch_id: Optional[UUID],
    year: int,
    month: int,
) -> tuple[dict, dict]:
    """Cloudbeds Data Insights fallback for country YoY."""
    from app.models.branch import Branch
    from app.services.cloudbeds import fetch_country_insights
    from app.config import settings

    q = db.query(Branch).filter(Branch.is_active.is_(True))
    if branch_id:
        q = q.filter(Branch.id == branch_id)
    branches = q.all()

    current_totals: dict[str, dict] = {}
    prev_totals: dict[str, dict] = {}

    for branch in branches:
        pid = branch.cloudbeds_property_id
        if not pid:
            continue
        api_key = settings.get_api_key_for_property(str(pid))
        if not api_key:
            continue

        try:
            curr = fetch_country_insights(str(pid), api_key, year, month)
        except Exception as exc:
            logger.warning("Country insights current failed %s: %s", branch.name, exc)
            curr = {}

        try:
            prev = fetch_country_insights(str(pid), api_key, year - 1, month)
        except Exception as exc:
            logger.warning("Country insights prev failed %s: %s", branch.name, exc)
            prev = {}

        for country, data in curr.items():
            if country not in current_totals:
                current_totals[country] = {"nights": 0, "revenue": 0, "guests": 0}
            current_totals[country]["nights"] += data["nights"]
            current_totals[country]["revenue"] += data["revenue"]
            current_totals[country]["guests"] += data["guests"]

        for country, data in prev.items():
            if country not in prev_totals:
                prev_totals[country] = {"nights": 0, "revenue": 0, "guests": 0}
            prev_totals[country]["nights"] += data["nights"]
            prev_totals[country]["revenue"] += data["revenue"]
            prev_totals[country]["guests"] += data["guests"]

    return current_totals, prev_totals



# ── Country Reservations Trend (7 weeks / 7 months, all countries) ───────────

@router.get("/country-reservations")
def get_country_reservations(
    view: str = Query("monthly", regex="^(weekly|monthly)$"),
    branch_id: Optional[UUID] = Query(None),
    limit: int = Query(500, ge=1, le=500),
    date_type: str = Query("check_in", regex="^(check_in|booked)$"),
    db: Session = Depends(get_db),
):
    """
    Top N countries with trend data over 7 periods.
    - monthly: last 7 months, grouped by month
    - weekly: last 7 weeks, grouped by ISO week
    - date_type: "check_in" filters/groups by check_in_date (default);
                 "booked" filters/groups by reservation_date (when booking
                 was made) — matches the OTA Mix page's date toggle.
    Returns: top_countries (sorted by total) + trend data per period per country.
    """
    today = date.today()

    if view == "monthly":
        periods = _build_monthly_periods(today, 7)
    else:
        periods = _build_weekly_periods(today, 7)

    # Query all periods in one shot
    overall_start = periods[0]["from"]
    overall_end = periods[-1]["to"]

    # Currency: when scoped to a branch we sum native; otherwise we sum VND
    # so cross-branch revenue stays comparable. Pass back the label so the
    # frontend doesn't have to re-derive it.
    currency = "VND"
    if branch_id:
        b = db.query(Branch.currency).filter(Branch.id == branch_id).first()
        if b and b.currency:
            currency = b.currency

    date_col = (
        Reservation.reservation_date if date_type == "booked" else Reservation.check_in_date
    )

    # Single-pass scan: group reservations by (period, country) and aggregate
    # per-country totals in Python. Replaces the prior 2-query pattern (top
    # countries + trend) which scanned the same date range twice — the table
    # is large enough that the second scan dominated response time on the
    # All-Branches view (~16s combined vs ~8s for one scan).
    top_countries, trend, trend_revenue = _query_country_breakdown(
        db, branch_id, overall_start, overall_end, view, date_col, limit
    )

    if not top_countries:
        return _envelope({
            "view": view, "periods": [], "countries": [], "trend": [],
            "trend_revenue": {},
            "currency": currency, "date_type": date_type,
            "data_synced_at": _last_reservations_synced_at(db, branch_id),
        })

    # 3) Build period labels
    period_labels = [p["label"] for p in periods]

    return _envelope({
        "view": view,
        "periods": period_labels,
        "countries": top_countries,
        "trend": trend,
        "trend_revenue": trend_revenue,
        "currency": currency,
        "date_type": date_type,
        "data_synced_at": _last_reservations_synced_at(db, branch_id),
    })


def _build_monthly_periods(today, count):
    """Build list of last N month periods [{label, from, to}]."""
    periods = []
    yr, mo = today.year, today.month
    for _ in range(count):
        first = date(yr, mo, 1)
        last = date(yr, mo, calendar.monthrange(yr, mo)[1])
        periods.append({
            "label": first.strftime("%b %Y"),
            "from": first,
            "to": last,
        })
        mo -= 1
        if mo == 0:
            mo = 12
            yr -= 1
    periods.reverse()
    return periods


def _build_weekly_periods(today, count):
    """Build list of last N week periods (Mon-Sun)."""
    periods = []
    week_start = today - timedelta(days=today.weekday())
    for _ in range(count):
        week_end = week_start + timedelta(days=6)
        periods.append({
            "label": week_start.strftime("%d %b"),
            "from": week_start,
            "to": week_end,
        })
        week_start -= timedelta(days=7)
    periods.reverse()
    return periods


def _query_country_breakdown(db, branch_id, d_from, d_to, view, date_col, limit):
    """Single-pass country breakdown. Returns (top_countries, trend,
    trend_revenue) where:

    - top_countries: list of {country_code, country, total_reservations,
      total_nights, total_revenue} sorted by reservation count desc, sliced
      to the top `limit`.
    - trend: dict {period_label: {country: count}} for the chart/table.
    - trend_revenue: dict {period_label: {country: revenue}} — same shape as
      trend but carrying revenue, so the Share % tab can switch the metric
      it distributes between reservations and revenue without a second query.

    Replaces the prior pattern of two separate scans
    (_query_top_countries + _query_monthly_trend) — the date range typically
    holds 25K+ reservations across all branches, and scanning twice doubled
    the dashboard load time.

    For monthly view, periods are grouped by (year, month) at SQL level.
    For weekly view, we return raw dates and bucket into ISO weeks in Python
    — postgres date_trunc('week') would also work but mixing extract() and
    date_trunc() in the same code path made the earlier helper cluttered.
    """
    revenue_col = (
        Reservation.grand_total_native if branch_id else Reservation.grand_total_vnd
    )
    code_expr = func.coalesce(Reservation.guest_country_code, "Unknown").label("code")
    country_expr = func.coalesce(Reservation.guest_country, "Unknown").label("country")

    if view == "monthly":
        period_cols = [
            extract("year", date_col).label("yr"),
            extract("month", date_col).label("mo"),
        ]
    else:
        # Group by raw date; bucket to ISO week in Python afterwards.
        period_cols = [date_col.label("d")]

    q = db.query(
        *period_cols,
        code_expr,
        country_expr,
        func.count(Reservation.id).label("cnt"),
        func.coalesce(func.sum(revenue_col), 0).label("revenue"),
        func.coalesce(func.sum(Reservation.nights), 0).label("nights"),
    ).filter(
        date_col >= d_from,
        date_col <= d_to,
        _status_active_filter(),
        ~func.lower(func.coalesce(Reservation.source, "")).in_(list(_EXCLUDED_SOURCES_REV)),
    ).group_by(
        *period_cols, code_expr, country_expr,
    )

    if branch_id:
        q = q.filter(Reservation.branch_id == branch_id)

    rows = q.all()

    # Aggregate per-country totals across all periods, and build trend dicts.
    totals = {}  # (code, country) -> {total, nights, revenue}
    trend = defaultdict(lambda: defaultdict(int))
    trend_revenue = defaultdict(lambda: defaultdict(float))

    for row in rows:
        code = row.code
        country = row.country
        cnt = int(row.cnt)
        nights = int(row.nights or 0)
        rev = float(row.revenue or 0)

        key = (code, country)
        if key not in totals:
            totals[key] = {"total": 0, "nights": 0, "revenue": 0.0}
        totals[key]["total"] += cnt
        totals[key]["nights"] += nights
        totals[key]["revenue"] += rev

        if view == "monthly":
            label = date(int(row.yr), int(row.mo), 1).strftime("%b %Y")
        else:
            d_val = row.d
            if d_val is None:
                continue
            week_start = d_val - timedelta(days=d_val.weekday())
            label = week_start.strftime("%d %b")
        trend[label][country] += cnt
        trend_revenue[label][country] += rev

    # Top countries sorted by total reservations desc, sliced.
    top = sorted(
        (
            {
                "country_code": code,
                "country": country,
                "total_reservations": v["total"],
                "total_nights": v["nights"],
                "total_revenue": v["revenue"],
            }
            for (code, country), v in totals.items()
        ),
        key=lambda x: -x["total_reservations"],
    )[:limit]

    # Convert nested defaultdicts to plain dicts for JSON serialization.
    trend_plain = {p: dict(c) for p, c in trend.items()}
    trend_revenue_plain = {p: dict(c) for p, c in trend_revenue.items()}

    return top, trend_plain, trend_revenue_plain


def _query_top_countries(db, branch_id, d_from, d_to, limit, date_col=None):
    """Get top N countries by total reservations in the full date range.

    NULL guest_country/guest_country_code is bucketed as "Unknown" so the
    same key flows through to the trend queries below (they filter by name).

    When a branch_id is given we sum grand_total_native (the branch's local
    currency); for the All-Branches view we fall back to grand_total_vnd so
    revenue across mixed-currency branches stays comparable.

    `date_col` selects which date column to filter on — Reservation.check_in_date
    (default) or Reservation.reservation_date (when the booking was made).
    """
    if date_col is None:
        date_col = Reservation.check_in_date
    code_expr = func.coalesce(Reservation.guest_country_code, "Unknown").label("code")
    country_expr = func.coalesce(Reservation.guest_country, "Unknown").label("country")
    revenue_col = (
        Reservation.grand_total_native if branch_id else Reservation.grand_total_vnd
    )
    q = db.query(
        code_expr,
        country_expr,
        func.count(Reservation.id).label("total"),
        func.coalesce(func.sum(revenue_col), 0).label("revenue"),
        func.coalesce(func.sum(Reservation.nights), 0).label("nights"),
    ).filter(
        date_col >= d_from,
        date_col <= d_to,
        _status_active_filter(),
        ~func.lower(func.coalesce(Reservation.source, "")).in_(list(_EXCLUDED_SOURCES_REV)),
    ).group_by(
        code_expr, country_expr,
    ).order_by(func.count(Reservation.id).desc())

    if branch_id:
        q = q.filter(Reservation.branch_id == branch_id)

    return [
        {
            "country_code": r.code,
            "country": r.country,
            "total_reservations": int(r.total),
            "total_revenue": float(r.revenue),
            "total_nights": int(r.nights),
        }
        for r in q.limit(limit).all()
    ]


def _query_monthly_trend(db, branch_id, d_from, d_to, country_names, date_col=None):
    """Per month × country reservation counts.

    Uses COALESCE(guest_country, 'Unknown') so NULL rows match the "Unknown"
    bucket from _query_top_countries. Without this, NULL IN (...) evaluates
    to FALSE in SQL and the Unknown column on the chart is silently empty.
    """
    if date_col is None:
        date_col = Reservation.check_in_date
    country_expr = func.coalesce(Reservation.guest_country, "Unknown").label("country")
    q = db.query(
        extract("year", date_col).label("yr"),
        extract("month", date_col).label("mo"),
        country_expr,
        func.count(Reservation.id).label("cnt"),
    ).filter(
        date_col >= d_from,
        date_col <= d_to,
        country_expr.in_(country_names),
        _status_active_filter(),
        ~func.lower(func.coalesce(Reservation.source, "")).in_(list(_EXCLUDED_SOURCES_REV)),
    ).group_by("yr", "mo", country_expr)

    if branch_id:
        q = q.filter(Reservation.branch_id == branch_id)

    # Build {period_label: {country: count}}
    result = defaultdict(lambda: defaultdict(int))
    for yr, mo, country, cnt in q.all():
        label = date(int(yr), int(mo), 1).strftime("%b %Y")
        result[label][country] = int(cnt)

    return dict(result)


def _query_weekly_trend(db, branch_id, d_from, d_to, country_names, date_col=None):
    """Per week × country reservation counts. NULL country bucketed as 'Unknown'."""
    if date_col is None:
        date_col = Reservation.check_in_date
    country_expr = func.coalesce(Reservation.guest_country, "Unknown")
    q = db.query(
        date_col.label("d"),
        country_expr.label("country"),
    ).filter(
        date_col >= d_from,
        date_col <= d_to,
        country_expr.in_(country_names),
        _status_active_filter(),
        ~func.lower(func.coalesce(Reservation.source, "")).in_(list(_EXCLUDED_SOURCES_REV)),
    )

    if branch_id:
        q = q.filter(Reservation.branch_id == branch_id)

    result = defaultdict(lambda: defaultdict(int))
    for d_val, country in q.all():
        if d_val is None:
            continue
        week_start = d_val - timedelta(days=d_val.weekday())
        label = week_start.strftime("%d %b")
        result[label][country] += 1

    return dict(result)
