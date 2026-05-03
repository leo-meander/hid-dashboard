"""
Metrics Engine — v2.0
Nightly rate attribution model:
  - OCC     = traditional in-house (spanning): rooms occupied on date / total_rooms
              (reservations where check_in_date <= date < check_out_date)
  - Revenue = nightly rate attribution: SUM(nightly_rate) from reservation_daily for date
              (accurate per-night revenue distribution)
  - total_sold / rooms_sold / dorms_sold = rooms occupied on date (from reservation_daily, OCC filter)
  - ADR     = Revenue(excl sources) / rooms_sold(ALL sources)
  - RevPAR  = ADR × OCC%

Split exclusion filters (v2.0):
  OCC / rooms_sold excludes:  cancelled/no-show statuses, maintenance
  OCC / rooms_sold includes:  blogger, kol, house use, special case, day use
  Revenue excludes:           cancelled/no-show statuses, blogger, kol, house use, special case, maintenance
  Revenue includes:           day use
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.branch import Branch
from app.models.daily_metrics import DailyMetrics
from app.models.reservation import Reservation
from app.models.reservation_daily import ReservationDaily

logger = logging.getLogger(__name__)

# ── Exclusion filter constants (v2.0 — split OCC vs Revenue) ─────────────────
EXCLUDED_STATUSES = {"cancelled", "canceled", "no_show", "noshow", "no show", "no-show"}

# OCC: only exclude maintenance (inactive in PMS) — blogger/kol/house use/day use count occupancy
EXCLUDED_SOURCES_OCC = {"maintain", "maintenance"}

# Revenue: exclude non-paying sources but INCLUDE day use (which has a fixed rate)
EXCLUDED_SOURCES_REVENUE = {
    "blogger", "kol", "house use", "houseuse",
    "special case",
    "maintain", "maintenance",
}

# Legacy combined set — used only by OTA mix / channel rate queries that don't need the split
EXCLUDED_SOURCES = {
    "house use", "houseuse", "maintain", "maintenance",
}


def _is_excluded_status(reservation) -> bool:
    """Check if reservation has an excluded status (cancelled/no-show)."""
    status = (getattr(reservation, "status", None) or "").lower().strip()
    status_norm = status.replace("-", "_").replace(" ", "_")
    return status in EXCLUDED_STATUSES or status_norm in EXCLUDED_STATUSES


# Categories that should be aggregated into a single row instead of broken out per raw source.
_AGGREGATED_CATEGORIES = {"Direct", "Local travel agency"}


def _channel_key(source_category: Optional[str], source: Optional[str]) -> str:
    """Channel label for mix/trend pivots.
    Direct + Local travel agency are rolled up under their category name; everything else
    (OTAs) keeps its raw source so each OTA shows as its own row.
    """
    cat = source_category or "OTA"
    if cat in _AGGREGATED_CATEGORIES:
        return cat
    return source or "Unknown"


def _is_excluded_occ(reservation) -> bool:
    """v2.0 OCC exclusion: cancelled/no-show + maintenance only."""
    if _is_excluded_status(reservation):
        return True
    source = (getattr(reservation, "source", None) or "").lower().strip()
    return source in EXCLUDED_SOURCES_OCC


def _is_excluded_revenue(reservation) -> bool:
    """v2.0 Revenue exclusion: cancelled/no-show + non-paying sources (NOT day use)."""
    if _is_excluded_status(reservation):
        return True
    source = (getattr(reservation, "source", None) or "").lower().strip()
    return source in EXCLUDED_SOURCES_REVENUE


# Keep old _is_excluded for backward compat with OTA mix queries
def _is_excluded(reservation) -> bool:
    """Legacy exclusion filter — used by OTA mix/channel rate queries."""
    if _is_excluded_status(reservation):
        return True
    source = (getattr(reservation, "source", None) or "").lower().strip()
    return source in EXCLUDED_SOURCES


def _room_night_expand(reservations) -> set[str]:
    """
    Room-night expansion: split comma-separated room_number fields,
    return set of distinct room identifiers occupied.
    Works with both ORM Reservation objects and lightweight _ResProxy objects.
    """
    rooms: set[str] = set()
    for r in reservations:
        room_num = getattr(r, "room_number", None) or getattr(r, "room_id", None)
        if not room_num:
            rooms.add(str(getattr(r, "id", id(r))))
            continue
        for rm in str(room_num).split(","):
            rm = rm.strip()
            if rm:
                rooms.add(rm)
    return rooms


def _room_expand_daily(daily_rows) -> set[str]:
    """Count distinct room_id values from ReservationDaily rows."""
    rooms: set[str] = set()
    for rd in daily_rows:
        if rd.room_id:
            rooms.add(rd.room_id)
        else:
            rooms.add(str(rd.reservation_id))
    return rooms


# ── per-day aggregation ────────────────────────────────────────────────────────

def compute_day(db: Session, branch: Branch, target_date: date) -> DailyMetrics:
    """
    v2.0: Aggregate metrics for one branch on one date → DailyMetrics row.
    OCC     = traditional in-house (spanning) with OCC exclusion filter
    Revenue = SUM(nightly_rate) from reservation_daily with Revenue exclusion filter
    ADR     = Revenue / rooms_sold
    RevPAR  = Revenue / total_rooms
    Upserts into daily_metrics table.
    """
    total_rooms: int = branch.total_rooms or 0
    total_room_count: int = branch.total_room_count or 0
    total_dorm_count: int = branch.total_dorm_count or 0

    # ── Revenue / total_sold: nightly rate from reservation_daily (v2.0) ─────
    daily_rows = db.query(ReservationDaily).filter(
        ReservationDaily.branch_id == branch.id,
        ReservationDaily.date == target_date,
    ).all()

    # Apply REVENUE exclusion filter (excludes blogger/kol/house use but NOT day use)
    revenue_rows = [rd for rd in daily_rows if not _is_excluded_revenue(rd)]

    # OCC filter for rooms_sold: include ALL sources (only exclude cancelled + maintenance)
    occ_rows = [rd for rd in daily_rows if not _is_excluded_occ(rd)]

    # rooms_sold / total_sold from OCC filter (ALL sources incl blogger/kol/house use)
    room_occ = [rd for rd in occ_rows if (rd.room_type_category or "").lower() == "room"]
    dorm_occ = [rd for rd in occ_rows if (rd.room_type_category or "").lower() == "dorm"]
    rooms_sold = len(_room_expand_daily(room_occ))
    dorms_sold = len(_room_expand_daily(dorm_occ))
    total_sold = rooms_sold + dorms_sold

    # Revenue from revenue-filtered rows only
    room_rev = [rd for rd in revenue_rows if (rd.room_type_category or "").lower() == "room"]
    dorm_rev = [rd for rd in revenue_rows if (rd.room_type_category or "").lower() == "dorm"]

    revenue_native = round(sum(float(rd.nightly_rate or 0) for rd in revenue_rows), 2)
    revenue_vnd    = round(sum(float(rd.nightly_rate_vnd or 0) for rd in revenue_rows), 2)
    room_revenue_native = round(sum(float(rd.nightly_rate or 0) for rd in room_rev), 2)
    dorm_revenue_native = round(sum(float(rd.nightly_rate or 0) for rd in dorm_rev), 2)

    # ── OCC: traditional in-house (spanning) with OCC exclusion filter ───────
    inhouse_raw = db.query(Reservation).filter(
        Reservation.branch_id == branch.id,
        Reservation.check_in_date <= target_date,
        Reservation.check_out_date > target_date,
    ).all()
    # Apply OCC exclusion filter (only excludes cancelled/no-show + maintenance)
    inhouse_res = [r for r in inhouse_raw if not _is_excluded_occ(r)]

    room_ih = [r for r in inhouse_res if (r.room_type_category or "").lower() == "room"]
    dorm_ih = [r for r in inhouse_res if (r.room_type_category or "").lower() == "dorm"]
    rooms_inhouse = len(_room_night_expand(room_ih))
    dorms_inhouse = len(_room_night_expand(dorm_ih))
    total_inhouse = rooms_inhouse + dorms_inhouse

    occ_pct      = round(total_inhouse / total_rooms, 4) if total_rooms > 0 else 0.0
    room_occ_pct = round(rooms_inhouse / total_room_count, 4) if total_room_count > 0 else None
    dorm_occ_pct = round(dorms_inhouse / total_dorm_count, 4) if total_dorm_count > 0 else None

    # ADR and RevPAR (in native currency) — using nightly rate revenue
    adr_native    = round(revenue_native / total_sold, 2) if total_sold > 0 else 0.0
    room_adr_native = round(room_revenue_native / rooms_sold, 2) if rooms_sold > 0 else 0.0
    dorm_adr_native = round(dorm_revenue_native / dorms_sold, 2) if dorms_sold > 0 else 0.0
    revpar_native = round(revenue_native / total_rooms, 2) if total_rooms > 0 else 0.0

    # New bookings made on this date (by reservation_date)
    new_bookings = db.query(Reservation).filter(
        Reservation.branch_id == branch.id,
        Reservation.reservation_date == target_date,
    ).count()

    # Cancellations + No-shows: count by STATUS for check-in date
    # (cancellation_date is often NULL — Cloudbeds bulk API lite payload doesn't include it)
    cancellations = db.query(Reservation).filter(
        Reservation.branch_id == branch.id,
        Reservation.check_in_date == target_date,
        func.lower(Reservation.status).in_(["cancelled", "canceled", "no_show", "noshow", "no show", "no-show"]),
    ).count()

    total_checkin = db.query(Reservation).filter(
        Reservation.branch_id == branch.id,
        Reservation.check_in_date == target_date,
    ).count()
    cancellation_pct = round(cancellations / total_checkin, 4) if total_checkin > 0 else 0.0

    # Upsert
    dm = db.query(DailyMetrics).filter_by(branch_id=branch.id, date=target_date).first()
    if not dm:
        dm = DailyMetrics(branch_id=branch.id, date=target_date)
        db.add(dm)

    dm.rooms_sold        = rooms_sold
    dm.dorms_sold        = dorms_sold
    dm.total_sold        = total_sold
    dm.occ_pct           = occ_pct
    dm.room_occ_pct      = room_occ_pct
    dm.dorm_occ_pct      = dorm_occ_pct
    dm.revenue_native       = revenue_native
    dm.revenue_vnd          = revenue_vnd
    dm.room_revenue_native  = room_revenue_native
    dm.dorm_revenue_native  = dorm_revenue_native
    dm.adr_native           = adr_native
    dm.room_adr_native      = room_adr_native
    dm.dorm_adr_native      = dorm_adr_native
    dm.revpar_native        = revpar_native
    dm.new_bookings         = new_bookings
    dm.cancellations        = cancellations
    dm.cancellation_pct     = cancellation_pct
    dm.computed_at          = datetime.now(timezone.utc)

    db.commit()
    db.refresh(dm)
    return dm


def recompute_branch_range(
    db: Session,
    branch: Branch,
    date_from: date,
    date_to: date,
) -> int:
    """
    v2.0: Fast bulk recompute using reservation_daily for revenue attribution.
    Fetches all data once, computes in Python, then bulk-upserts.
    """
    from collections import defaultdict

    total_rooms: int = branch.total_rooms or 0
    total_room_count: int = branch.total_room_count or 0
    total_dorm_count: int = branch.total_dorm_count or 0

    # ── Lightweight proxy for session-independent computation ────────────────

    class _ResProxy:
        """Lightweight plain-Python copy of a Reservation row."""
        __slots__ = ("id", "status", "source", "room_number", "room_type_category",
                     "check_in_date", "check_out_date", "nights",
                     "grand_total_native", "grand_total_vnd")

        def __init__(self, r: Reservation):
            self.id                = r.id
            self.status            = r.status
            self.source            = r.source
            self.room_number       = r.room_number
            self.room_type_category = r.room_type_category
            self.check_in_date     = r.check_in_date
            self.check_out_date    = r.check_out_date
            self.nights            = r.nights
            self.grand_total_native = r.grand_total_native
            self.grand_total_vnd   = r.grand_total_vnd

    class _DailyProxy:
        """Lightweight proxy for ReservationDaily row."""
        __slots__ = ("reservation_id", "date", "room_id", "nightly_rate", "nightly_rate_vnd",
                     "status", "source", "source_category", "room_type_category")

        def __init__(self, rd: ReservationDaily):
            self.reservation_id   = rd.reservation_id
            self.date             = rd.date
            self.room_id          = rd.room_id
            self.nightly_rate     = rd.nightly_rate
            self.nightly_rate_vnd = rd.nightly_rate_vnd
            self.status           = rd.status
            self.source           = rd.source
            self.source_category  = rd.source_category
            self.room_type_category = rd.room_type_category

    # ── Fetch all data ──────────────────────────────────────────────────────

    # 1. reservation_daily rows for revenue (v2.0)
    daily_raw = db.query(ReservationDaily).filter(
        ReservationDaily.branch_id == branch.id,
        ReservationDaily.date >= date_from,
        ReservationDaily.date <= date_to,
    ).all()
    all_daily = [_DailyProxy(rd) for rd in daily_raw]

    # 2. Spanning reservations for OCC
    spanning_raw = db.query(Reservation).filter(
        Reservation.branch_id == branch.id,
        Reservation.check_in_date <= date_to,
        Reservation.check_out_date > date_from,
    ).all()
    spanning_res = [_ResProxy(r) for r in spanning_raw]

    # 3. Booking counts (by reservation_date)
    booking_res = db.query(Reservation).filter(
        Reservation.branch_id == branch.id,
        Reservation.reservation_date >= date_from,
        Reservation.reservation_date <= date_to,
    ).with_entities(Reservation.reservation_date).all()

    # 4. Cancellation + No-show counts by CHECK-IN date + status
    #    (cancellation_date is often NULL in Cloudbeds bulk API lite payload)
    cancel_by_checkin = db.query(
        Reservation.check_in_date,
        func.count(Reservation.id),
    ).filter(
        Reservation.branch_id == branch.id,
        Reservation.check_in_date >= date_from,
        Reservation.check_in_date <= date_to,
        func.lower(Reservation.status).in_(["cancelled", "canceled", "no_show", "noshow", "no show", "no-show"]),
    ).group_by(Reservation.check_in_date).all()

    # 5. Total reservations by check-in date (for cancel % denominator)
    total_by_checkin = db.query(
        Reservation.check_in_date,
        func.count(Reservation.id),
    ).filter(
        Reservation.branch_id == branch.id,
        Reservation.check_in_date >= date_from,
        Reservation.check_in_date <= date_to,
    ).group_by(Reservation.check_in_date).all()

    # ── Precompute day-indexed lookups ───────────────────────────────────────

    new_bookings_map: dict[date, int] = defaultdict(int)
    for (rd,) in booking_res:
        if rd:
            new_bookings_map[rd] += 1

    cancellations_map: dict[date, int] = defaultdict(int)
    for ci_date, cnt in cancel_by_checkin:
        if ci_date:
            cancellations_map[ci_date] = cnt

    total_checkin_map: dict[date, int] = defaultdict(int)
    for ci_date, cnt in total_by_checkin:
        if ci_date:
            total_checkin_map[ci_date] = cnt

    # OCC filter: exclude cancelled/no-show + maintenance only
    spanning_valid_occ = [r for r in spanning_res if not _is_excluded_occ(r)]

    # Revenue filter: exclude cancelled/no-show + non-paying sources (NOT day use)
    valid_daily_revenue = [rd for rd in all_daily if not _is_excluded_revenue(rd)]

    # OCC filter for rooms_sold: include ALL sources (only exclude cancelled + maintenance)
    valid_daily_occ = [rd for rd in all_daily if not _is_excluded_occ(rd)]

    # Pre-index reservation_daily by date for O(1) lookup
    daily_by_date: dict = defaultdict(list)
    for rd in valid_daily_revenue:
        daily_by_date[rd.date].append(rd)

    daily_occ_by_date: dict = defaultdict(list)
    for rd in valid_daily_occ:
        daily_occ_by_date[rd.date].append(rd)

    # Load existing daily_metrics for upsert
    existing_map: dict[date, DailyMetrics] = {
        dm.date: dm
        for dm in db.query(DailyMetrics).filter(
            DailyMetrics.branch_id == branch.id,
            DailyMetrics.date >= date_from,
            DailyMetrics.date <= date_to,
        ).all()
    }

    # ── Iterate days ────────────────────────────────────────────────────────

    current = date_from
    count = 0

    while current <= date_to:
        # ── Revenue from reservation_daily (v2.0 nightly rate) ───────────────
        day_revenue_rows = daily_by_date.get(current, [])
        day_occ_rows = daily_occ_by_date.get(current, [])

        # rooms_sold from OCC filter (ALL sources incl blogger/kol/house use)
        room_occ = [rd for rd in day_occ_rows if (rd.room_type_category or "").lower() == "room"]
        dorm_occ = [rd for rd in day_occ_rows if (rd.room_type_category or "").lower() == "dorm"]
        rooms_sold = len(_room_expand_daily(room_occ))
        dorms_sold = len(_room_expand_daily(dorm_occ))
        total_sold = rooms_sold + dorms_sold

        # Revenue from revenue-filtered rows only
        room_rev = [rd for rd in day_revenue_rows if (rd.room_type_category or "").lower() == "room"]
        dorm_rev = [rd for rd in day_revenue_rows if (rd.room_type_category or "").lower() == "dorm"]

        # nightly_rate = grand_total_native / nights, which includes non-accommodation charges.
        # Use it only for room/dorm split proportions — not as the source-of-truth for revenue.
        nightly_room_rev = round(sum(float(rd.nightly_rate or 0) for rd in room_rev), 2)
        nightly_dorm_rev = round(sum(float(rd.nightly_rate or 0) for rd in dorm_rev), 2)
        nightly_total_rev = nightly_room_rev + nightly_dorm_rev

        # Prefer accommodation-only revenue from sync_daily_revenue (Cloudbeds Room Revenue
        # transactions). Fall back to nightly_rate proration only when no transaction data exists.
        _dm_pre = existing_map.get(current)
        txn_revenue = float(_dm_pre.revenue_native or 0) if _dm_pre else 0.0
        txn_revenue_vnd = float(_dm_pre.revenue_vnd or 0) if _dm_pre else 0.0

        if txn_revenue > 0:
            revenue_native = txn_revenue
            revenue_vnd = txn_revenue_vnd
            room_ratio = nightly_room_rev / nightly_total_rev if nightly_total_rev > 0 else 1.0
            room_revenue_native = round(revenue_native * room_ratio, 2)
            dorm_revenue_native = round(revenue_native - room_revenue_native, 2)
        else:
            revenue_native = round(sum(float(rd.nightly_rate or 0) for rd in day_revenue_rows), 2)
            revenue_vnd    = round(sum(float(rd.nightly_rate_vnd or 0) for rd in day_revenue_rows), 2)
            room_revenue_native = nightly_room_rev
            dorm_revenue_native = nightly_dorm_rev

        adr_native      = round(revenue_native / total_sold, 2) if total_sold > 0 else 0.0
        room_adr_native = round(room_revenue_native / rooms_sold, 2) if rooms_sold > 0 else 0.0
        dorm_adr_native = round(dorm_revenue_native / dorms_sold, 2) if dorms_sold > 0 else 0.0
        revpar_native   = round(revenue_native / total_rooms, 2) if total_rooms > 0 else 0.0

        # ── OCC: traditional in-house with OCC exclusion filter ──────────────
        inhouse_res = [
            r for r in spanning_valid_occ
            if r.check_in_date <= current
            and (r.check_out_date or current + timedelta(days=1)) > current
        ]
        room_ih = [r for r in inhouse_res if (r.room_type_category or "").lower() == "room"]
        dorm_ih = [r for r in inhouse_res if (r.room_type_category or "").lower() == "dorm"]
        rooms_inhouse = len(_room_night_expand(room_ih))
        dorms_inhouse = len(_room_night_expand(dorm_ih))
        total_inhouse = rooms_inhouse + dorms_inhouse

        occ_pct      = round(total_inhouse / total_rooms, 4) if total_rooms > 0 else 0.0
        room_occ_pct = round(rooms_inhouse / total_room_count, 4) if total_room_count > 0 else None
        dorm_occ_pct = round(dorms_inhouse / total_dorm_count, 4) if total_dorm_count > 0 else None

        new_bookings  = new_bookings_map.get(current, 0)
        cancellations = cancellations_map.get(current, 0)
        total_checkin = total_checkin_map.get(current, 0)
        cancellation_pct = round(cancellations / total_checkin, 4) if total_checkin > 0 else 0.0

        dm = existing_map.get(current)
        if not dm:
            dm = DailyMetrics(branch_id=branch.id, date=current)
            db.add(dm)

        dm.rooms_sold       = rooms_sold
        dm.dorms_sold       = dorms_sold
        dm.total_sold       = total_sold
        dm.occ_pct          = occ_pct
        dm.room_occ_pct     = room_occ_pct
        dm.dorm_occ_pct     = dorm_occ_pct
        dm.revenue_native       = revenue_native
        dm.revenue_vnd          = revenue_vnd
        dm.room_revenue_native  = room_revenue_native
        dm.dorm_revenue_native  = dorm_revenue_native
        dm.adr_native           = adr_native
        dm.room_adr_native      = room_adr_native
        dm.dorm_adr_native      = dorm_adr_native
        dm.revpar_native        = revpar_native
        dm.new_bookings     = new_bookings
        dm.cancellations    = cancellations
        dm.cancellation_pct = cancellation_pct
        dm.computed_at      = datetime.now(timezone.utc)

        current += timedelta(days=1)
        count += 1

        if count % 90 == 0:
            db.commit()

    db.commit()
    return count


def recompute_occ_and_bookings(
    db: Session,
    branch: Branch,
    date_from: date,
    date_to: date,
) -> int:
    """
    Recompute only OCC/rooms_sold/new_bookings/cancellations — never revenue/ADR/RevPAR.
    Revenue is authoritative from sync_cloudbeds_filtered (Insights API) and must not
    be overwritten by local reservation_daily proration.
    """
    from collections import defaultdict

    total_rooms: int = branch.total_rooms or 0
    total_room_count: int = branch.total_room_count or 0
    total_dorm_count: int = branch.total_dorm_count or 0

    class _ResProxy:
        __slots__ = ("id", "status", "source", "room_number", "room_type_category",
                     "check_in_date", "check_out_date", "nights")

        def __init__(self, r: Reservation):
            self.id                 = r.id
            self.status             = r.status
            self.source             = r.source
            self.room_number        = r.room_number
            self.room_type_category = r.room_type_category
            self.check_in_date      = r.check_in_date
            self.check_out_date     = r.check_out_date
            self.nights             = r.nights

    class _DailyProxy:
        __slots__ = ("reservation_id", "date", "room_id", "status", "source",
                     "source_category", "room_type_category")

        def __init__(self, rd: ReservationDaily):
            self.reservation_id   = rd.reservation_id
            self.date             = rd.date
            self.room_id          = rd.room_id
            self.status           = rd.status
            self.source           = rd.source
            self.source_category  = rd.source_category
            self.room_type_category = rd.room_type_category

    daily_raw = db.query(ReservationDaily).filter(
        ReservationDaily.branch_id == branch.id,
        ReservationDaily.date >= date_from,
        ReservationDaily.date <= date_to,
    ).all()
    all_daily = [_DailyProxy(rd) for rd in daily_raw]

    spanning_raw = db.query(Reservation).filter(
        Reservation.branch_id == branch.id,
        Reservation.check_in_date <= date_to,
        Reservation.check_out_date > date_from,
    ).all()
    spanning_res = [_ResProxy(r) for r in spanning_raw]

    booking_res = db.query(Reservation).filter(
        Reservation.branch_id == branch.id,
        Reservation.reservation_date >= date_from,
        Reservation.reservation_date <= date_to,
    ).with_entities(Reservation.reservation_date).all()

    cancel_by_checkin = db.query(
        Reservation.check_in_date,
        func.count(Reservation.id),
    ).filter(
        Reservation.branch_id == branch.id,
        Reservation.check_in_date >= date_from,
        Reservation.check_in_date <= date_to,
        func.lower(Reservation.status).in_(["cancelled", "canceled", "no_show", "noshow", "no show", "no-show"]),
    ).group_by(Reservation.check_in_date).all()

    total_by_checkin = db.query(
        Reservation.check_in_date,
        func.count(Reservation.id),
    ).filter(
        Reservation.branch_id == branch.id,
        Reservation.check_in_date >= date_from,
        Reservation.check_in_date <= date_to,
    ).group_by(Reservation.check_in_date).all()

    new_bookings_map: dict[date, int] = defaultdict(int)
    for (rd,) in booking_res:
        if rd:
            new_bookings_map[rd] += 1

    cancellations_map: dict[date, int] = defaultdict(int)
    for ci_date, cnt in cancel_by_checkin:
        if ci_date:
            cancellations_map[ci_date] = cnt

    total_checkin_map: dict[date, int] = defaultdict(int)
    for ci_date, cnt in total_by_checkin:
        if ci_date:
            total_checkin_map[ci_date] = cnt

    spanning_valid_occ = [r for r in spanning_res if not _is_excluded_occ(r)]
    valid_daily_occ    = [rd for rd in all_daily if not _is_excluded_occ(rd)]

    daily_occ_by_date: dict = defaultdict(list)
    for rd in valid_daily_occ:
        daily_occ_by_date[rd.date].append(rd)

    existing_map: dict[date, DailyMetrics] = {
        dm.date: dm
        for dm in db.query(DailyMetrics).filter(
            DailyMetrics.branch_id == branch.id,
            DailyMetrics.date >= date_from,
            DailyMetrics.date <= date_to,
        ).all()
    }

    current = date_from
    count = 0

    while current <= date_to:
        day_occ_rows = daily_occ_by_date.get(current, [])

        room_occ  = [rd for rd in day_occ_rows if (rd.room_type_category or "").lower() == "room"]
        dorm_occ  = [rd for rd in day_occ_rows if (rd.room_type_category or "").lower() == "dorm"]
        rooms_sold = len(_room_expand_daily(room_occ))
        dorms_sold = len(_room_expand_daily(dorm_occ))
        total_sold = rooms_sold + dorms_sold

        inhouse_res = [
            r for r in spanning_valid_occ
            if r.check_in_date <= current
            and (r.check_out_date or current + timedelta(days=1)) > current
        ]
        room_ih = [r for r in inhouse_res if (r.room_type_category or "").lower() == "room"]
        dorm_ih = [r for r in inhouse_res if (r.room_type_category or "").lower() == "dorm"]
        rooms_inhouse = len(_room_night_expand(room_ih))
        dorms_inhouse = len(_room_night_expand(dorm_ih))
        total_inhouse = rooms_inhouse + dorms_inhouse

        occ_pct      = round(total_inhouse / total_rooms, 4) if total_rooms > 0 else 0.0
        room_occ_pct = round(rooms_inhouse / total_room_count, 4) if total_room_count > 0 else None
        dorm_occ_pct = round(dorms_inhouse / total_dorm_count, 4) if total_dorm_count > 0 else None

        new_bookings     = new_bookings_map.get(current, 0)
        cancellations    = cancellations_map.get(current, 0)
        total_checkin    = total_checkin_map.get(current, 0)
        cancellation_pct = round(cancellations / total_checkin, 4) if total_checkin > 0 else 0.0

        dm = existing_map.get(current)
        if not dm:
            dm = DailyMetrics(branch_id=branch.id, date=current)
            db.add(dm)

        dm.rooms_sold       = rooms_sold
        dm.dorms_sold       = dorms_sold
        dm.total_sold       = total_sold
        dm.occ_pct          = occ_pct
        dm.room_occ_pct     = room_occ_pct
        dm.dorm_occ_pct     = dorm_occ_pct
        dm.new_bookings     = new_bookings
        dm.cancellations    = cancellations
        dm.cancellation_pct = cancellation_pct
        dm.computed_at      = datetime.now(timezone.utc)

        current += timedelta(days=1)
        count += 1

        if count % 90 == 0:
            db.commit()

    db.commit()
    return count


async def nightly_metrics_job(db_factory) -> None:
    """
    v2.4: Nightly job — populate reservation_daily, recompute daily_metrics,
    then overlay Cloudbeds Data Insights OCC/ADR/RevPAR/Revenue.

    Coverage:
      - reservation_daily + compute_day: last 14 days + today (catch retroactive updates)
      - Cloudbeds Insights: Jan 1 of current year through end of NEXT month
    This ensures Revenue KPI yearly grid + Daily/Weekly/Monthly dashboards
    always have complete Insights data for the entire year.
    """
    import calendar
    from app.config import settings
    from app.services.cloudbeds import (
        populate_reservation_daily, sync_cloudbeds_occupancy,
        sync_cloudbeds_filtered, sync_branch_revenue,
    )

    db: Session = db_factory()
    try:
        today = datetime.now(timezone.utc).date()
        lookback_start = today - timedelta(days=14)

        # Insights sync boundaries: Jan 1 through Dec 31 of current year
        # Full-year coverage ensures Revenue KPI yearly grid has accurate actuals
        # for ALL months including forward bookings (Jun–Dec)
        insights_start = date(today.year, 1, 1)
        insights_end = date(today.year, 12, 31)

        branches = db.query(Branch).filter_by(is_active=True).all()
        for branch in branches:
            try:
                # Get API credentials for actual nightly rate fetch
                pid = branch.cloudbeds_property_id
                api_key = settings.get_api_key_for_property(str(pid)) if pid else None

                # Step 1: update grand_total_native to accommodation-only amounts
                # so that populate_reservation_daily proration (grand_total/nights)
                # uses the correct base — not a total that includes extras/taxes.
                if pid and api_key:
                    try:
                        sync_branch_revenue(
                            str(branch.id), str(pid), branch.currency, api_key,
                            date_from=lookback_start, date_to=today,
                        )
                    except Exception as rev_err:
                        logger.warning(
                            "sync_branch_revenue failed branch=%s: %s (proration fallback used)",
                            branch.name, rev_err,
                        )

                # Step 2: populate reservation_daily for last 14 days + today
                populate_reservation_daily(
                    db, str(branch.id),
                    date_from=lookback_start, date_to=today,
                    property_id=pid,
                    currency=branch.currency,
                    api_key=api_key,
                )
                # Step 3: recompute OCC/rooms_sold/bookings only — revenue stays from Insights
                recompute_occ_and_bookings(db, branch, lookback_start, today)

                # Step 3: overlay Cloudbeds Data Insights for extended range
                # From 14 days ago through end of next month — covers Daily Brief,
                # Weekly, Monthly dashboards + forecast data
                if pid and api_key:
                    try:
                        sync_cloudbeds_occupancy(
                            db, str(branch.id), pid, branch.currency, api_key,
                            date_from=insights_start, date_to=insights_end,
                        )
                        # Step 4: sync room/dorm split from filtered Insights API
                        sync_cloudbeds_filtered(
                            db, str(branch.id), pid, branch.currency, api_key,
                            total_rooms=branch.total_rooms,
                            total_room_count=branch.total_room_count or 0,
                            total_dorm_count=branch.total_dorm_count or 0,
                            date_from=insights_start, date_to=insights_end,
                        )
                    except Exception as occ_err:
                        logger.warning(
                            "Cloudbeds Insights sync failed branch=%s: %s (computed values retained)",
                            branch.name, occ_err,
                        )

                logger.info(
                    f"Metrics v2.4 OK branch={branch.name} "
                    f"compute={lookback_start}..{today}, "
                    f"insights={insights_start}..{insights_end}"
                )
            except Exception as e:
                logger.error(f"Metrics FAIL branch={branch.name}: {e}")
    finally:
        db.close()


async def cloudbeds_insights_sync_job(db_factory) -> None:
    """
    Standalone job to sync Cloudbeds Data Insights (OCC/ADR/RevPAR/Revenue).
    Runs independently of nightly_metrics_job so revenue/OCC stays fresh
    throughout the day.

    Coverage: Jan 1 through Dec 31 of current year.
    This ensures the Revenue KPI yearly grid always has complete, accurate
    actual revenue from Cloudbeds Insights API for ALL months of the year,
    including forward bookings for future months.
    """
    from app.config import settings
    from app.services.cloudbeds import sync_cloudbeds_occupancy, sync_cloudbeds_filtered

    db: Session = db_factory()
    try:
        today = datetime.now(timezone.utc).date()
        sync_start = date(today.year, 1, 1)
        sync_end = date(today.year, 12, 31)

        branches = db.query(Branch).filter_by(is_active=True).all()
        for branch in branches:
            pid = branch.cloudbeds_property_id
            api_key = settings.get_api_key_for_property(str(pid)) if pid else None
            if not pid or not api_key:
                continue
            try:
                sync_cloudbeds_occupancy(
                    db, str(branch.id), pid, branch.currency, api_key,
                    date_from=sync_start, date_to=sync_end,
                )
                sync_cloudbeds_filtered(
                    db, str(branch.id), pid, branch.currency, api_key,
                    total_rooms=branch.total_rooms,
                    total_room_count=branch.total_room_count or 0,
                    total_dorm_count=branch.total_dorm_count or 0,
                    date_from=sync_start, date_to=sync_end,
                )
                logger.info(f"Insights sync OK branch={branch.name} [{sync_start}..{sync_end}]")
            except Exception as e:
                logger.warning(f"Insights sync FAIL branch={branch.name}: {e}")
    finally:
        db.close()


# ── query helpers ──────────────────────────────────────────────────────────────

def get_daily_metrics(
    db: Session,
    branch_id: Optional[UUID],
    date_from: date,
    date_to: date,
) -> list[DailyMetrics]:
    q = db.query(DailyMetrics).filter(
        DailyMetrics.date >= date_from,
        DailyMetrics.date <= date_to,
    )
    if branch_id:
        q = q.filter(DailyMetrics.branch_id == branch_id)
    return q.order_by(DailyMetrics.branch_id, DailyMetrics.date).all()


def get_ota_mix(
    db: Session,
    branch_id: Optional[UUID],
    date_from: date,
    date_to: date,
) -> dict:
    """Channel mix by individual OTA source (Direct aggregated, OTA broken out by name)."""
    q = db.query(
        Reservation.source,
        Reservation.source_category,
        func.count(Reservation.id).label("count"),
        func.coalesce(func.sum(Reservation.grand_total_native), 0).label("revenue_native"),
        func.coalesce(func.sum(Reservation.grand_total_vnd), 0).label("revenue_vnd"),
    ).filter(
        Reservation.check_in_date >= date_from,
        Reservation.check_in_date <= date_to,
        func.lower(Reservation.status).notin_(list(EXCLUDED_STATUSES)),
        func.lower(Reservation.source).notin_(list(EXCLUDED_SOURCES)),
    )
    if branch_id:
        q = q.filter(Reservation.branch_id == branch_id)

    rows = q.group_by(Reservation.source, Reservation.source_category).all()

    channels: dict = {}
    for row in rows:
        cat = row.source_category or "OTA"
        key = _channel_key(cat, row.source)
        if key not in channels:
            channels[key] = {"count": 0, "revenue_native": 0.0, "revenue_vnd": 0.0, "category": cat}
        channels[key]["count"] += row.count
        channels[key]["revenue_native"] += float(row.revenue_native)
        channels[key]["revenue_vnd"] += float(row.revenue_vnd)
    return channels


def get_channel_rates(
    db: Session,
    branch_id: Optional[UUID],
    date_from: date,
    date_to: date,
) -> list[dict]:
    """Cancellation rate and check-in rate by channel (individual OTA source or Direct).
    Includes ALL statuses so cancelled bookings count toward the totals.
    """
    from sqlalchemy import case as sa_case

    q = db.query(
        Reservation.source,
        Reservation.source_category,
        func.count(Reservation.id).label("total"),
        func.sum(sa_case((Reservation.status.in_(["cancelled", "canceled"]), 1), else_=0)).label("cancelled"),
        func.sum(sa_case((Reservation.status.in_(["no_show", "noshow"]), 1), else_=0)).label("no_show"),
        func.sum(sa_case((Reservation.status.in_(["checked_in", "checked_out"]), 1), else_=0)).label("checked_in"),
    ).filter(
        Reservation.check_in_date >= date_from,
        Reservation.check_in_date <= date_to,
        func.lower(Reservation.source).notin_(list(EXCLUDED_SOURCES)),
    )
    if branch_id:
        q = q.filter(Reservation.branch_id == branch_id)

    rows = q.group_by(Reservation.source, Reservation.source_category).all()

    channels: dict = {}
    for row in rows:
        cat = row.source_category or "OTA"
        key = _channel_key(cat, row.source)
        if key not in channels:
            channels[key] = {"total": 0, "cancelled": 0, "no_show": 0, "checked_in": 0, "category": cat}
        channels[key]["total"]      += row.total
        channels[key]["cancelled"]  += int(row.cancelled or 0)
        channels[key]["no_show"]    += int(row.no_show or 0)
        channels[key]["checked_in"] += int(row.checked_in or 0)

    result = []
    for channel, v in sorted(channels.items(), key=lambda x: -x[1]["total"]):
        total        = v["total"]
        cancelled    = v["cancelled"]
        no_show      = v["no_show"]
        checked_in   = v["checked_in"]
        non_cancelled = total - cancelled
        result.append({
            "channel":      channel,
            "category":     v["category"],
            "total":        total,
            "cancelled":    cancelled,
            "no_show":      no_show,
            "checked_in":   checked_in,
            "confirmed":    max(0, non_cancelled - no_show - checked_in),
            "cancel_rate":  round(cancelled / total, 4) if total > 0 else 0,
            "checkin_rate": round(checked_in / non_cancelled, 4) if non_cancelled > 0 else 0,
            "noshow_rate":  round(no_show / non_cancelled, 4) if non_cancelled > 0 else 0,
        })
    return result


def get_ota_trend(
    db: Session,
    branch_id: Optional[UUID],
    mode: str = "daily",
) -> dict:
    """OTA channel share pivot table.
    mode: daily (last 7 days) | weekly (last 7 weeks) | monthly (last 3 months)
    Returns: periods (labels) × channels (rows) with count + pct per cell.
    """
    from collections import defaultdict

    today = date.today()

    if mode == "daily":
        date_from = today - timedelta(days=6)
    elif mode == "weekly":
        start_of_this_week = today - timedelta(days=today.weekday())
        date_from = start_of_this_week - timedelta(weeks=6)
    else:  # monthly
        m, y = today.month - 2, today.year
        while m <= 0:
            m += 12; y -= 1
        date_from = date(y, m, 1)

    # Choose period expression
    if mode == "daily":
        period_expr = Reservation.check_in_date
    elif mode == "weekly":
        period_expr = func.date_trunc("week", Reservation.check_in_date)
    else:
        period_expr = func.date_trunc("month", Reservation.check_in_date)

    q = db.query(
        period_expr.label("period"),
        Reservation.source,
        Reservation.source_category,
        func.count(Reservation.id).label("count"),
    ).filter(
        Reservation.check_in_date >= date_from,
        Reservation.check_in_date <= today,
        Reservation.status.notin_(list(EXCLUDED_STATUSES)),
        func.lower(Reservation.source).notin_(list(EXCLUDED_SOURCES)),
    )
    if branch_id:
        q = q.filter(Reservation.branch_id == branch_id)
    q = q.group_by(period_expr, Reservation.source, Reservation.source_category)
    rows = q.all()

    # Normalize period keys → date objects
    period_channel: dict = defaultdict(lambda: defaultdict(int))
    all_channels: set = set()
    for row in rows:
        cat = row.source_category or "OTA"
        channel = _channel_key(cat, row.source)
        p = row.period
        if hasattr(p, "date"):
            p = p.date()
        elif not isinstance(p, date):
            p = date.fromisoformat(str(p)[:10])
        period_channel[p][channel] += row.count
        all_channels.add(channel)

    # Build canonical period list
    if mode == "daily":
        periods = [today - timedelta(days=i) for i in range(6, -1, -1)]
        labels  = [d.strftime("%b %d") for d in periods]
    elif mode == "weekly":
        start_of_this_week = today - timedelta(days=today.weekday())
        periods = [start_of_this_week - timedelta(weeks=i) for i in range(6, -1, -1)]
        labels  = [f"W{d.isocalendar()[1]:02d} ({d.strftime('%m/%d')})" for d in periods]
    else:
        periods = []
        for i in range(2, -1, -1):
            m, y = today.month - i, today.year
            while m <= 0:
                m += 12; y -= 1
            periods.append(date(y, m, 1))
        labels = [d.strftime("%b %Y") for d in periods]

    # Sort: OTAs by total desc, Direct always last
    channel_totals: dict = defaultdict(int)
    for pc in period_channel.values():
        for ch, cnt in pc.items():
            channel_totals[ch] += cnt

    sorted_channels = sorted(all_channels - _AGGREGATED_CATEGORIES, key=lambda c: -channel_totals[c])
    if "Local travel agency" in all_channels:
        sorted_channels.append("Local travel agency")
    if "Direct" in all_channels:
        sorted_channels.append("Direct")

    # Build result
    result_channels = []
    for channel in sorted_channels:
        cells = []
        for p in periods:
            cnt = period_channel.get(p, {}).get(channel, 0)
            period_total = sum(period_channel.get(p, {}).values())
            pct = cnt / period_total if period_total > 0 else 0
            cells.append({"count": cnt, "pct": round(pct, 4)})
        result_channels.append({
            "channel":   channel,
            "is_direct": channel == "Direct",
            "total":     channel_totals[channel],
            "cells":     cells,
        })

    return {"mode": mode, "periods": labels, "channels": result_channels}


def get_rates_trend(
    db: Session,
    branch_id: Optional[UUID],
    mode: str = "daily",
    date_type: str = "check_in",
) -> dict:
    """Cancel rate & check-in rate pivot: per channel × per time period.
    mode: daily (last 7 days) | weekly (last 7 weeks) | monthly (last 3 months)
    date_type: check_in (by check-in date) | booked (by reservation/booking date)
    """
    from collections import defaultdict
    from sqlalchemy import case as sa_case

    today = date.today()

    # Pick the date column based on date_type
    date_col = Reservation.reservation_date if date_type == "booked" else Reservation.check_in_date

    if mode == "daily":
        date_from = today - timedelta(days=6)
    elif mode == "weekly":
        start_of_this_week = today - timedelta(days=today.weekday())
        date_from = start_of_this_week - timedelta(weeks=6)
    else:
        m, y = today.month - 2, today.year
        while m <= 0:
            m += 12; y -= 1
        date_from = date(y, m, 1)

    if mode == "daily":
        period_expr = date_col
    elif mode == "weekly":
        period_expr = func.date_trunc("week", date_col)
    else:
        period_expr = func.date_trunc("month", date_col)

    q = db.query(
        period_expr.label("period"),
        Reservation.source,
        Reservation.source_category,
        func.count(Reservation.id).label("total"),
        func.sum(sa_case((Reservation.status.in_(["cancelled", "canceled"]), 1), else_=0)).label("cancelled"),
        func.sum(sa_case((Reservation.status.in_(["no_show", "noshow"]), 1), else_=0)).label("no_show"),
        func.sum(sa_case((Reservation.status.in_(["checked_in", "checked_out"]), 1), else_=0)).label("checked_in"),
    ).filter(
        date_col >= date_from,
        date_col <= today,
        date_col.isnot(None),
        func.lower(Reservation.source).notin_(list(EXCLUDED_SOURCES)),
    )
    if branch_id:
        q = q.filter(Reservation.branch_id == branch_id)
    q = q.group_by(period_expr, Reservation.source, Reservation.source_category)
    rows = q.all()

    # Aggregate into period × channel buckets
    _empty = lambda: {"total": 0, "cancelled": 0, "no_show": 0, "checked_in": 0}
    period_channel: dict = defaultdict(lambda: defaultdict(_empty))
    all_channels: set = set()

    for row in rows:
        cat = row.source_category or "OTA"
        channel = _channel_key(cat, row.source)
        p = row.period
        if hasattr(p, "date"):
            p = p.date()
        elif not isinstance(p, date):
            p = date.fromisoformat(str(p)[:10])
        d = period_channel[p][channel]
        d["total"]      += row.total
        d["cancelled"]  += int(row.cancelled or 0)
        d["no_show"]    += int(row.no_show or 0)
        d["checked_in"] += int(row.checked_in or 0)
        all_channels.add(channel)

    # Build canonical period list
    if mode == "daily":
        periods = [today - timedelta(days=i) for i in range(6, -1, -1)]
        labels  = [d.strftime("%b %d") for d in periods]
    elif mode == "weekly":
        start_of_this_week = today - timedelta(days=today.weekday())
        periods = [start_of_this_week - timedelta(weeks=i) for i in range(6, -1, -1)]
        labels  = [f"W{d.isocalendar()[1]:02d} ({d.strftime('%m/%d')})" for d in periods]
    else:
        periods = []
        for i in range(2, -1, -1):
            m, y = today.month - i, today.year
            while m <= 0:
                m += 12; y -= 1
            periods.append(date(y, m, 1))
        labels = [d.strftime("%b %Y") for d in periods]

    # Sort: OTAs by total desc, Direct always last
    channel_totals: dict = defaultdict(int)
    for pc in period_channel.values():
        for ch, v in pc.items():
            channel_totals[ch] += v["total"]

    sorted_channels = sorted(all_channels - _AGGREGATED_CATEGORIES, key=lambda c: -channel_totals[c])
    if "Local travel agency" in all_channels:
        sorted_channels.append("Local travel agency")
    if "Direct" in all_channels:
        sorted_channels.append("Direct")

    # Pre-compute total checked_in across ALL channels per period
    period_total_checkin: dict = {}
    for p in periods:
        period_total_checkin[p] = sum(
            (period_channel.get(p, {}).get(ch) or _empty())["checked_in"]
            for ch in all_channels
        )

    result_channels = []
    for channel in sorted_channels:
        cancel_cells  = []
        checkin_cells = []
        for p in periods:
            v = period_channel.get(p, {}).get(channel) or _empty()
            total         = v["total"]
            cancelled     = v["cancelled"]
            total_ckin    = period_total_checkin.get(p, 0)
            cancel_cells.append({
                "total":     total,
                "cancelled": cancelled,
                "rate":      round(cancelled / total, 4) if total > 0 else None,
            })
            # Check-in rate = channel checked_in / ALL channels checked_in (this period)
            checkin_cells.append({
                "total":      total_ckin,
                "checked_in": v["checked_in"],
                "rate":       round(v["checked_in"] / total_ckin, 4) if total_ckin > 0 else None,
            })
        result_channels.append({
            "channel":       channel,
            "is_direct":     channel == "Direct",
            "total":         channel_totals[channel],
            "cancel_cells":  cancel_cells,
            "checkin_cells": checkin_cells,
        })

    return {"mode": mode, "periods": labels, "channels": result_channels}


def get_country_yoy(
    db: Session,
    branch_id: Optional[UUID],
    year: int,
    month: Optional[int] = None,
) -> list[dict]:
    results = []
    for y in [year - 1, year]:
        q = db.query(
            Reservation.guest_country_code,
            Reservation.guest_country,
            func.count(Reservation.id).label("count"),
            func.coalesce(func.sum(Reservation.grand_total_native), 0).label("revenue_native"),
        ).filter(
            func.extract("year", Reservation.check_in_date) == y,
            Reservation.status.notin_(list(EXCLUDED_STATUSES)),
        )
        if branch_id:
            q = q.filter(Reservation.branch_id == branch_id)
        if month:
            q = q.filter(func.extract("month", Reservation.check_in_date) == month)

        rows = q.group_by(
            Reservation.guest_country_code, Reservation.guest_country,
        ).order_by(func.count(Reservation.id).desc()).all()

        results.extend({
            "year": y,
            "country_code": row.guest_country_code,
            "country": row.guest_country,
            "count": row.count,
            "revenue_native": float(row.revenue_native),
        } for row in rows)
    return results


def get_country_yoy_insights_local(
    db: Session,
    branch_id: Optional[UUID],
    year: int,
    month: int,
) -> dict:
    """
    Country YoY comparison using local reservations table.
    Returns {current: {country: {nights, revenue, guests}},
             previous: {country: {nights, revenue, guests}}}
    """

    def _query_month(target_year: int, target_month: int) -> dict[str, dict]:
        # COALESCE NULL/empty/junk to "Unknown" so all reservations are accounted
        # for. Previous behaviour silently dropped them, making YoY totals lower
        # than the main dashboard.
        country_expr = func.coalesce(
            func.nullif(func.nullif(Reservation.guest_country, ""), "0"),
            "Unknown",
        ).label("country")
        q = db.query(
            country_expr,
            func.coalesce(func.sum(Reservation.nights), 0).label("nights"),
            func.coalesce(func.sum(Reservation.grand_total_native), 0).label("revenue"),
            func.count(Reservation.id).label("guests"),
        ).filter(
            func.extract("year", Reservation.check_in_date) == target_year,
            func.extract("month", Reservation.check_in_date) == target_month,
            Reservation.status.notin_(list(EXCLUDED_STATUSES)),
        )
        if branch_id:
            q = q.filter(Reservation.branch_id == branch_id)

        rows = q.group_by(country_expr).all()
        return {
            row.country: {
                "nights": int(row.nights),
                "revenue": float(row.revenue),
                "guests": int(row.guests),
            }
            for row in rows
        }

    return {
        "current": _query_month(year, month),
        "previous": _query_month(year - 1, month),
    }
