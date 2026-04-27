"""
Pickup Engine — booking pickup ratio model for revenue forecasting.

Industry-standard approach:
  Forecast = OTB (On-The-Books) + Expected Pickup
  Expected Pickup = OTB × pickup_ratio

pickup_ratio at booking window D:
  = mean over past months of (final_rev - OTB_at_D) / OTB_at_D

Where:
  - OTB_at_D  = revenue booked ≥ D days before the 1st of that month
  - final_rev = total revenue for that month (all reservations)
  - D         = (first_day_of_next_month - today).days

Room/Dorm split: computed separately using room_type_category.
Applies the same source exclusions as Cloudbeds Insights revenue
(Blogger, House Use, KOL, Special case excluded).

Notes:
- Requires ≥ MIN_MONTHS complete months of reservation data.
- Falls back to OTB-only (ratio=0) if insufficient history.
- Accuracy improves as more months accumulate (recommend 6+).
"""

from __future__ import annotations

import calendar
import logging
from datetime import date, timedelta
from typing import Optional
from uuid import UUID

from sqlalchemy import func, not_, or_
from sqlalchemy.orm import Session

from app.models.reservation import Reservation

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

# Minimum complete months needed for a reliable pickup ratio
MIN_MONTHS = 2

# Max months to look back (keeps query fast; 6 months is typical industry window)
MAX_LOOKBACK = 6

# Sources excluded from revenue (mirrors _REVENUE_EXCLUDED_SOURCES in cloudbeds.py)
_EXCLUDED_SOURCES = {"Blogger", "House Use", "KOL", "Special case"}

# Statuses that mean "no revenue"
_EXCLUDED_STATUSES = {
    "cancelled", "canceled", "no_show", "noshow", "no show", "no-show",
}


# ── Internal helpers ──────────────────────────────────────────────────────────

def _monthly_revenue(
    db: Session,
    branch_id: UUID,
    year: int,
    month: int,
    booked_by: date,                          # only bookings made on or before this date
    room_type_category: Optional[str] = None, # "Room", "Dorm", or None (total)
) -> float:
    """
    Sum of grand_total_native for reservations arriving in (year, month)
    that were booked on or before booked_by, passing source/status filters.
    """
    first_day = date(year, month, 1)
    last_day  = date(year, month, calendar.monthrange(year, month)[1])

    q = (
        db.query(func.coalesce(func.sum(Reservation.grand_total_native), 0))
        .filter(
            Reservation.branch_id      == branch_id,
            Reservation.check_in_date >= first_day,
            Reservation.check_in_date <= last_day,
            # NULL reservation_date = booking date unknown → treat as booked early (include)
            or_(
                Reservation.reservation_date == None,  # noqa: E711
                Reservation.reservation_date <= booked_by,
            ),
            # NULL status = not cancelled (include); only exclude explicit cancel strings
            or_(
                Reservation.status == None,             # noqa: E711
                not_(Reservation.status.in_(_EXCLUDED_STATUSES)),
            ),
            # NULL source = unknown (include); only exclude known excluded names
            or_(
                Reservation.source == None,             # noqa: E711
                not_(Reservation.source.in_(_EXCLUDED_SOURCES)),
            ),
            Reservation.grand_total_native > 0,
        )
    )
    if room_type_category:
        q = q.filter(Reservation.room_type_category == room_type_category)

    return float(q.scalar() or 0)


# ── Public API ────────────────────────────────────────────────────────────────

def compute_pickup_ratio(
    db: Session,
    branch_id: UUID,
    room_type_category: Optional[str],
    booking_window_days: int,
    lookback_months: int = MAX_LOOKBACK,
) -> Optional[float]:
    """
    Compute average pickup ratio from historical complete months.

    Returns None if fewer than MIN_MONTHS of usable data exist
    (caller should fall back to OTB-only forecast in that case).

    Args:
        room_type_category: "Room", "Dorm", or None for total.
        booking_window_days: days before 1st of month to snapshot OTB.
            e.g. 4  → snapshot taken 4 days before month start
                  30 → snapshot taken 30 days before month start
    """
    today = date.today()
    ratios: list[float] = []
    months_checked = 0

    # Walk backwards through completed months
    cur_first = date(today.year, today.month, 1)

    while months_checked < lookback_months:
        # Move to previous month
        prev_last  = cur_first - timedelta(days=1)
        year, month = prev_last.year, prev_last.month
        cur_first  = date(year, month, 1)

        snapshot_date = cur_first - timedelta(days=booking_window_days)

        otb   = _monthly_revenue(db, branch_id, year, month, snapshot_date,  room_type_category)
        final = _monthly_revenue(db, branch_id, year, month, prev_last,       room_type_category)

        if otb > 0 and final > 0:
            ratio = (final - otb) / otb
            ratios.append(ratio)
            logger.debug(
                "Pickup ratio %s/%d %s BW-%d: OTB=%.0f final=%.0f ratio=%.3f",
                year, month,
                room_type_category or "total",
                booking_window_days,
                otb, final, ratio,
            )
        else:
            logger.debug(
                "Pickup ratio %s/%d %s BW-%d: skipped (OTB=%.0f final=%.0f)",
                year, month,
                room_type_category or "total",
                booking_window_days,
                otb, final,
            )

        months_checked += 1

    if len(ratios) < MIN_MONTHS:
        logger.info(
            "pickup_ratio branch=%s cat=%s BW-%d: insufficient data (%d/%d months)",
            branch_id, room_type_category, booking_window_days,
            len(ratios), MIN_MONTHS,
        )
        return None

    avg = sum(ratios) / len(ratios)
    logger.info(
        "pickup_ratio branch=%s cat=%s BW-%d: %.3f (from %d months)",
        branch_id, room_type_category, booking_window_days, avg, len(ratios),
    )
    return avg


def compute_otb_revenue(
    db: Session,
    branch_id: UUID,
    year: int,
    month: int,
    room_type_category: Optional[str] = None,
    as_of: Optional[date] = None,
) -> float:
    """
    Current OTB revenue for (year, month) by room type, booked on or before as_of.
    Defaults to today.
    """
    if as_of is None:
        as_of = date.today()
    return _monthly_revenue(db, branch_id, year, month, as_of, room_type_category)


def forecast_with_pickup(
    db: Session,
    branch_id: UUID,
    year: int,
    month: int,
    has_dorm: bool,
    booking_window_days: int,
    lookback_months: int = MAX_LOOKBACK,
) -> dict:
    """
    Compute pickup-adjusted forecast for (year, month), split by Room/Dorm.

    Returns:
        {
            "forecast":       float,  # total forecast (Room + Dorm)
            "forecast_room":  float,
            "forecast_dorm":  float | None,
            "otb_total":      float,
            "otb_room":       float,
            "otb_dorm":       float | None,
            "pickup_ratio":   float | None,  # total ratio used
            "pickup_room":    float | None,
            "pickup_dorm":    float | None,
            "sample_months":  int,
            "fallback":       bool,  # True if no pickup ratio available
        }
    """
    otb_total = compute_otb_revenue(db, branch_id, year, month)
    otb_room  = compute_otb_revenue(db, branch_id, year, month, "Room")
    otb_dorm  = compute_otb_revenue(db, branch_id, year, month, "Dorm") if has_dorm else None

    pickup_total = compute_pickup_ratio(db, branch_id, None,   booking_window_days, lookback_months)
    pickup_room  = compute_pickup_ratio(db, branch_id, "Room", booking_window_days, lookback_months)
    pickup_dorm  = compute_pickup_ratio(db, branch_id, "Dorm", booking_window_days, lookback_months) if has_dorm else None

    fallback = False

    if has_dorm and pickup_room is not None and pickup_dorm is not None:
        forecast_room = round(otb_room  * (1 + pickup_room), 2)
        forecast_dorm = round(otb_dorm  * (1 + pickup_dorm), 2)
        forecast      = round(forecast_room + forecast_dorm,  2)
    elif pickup_total is not None:
        # Single-ratio fallback (rooms-only branch, or dorm ratio missing)
        forecast_room = round(otb_room  * (1 + pickup_total), 2)
        forecast_dorm = round(otb_dorm  * (1 + pickup_total), 2) if otb_dorm else None
        forecast      = round(otb_total * (1 + pickup_total), 2)
    else:
        # No historical data: use pure OTB as floor forecast
        fallback      = True
        forecast_room = round(otb_room,  2)
        forecast_dorm = round(otb_dorm,  2) if otb_dorm is not None else None
        forecast      = round(otb_total, 2)

    # Count sample months used (use total ratio's sample as proxy)
    sample_months = 0
    if pickup_total is not None:
        # Recount quickly (already computed above; estimate from lookback)
        sample_months = min(lookback_months, MAX_LOOKBACK)

    return {
        "forecast":      forecast,
        "forecast_room": forecast_room,
        "forecast_dorm": forecast_dorm,
        "otb_total":     round(otb_total, 2),
        "otb_room":      round(otb_room,  2),
        "otb_dorm":      round(otb_dorm,  2) if otb_dorm is not None else None,
        "pickup_ratio":  round(pickup_total, 4) if pickup_total is not None else None,
        "pickup_room":   round(pickup_room,  4) if pickup_room  is not None else None,
        "pickup_dorm":   round(pickup_dorm,  4) if pickup_dorm  is not None else None,
        "sample_months": sample_months,
        "fallback":      fallback,
    }
