"""Pace forecast — where a stay month lands if it keeps filling the way it is.

Fill Pace answers "are we ahead or behind". This answers the question that
always follows it: "so where does the month finish, and does that clear the
target". Same rows, same window, one more step.

THE METHOD, AND WHY IT IS THIS ONE
──────────────────────────────────
Three estimators can be built out of the three numbers Fill Pace already has —
what is on the books now, what was on the books at the same countdown last
year, and where last year actually finished:

    multiplicative   otb ÷ (ly_otb / ly_final)        "we are at 5% of final"
    additive         otb + (ly_final - ly_otb)        "this much is still to come"
    growth-scaled    otb + (ly_final - ly_otb) × g    g = otb / ly_otb

They were backtested over five settled stay months (Apr–Aug 2026), each read at
30, 60 and 90 days out, per branch, against what the month actually did:

    additive          MAPE  7.1%   bias  +0.3%
    growth × √g       MAPE 16.0%   bias  +5.8%
    multiplicative    MAPE 23.4%   bias  +8.4%   (one case +93%)

Additive wins, and it is not close. The reason is the denominator: at 60 days
out a month is 3-6% sold, so `ly_otb` is a handful of nights and every
estimator that divides by it multiplies that handful's noise into the answer.
Adding last year's remaining pickup asks nothing of it.

Two conditions come with that number, and dropping either one breaks it:

1. FORECAST PER BRANCH, THEN SUM. Never forecast the group as one pool. Group
   `ly_otb` includes branches that were not open a year ago, so the group's
   apparent year-over-year speed is mostly new inventory — measured at 1.2x to
   1.45x here, none of it pace.

2. GATE THE YEAR-AGO BASE. A branch-month is only forecastable from its own
   history when that history is real: at least MIN_LY_BASE_ROOM_NIGHTS on the
   books at the same countdown, and a year-ago month that actually traded
   (MIN_LY_FINAL_OCC). Taipei's October 2025 finished at 26% because it opened
   mid-month; Oani had not opened at all. Feeding either into `ly_final -
   ly_otb` forecasts the ramp, not the month.

Where the gate fails the month is not skipped. The branch keeps its own LEVEL
— the occupancy it has actually been running this year — and borrows only the
SHAPE from the branches that DID trade in the same city: how much busier the
market's October is than its summer. It is labelled `proxy` so the page can say
so. Where there is no sibling either, no number is published.

WHAT THE BAND MEANS
───────────────────
The same backtest gives the spread, not just the average: p10 −7%, p90 +14% on
a branch-month. That is the published band, widened past the range the backtest
covers (see BAND_*). It is an honest interval from measured error, not a
confidence interval from a model — there is no model.

MONEY IS PRICED FROM daily_metrics, NOT FROM THE RESERVATIONS ROWS
──────────────────────────────────────────────────────────────────
Forecast nights come from `reservations`. Forecast revenue does NOT reuse that
table's money. Summed per stay month, `reservations.grand_total_native` holds
~100% of the revenue daily_metrics reports for months still in the future and
~40% for months that have finished — the same decay in every branch in the same
month, which is a sync artefact rather than five simultaneous rate collapses.
Room-nights are unaffected (they come from the dates). So nights are priced at
the ADR daily_metrics reports for the same month a year earlier, moved by the
branch's own realised ADR trend (`adr_yoy`), and the month-to-date money also
comes from daily_metrics. That keeps the forecast on the same basis as the KPI
page it will be compared against.
"""
from __future__ import annotations

import calendar
import logging
from datetime import date
from typing import Optional

from sqlalchemy import extract, func
from sqlalchemy.orm import Session

from app.models.daily_metrics import DailyMetrics
from app.models.kpi import KPITarget
from app.services.currency import get_cached_rate

logger = logging.getLogger(__name__)

# A year-ago countdown position with fewer nights than this on the books is not
# a base, it is a rounding error — the same floor Fill Pace puts under its
# seasonal norm, for the same reason.
MIN_LY_BASE_ROOM_NIGHTS = 30

# And a year-ago month that finished below this never traded normally: it was
# opening, closed for works, or otherwise not the month it is being used to
# predict. Taipei finished October 2025 at 26.4% having opened mid-month;
# forecasting October 2026 from it would forecast the opening.
MIN_LY_FINAL_OCC = 0.40

# No forecast is allowed to sell more than this share of the house. The
# estimator has no idea inventory exists — it adds last year's remaining pickup
# to a book that may already be ahead — and without a ceiling it cheerfully
# returns 104% occupancy. When the cap binds, the month is reported as
# `capacity_capped` so nobody reads the ceiling as a prediction.
MAX_FORECAST_OCC = 0.95

# Measured p10 / p90 of the additive estimator's error over the backtest.
BAND_LOW = -0.07
BAND_HIGH = 0.14
# Past the range the backtest covers (30-90 days out) the band is widened
# rather than quietly extrapolated. December read in September is 107 days out.
BAND_UNTESTED_DAYS = 90
BAND_WIDEN = 1.6

# How far the borrowed seasonal shape is allowed to move a branch's own level.
#
# The index divides a donor's year-ago target month by its year-ago summer, so
# a donor whose summer was itself disrupted inflates it — Taipei ran 42-49%
# through the 2025 works it reopened from, which alone pushed the November
# index to 1.34 and would have put Oani, a property that has not cleared 73%
# in any month of its life, at 92%. A market's month-against-summer swing
# outside this range is a statement about the donor, not about the season, and
# the month is flagged `proxy_index_clipped` when it lands here.
PROXY_INDEX_MIN, PROXY_INDEX_MAX = 0.75, 1.25

# A branch's ADR trend is measured over this many settled months. Three is
# enough to survive one odd month and short enough to still be this year.
ADR_TREND_MONTHS = 3
# ADR does move, but a 2x move measured off a thin month is measurement, not
# pricing. Anything outside this is clipped and flagged.
ADR_YOY_MIN, ADR_YOY_MAX = 0.6, 1.5


# ── nights ───────────────────────────────────────────────────────────────────

def _usable_base(cell: dict) -> bool:
    """Whether a branch-month can be forecast from its own year-ago month."""
    if not cell["ly_final_nights"] or not cell["capacity"]:
        return False
    if cell["ly_otb_nights"] < MIN_LY_BASE_ROOM_NIGHTS:
        return False
    return cell["ly_final_nights"] / cell["capacity"] >= MIN_LY_FINAL_OCC


def _band_for(days_out: int) -> tuple[float, float]:
    """The measured error band, widened where the backtest did not reach."""
    if days_out > BAND_UNTESTED_DAYS:
        return BAND_LOW * BAND_WIDEN, BAND_HIGH * BAND_WIDEN
    return BAND_LOW, BAND_HIGH


def _donor_pool(cells: list[dict], cell: dict) -> tuple[list[dict], str]:
    """Branches whose year-ago months can stand in for one that has none.

    THE SAME STAY MONTH, always. A quarter is read three months at a time, and
    pooling across them borrows December's shape for October and hands back one
    seasonal index for all three — which is the tell that it has happened.

    Same city first — one market, one calendar, one set of holidays — and only
    if the city has nobody to ask, anywhere in scope. The two are NOT
    equivalent and do not share a label: Oani borrowing 1948's October is one
    market's seasonality lent to another property in it, while a Taipei shape
    lent to Osaka is a guess about Japan made from Taiwan, and comes back as
    `proxy_other_market` so the page can say which of the two it is looking at.
    """
    month = [c for c in cells
             if (c["year"], c["month"]) == (cell["year"], cell["month"])
             and _usable_base(c)]
    same_city = [c for c in month if c["city"] == cell["city"]]
    if same_city:
        return same_city, "proxy"
    if month:
        return month, "proxy_other_market"
    return [], "no_base"


def _seasonal_index(pool: list[dict], occ: dict, branch_meta: dict,
                    ref_months: list[tuple[int, int]]) -> Optional[float]:
    """How much busier the donors' target month is than their recent norm.

    A year ago, pooled across the donor branches: occupancy in the month being
    forecast, over occupancy in the same reference months this branch's own
    level was measured from. 1.06 means the market's October runs six per cent
    above its summer.

    Pooled on nights and capacity, never averaged across branches — a 138-unit
    property and a 69-unit one do not get one vote each.
    """
    month_sold = month_capacity = ref_sold = ref_capacity = 0.0
    for c in pool:
        rooms = branch_meta.get(c["branch_id"], {}).get("total_rooms") or 0
        if not rooms:
            continue
        month_sold += c["ly_final_nights"]
        month_capacity += rooms * _days_in_month(c["year"] - 1, c["month"])
        for (y, m) in ref_months:
            sold = (occ.get((c["branch_id"], y - 1, m)) or {}).get("nights")
            if sold is None:
                continue
            ref_sold += sold
            ref_capacity += rooms * _days_in_month(y - 1, m)
    if not month_capacity or not ref_capacity or not ref_sold:
        return None
    return (month_sold / month_capacity) / (ref_sold / ref_capacity)


def _own_level(cell: dict, occ: dict, branch_meta: dict,
               ref_months: list[tuple[int, int]]) -> Optional[float]:
    """The branch's own occupancy over the last settled months.

    Where a branch has no year-ago month, it still has a present. Oani ran
    65-73% every month this year; that is a far better starting point for its
    October than anything borrowed, and all the neighbours are needed for is
    the seasonal shape on top of it.
    """
    rooms = branch_meta.get(cell["branch_id"], {}).get("total_rooms") or 0
    if not rooms:
        return None
    sold = capacity = 0.0
    for (y, m) in ref_months:
        nights = (occ.get((cell["branch_id"], y, m)) or {}).get("nights")
        if nights is None:
            continue
        sold += nights
        capacity += rooms * _days_in_month(y, m)
    return sold / capacity if capacity else None


def _forecast_cell(cell: dict, cells: list[dict], occ: dict, branch_meta: dict,
                   ref_months: list[tuple[int, int]]) -> dict:
    """One branch, one stay month: nights at the end of it.

    A month already over is not forecast — what is on the books IS the month,
    and the honest label for that is `actual`.

    Without a year-ago month of its own the branch is not forecast by handing
    it a neighbour's numbers wholesale. It keeps its own LEVEL — the occupancy
    it has actually been running this year — and borrows only the SHAPE, how
    much busier the market's October is than its summer. Adding a neighbour's
    remaining pickup instead double-counts every property that books earlier
    than that neighbour does: Oani is 40% sold at sixteen days out where 1948
    is 31%, and adding 1948's whole remaining run-up on top of that sold Oani
    out at 95% three months running. Its own year has never once gone past 73%.
    """
    otb = cell["otb_nights"]
    capacity = cell["capacity"]
    ceiling = capacity * MAX_FORECAST_OCC
    proxy = {}

    if cell["status"] == "finished":
        return {**cell, "basis": "actual", "nights": otb, "low": otb, "high": otb,
                "capacity_capped": False, **proxy}

    if _usable_base(cell):
        basis = "ly_pickup"
        raw = otb + (cell["ly_final_nights"] - cell["ly_otb_nights"])
    else:
        pool, basis = _donor_pool(cells, cell)
        index = _seasonal_index(pool, occ, branch_meta, ref_months) if pool else None
        level = _own_level(cell, occ, branch_meta, ref_months)
        if not pool or index is None:
            return {**cell, "basis": "no_base", "nights": None, "low": None,
                    "high": None, "capacity_capped": False}
        if level is None:
            # A property with no trading history at all — opening inside the
            # forecast window. All that is left is to assume it performs like
            # the market it opened into, which is a guess and is labelled one.
            level = sum(c["ly_final_nights"] for c in pool) / sum(
                (branch_meta.get(c["branch_id"], {}).get("total_rooms") or 0)
                * _days_in_month(c["year"] - 1, c["month"]) for c in pool)
            basis = f"{basis}_level"
        clipped = min(max(index, PROXY_INDEX_MIN), PROXY_INDEX_MAX)
        proxy = {
            "proxy_donors": sorted({branch_meta.get(c["branch_id"], {}).get("name")
                                    for c in pool}),
            "proxy_level_occ_pct": round(level * 100, 2),
            "proxy_seasonal_index": round(clipped, 3),
            "proxy_index_clipped": clipped != index,
        }
        index = clipped
        # Never under what is already sold: the book does not shrink.
        raw = max(otb, level * index * capacity)

    # Never below what is already sold: the book does not shrink, and a band
    # that dips under it would be describing cancellations this cannot see.
    low_mult, high_mult = _band_for(cell["days_out"])
    nights = min(raw, ceiling)
    return {
        **cell,
        **proxy,
        "basis": basis,
        "nights": round(nights, 1),
        "low": round(max(otb, min(raw * (1 + low_mult), ceiling)), 1),
        "high": round(max(otb, min(raw * (1 + high_mult), ceiling)), 1),
        "capacity_capped": raw > ceiling,
    }


# ── money ────────────────────────────────────────────────────────────────────

def _days_in_month(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def _monthly_adr(db: Session, branch_ids: list, months: list[tuple[int, int]]) -> dict:
    """ADR per (branch, year, month) from daily_metrics — revenue ÷ nights sold.

    One query for every month asked for. Months with no rows simply do not
    appear; the caller treats a missing ADR as "cannot price this month",
    which is the truth rather than a zero.
    """
    if not branch_ids or not months:
        return {}
    first = date(min(m[0] for m in months), 1, 1)
    last_year = max(m[0] for m in months)
    last = date(last_year, 12, 31)
    rows = db.query(
        DailyMetrics.branch_id,
        extract("year", DailyMetrics.date).label("y"),
        extract("month", DailyMetrics.date).label("m"),
        func.coalesce(func.sum(DailyMetrics.revenue_native), 0).label("revenue"),
        func.coalesce(func.sum(DailyMetrics.total_sold), 0).label("sold"),
    ).filter(
        DailyMetrics.branch_id.in_(branch_ids),
        DailyMetrics.date >= first,
        DailyMetrics.date <= last,
    ).group_by(DailyMetrics.branch_id, "y", "m").all()

    out = {}
    for r in rows:
        key = (str(r.branch_id), int(r.y), int(r.m))
        out[key] = {
            "revenue": float(r.revenue or 0),
            "nights": int(r.sold or 0),
            "adr": (float(r.revenue) / int(r.sold)) if r.sold else None,
        }
    return out


def _adr_yoy(adr_map: dict, branch_id: str, ref_months: list[tuple[int, int]]) -> Optional[float]:
    """How this branch's realised ADR moved year over year, over settled months.

    Pooled rather than averaged month by month: revenue and nights are summed
    across the reference months on both sides and divided once, so a quiet
    month cannot carry the same weight as a busy one.
    """
    rev_now = nights_now = rev_ly = nights_ly = 0.0
    for (y, m) in ref_months:
        now = adr_map.get((branch_id, y, m))
        ly = adr_map.get((branch_id, y - 1, m))
        if not now or not ly or not now["nights"] or not ly["nights"]:
            continue
        rev_now += now["revenue"]; nights_now += now["nights"]
        rev_ly += ly["revenue"]; nights_ly += ly["nights"]
    if not nights_now or not nights_ly or not rev_ly:
        return None
    return (rev_now / nights_now) / (rev_ly / nights_ly)


def _targets(db: Session, branch_ids: list, months: list[tuple[int, int]]) -> dict:
    """Revenue targets per (branch, year, month), native and VND."""
    if not branch_ids or not months:
        return {}
    rows = db.query(KPITarget).filter(
        KPITarget.branch_id.in_(branch_ids),
        KPITarget.year.in_(sorted({y for y, _ in months})),
        KPITarget.month.in_(sorted({m for _, m in months})),
    ).all()
    return {
        (str(r.branch_id), r.year, r.month): {
            "native": float(r.target_revenue_native or 0),
            "vnd": float(r.target_revenue_vnd or 0),
        }
        for r in rows
        if (r.year, r.month) in months
    }


def _settled_months(as_of: date, count: int) -> list[tuple[int, int]]:
    """The last `count` calendar months that have finished before `as_of`."""
    out = []
    y, m = as_of.year, as_of.month
    for _ in range(count):
        m -= 1
        if m == 0:
            y, m = y - 1, 12
        out.append((y, m))
    return out


# ── entry point ──────────────────────────────────────────────────────────────

def build_forecast(
    db: Session,
    cells: list[dict],
    *,
    as_of: date,
    branch_meta: dict,
    scoped_sources: bool,
) -> dict:
    """Where the selected stay months land, and what that is against target.

    `cells` is one row per (branch, stay month) — the numbers Fill Pace has
    already measured, handed over rather than re-queried:

        branch_id, year, month, city, days_out, capacity, status,
        otb_nights, ly_otb_nights, ly_final_nights

    `scoped_sources` says whether the caller narrowed to a set of booking
    sources. It does, because a forecast for "Agoda only" cannot be capped at
    95% of the house — the rest of the house is being filled by everyone else.
    Under a source filter the nights forecast still runs; the capacity ceiling
    and the occupancy percentages do not.
    """
    if not cells:
        return {"available": False, "reason": "no_months"}

    months = sorted({(c["year"], c["month"]) for c in cells})
    branch_ids = sorted({c["branch_id"] for c in cells})
    adr_months = months + [(y - 1, m) for (y, m) in months]
    ref_months = _settled_months(as_of, ADR_TREND_MONTHS)
    # One pass over daily_metrics serves three readers: the ADR a night still
    # to come is priced at, the trend that moves it, and the occupancy levels
    # the proxy needs for a branch with no year-ago month.
    adr_map = _monthly_adr(db, branch_ids, adr_months + ref_months
                           + [(y - 1, m) for (y, m) in ref_months])
    targets = _targets(db, branch_ids, months)
    forecast_cells = [_forecast_cell(c, cells, adr_map, branch_meta, ref_months)
                      for c in cells]
    adr_trend = {b: _adr_yoy(adr_map, b, ref_months) for b in branch_ids}

    priced = []
    for c in forecast_cells:
        bid, y, m = c["branch_id"], c["year"], c["month"]
        trend = adr_trend.get(bid)
        clipped = None
        if trend is not None:
            clipped = min(max(trend, ADR_YOY_MIN), ADR_YOY_MAX)
        ly_adr = (adr_map.get((bid, y - 1, m)) or {}).get("adr")
        book = adr_map.get((bid, y, m)) or {}
        # Nights already sold are already priced — they are in the book at
        # whatever they were sold for. Only what is still to come needs an ADR.
        adr_remaining = ly_adr * clipped if (ly_adr and clipped) else (
            ly_adr or book.get("adr")
        )

        revenue = low = high = None
        if c["nights"] is not None and adr_remaining and book.get("nights") is not None:
            booked_nights = book["nights"]
            booked_revenue = book["revenue"]
            revenue = booked_revenue + max(0.0, c["nights"] - booked_nights) * adr_remaining
            low = booked_revenue + max(0.0, c["low"] - booked_nights) * adr_remaining
            high = booked_revenue + max(0.0, c["high"] - booked_nights) * adr_remaining

        target = targets.get((bid, y, m), {})
        priced.append({
            **c,
            "adr_remaining": round(adr_remaining, 2) if adr_remaining else None,
            "adr_yoy": round(trend, 3) if trend is not None else None,
            "adr_yoy_clipped": trend is not None and clipped != trend,
            "booked_revenue_native": round(book["revenue"], 2) if book else None,
            "booked_nights_metrics": book.get("nights"),
            "revenue_native": round(revenue, 2) if revenue is not None else None,
            "revenue_low_native": round(low, 2) if low is not None else None,
            "revenue_high_native": round(high, 2) if high is not None else None,
            "target_native": target.get("native"),
        })

    return {
        "available": True,
        "as_of": as_of.isoformat(),
        "capacity_basis": not scoped_sources,
        "method": "ly_pickup_additive",
        "band": {"low_pct": BAND_LOW * 100, "high_pct": BAND_HIGH * 100},
        "total": _roll_up(priced, branch_meta, capacity_basis=not scoped_sources),
        "months": _by_month(priced, branch_meta, capacity_basis=not scoped_sources),
        "branches": _by_branch(priced, branch_meta, capacity_basis=not scoped_sources),
    }


# ── aggregation ──────────────────────────────────────────────────────────────
#
# Every roll-up below sums nights and money and divides once at the end. A
# quarter's occupancy is not the mean of three months' percentages, and the
# group's is not the mean of five branches'.

def _sum(cells: list[dict], key: str) -> Optional[float]:
    """Sum a field, or None when any cell in the set could not be forecast.

    A partial total reads as a whole one, and a quarter missing Oani is not a
    quarter. Where a branch-month has no number the total says so instead.
    """
    values = [c.get(key) for c in cells]
    if any(v is None for v in values):
        return None
    return round(sum(values), 2)


def _vnd(cells: list[dict], key: str, branch_meta: dict) -> Optional[float]:
    """Native amounts converted and summed — the only way to add TWD to JPY."""
    total = 0.0
    for c in cells:
        value = c.get(key)
        if value is None:
            return None
        currency = branch_meta.get(c["branch_id"], {}).get("currency") or "VND"
        rate = get_cached_rate(currency, "VND") or 1.0
        total += value * rate
    return round(total, 2)


def _block(cells: list[dict], branch_meta: dict, capacity_basis: bool) -> dict:
    capacity = sum(c["capacity"] for c in cells)
    nights = _sum(cells, "nights")
    otb = round(sum(c["otb_nights"] for c in cells), 2)
    ly_final = round(sum(c["ly_final_nights"] for c in cells), 2)
    bases = {c["basis"] for c in cells}
    target_native = _sum(cells, "target_native")
    revenue_native = _sum(cells, "revenue_native")
    target_vnd = _vnd(cells, "target_native", branch_meta)
    revenue_vnd = _vnd(cells, "revenue_native", branch_meta)

    def occ(value):
        if value is None or not capacity or not capacity_basis:
            return None
        return round(value / capacity * 100, 2)

    def hit(value, target):
        if value is None or not target:
            return None
        return round(value / target * 100, 1)

    return {
        "room_nights": nights,
        "room_nights_low": _sum(cells, "low"),
        "room_nights_high": _sum(cells, "high"),
        "otb_room_nights": otb,
        "available_room_nights": capacity if capacity_basis else None,
        "occ_pct": occ(nights),
        "occ_pct_low": occ(_sum(cells, "low")),
        "occ_pct_high": occ(_sum(cells, "high")),
        "ly_final_room_nights": ly_final,
        "ly_final_occ_pct": occ(ly_final),
        "occ_pts_vs_ly": (
            round((nights - ly_final) / capacity * 100, 2)
            if nights is not None and capacity and capacity_basis else None
        ),
        "revenue_native": revenue_native,
        "revenue_low_native": _sum(cells, "revenue_low_native"),
        "revenue_high_native": _sum(cells, "revenue_high_native"),
        "revenue_vnd": revenue_vnd,
        "target_native": target_native,
        "target_vnd": target_vnd,
        "achievement_pct": hit(revenue_native, target_native),
        "achievement_low_pct": hit(_sum(cells, "revenue_low_native"), target_native),
        "achievement_high_pct": hit(_sum(cells, "revenue_high_native"), target_native),
        "achievement_vnd_pct": hit(revenue_vnd, target_vnd),
        "basis": sorted(bases),
        "capacity_capped": any(c["capacity_capped"] for c in cells),
        "unforecastable": [
            {"branch_id": c["branch_id"],
             "branch_name": branch_meta.get(c["branch_id"], {}).get("name"),
             "stay_month": f"{c['year']:04d}-{c['month']:02d}"}
            for c in cells if c["basis"] == "no_base"
        ],
        "proxy_months": [
            {"branch_id": c["branch_id"],
             "branch_name": branch_meta.get(c["branch_id"], {}).get("name"),
             "stay_month": f"{c['year']:04d}-{c['month']:02d}",
             "basis": c["basis"],
             "donors": c.get("proxy_donors"),
             "level_occ_pct": c.get("proxy_level_occ_pct"),
             "seasonal_index": c.get("proxy_seasonal_index"),
             "index_clipped": bool(c.get("proxy_index_clipped"))}
            for c in cells if c["basis"].startswith("proxy")
        ],
    }


def _roll_up(cells: list[dict], branch_meta: dict, capacity_basis: bool) -> dict:
    """Everything in scope, as one number per measure.

    The currency label is dropped the moment the scope spans more than one:
    the group runs TWD, JPY and VND, and a single symbol over the sum would
    pick one of them and be wrong about the other two. VND totals are still
    reported, because that is the currency the group plans in.
    """
    currencies = {branch_meta.get(c["branch_id"], {}).get("currency") for c in cells}
    currencies.discard(None)
    block = _block(cells, branch_meta, capacity_basis)
    block["currency"] = currencies.pop() if len(currencies) == 1 else None
    return block


def _by_month(cells: list[dict], branch_meta: dict, capacity_basis: bool) -> list[dict]:
    out = []
    for (y, m) in sorted({(c["year"], c["month"]) for c in cells}):
        rows = [c for c in cells if (c["year"], c["month"]) == (y, m)]
        out.append({
            "stay_month": f"{y:04d}-{m:02d}",
            "days_out": rows[0]["days_out"],
            **_roll_up(rows, branch_meta, capacity_basis),
        })
    return out


def _by_branch(cells: list[dict], branch_meta: dict, capacity_basis: bool) -> list[dict]:
    out = []
    for bid in sorted({c["branch_id"] for c in cells}):
        rows = [c for c in cells if c["branch_id"] == bid]
        meta = branch_meta.get(bid, {})
        out.append({
            "branch_id": bid,
            "branch_name": meta.get("name"),
            "currency": meta.get("currency"),
            "adr_yoy": rows[0].get("adr_yoy"),
            # Who this branch borrowed from, when it had to. The same donors
            # across every month, unlike the index they lend.
            "proxy_donors": next((r["proxy_donors"] for r in rows
                                  if r.get("proxy_donors")), None),
            **_block(rows, branch_meta, capacity_basis),
        })
    out.sort(key=lambda r: -(r["room_nights"] or 0))
    return out
