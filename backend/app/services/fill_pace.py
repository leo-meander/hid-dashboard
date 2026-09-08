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
# Each stay month costs two grouped queries (this year and last). A dozen keeps
# a whole year of months readable without turning one page load into fifty.
MAX_STAY_MONTHS = 12


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


# ── combining months ─────────────────────────────────────────────────────────
#
# Several stay months can be read at once — "how is Q4 filling" is one question,
# not three. Each month is still measured against its OWN countdown: on 8 Sep,
# October is 23 days out and December is 84, and each is compared with the point
# last year that sat the same distance from its own month. Only then are they
# added up. Lining all three against one calendar offset instead would compare
# December with a point last year that was seven weeks closer to check-in.
#
# The per-month rows come back alongside the total, because "Q4 is on pace"
# routinely hides one month carrying two others.

# Every field in a summary is an additive count, so months combine by summing.
_SUM_KEYS = (
    "opening_room_nights", "otb_room_nights", "otb_bookings",
    "otb_revenue_native", "otb_revenue_vnd",
    "pickup_room_nights", "pickup_bookings", "pickup_revenue_native",
    "final_room_nights", "final_bookings",
    "undated_room_nights", "undated_bookings",
)

_CURVE_SUM_KEYS = (
    "otb_room_nights", "otb_bookings", "otb_revenue_native",
    "day_room_nights", "day_bookings",
)


def _sum_summaries(summaries: list[dict]) -> dict:
    out = {k: 0.0 for k in _SUM_KEYS}
    for s in summaries:
        for k in _SUM_KEYS:
            out[k] += s.get(k, 0) or 0
    for k in ("otb_bookings", "pickup_bookings", "final_bookings", "undated_bookings"):
        out[k] = int(out[k])
    for k in set(_SUM_KEYS) - {"otb_bookings", "pickup_bookings",
                               "final_bookings", "undated_bookings"}:
        out[k] = round(out[k], 2)
    return out


def _sum_curves(curves: list[list[dict]]) -> list[dict]:
    """Add several months' curves point by point.

    Point i is the same booking date in every month's curve — the window is one
    stretch of calendar time whichever month is being filled — so the sum is
    "everything on the books for any of these months, as of that date".
    """
    if not curves:
        return []
    out = []
    for i in range(len(curves[0])):
        point = {"date": curves[0][i]["date"]}
        for k in _CURVE_SUM_KEYS:
            total = sum(c[i].get(k, 0) or 0 for c in curves)
            point[k] = int(total) if k.endswith("bookings") else round(total, 2)
        out.append(point)
    return out


def _group_summaries(rows, as_of_dates: list[date], key_of) -> dict[str, dict]:
    """Split rows by some key and summarise each group over the same window."""
    grouped: dict[str, list] = {}
    for r in rows:
        grouped.setdefault(key_of(r), []).append(r)
    return {k: build_curve(v, as_of_dates)[1] for k, v in grouped.items()}


def _accumulate(store: dict[str, list[dict]], summaries: dict[str, dict]) -> None:
    for key, summary in summaries.items():
        store.setdefault(key, []).append(summary)


# ── main entry point ─────────────────────────────────────────────────────────

def get_fill_pace(
    db: Session,
    branch_id: Optional[UUID],
    months: list[tuple[int, int]],
    days: int = 60,
    as_of: Optional[date] = None,
    sources: Optional[list[str]] = None,
    room_category: Optional[str] = None,
    compare_last_year: bool = True,
) -> dict:
    """Fill pace for one or more stay months, optionally narrowed to a set of
    sources.

    `days` is the booking window ending at `as_of` (default today). The year-ago
    comparison reads the same countdown to each month one year back — never the
    same calendar dates — so both curves sit at the same distance from check-in
    at every point, month by month.

    `months` is a list of (year, month). Several are read as one question — "how
    is Q4 filling" — with the total on top and a row per month underneath,
    because a healthy quarter routinely hides one month carrying the others.

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

    months = sorted(set(months))[:MAX_STAY_MONTHS] or [_next_month(as_of)]
    room_category = _normalise_room_category(room_category)
    inv_attr = _INVENTORY_ATTR.get(room_category, "total_rooms")
    wanted = set(sources) if sources else None

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

    # Per-month accumulators. Each month contributes its own countdown, and the
    # totals are summed only after each has been measured against it.
    stay_days = ly_stay_days = 0
    cur_summaries: list[dict] = []
    ly_summaries: list[dict] = []
    cur_curves: list[list[dict]] = []
    ly_curves: list[list[dict]] = []
    month_rows: list[dict] = []
    src_cur: dict[str, list[dict]] = {}
    src_ly: dict[str, list[dict]] = {}
    src_category: dict[str, str] = {}
    br_cur: dict[str, list[dict]] = {}
    br_ly: dict[str, list[dict]] = {}

    for (year, month) in months:
        month_start, _, dim = month_bounds(year, month)
        days_out = [(month_start - d).days for d in as_of_dates]
        avail = units * dim
        stay_days += dim

        rows = [r for r in fetch_month_rows(db, branch_id, year, month, room_category)
                if str(r.branch_id) in branches]
        scoped = [r for r in rows if _matches_sources(r, wanted)]
        curve, summary = build_curve(scoped, as_of_dates)

        cur_summaries.append(summary)
        cur_curves.append(curve)
        _accumulate(src_cur, _group_summaries(rows, as_of_dates, _source_key))
        _accumulate(br_cur, _group_summaries(scoped, as_of_dates, _branch_key))
        for r in rows:
            src_category.setdefault(_source_key(r), r.source_category or "OTA")

        month_row = {
            "stay_month": f"{year:04d}-{month:02d}",
            "days_in_month": dim,
            "days_out": {"from": days_out[0], "to": days_out[-1]},
            **_decorate(summary, avail),
        }

        if compare_last_year:
            ly_start, _, ly_dim = month_bounds(year - 1, month)
            # Aligned by distance from the month, not by calendar date.
            ly_dates = [ly_start - timedelta(days=do) for do in days_out]
            ly_avail = units * ly_dim
            ly_stay_days += ly_dim

            ly_all = [r for r in fetch_month_rows(db, branch_id, year - 1, month, room_category)
                      if str(r.branch_id) in branches]
            ly_scoped = [r for r in ly_all if _matches_sources(r, wanted)]
            ly_curve, ly_summary = build_curve(ly_scoped, ly_dates)

            ly_summaries.append(ly_summary)
            ly_curves.append(ly_curve)
            _accumulate(src_ly, _group_summaries(ly_all, ly_dates, _source_key))
            _accumulate(br_ly, _group_summaries(ly_scoped, ly_dates, _branch_key))
            for r in ly_all:
                src_category.setdefault(_source_key(r), r.source_category or "OTA")

            month_row["last_year"] = {
                "stay_month": f"{year - 1:04d}-{month:02d}",
                "as_of": ly_dates[-1].isoformat(),
                "window": {"from": ly_dates[0].isoformat(), "to": ly_dates[-1].isoformat()},
                **_decorate(ly_summary, ly_avail, include_final=True),
            }
            month_row["vs_last_year"] = _compare(summary, ly_summary, avail, ly_avail)

        month_rows.append(month_row)

    available = units * stay_days
    ly_available = units * ly_stay_days
    total = _sum_summaries(cur_summaries)
    curve = _sum_curves(cur_curves)
    single = months[0] if len(months) == 1 else None

    result: dict = {
        "stay_months": [f"{y:04d}-{m:02d}" for (y, m) in months],
        "stay_days": stay_days,
        "as_of": as_of.isoformat(),
        "days": days,
        "window": {"from": window_from.isoformat(), "to": as_of.isoformat()},
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
        "current": _decorate(total, available),
        "months": month_rows,
    }
    # A countdown only names a point in time when there is one month to count
    # down to. Across several it is omitted, and the curve is read by booking
    # date instead — which is the same date in every month's curve anyway.
    if single:
        result["days_out"] = month_rows[0]["days_out"]

    if compare_last_year:
        ly_total = _sum_summaries(ly_summaries)
        ly_curve = _sum_curves(ly_curves)
        result["last_year"] = {
            "stay_months": [f"{y - 1:04d}-{m:02d}" for (y, m) in months],
            "stay_days": ly_stay_days,
            **_decorate(ly_total, ly_available, include_final=True),
        }
        if single:
            result["last_year"]["as_of"] = month_rows[0]["last_year"]["as_of"]
            result["last_year"]["window"] = month_rows[0]["last_year"]["window"]
        result["vs_last_year"] = _compare(total, ly_total, available, ly_available)

        for i, point in enumerate(curve):
            point["otb_occ_pct"] = pct(point["otb_room_nights"], available)
            point["ly_otb_room_nights"] = ly_curve[i]["otb_room_nights"]
            point["ly_otb_occ_pct"] = pct(ly_curve[i]["otb_room_nights"], ly_available)
            point["ly_day_room_nights"] = ly_curve[i]["day_room_nights"]
            if single:
                point["days_out"] = month_rows[0]["days_out"]["from"] - i
                point["ly_date"] = ly_curves[0][i]["date"]
    else:
        for i, point in enumerate(curve):
            point["otb_occ_pct"] = pct(point["otb_room_nights"], available)
            if single:
                point["days_out"] = month_rows[0]["days_out"]["from"] - i

    result["curve"] = curve
    result["by_source"] = _assemble(
        src_cur, src_ly, available, ly_available, compare_last_year,
        name_key="source", extra=lambda k: {"category": src_category.get(k, "OTA")},
    )
    if not branch_id:
        result["branches"] = _assemble(
            br_cur, br_ly, available, ly_available, compare_last_year,
            name_key="branch_id",
            extra=lambda k: {
                "branch_name": branches[k]["name"],
                "currency": branches[k]["currency"],
                "units_in_scope": branches[k]["units"],
            },
            # Each branch is measured against its own inventory: a 12-room
            # property and a 60-room one are not comparable on room-nights, and
            # a group percentage must never be the mean of two branch ones.
            avail_of=lambda k: branches[k]["units"] * stay_days,
            ly_avail_of=lambda k: branches[k]["units"] * ly_stay_days,
        )
    return result


def _assemble(
    cur: dict[str, list[dict]],
    ly: dict[str, list[dict]],
    available: float,
    ly_available: float,
    compare_last_year: bool,
    name_key: str,
    extra,
    avail_of=None,
    ly_avail_of=None,
) -> list[dict]:
    """Turn per-month accumulations into one row per source or branch.

    Months are summed first and the rates computed once from the totals, never
    averaged across months — a 31-night month and a 28-night one do not carry
    equal weight in a quarter's occupancy.
    """
    out = []
    for key in set(cur) | set(ly):
        avail = avail_of(key) if avail_of else available
        ly_avail = ly_avail_of(key) if ly_avail_of else ly_available
        summary = _sum_summaries(cur.get(key, []))
        row = {name_key: key, **extra(key), **_decorate(summary, avail)}
        if compare_last_year:
            ly_summary = _sum_summaries(ly.get(key, []))
            row["last_year"] = _decorate(ly_summary, ly_avail, include_final=True)
            row["vs_last_year"] = _compare(summary, ly_summary, avail, ly_avail)
        out.append(row)

    out.sort(key=lambda r: -r["otb_room_nights"])
    return out


# ── internals ────────────────────────────────────────────────────────────────

def _next_month(today: date) -> tuple[int, int]:
    """The default stay month: the first whole one still ahead of us."""
    return (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)


def _source_key(row) -> str:
    """The raw source a booking came through, which is what the table keys on.
    Cloudbeds leaves it empty on a few rows; those collect under one label
    rather than disappearing from a table whose rows must sum to the month."""
    return row.source or "Unknown"


def _branch_key(row) -> str:
    return str(row.branch_id)


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
