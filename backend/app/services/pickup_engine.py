"""
Pickup Engine — booking pickup ratio model for revenue forecasting.

Industry-standard approach:
  Forecast = OTB_room × (1 + pickup_ratio_room)
           + OTB_dorm × (1 + pickup_ratio_dorm)

Where:
  OTB revenue   = Cloudbeds Insights total (daily_metrics), split by Room/Dorm
                  using nights ratio × room_adr / dorm_adr from reservations
  pickup_ratio  = nights-based historical pickup (nights always populated,
                  unlike grand_total_native which is often NULL for new bookings)

Nights-based pickup ratio at booking window D (days before 1st of month):
  pickup_ratio = (final_nights - advance_nights_at_window) / advance_nights_at_window

Where advance_nights = SUM(nights) WHERE reservation_date <= first_day_M - D

Uses reservation.nights (always set on ingestion) and reservation.room_type_category
(derived from room_type name, "Dorm" = name contains "Dorm").

Room/Dorm OTB split:
  otb_room = total_cloudbeds_otb_revenue × (room_nights / total_nights)
  otb_dorm = total_cloudbeds_otb_revenue × (dorm_nights / total_nights)

Source exclusions (Blogger, House Use, KOL, Special case) applied.
Falls back to OTB-only (ratio=0) if < MIN_MONTHS of historical data.
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

MIN_MONTHS    = 2       # minimum complete months needed for pickup ratio
MAX_LOOKBACK  = 6       # max months to look back
MAX_RATIO     = 2.0     # cap pickup ratio at 200% to prevent extreme outliers
                        # (hostel last-minute bookings can be high but >200% signals sparse data)

_EXCLUDED_SOURCES  = {"Blogger", "House Use", "KOL", "Special case"}
_EXCLUDED_STATUSES = {
    "cancelled", "canceled", "no_show", "noshow", "no show", "no-show",
}


# ── Internal helpers ──────────────────────────────────────────────────────────

def _monthly_nights(
    db: Session,
    branch_id: UUID,
    year: int,
    month: int,
    booked_by: date,
    room_type_category: Optional[str] = None,
) -> float:
    """
    Sum of nights for reservations arriving in (year, month),
    booked on or before booked_by, passing source/status filters.

    Uses nights (always populated) instead of grand_total_native (often NULL
    for new/future reservations that haven't been through sync_branch_revenue).
    """
    first_day = date(year, month, 1)
    last_day  = date(year, month, calendar.monthrange(year, month)[1])

    q = (
        db.query(func.coalesce(func.sum(Reservation.nights), 0))
        .filter(
            Reservation.branch_id      == branch_id,
            Reservation.check_in_date >= first_day,
            Reservation.check_in_date <= last_day,
            # NULL reservation_date → treat as booked early (include in any window)
            or_(
                Reservation.reservation_date == None,   # noqa: E711
                Reservation.reservation_date <= booked_by,
            ),
            # NULL status → not cancelled (include)
            or_(
                Reservation.status == None,             # noqa: E711
                not_(Reservation.status.in_(_EXCLUDED_STATUSES)),
            ),
            # NULL source → unknown, not a known excluded source (include)
            or_(
                Reservation.source == None,             # noqa: E711
                not_(Reservation.source.in_(_EXCLUDED_SOURCES)),
            ),
            Reservation.nights > 0,
        )
    )
    if room_type_category:
        q = q.filter(Reservation.room_type_category == room_type_category)

    return float(q.scalar() or 0)


# ── Public API ────────────────────────────────────────────────────────────────

def compute_pickup_ratio_nights(
    db: Session,
    branch_id: UUID,
    room_type_category: Optional[str],
    booking_window_days: int,
    lookback_months: int = MAX_LOOKBACK,
) -> Optional[float]:
    """
    Compute average nights-based pickup ratio from historical complete months.

    pickup_ratio = mean over past months of
        (final_nights - advance_nights_at_window) / advance_nights_at_window

    Returns None if fewer than MIN_MONTHS of usable data.
    Caps ratio at MAX_RATIO to filter out sparse-data outliers.
    """
    today = date.today()
    ratios: list[float] = []
    months_checked = 0

    cur_first = date(today.year, today.month, 1)

    while months_checked < lookback_months:
        prev_last   = cur_first - timedelta(days=1)
        year, month = prev_last.year, prev_last.month
        cur_first   = date(year, month, 1)

        snapshot_date = cur_first - timedelta(days=booking_window_days)

        advance = _monthly_nights(db, branch_id, year, month, snapshot_date, room_type_category)
        final   = _monthly_nights(db, branch_id, year, month, prev_last,      room_type_category)

        if advance > 0 and final > 0:
            raw_ratio = (final - advance) / advance
            ratio = min(raw_ratio, MAX_RATIO)   # cap outliers
            ratios.append(ratio)
            logger.debug(
                "Nights pickup %d/%d %s BW-%d: advance=%.0f final=%.0f ratio=%.3f (raw=%.3f)",
                year, month, room_type_category or "total",
                booking_window_days, advance, final, ratio, raw_ratio,
            )
        else:
            logger.debug(
                "Nights pickup %d/%d %s BW-%d: skipped (advance=%.0f final=%.0f)",
                year, month, room_type_category or "total",
                booking_window_days, advance, final,
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
        "pickup_ratio branch=%s cat=%s BW-%d: %.3f (from %d months, capped at %.1f)",
        branch_id, room_type_category, booking_window_days, avg, len(ratios), MAX_RATIO,
    )
    return avg


def get_otb_nights_split(
    db: Session,
    branch_id: UUID,
    year: int,
    month: int,
) -> dict:
    """
    Return current OTB nights split by Room/Dorm as fractions of total.
    Uses reservations.nights (always populated).

    Returns:
        { "room_fraction": float, "dorm_fraction": float,
          "room_nights": int, "dorm_nights": int, "total_nights": int }
    """
    today = date.today()
    total = _monthly_nights(db, branch_id, year, month, today)
    room  = _monthly_nights(db, branch_id, year, month, today, "Room")
    dorm  = _monthly_nights(db, branch_id, year, month, today, "Dorm")

    room_frac = (room / total) if total > 0 else 0.0
    dorm_frac = (dorm / total) if total > 0 else 0.0

    # If room_type_category not populated on some reservations, uncategorised
    # nights float in total but not in room+dorm. Normalise so they add up.
    categorised = room + dorm
    if categorised > 0 and total > 0:
        room_frac = room / categorised
        dorm_frac = dorm / categorised

    return {
        "room_fraction": round(room_frac, 4),
        "dorm_fraction": round(dorm_frac, 4),
        "room_nights":   int(room),
        "dorm_nights":   int(dorm),
        "total_nights":  int(total),
    }


def forecast_with_pickup(
    db: Session,
    branch_id: UUID,
    year: int,
    month: int,
    has_dorm: bool,
    booking_window_days: int,
    cloudbeds_otb_revenue: float,       # total OTB from Cloudbeds Insights (daily_metrics)
    cloudbeds_room_adr: Optional[float],
    cloudbeds_dorm_adr: Optional[float],
    lookback_months: int = MAX_LOOKBACK,
) -> dict:
    """
    Compute pickup-adjusted forecast for (year, month), split by Room/Dorm.

    Algorithm:
      1. OTB split: room_fraction & dorm_fraction from reservations.nights
      2. OTB_room = cloudbeds_otb_revenue × room_fraction
         OTB_dorm = cloudbeds_otb_revenue × dorm_fraction
         (Preserves Cloudbeds revenue accuracy; only nights used for split)
      3. Nights-based pickup ratios from historical months
      4. forecast_room = OTB_room × (1 + pickup_ratio_room)
         forecast_dorm = OTB_dorm × (1 + pickup_ratio_dorm)

    Returns:
        {
            "forecast":       float,
            "forecast_room":  float,
            "forecast_dorm":  float | None,
            "otb_room":       float,
            "otb_dorm":       float | None,
            "pickup_room":    float | None,   # nights-based ratio applied
            "pickup_dorm":    float | None,
            "pickup_ratio":   float | None,   # total nights ratio
            "nights_split":   dict,
            "fallback":       bool,
        }
    """
    # ── Step 1: OTB revenue split via nights fraction ─────────────────────
    nights_split = get_otb_nights_split(db, branch_id, year, month)

    if has_dorm and nights_split["total_nights"] > 0:
        otb_room = round(cloudbeds_otb_revenue * nights_split["room_fraction"], 2)
        otb_dorm = round(cloudbeds_otb_revenue * nights_split["dorm_fraction"], 2)
    elif nights_split["total_nights"] > 0:
        otb_room = round(cloudbeds_otb_revenue * nights_split["room_fraction"], 2)
        otb_dorm = None
    else:
        otb_room = round(cloudbeds_otb_revenue, 2)
        otb_dorm = None

    # ── Step 2: Nights-based pickup ratios ───────────────────────────────
    pickup_total = compute_pickup_ratio_nights(db, branch_id, None,   booking_window_days, lookback_months)
    pickup_room  = compute_pickup_ratio_nights(db, branch_id, "Room", booking_window_days, lookback_months)
    pickup_dorm  = compute_pickup_ratio_nights(db, branch_id, "Dorm", booking_window_days, lookback_months) if has_dorm else None

    # ── Step 3: Apply pickup to OTB ───────────────────────────────────────
    fallback = False

    if has_dorm and otb_dorm is not None and pickup_room is not None and pickup_dorm is not None:
        forecast_room = round(otb_room * (1 + pickup_room), 2)
        forecast_dorm = round(otb_dorm * (1 + pickup_dorm), 2)
        forecast      = round(forecast_room + forecast_dorm, 2)
    elif pickup_total is not None:
        forecast_room = round(otb_room * (1 + pickup_total), 2)
        forecast_dorm = round(otb_dorm * (1 + pickup_total), 2) if otb_dorm is not None else None
        forecast      = round(cloudbeds_otb_revenue * (1 + pickup_total), 2)
    else:
        fallback      = True
        forecast_room = otb_room
        forecast_dorm = otb_dorm
        forecast      = round(cloudbeds_otb_revenue, 2)

    return {
        "forecast":      forecast,
        "forecast_room": forecast_room,
        "forecast_dorm": forecast_dorm,
        "otb_room":      otb_room,
        "otb_dorm":      otb_dorm,
        "pickup_room":   round(pickup_room,  4) if pickup_room  is not None else None,
        "pickup_dorm":   round(pickup_dorm,  4) if pickup_dorm  is not None else None,
        "pickup_ratio":  round(pickup_total, 4) if pickup_total is not None else None,
        "nights_split":  nights_split,
        "fallback":      fallback,
    }
