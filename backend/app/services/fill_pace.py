"""
Fill Pace — how fast a stay month is filling up, and whether that is faster or
slower than the same run-up one year earlier.

Every other Performance page reads one axis: stays that happened inside a date
range. This reads the other one. Pick a stay month (December), look back over a
booking window (the last 60 days), and the question becomes "of December, how
much was already sold at each point in that window, and how does the shape of
that line compare with the same countdown to last December".

Two words carry the page, and they are not interchangeable:

  on the books (OTB) — room-nights in the stay month booked on or before an
                       as-of date. Cumulative; it only ever goes up.
  pickup             — room-nights added inside the booking window. The slope
                       of that line. This is the *speed*; OTB is the position.

Both years are compared at the same distance from their month, never at the
same calendar date — 60 days before 1 Dec 2026 is matched against 60 days
before 1 Dec 2025, so the comparison is like for like.

One honest limitation, surfaced on the page rather than buried here: the curve
is reconstructed from today's snapshot of the reservations table. A booking
made in June and cancelled in July is absent from every point on the line, not
just the points after it was cancelled. Last year's month has had all of its
cancellations resolved; a future month's have not happened yet. The current
curve is therefore the more generous of the two by construction, and a small
year-over-year lead is not proof of a real one.
"""
from __future__ import annotations

import calendar
import logging
from datetime import date, timedelta
from typing import Optional
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.branch import Branch
from app.models.reservation import Reservation
from app.services.metrics_engine import (
    EXCLUDED_STATUSES,
    EXCLUDED_SOURCES_OCC,
    EXCLUDED_SOURCES_REVENUE,
)

logger = logging.getLogger(__name__)

# Inventory column that matches each room-type filter. Counting private-room
# nights against the whole-branch inventory would understate occupancy badly,
# so the denominator always follows the numerator's filter.
_INVENTORY_ATTR = {"Room": "total_room_count", "Dorm": "total_dorm_count"}

MAX_WINDOW_DAYS = 365


# ── small helpers ────────────────────────────────────────────────────────────

def month_bounds(year: int, month: int) -> tuple[date, date, int]:
    """(first day, day AFTER the last day, days in month)."""
    dim = calendar.monthrange(year, month)[1]
    start = date(year, month, 1)
    return start, start + timedelta(days=dim), dim


def pct(part: float, whole: float) -> Optional[float]:
    """A rate in percent, or None when there is no denominator to divide by."""
    return round(part / whole * 100, 2) if whole else None


def pts(part_a: float, whole_a: float, part_b: float, whole_b: float) -> Optional[float]:
    """Gap between two rates, in percentage POINTS."""
    if not whole_a or not whole_b:
        return None
    return round((part_a / whole_a - part_b / whole_b) * 100, 2)


def change_pct(now: float, before: float) -> Optional[float]:
    """Relative change against a year-ago base.

    None when the base is zero: a month with no bookings last year has no
    growth rate, and printing one (or an infinity) would invent a number. The
    frontend shows "no base" instead — which is the real answer for Taipei and
    Oani in months they had not opened yet.
    """
    if not before:
        return None
    return round((now - before) / before * 100, 2)


def _empty_totals() -> dict:
    return {
        "room_nights": 0.0,
        "bookings": 0,
        "revenue_native": 0.0,
        "revenue_vnd": 0.0,
    }


# ── query ────────────────────────────────────────────────────────────────────

def fetch_month_rows(
    db: Session,
    branch_id: Optional[UUID],
    year: int,
    month: int,
    room_category: Optional[str] = None,
) -> list:
    """Room-nights in one stay month, bucketed by the date they were booked.

    Grouped by (branch, booking date, source category, source) so the same row
    set answers the headline, the per-branch table and the per-source table
    without going back to the database for each.

    Nights are clipped to the month: a stay running 28 Nov → 3 Dec contributes
    only its two December nights to December, never the whole stay to both
    months. Revenue is prorated on the same basis (grand_total ÷ nights of the
    stay × nights inside the month), because a booking's total covers the whole
    stay and charging all of it to one month would double-count the boundary.

    Exclusions follow the engine's standing split: occupancy drops cancelled /
    no-show and maintenance but keeps blogger / KOL / house use (those bodies
    are in the beds), while revenue additionally drops those non-paying
    sources. Zero-night rows (day use) are dropped outright — they add no
    room-nights and their zero span has no rate to prorate by.
    """
    month_start, month_end_excl, _ = month_bounds(year, month)

    nights_in_month = func.greatest(
        0,
        func.least(Reservation.check_out_date, month_end_excl)
        - func.greatest(Reservation.check_in_date, month_start),
    )
    span = Reservation.check_out_date - Reservation.check_in_date
    revenue_native = (
        func.coalesce(Reservation.grand_total_native, 0) * nights_in_month / func.nullif(span, 0)
    )
    revenue_vnd = (
        func.coalesce(Reservation.grand_total_vnd, 0) * nights_in_month / func.nullif(span, 0)
    )
    paying = ~func.lower(func.coalesce(Reservation.source, "")).in_(
        [s.lower() for s in EXCLUDED_SOURCES_REVENUE]
    )

    q = db.query(
        Reservation.branch_id,
        Reservation.reservation_date,
        Reservation.source_category,
        Reservation.source,
        func.sum(nights_in_month).label("room_nights"),
        func.count(Reservation.id).label("bookings"),
        func.coalesce(func.sum(revenue_native).filter(paying), 0).label("revenue_native"),
        func.coalesce(func.sum(revenue_vnd).filter(paying), 0).label("revenue_vnd"),
    ).filter(
        Reservation.check_in_date < month_end_excl,
        Reservation.check_out_date > month_start,
        Reservation.check_out_date > Reservation.check_in_date,
        ~func.lower(func.coalesce(Reservation.status, "")).in_(list(EXCLUDED_STATUSES)),
        ~func.lower(func.coalesce(Reservation.source, "")).in_(
            [s.lower() for s in EXCLUDED_SOURCES_OCC]
        ),
    )

    if branch_id:
        q = q.filter(Reservation.branch_id == branch_id)
    if room_category:
        q = q.filter(
            func.lower(func.coalesce(Reservation.room_type_category, "")) == room_category.lower()
        )

    return q.group_by(
        Reservation.branch_id,
        Reservation.reservation_date,
        Reservation.source_category,
        Reservation.source,
    ).all()


# ── curve building ───────────────────────────────────────────────────────────

def build_curve(rows, as_of_dates: list[date]) -> tuple[list[dict], dict]:
    """Walk booking-date buckets into a cumulative on-the-books curve.

    `as_of_dates` are the snapshot dates to report, ascending and contiguous.
    Anything booked before the first one forms the opening balance, so the
    window shows the slope without pretending the month started empty.
    Anything booked after the last one is counted in `final` but left off the
    curve — that is what makes `final` readable as "where the month ended up"
    for a month already in the past.

    Rows with no reservation_date are on the books but cannot be placed in
    time. They join the opening balance and are reported separately, so a
    figure that cannot be dated is visible rather than silently invented.
    """
    if not as_of_dates:
        return [], {}

    start = as_of_dates[0]
    opening = _empty_totals()
    undated = {"room_nights": 0.0, "bookings": 0}
    final = _empty_totals()
    by_date: dict[date, dict] = {}

    for r in rows:
        vals = {
            "room_nights": float(r.room_nights or 0),
            "bookings": int(r.bookings or 0),
            "revenue_native": float(r.revenue_native or 0),
            "revenue_vnd": float(r.revenue_vnd or 0),
        }
        for k in final:
            final[k] += vals[k]

        booked_on = r.reservation_date
        if booked_on is None:
            undated["room_nights"] += vals["room_nights"]
            undated["bookings"] += vals["bookings"]
            for k in opening:
                opening[k] += vals[k]
            continue
        if booked_on < start:
            for k in opening:
                opening[k] += vals[k]
            continue

        bucket = by_date.setdefault(booked_on, _empty_totals())
        for k in bucket:
            bucket[k] += vals[k]

    running = dict(opening)
    curve = []
    for d in as_of_dates:
        day = by_date.get(d)
        if day:
            for k in running:
                running[k] += day[k]
        curve.append({
            "date": d.isoformat(),
            "otb_room_nights": round(running["room_nights"], 2),
            "otb_bookings": running["bookings"],
            "otb_revenue_native": round(running["revenue_native"], 2),
            "day_room_nights": round(day["room_nights"], 2) if day else 0.0,
            "day_bookings": day["bookings"] if day else 0,
        })

    summary = {
        "opening_room_nights": round(opening["room_nights"], 2),
        "otb_room_nights": round(running["room_nights"], 2),
        "otb_bookings": running["bookings"],
        "otb_revenue_native": round(running["revenue_native"], 2),
        "otb_revenue_vnd": round(running["revenue_vnd"], 2),
        "pickup_room_nights": round(running["room_nights"] - opening["room_nights"], 2),
        "pickup_bookings": running["bookings"] - opening["bookings"],
        "pickup_revenue_native": round(running["revenue_native"] - opening["revenue_native"], 2),
        "final_room_nights": round(final["room_nights"], 2),
        "final_bookings": final["bookings"],
        "undated_room_nights": round(undated["room_nights"], 2),
        "undated_bookings": undated["bookings"],
    }
    return curve, summary


def _decorate(summary: dict, available: float, include_final: bool = False) -> dict:
    """Attach the occupancy rates a room-night count implies."""
    out = dict(summary)
    out["available_room_nights"] = available
    out["otb_occ_pct"] = pct(summary["otb_room_nights"], available)
    out["pickup_occ_pct"] = pct(summary["pickup_room_nights"], available)
    out["pickup_share_of_otb_pct"] = pct(
        summary["pickup_room_nights"], summary["otb_room_nights"]
    )
    if include_final:
        out["final_occ_pct"] = pct(summary["final_room_nights"], available)
        # How much of the month was still to come after this point last year —
        # the number that says whether a lead now actually survives to check-in.
        out["remaining_after_window_room_nights"] = round(
            summary["final_room_nights"] - summary["otb_room_nights"], 2
        )
        out["remaining_after_window_occ_pct"] = pct(
            summary["final_room_nights"] - summary["otb_room_nights"], available
        )
    else:
        out.pop("final_room_nights", None)
        out.pop("final_bookings", None)
    return out


def _compare(cur: dict, ly: dict, avail: float, ly_avail: float) -> dict:
    """Current versus year-ago, in the three ways the question gets asked."""
    return {
        # Where we stand: the occupancy gap at the same distance from the month.
        "otb_occ_pts": pts(cur["otb_room_nights"], avail, ly["otb_room_nights"], ly_avail),
        "otb_room_nights_pct": change_pct(cur["otb_room_nights"], ly["otb_room_nights"]),
        # How fast we are filling: pickup inside the window, both years.
        "pickup_occ_pts": pts(cur["pickup_room_nights"], avail, ly["pickup_room_nights"], ly_avail),
        "pickup_room_nights_pct": change_pct(
            cur["pickup_room_nights"], ly["pickup_room_nights"]
        ),
        "pickup_bookings_pct": change_pct(cur["pickup_bookings"], ly["pickup_bookings"]),
        # One number for "faster or slower": >1 means this year picked up more
        # room-nights in the same countdown than last year did.
        "pace_index": (
            round(cur["pickup_room_nights"] / ly["pickup_room_nights"], 3)
            if ly["pickup_room_nights"] else None
        ),
    }


# ── main entry point ─────────────────────────────────────────────────────────

def get_fill_pace(
    db: Session,
    branch_id: Optional[UUID],
    year: int,
    month: int,
    days: int = 60,
    as_of: Optional[date] = None,
    sources: Optional[list[str]] = None,
    room_category: Optional[str] = None,
    compare_last_year: bool = True,
) -> dict:
    """Fill pace for one stay month, optionally narrowed to a set of sources.

    `days` is the booking window ending at `as_of` (default today). The year-ago
    comparison reads the same countdown to the same month one year back — never
    the same calendar dates — so both curves sit at the same distance from
    check-in at every point.

    `sources` is a set, not one name: "how fast is our own website filling
    December" and "how fast are the OTAs filling it" are both one selection.
    A value matches a booking's raw source, or its category ("Direct", "OTA",
    "Local travel agency") as a shorthand for every source under it.

    The selection narrows the headline and the curve only. The per-source table
    is always built from the full month, so it stays usable as the answer to
    "which source is pacing ahead" rather than collapsing to what is selected.
    """
    as_of = as_of or date.today()
    days = max(1, min(int(days), MAX_WINDOW_DAYS))
    window_from = as_of - timedelta(days=days - 1)
    as_of_dates = [window_from + timedelta(days=i) for i in range(days)]

    month_start, _, dim = month_bounds(year, month)
    # Distance from the month, which is what the two years are aligned on.
    days_out = [(month_start - d).days for d in as_of_dates]

    room_category = _normalise_room_category(room_category)
    inv_attr = _INVENTORY_ATTR.get(room_category, "total_rooms")

    branches = {
        str(b.id): {
            "name": b.name,
            "currency": b.currency,
            "total_rooms": b.total_rooms or 0,
            "units": getattr(b, inv_attr, None) or 0,
        }
        for b in db.query(Branch).filter_by(is_active=True).all()
    }
    if branch_id:
        branches = {k: v for k, v in branches.items() if k == str(branch_id)}

    units = sum(b["units"] for b in branches.values())
    available = units * dim

    wanted = set(sources) if sources else None

    rows = fetch_month_rows(db, branch_id, year, month, room_category)
    rows = [r for r in rows if str(r.branch_id) in branches]

    scoped = [r for r in rows if _matches_sources(r, wanted)]
    curve, summary = build_curve(scoped, as_of_dates)
    current = _decorate(summary, available)

    result: dict = {
        "stay_month": f"{year:04d}-{month:02d}",
        "days_in_month": dim,
        "as_of": as_of.isoformat(),
        "days": days,
        "window": {"from": window_from.isoformat(), "to": as_of.isoformat()},
        "days_out": {"from": days_out[0], "to": days_out[-1]},
        "sources": sorted(wanted) if wanted else [],
        "room_category": room_category,
        "scope": {
            "branch_id": str(branch_id) if branch_id else None,
            "branch_count": len(branches),
            "units_in_scope": units,
            "inventory_basis": inv_attr,
            "available_room_nights": available,
            "currency": _single_currency(branches),
        },
        "current": current,
    }

    ly_rows: list = []
    ly_available = 0.0
    if compare_last_year:
        ly_year, ly_month = year - 1, month
        ly_month_start, _, ly_dim = month_bounds(ly_year, ly_month)
        # Aligned by distance from the month, not by calendar date.
        ly_dates = [ly_month_start - timedelta(days=do) for do in days_out]
        ly_available = units * ly_dim

        ly_rows = fetch_month_rows(db, branch_id, ly_year, ly_month, room_category)
        ly_rows = [r for r in ly_rows if str(r.branch_id) in branches]
        ly_scoped = [r for r in ly_rows if _matches_sources(r, wanted)]
        ly_curve, ly_summary = build_curve(ly_scoped, ly_dates)
        last_year = _decorate(ly_summary, ly_available, include_final=True)

        result["last_year"] = {
            "stay_month": f"{ly_year:04d}-{ly_month:02d}",
            "days_in_month": ly_dim,
            "as_of": ly_dates[-1].isoformat(),
            "window": {"from": ly_dates[0].isoformat(), "to": ly_dates[-1].isoformat()},
            **last_year,
        }
        result["vs_last_year"] = _compare(summary, ly_summary, available, ly_available)

        # One row per point so the chart can plot both lines against days_out.
        ly_by_date = {p["date"]: p for p in ly_curve}
        merged = []
        for i, point in enumerate(curve):
            ly_point = ly_by_date.get(ly_dates[i].isoformat(), {})
            merged.append({
                "days_out": days_out[i],
                **point,
                "otb_occ_pct": pct(point["otb_room_nights"], available),
                "ly_date": ly_dates[i].isoformat(),
                "ly_otb_room_nights": ly_point.get("otb_room_nights", 0.0),
                "ly_otb_occ_pct": pct(ly_point.get("otb_room_nights", 0.0), ly_available),
                "ly_day_room_nights": ly_point.get("day_room_nights", 0.0),
            })
        result["curve"] = merged
    else:
        result["curve"] = [
            {
                "days_out": days_out[i],
                **point,
                "otb_occ_pct": pct(point["otb_room_nights"], available),
            }
            for i, point in enumerate(curve)
        ]

    result["by_source"] = _source_breakdown(
        rows, ly_rows, as_of_dates, days_out, year, month, units,
        available, ly_available, compare_last_year,
    )
    if not branch_id:
        result["branches"] = _branch_breakdown(
            rows, ly_rows, as_of_dates, days_out, year, month, branches,
            dim, wanted, compare_last_year,
        )
    return result


# ── breakdowns ───────────────────────────────────────────────────────────────

def _source_breakdown(
    rows, ly_rows, as_of_dates, days_out, year, month, units,
    available, ly_available, compare_last_year,
) -> list[dict]:
    """Per-source pace, so "which source is filling December" has an answer.

    One row per raw source — website, walk-in, Agoda, each on its own. The mix
    pages roll every direct source into a single "Direct" row because the
    question there is how much we booked ourselves; here the question is which
    individual channel to push, and a rolled-up row cannot answer it. The rows
    partition the month, so their room-nights sum back to the whole.

    Built from the unfiltered month either way — the selection above narrows the
    headline, not this table.
    """
    ly_dates = _ly_dates(year, month, days_out) if compare_last_year else []

    grouped: dict[str, list] = {}
    categories: dict[str, str] = {}
    for r in rows:
        key = _source_key(r)
        grouped.setdefault(key, []).append(r)
        categories.setdefault(key, r.source_category or "OTA")

    ly_grouped: dict[str, list] = {}
    for r in ly_rows:
        key = _source_key(r)
        ly_grouped.setdefault(key, []).append(r)
        categories.setdefault(key, r.source_category or "OTA")

    out = []
    for key in set(grouped) | set(ly_grouped):
        _, summary = build_curve(grouped.get(key, []), as_of_dates)
        summary = summary or _blank_summary()
        row = {
            "source": key,
            "category": categories.get(key, "OTA"),
            **_decorate(summary, available),
        }
        if compare_last_year:
            _, ly_summary = build_curve(ly_grouped.get(key, []), ly_dates)
            ly_summary = ly_summary or _blank_summary()
            row["last_year"] = _decorate(ly_summary, ly_available, include_final=True)
            row["vs_last_year"] = _compare(summary, ly_summary, available, ly_available)
        out.append(row)

    out.sort(key=lambda r: -r["otb_room_nights"])
    return out


def _branch_breakdown(
    rows, ly_rows, as_of_dates, days_out, year, month, branches,
    dim, wanted, compare_last_year,
) -> list[dict]:
    """Per-branch pace when the group is in scope.

    Room-nights are summed, never averaged across branches — a 12-room property
    and a 60-room one do not contribute equally to a group occupancy figure,
    and averaging their percentages would say they do.
    """
    ly_dates = _ly_dates(year, month, days_out) if compare_last_year else []
    ly_dim = calendar.monthrange(year - 1, month)[1]

    out = []
    for bid, info in branches.items():
        avail = info["units"] * dim
        ly_avail = info["units"] * ly_dim
        scoped = [r for r in rows if str(r.branch_id) == bid and _matches_sources(r, wanted)]
        _, summary = build_curve(scoped, as_of_dates)
        summary = summary or _blank_summary()
        row = {
            "branch_id": bid,
            "branch_name": info["name"],
            "currency": info["currency"],
            "units_in_scope": info["units"],
            **_decorate(summary, avail),
        }
        if compare_last_year:
            ly_scoped = [
                r for r in ly_rows
                if str(r.branch_id) == bid and _matches_sources(r, wanted)
            ]
            _, ly_summary = build_curve(ly_scoped, ly_dates)
            ly_summary = ly_summary or _blank_summary()
            row["last_year"] = _decorate(ly_summary, ly_avail, include_final=True)
            row["vs_last_year"] = _compare(summary, ly_summary, avail, ly_avail)
        out.append(row)

    out.sort(key=lambda r: -r["otb_room_nights"])
    return out


# ── internals ────────────────────────────────────────────────────────────────

def _ly_dates(year: int, month: int, days_out: list[int]) -> list[date]:
    ly_month_start = month_bounds(year - 1, month)[0]
    return [ly_month_start - timedelta(days=do) for do in days_out]


def _blank_summary() -> dict:
    return {
        "opening_room_nights": 0.0,
        "otb_room_nights": 0.0,
        "otb_bookings": 0,
        "otb_revenue_native": 0.0,
        "otb_revenue_vnd": 0.0,
        "pickup_room_nights": 0.0,
        "pickup_bookings": 0,
        "pickup_revenue_native": 0.0,
        "final_room_nights": 0.0,
        "final_bookings": 0,
        "undated_room_nights": 0.0,
        "undated_bookings": 0,
    }


def _source_key(row) -> str:
    """The raw source a booking came through, which is what the table keys on.
    Cloudbeds leaves it empty on a few rows; those collect under one label
    rather than disappearing from a table whose rows must sum to the month."""
    return row.source or "Unknown"


def _matches_sources(row, wanted: Optional[set]) -> bool:
    """Whether a booking falls inside the selected set of sources.

    An empty selection means every source. A value matches the raw source, or
    the category as a shorthand for everything under it — so "Direct" still
    selects website, walk-in, phone, email and the rest in one go, without the
    table needing an overlapping row for it.
    """
    if not wanted:
        return True
    return _source_key(row) in wanted or (row.source_category or "") in wanted


def _normalise_room_category(value: Optional[str]) -> Optional[str]:
    """Accept any casing, return exactly what ingestion stores ("Room"/"Dorm")."""
    if not value:
        return None
    return {"room": "Room", "dorm": "Dorm"}.get(str(value).strip().lower())


def _single_currency(branches: dict) -> Optional[str]:
    """The currency to label money with, or None when the scope mixes several —
    the group spans TWD, JPY and VND, and one symbol over the sum would lie.
    """
    currencies = {b["currency"] for b in branches.values() if b.get("currency")}
    return currencies.pop() if len(currencies) == 1 else None
