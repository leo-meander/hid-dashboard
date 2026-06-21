"""
KPI Engine — v5.1 (Cloudbeds Insights API with 1-hour in-memory cache)

Revenue, ADR, rooms_sold all come from Cloudbeds Data Insights API
via custom reports with proper filters — cached for 1 hour.
First request per branch/month: ~5s (API call).
Subsequent requests within 1 hour: instant (from cache).

Revenue EXCLUDES sources: "House use", "Blogger", "KOL", "Special case", "Work Exchange"
Rooms Sold / OCC counts ALL sources (no exclusions).
Room/Dorm split uses room_type filter (Dorm = name contains 'Dorm').
Each month uses its OWN ADR.

Forecast formula:
  ADR = Excluded Revenue / Excluded Rooms Sold (only paying rooms)
  Predicted Room Sold = Round(predicted_occ x inventory x total_days, 0)
  Forecast Revenue = ADR x Predicted Room Sold
"""
from __future__ import annotations

import calendar
import logging

from datetime import date, datetime, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.daily_metrics import DailyMetrics
from app.models.kpi import KPITarget
from app.models.reservation import Reservation
from app.models.reservation_daily import ReservationDaily

logger = logging.getLogger(__name__)

def _get_last_synced(db: Session, branch_id: str, year: int, month: int) -> str | None:
    """Return ISO timestamp of the latest computed_at for this branch/month."""
    first_day = date(year, month, 1)
    last_day = date(year, month, _days_in_month(year, month))
    row = db.query(func.max(DailyMetrics.computed_at)).filter(
        DailyMetrics.branch_id == branch_id,
        DailyMetrics.date >= first_day,
        DailyMetrics.date <= last_day,
    ).scalar()
    return row.isoformat() if row else None

# Statuses excluded from all queries (cancelled / no-show reservations)
_EXCLUDED_STATUSES = {"cancelled", "canceled", "no_show", "noshow", "no show", "no-show"}

# Sources excluded from revenue, ADR, OCC, and rooms sold — consistent everywhere
_EXCLUDED_SOURCES = {
    "blogger", "house use", "houseuse",
    "kol",
    "special case",
    "work exchange",
}


# ── helpers ────────────────────────────────────────────────────────────────────

def _days_in_month(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _base_reservation_daily_query(
    db: Session, branch_id: UUID, first_day: date, last_day: date,
    *, exclude_sources: bool = False,
):
    """
    Base query on reservation_daily joined to reservations, filtering:
    - branch_id matches
    - date within [first_day, last_day]
    - reservation status is not cancelled/no-show
    - If exclude_sources=True: also exclude House use, Blogger, KOL, Special case, Work Exchange
      (use for REVENUE queries only — OCC/rooms_sold count ALL sources)
    Returns a SQLAlchemy query on ReservationDaily.
    """
    q = (
        db.query(ReservationDaily)
        .join(Reservation, ReservationDaily.reservation_id == Reservation.id)
        .filter(
            ReservationDaily.branch_id == branch_id,
            ReservationDaily.date >= first_day,
            ReservationDaily.date <= last_day,
            # Exclude cancelled/no-show
            ~func.lower(func.coalesce(Reservation.status, "")).in_(list(_EXCLUDED_STATUSES)),
        )
    )
    if exclude_sources:
        q = q.filter(
            ~func.lower(func.coalesce(Reservation.source, "")).in_(list(_EXCLUDED_SOURCES)),
        )
    return q


def _count_dorm_beds(room_number: str) -> int:
    """
    Count individual dorm beds from room_number string.
    Dorm beds use patterns like '211-1, 211-2' (hyphenated).
    Returns at least 1.
    """
    if not room_number:
        return 1
    parts = [p.strip() for p in room_number.split(",") if p.strip()]
    return max(len(parts), 1)


def _is_combo_booking(room_type: str) -> bool:
    """
    Detect combo bookings that mix room types (e.g. 'Superior Room, 8 Beds Dormitory').
    These should be excluded from both Room and Dorm ADR to avoid cross-contamination.
    """
    rt = (room_type or "").lower()
    room_keywords = ("superior", "balcony", "double", "twin", "single", "deluxe", "family", "private", "standard")
    dorm_keywords = ("dorm", "bed ")
    has_room = any(k in rt for k in room_keywords)
    has_dorm = any(k in rt for k in dorm_keywords)
    return has_room and has_dorm


def _get_revenue_from_insights(
    db: Session, branch_id: UUID, year: int, month: int,
) -> tuple[float, float]:
    """
    Actual revenue from daily_metrics (synced from Cloudbeds Data Insights API).
    Uses room_revenue (accommodation only, no tax/fees) — matches Cloudbeds dashboard.
    Returns (revenue_native, revenue_vnd).
    """
    first_day = date(year, month, 1)
    last_day = date(year, month, _days_in_month(year, month))

    row = db.query(
        func.coalesce(func.sum(DailyMetrics.revenue_native), 0),
        func.coalesce(func.sum(DailyMetrics.revenue_vnd), 0),
    ).filter(
        DailyMetrics.branch_id == branch_id,
        DailyMetrics.date >= first_day,
        DailyMetrics.date <= last_day,
    ).one()

    return float(row[0]), float(row[1])


def _get_adr_occ_from_insights(
    db: Session, branch_id: UUID, first_day: date, last_day: date,
) -> tuple[Optional[float], int]:
    """
    ADR and rooms_sold from daily_metrics (Cloudbeds Insights).
    ADR = total_room_revenue / total_rooms_sold.
    Returns: (adr, total_rooms_sold).
    """
    row = db.query(
        func.coalesce(func.sum(DailyMetrics.revenue_native), 0),
        func.coalesce(func.sum(DailyMetrics.rooms_sold), 0),
    ).filter(
        DailyMetrics.branch_id == branch_id,
        DailyMetrics.date >= first_day,
        DailyMetrics.date <= last_day,
    ).one()

    total_rev = float(row[0])
    total_sold = int(row[1])
    adr = round(total_rev / total_sold, 2) if total_sold > 0 else None

    return adr, total_sold


def _get_insights_filtered(
    db: Session, branch_id: UUID, year: int, month: int,
) -> dict:
    """
    Get revenue, rooms_sold, ADR with room/dorm split from daily_metrics.

    Source pipeline:
      - total_sold / occ_pct: sync_cloudbeds_occupancy (stock report 110, ALL sources)
      - revenue_native / adr_native / revpar_native: sync_cloudbeds_filtered
        (filtered revenue / unfiltered total_sold) — overrides the unfiltered
        values from sync_cloudbeds_occupancy
      - rooms_sold / dorms_sold: sync_cloudbeds_filtered (UNFILTERED split)
      - room_revenue_native / dorm_revenue_native: sync_cloudbeds_filtered (filtered)

    ADR rule applied here: total_rev (filtered) / total_sold (ALL sources);
    same for room/dorm split.

    Returns dict with keys: total_rev, total_sold, total_adr,
    room_rev, room_sold, room_adr, dorm_rev, dorm_sold, dorm_adr, has_dorm.
    """
    empty = {"total_rev": 0, "total_sold": 0, "total_adr": 0,
             "room_rev": 0, "room_sold": 0, "room_adr": 0,
             "dorm_rev": 0, "dorm_sold": 0, "dorm_adr": 0, "has_dorm": False}

    first_day = date(year, month, 1)
    last_day = date(year, month, _days_in_month(year, month))

    row = db.query(
        func.coalesce(func.sum(DailyMetrics.revenue_native), 0),
        func.coalesce(func.sum(DailyMetrics.total_sold), 0),
        func.coalesce(func.sum(DailyMetrics.rooms_sold), 0),
        func.coalesce(func.sum(DailyMetrics.dorms_sold), 0),
        func.coalesce(func.sum(DailyMetrics.room_revenue_native), 0),
        func.coalesce(func.sum(DailyMetrics.dorm_revenue_native), 0),
    ).filter(
        DailyMetrics.branch_id == branch_id,
        DailyMetrics.date >= first_day,
        DailyMetrics.date <= last_day,
    ).one()

    total_rev = float(row[0])
    total_sold = int(row[1])
    room_sold = int(row[2])
    dorm_sold = int(row[3])
    room_rev = float(row[4])
    dorm_rev = float(row[5])

    if total_sold > 0:
        # ADR rule: filtered revenue / unfiltered rooms_sold (excl Blogger / KOL /
        # House Use / Special Case / Work Exchange in numerator only). Per-day
        # values populated by sync_cloudbeds_filtered.
        total_adr = round(total_rev / total_sold, 2)
        room_adr = round(room_rev / room_sold, 2) if room_sold > 0 else 0
        dorm_adr = round(dorm_rev / dorm_sold, 2) if dorm_sold > 0 else 0

        result = {
            "total_rev": total_rev, "total_sold": total_sold, "total_adr": total_adr,
            "room_rev": room_rev, "room_sold": room_sold, "room_adr": room_adr,
            "dorm_rev": dorm_rev, "dorm_sold": dorm_sold, "dorm_adr": dorm_adr,
            "has_dorm": dorm_sold > 0,
        }
        logger.debug(
            "Insights from DB for %s %d/%d: rev=%.0f sold=%d adr=%.2f",
            branch_id, year, month, total_rev, total_sold, total_adr,
        )
        return result

    # ── FALLBACK: Cloudbeds API (only if DB has no data) ─────────────────
    from app.models.branch import Branch
    from app.config import settings

    branch = db.query(Branch).filter_by(id=branch_id, is_active=True).first()
    if not branch:
        return empty

    pid = branch.cloudbeds_property_id
    api_key = settings.get_api_key_for_property(str(pid)) if pid else None

    if pid and api_key:
        try:
            from app.services.cloudbeds import fetch_occupancy_filtered
            result = fetch_occupancy_filtered(str(pid), api_key, year, month)
            if result.get("total_sold", 0) > 0:
                logger.info(
                    "Insights API fallback for %s %d/%d: rev=%.0f sold=%d adr=%.2f",
                    branch.name, year, month,
                    result["total_rev"], result["total_sold"], result["total_adr"],
                )
                return result
        except Exception as e:
            logger.warning("Insights API fallback failed for %s: %s", branch.name, e)

    return empty


def _get_room_dorm_adr_from_daily(
    db: Session, branch_id: UUID, first_day: date, last_day: date,
) -> tuple[Optional[float], Optional[float], float, int, float, int]:
    """
    Room/Dorm ADR from reservation_daily.
    ADR = revenue (excl sources) / rooms_sold (ALL sources).
    Revenue excludes "House use", "Blogger", "KOL", "Special case".
    Rooms sold / OCC counts ALL sources.
    For Dorm: counts per-BED (not per-reservation), excludes combo bookings.

    Returns: (room_adr, dorm_adr, room_revenue, room_sold, dorm_revenue, dorm_sold)
    """
    # ── Room revenue (EXCLUDE sources) ──────────────────────────────────────
    rev_base = _base_reservation_daily_query(db, branch_id, first_day, last_day, exclude_sources=True)
    room_rev_row = rev_base.filter(
        func.lower(ReservationDaily.room_type_category) == "room",
    ).with_entities(
        func.coalesce(func.sum(ReservationDaily.nightly_rate), 0),
    ).one()
    room_rev = float(room_rev_row[0])

    # ── Room sold (ALL sources) ─────────────────────────────────────────────
    all_base = _base_reservation_daily_query(db, branch_id, first_day, last_day, exclude_sources=False)
    room_sold_row = all_base.filter(
        func.lower(ReservationDaily.room_type_category) == "room",
    ).with_entities(
        func.count(ReservationDaily.id),
    ).one()
    room_nights = int(room_sold_row[0])

    room_adr = round(room_rev / room_nights, 2) if room_nights > 0 else None

    # ── Dorm revenue (EXCLUDE sources) + Dorm sold (ALL sources) ────────────
    dorm_rev_q = (
        _base_reservation_daily_query(db, branch_id, first_day, last_day, exclude_sources=True)
        .filter(func.lower(ReservationDaily.room_type_category) == "dorm")
    )
    dorm_rev_rows = dorm_rev_q.with_entities(
        ReservationDaily.nightly_rate,
        Reservation.room_number,
        Reservation.room_type,
    ).all()

    dorm_total_rev = 0.0
    for row in dorm_rev_rows:
        if _is_combo_booking(row.room_type):
            continue
        dorm_total_rev += float(row.nightly_rate or 0)

    # Dorm beds sold (ALL sources)
    dorm_all_q = (
        _base_reservation_daily_query(db, branch_id, first_day, last_day, exclude_sources=False)
        .filter(func.lower(ReservationDaily.room_type_category) == "dorm")
    )
    dorm_all_rows = dorm_all_q.with_entities(
        Reservation.room_number,
        Reservation.room_type,
    ).all()

    dorm_total_beds = 0
    for row in dorm_all_rows:
        if _is_combo_booking(row.room_type):
            continue
        dorm_total_beds += _count_dorm_beds(row.room_number)

    dorm_adr = round(dorm_total_rev / dorm_total_beds, 2) if dorm_total_beds > 0 else None

    return room_adr, dorm_adr, room_rev, room_nights, dorm_total_rev, dorm_total_beds


def _get_total_adr_from_daily(
    db: Session, branch_id: UUID, first_day: date, last_day: date,
) -> tuple[Optional[float], float, int]:
    """
    Overall ADR from reservation_daily (all room types combined).
    ADR = total_revenue / total_room_nights_sold.
    Returns: (adr, total_revenue, total_sold).
    """
    base = _base_reservation_daily_query(db, branch_id, first_day, last_day)

    row = base.with_entities(
        func.coalesce(func.sum(ReservationDaily.nightly_rate), 0),
        func.count(ReservationDaily.id),
    ).one()

    total_rev = float(row[0])
    total_sold = int(row[1])
    adr = round(total_rev / total_sold, 2) if total_sold > 0 else None

    return adr, total_rev, total_sold


# ── core calculations ──────────────────────────────────────────────────────────

def get_actual_revenue(db: Session, branch_id: UUID, year: int, month: int) -> float:
    """
    Revenue from Cloudbeds Insights (daily_metrics.revenue_native = room_revenue).
    This is accommodation revenue only — matches Cloudbeds dashboard exactly.
    """
    rev_native, _ = _get_revenue_from_insights(db, branch_id, year, month)
    return rev_native


def get_actual_revenue_vnd(db: Session, branch_id: UUID, year: int, month: int) -> float:
    """Revenue in VND from Cloudbeds Insights (daily_metrics)."""
    _, rev_vnd = _get_revenue_from_insights(db, branch_id, year, month)
    return rev_vnd


def calculate_achievement_pct(actual: float, target: float) -> Optional[float]:
    """Return actual/target as a decimal (e.g. 0.85 = 85%). None if no target."""
    if not target:
        return None
    return round(actual / target, 4)


# ── public API ─────────────────────────────────────────────────────────────────

def compute_next_month_forecast(
    db: Session,
    branch_id: UUID,
    total_rooms: int,
    cur_year: int,
    cur_month: int,
    total_room_count: int = 0,
    total_dorm_count: int = 0,
) -> dict:
    """
    Forecast revenue for NEXT month using NEXT month's own ADR from Cloudbeds API.
    Formula:
      Predict Room Sold = Round(total_days x num_rooms x Predict_OCC, 0)
      Forecast Revenue = Next Month ADR x Predict Room Sold
    Revenue excludes Blogger/HouseUse/Special case.
    Rooms sold counts ALL sources.
    Room/Dorm split comes directly from Cloudbeds API filtered by room type.
    """
    next_month = cur_month + 1 if cur_month < 12 else 1
    next_year = cur_year if cur_month < 12 else cur_year + 1
    total_days = _days_in_month(next_year, next_month)

    has_split = total_room_count > 0 and total_dorm_count > 0

    # ── Next-month data from daily_metrics cache ─────────────────────────
    insights = _get_insights_filtered(db, branch_id, next_year, next_month)

    total_revenue = insights["total_rev"]
    total_sold = insights["total_sold"]
    total_adr = insights["total_adr"]

    # Room/Dorm ADR from cached daily_metrics
    room_adr = insights.get("room_adr") or None
    dorm_adr = insights.get("dorm_adr") or None

    # For rooms-only branches, room_adr = total_adr
    if total_room_count > 0 and total_dorm_count == 0:
        room_adr = total_adr

    # ── Booked OCC split for next month (ALL sources, same rule as actuals) ──
    booked_room_occ_next = (
        round(insights.get("room_sold", 0) / (total_room_count * total_days), 4)
        if total_room_count > 0 else None
    )
    booked_dorm_occ_next = (
        round(insights.get("dorm_sold", 0) / (total_dorm_count * total_days), 4)
        if total_dorm_count > 0 else None
    )

    # ── Predicted OCC for next month ──────────────────────────────────────
    target_row = (
        db.query(KPITarget)
        .filter_by(branch_id=branch_id, year=next_year, month=next_month)
        .first()
    )
    predicted_occ_next = float(target_row.predicted_occ_pct) if (target_row and target_row.predicted_occ_pct) else None
    predicted_room_occ_next = float(target_row.predicted_room_occ_pct) if (target_row and target_row.predicted_room_occ_pct) else None
    predicted_dorm_occ_next = float(target_row.predicted_dorm_occ_pct) if (target_row and target_row.predicted_dorm_occ_pct) else None
    next_month_target = float(target_row.target_revenue_native) if (target_row and target_row.target_revenue_native) else None

    # ── Forecast ──────────────────────────────────────────────────────────
    # Forecast (with split):  room_adr × pred_room_sold + dorm_adr × pred_dorm_sold
    # Forecast (no split):    total_adr × pred_sold
    room_forecast = dorm_forecast = None
    forecast = None

    if (has_split
            and predicted_room_occ_next is not None
            and predicted_dorm_occ_next is not None
            and room_adr and dorm_adr):
        pred_room_sold = round(total_days * total_room_count * predicted_room_occ_next)
        pred_dorm_sold = round(total_days * total_dorm_count * predicted_dorm_occ_next)
        room_forecast = round(room_adr * pred_room_sold, 2)
        dorm_forecast = round(dorm_adr * pred_dorm_sold, 2)
        forecast = round(room_forecast + dorm_forecast, 2)
    elif (has_split
            and predicted_room_occ_next is not None
            and predicted_dorm_occ_next is not None
            and total_adr):
        # Fallback: split OCC available but split ADR missing
        pred_room_sold = round(total_days * total_room_count * predicted_room_occ_next)
        pred_dorm_sold = round(total_days * total_dorm_count * predicted_dorm_occ_next)
        pred_sold = pred_room_sold + pred_dorm_sold
        forecast = round(total_adr * pred_sold, 2)
    elif total_adr and predicted_occ_next and total_rooms > 0:
        pred_sold = round(total_days * total_rooms * predicted_occ_next)
        forecast = round(total_adr * pred_sold, 2)

    return {
        "next_year": next_year,
        "next_month": next_month,
        "next_month_total_days": total_days,
        "next_month_target_native": next_month_target,
        "next_month_adr": round(total_adr, 2) if total_adr else None,
        "next_month_room_adr": room_adr,
        "next_month_dorm_adr": dorm_adr,
        "next_month_booked_revenue": round(total_revenue, 2),
        "next_month_booked_nights": total_sold,
        "booked_room_occ_next": booked_room_occ_next,
        "booked_dorm_occ_next": booked_dorm_occ_next,
        "predicted_occ_next": predicted_occ_next,
        "predicted_room_occ_next": predicted_room_occ_next,
        "predicted_dorm_occ_next": predicted_dorm_occ_next,
        "next_month_forecast_native": forecast,
        "next_month_room_forecast": room_forecast,
        "next_month_dorm_forecast": dorm_forecast,
        "next_month_forecast_room_adr": room_adr,
        "next_month_forecast_dorm_adr": dorm_adr,
    }


def compute_kpi_summary(
    db: Session,
    branch_id: UUID,
    year: int,
    month: int,
    total_rooms: int,
    total_room_count: int = 0,
    total_dorm_count: int = 0,
) -> dict:
    """
    Return full KPI summary dict for a branch/month.
    Revenue & ADR from Cloudbeds Insights API with proper filtering:
    - Revenue excludes Blogger, House Use, Special case
    - Rooms sold counts ALL sources
    - Room/Dorm split via room type filter (Dorm = name contains 'Dorm')
    Forecast = ADR x Round(total_days x num_rooms x predicted_occ, 0).
    """
    today = _today()
    total_days = _days_in_month(year, month)

    # Days elapsed in this month (cap at total_days)
    if today.year == year and today.month == month:
        days_elapsed = today.day
    elif (year, month) < (today.year, today.month):
        days_elapsed = total_days
    else:
        days_elapsed = 0

    # Target
    target_row = (
        db.query(KPITarget)
        .filter_by(branch_id=branch_id, year=year, month=month)
        .first()
    )

    target_revenue_native = float(target_row.target_revenue_native) if target_row else None
    target_revenue_vnd = float(target_row.target_revenue_vnd) if target_row else None
    predicted_occ_pct = float(target_row.predicted_occ_pct) if (target_row and target_row.predicted_occ_pct) else None
    predicted_room_occ = float(target_row.predicted_room_occ_pct) if (target_row and target_row.predicted_room_occ_pct) else None
    predicted_dorm_occ = float(target_row.predicted_dorm_occ_pct) if (target_row and target_row.predicted_dorm_occ_pct) else None

    # ── Actuals from Cloudbeds Insights API (cached 1 hour) ────────────────
    insights = _get_insights_filtered(db, branch_id, year, month)

    actual_native = insights["total_rev"]
    total_sold = insights["total_sold"]
    avg_adr = insights["total_adr"]

    # VND conversion
    from app.services.cloudbeds import get_cached_rate
    from app.models.branch import Branch
    branch_obj = db.query(Branch).filter_by(id=branch_id).first()
    vnd_rate = get_cached_rate(branch_obj.currency or "USD", "VND") if branch_obj else None
    actual_vnd = round(actual_native * vnd_rate, 2) if vnd_rate and actual_native else 0.0

    # Room/Dorm ADR directly from filtered Cloudbeds API
    room_adr = insights.get("room_adr") or None
    dorm_adr = insights.get("dorm_adr") or None
    has_split = total_room_count > 0 and total_dorm_count > 0

    # For rooms-only branches, room_adr = total_adr
    if total_room_count > 0 and total_dorm_count == 0:
        room_adr = avg_adr

    # ── Actual OCC from daily_metrics ────────────────────────────────────
    # _get_insights_filtered sums nights sold over the WHOLE calendar month
    # (incl. future on-the-books dates) — the same basis as the booked revenue
    # above. So the OCC denominator must be full-month capacity (total_days),
    # NOT days_elapsed; using days_elapsed double-counted future nights against
    # an elapsed-only denominator and produced >100% occupancy mid-month.
    actual_occ = round(total_sold / (total_rooms * total_days), 4) if (total_rooms > 0 and total_days > 0) else None
    actual_room_occ = None
    actual_dorm_occ = None
    if has_split and total_days > 0:
        room_sold_total = insights.get("room_sold", 0)
        dorm_sold_total = insights.get("dorm_sold", 0)
        actual_room_occ = round(room_sold_total / (total_room_count * total_days), 4) if total_room_count > 0 else None
        actual_dorm_occ = round(dorm_sold_total / (total_dorm_count * total_days), 4) if total_dorm_count > 0 else None

    # ── Forecasts ─────────────────────────────────────────────────────────
    # Forecast (with split):  room_adr × pred_room_sold + dorm_adr × pred_dorm_sold
    # Forecast (no split):    avg_adr × pred_sold
    # After UPD-209/UPD-210, sync_cloudbeds_filtered writes room_adr / dorm_adr
    # directly from Cloudbeds Insights custom reports per room type — they are
    # reliable, so we use them here for an accurate split-based forecast.
    room_forecast = dorm_forecast = None
    occ_forecast = None

    if (has_split
            and predicted_room_occ is not None
            and predicted_dorm_occ is not None
            and room_adr and dorm_adr):
        pred_room_sold = round(total_days * total_room_count * predicted_room_occ)
        pred_dorm_sold = round(total_days * total_dorm_count * predicted_dorm_occ)
        room_forecast = round(room_adr * pred_room_sold, 2)
        dorm_forecast = round(dorm_adr * pred_dorm_sold, 2)
        occ_forecast = round(room_forecast + dorm_forecast, 2)
    elif (has_split
            and predicted_room_occ is not None
            and predicted_dorm_occ is not None
            and avg_adr):
        # Fallback: split OCC available but split ADR missing
        pred_room_sold = round(total_days * total_room_count * predicted_room_occ)
        pred_dorm_sold = round(total_days * total_dorm_count * predicted_dorm_occ)
        pred_sold = pred_room_sold + pred_dorm_sold
        occ_forecast = round(avg_adr * pred_sold, 2)
    elif avg_adr and predicted_occ_pct and total_rooms > 0:
        pred_sold = round(total_days * total_rooms * predicted_occ_pct)
        occ_forecast = round(avg_adr * pred_sold, 2)

    # Achievement
    achievement_pct = calculate_achievement_pct(actual_native, target_revenue_native)

    return {
        "branch_id": str(branch_id),
        "year": year,
        "month": month,
        "total_days": total_days,
        "days_elapsed": days_elapsed,
        "total_rooms": total_rooms,
        # Target
        "target_revenue_native": target_revenue_native,
        "target_revenue_vnd": target_revenue_vnd,
        "predicted_occ_pct": predicted_occ_pct,
        "predicted_room_occ_pct": predicted_room_occ,
        "predicted_dorm_occ_pct": predicted_dorm_occ,
        # Actuals
        "actual_revenue_native": round(actual_native, 2),
        "actual_revenue_vnd": round(actual_vnd, 2),
        "actual_occ_pct": actual_occ,
        "actual_room_occ_pct": actual_room_occ,
        "actual_dorm_occ_pct": actual_dorm_occ,
        "avg_occ_pct": actual_occ,
        "avg_adr_native": round(avg_adr, 2) if avg_adr else None,
        "room_adr_native": room_adr,
        "dorm_adr_native": dorm_adr,
        "nights_booked": total_sold,
        # KPI
        "achievement_pct": achievement_pct,
        # Forecasts
        "occ_forecast_native": occ_forecast,
        "room_forecast_native": room_forecast,
        "dorm_forecast_native": dorm_forecast,
        "forecast_room_adr": room_adr,
        "forecast_dorm_adr": dorm_adr,
        # Room/Dorm capability flag
        "has_room_dorm_split": has_split and room_adr and dorm_adr,
        # Sync metadata — when was the Insights cache last refreshed
        "data_synced_at": _get_last_synced(db, str(branch_id), year, month),
    }
