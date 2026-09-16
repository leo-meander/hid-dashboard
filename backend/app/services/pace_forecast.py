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

Where the gate fails, NOTHING IS BORROWED FROM ANOTHER BRANCH. A property is
its own business: 1948 is a 69-room hostel, Oani a 92-room premium hotel, and
one's October says nothing reliable about the other's — they sell to different
people, at different prices, on different booking curves. An earlier version of
this did lend a curve across branches and it was wrong twice over, once in
principle and once in the numbers: Oani is 41% booked at sixteen days out where
1948 is 31%, so adding 1948's remaining run-up double-counted everything Oani
had already sold and put it at 95% for three straight months, against a branch
that has never cleared 73% in any month of its life.

What is left is the branch's own present. Oani has run 65-73% every month this
year, so its Q4 is projected at the occupancy it has actually been holding —
`own_run_rate`, carrying no seasonality, which the page says out loud because
Q4 is not August. A branch with no year-ago month AND no months of its own is
not projected at all: it drops out of the totals, and the card names it along
with the target that came out with it, so a partial projection is never read
against a whole quarter's target.

TWO WAYS OF COUNTING A SOLD NIGHT, AND THE ONE THE TARGET IS SET ON
──────────────────────────────────────────────────────────────────
`reservations` holds one row per booking. A booking that takes three dorm beds
for two nights is one row spanning two nights, and summing nights over those
rows counts it as TWO. daily_metrics — the table the KPI page, the targets and
every "actual" in this app are built on — counts the beds, and calls it six.

Measured on October 2026's book, daily_metrics over reservations, per branch:
1948 1.04, Oani 1.09, Osaka 1.09, Saigon 1.12, **Taipei 1.28** — Taipei being
108 dorm beds to 30 rooms. So a forecast built purely out of `reservations`
would be quoting occupancy a fifth low for Taipei, comparing it against a KPI
page reading a fifth higher, and pricing a fifth too few nights.

The booking CURVE only exists in `reservations` — daily_metrics has no booking
date, so it cannot say what was on the books sixty days out. The shape
therefore comes from there and is converted once: the pickup last year still
had to come is multiplied by that branch's own ratio of the two counts, taken
over the same stay months from the same bookings (`_bed_factors`). Everything
downstream — the occupancy, the capacity ceiling, the nights that get priced —
is then on the basis the target was set on.

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

# The two counts of a sold night, and how far apart they are allowed to read.
# The ratio is taken from the same bookings counted both ways, so it cannot
# sanely fall below 1 — one booking is at least one unit — and a branch whose
# every booking took two beds would sit near 2. Outside that it is measurement
# noise off a thin book, so it is clamped; and under this many reservation
# nights there is not enough book to take a ratio from at all, in which case
# the two counts are treated as the same.
MIN_FACTOR_BASE_ROOM_NIGHTS = 30
BED_FACTOR_MIN, BED_FACTOR_MAX = 1.0, 2.0

# A branch's ADR trend is measured over this many settled months. Three is
# enough to survive one odd month and short enough to still be this year.
ADR_TREND_MONTHS = 3
# ADR does move, but a 2x move measured off a thin month is measurement, not
# pricing. Anything outside this is clipped and flagged.
ADR_YOY_MIN, ADR_YOY_MAX = 0.6, 1.5


# ── nights ───────────────────────────────────────────────────────────────────

def _usable_base(cell: dict, factor: float = 1.0) -> bool:
    """Whether a branch-month can be forecast from its own year-ago month.

    The occupancy test converts first: `ly_final_nights` counts reservations
    and `capacity` counts units, so on a dorm-heavy branch the raw ratio reads
    a fifth low and a month that traded at 44% would be thrown out as a ramp.
    """
    if not cell["ly_final_nights"] or not cell["capacity"]:
        return False
    if cell["ly_otb_nights"] < MIN_LY_BASE_ROOM_NIGHTS:
        return False
    return cell["ly_final_nights"] * factor / cell["capacity"] >= MIN_LY_FINAL_OCC


def _bed_factors(cells: list[dict], occ: dict) -> dict:
    """How many sold units one reservation-night actually represents, per branch.

    Both sides are the same bookings for the same stay months, counted two
    ways: `reservations` rows on one side, daily_metrics on the other. Pooled
    across the months in scope rather than taken month by month, because a
    December read at six per cent sold has too little book to divide by.
    """
    res: dict[str, float] = {}
    bed: dict[str, float] = {}
    for c in cells:
        book = occ.get((c["branch_id"], c["year"], c["month"]))
        if not book or book.get("nights") is None:
            continue
        res[c["branch_id"]] = res.get(c["branch_id"], 0.0) + c["otb_nights"]
        bed[c["branch_id"]] = bed.get(c["branch_id"], 0.0) + book["nights"]
    return {
        bid: min(max(bed[bid] / nights, BED_FACTOR_MIN), BED_FACTOR_MAX)
        for bid, nights in res.items()
        if nights >= MIN_FACTOR_BASE_ROOM_NIGHTS and bed.get(bid)
    }


def _band_for(days_out: int) -> tuple[float, float]:
    """The measured error band, widened where the backtest did not reach."""
    if days_out > BAND_UNTESTED_DAYS:
        return BAND_LOW * BAND_WIDEN, BAND_HIGH * BAND_WIDEN
    return BAND_LOW, BAND_HIGH


def _own_level(cell: dict, occ: dict, branch_meta: dict,
               ref_months: list[tuple[int, int]]) -> Optional[float]:
    """The branch's own occupancy over the last settled months.

    Where a branch has no year-ago month, it still has a present, and its own
    present is the only honest thing to project it from. Oani ran 65-73% every
    month this year.

    Occupancy here is whole-house — daily_metrics counts every room and bed
    sold — so under a room-type filter this level is the house's, not the
    filtered inventory's. The months it reads are settled ones, so it is a
    realised number rather than a book still filling.
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


def _forecast_cell(cell: dict, occ: dict, branch_meta: dict,
                   ref_months: list[tuple[int, int]], factors: dict) -> dict:
    """One branch, one stay month: units sold at the end of it.

    Units, not reservations — see the module docstring. What is on the books
    comes from daily_metrics, which counts every bed; the pickup still to come
    comes from the reservations curve, which is the only place a booking date
    exists, and is converted with the branch's own ratio between the two.

    Three outcomes, in order, and nothing in any of them comes from another
    branch:

      actual         the month is already over — what is on the books IS the
                     month, and calling that a forecast would be a lie
      ly_pickup      it has a year-ago month worth reading: on the books now,
                     plus what last year still had to come from here
      own_run_rate   it does not, so it holds the occupancy it has actually
                     been running this year, with no seasonal adjustment at
                     all — Q4 is not August, and the card says so

    And where it has neither a year-ago month nor months of its own, no number:
    `no_base` drops out of every total rather than being filled in from
    somewhere it does not belong.
    """
    factor = factors.get(cell["branch_id"], 1.0)
    book = occ.get((cell["branch_id"], cell["year"], cell["month"])) or {}
    # The book in units. Where daily_metrics has nothing for the month, the
    # reservations count is converted instead rather than quietly changing
    # basis half way through the sum.
    otb = book["nights"] if book.get("nights") is not None else cell["otb_nights"] * factor
    capacity = cell["capacity"]
    ceiling = capacity * MAX_FORECAST_OCC
    extra = {"bed_factor": round(factor, 3), "otb_units": round(otb, 1)}

    if cell["status"] == "finished":
        return {**cell, **extra, "basis": "actual", "nights": otb, "low": otb,
                "high": otb, "capacity_capped": False}

    if _usable_base(cell, factor):
        basis = "ly_pickup"
        raw = otb + (cell["ly_final_nights"] - cell["ly_otb_nights"]) * factor
    else:
        level = _own_level(cell, occ, branch_meta, ref_months)
        if level is None:
            return {**cell, **extra, "basis": "no_base", "nights": None,
                    "low": None, "high": None, "capacity_capped": False}
        basis = "own_run_rate"
        extra["run_rate_occ_pct"] = round(level * 100, 2)
        # Never under what is already sold: a month can be ahead of the run
        # rate the moment it is read, and the book does not shrink.
        raw = max(otb, level * capacity)

    # The band, likewise floored at the book. A low end under it would be
    # describing cancellations this cannot see.
    low_mult, high_mult = _band_for(cell["days_out"])
    nights = min(raw, ceiling)
    return {
        **cell,
        **extra,
        "basis": basis,
        "nights": round(nights, 1),
        "low": round(max(otb, min(raw * (1 + low_mult), ceiling)), 1),
        "high": round(max(otb, min(raw * (1 + high_mult), ceiling)), 1),
        "capacity_capped": raw > ceiling,
    }


# ── keeping today's speed ────────────────────────────────────────────────────
#
# A second reading of the same month, and a deliberately literal one: measure
# how many room-nights a booking day is currently adding, and carry that number
# forward, flat, to the end of the stay month.
#
#     nights   = on the books + (room-nights per booking day × days left to sell)
#     revenue  = booked so far + those nights × the rate they are selling at now
#
# Both inputs come from the window the page is set to. Change it from 30 days to
# 90 and the speed, the rate and the answer all change with it — that is the
# point of the control.
#
# WHAT IT IS NOT: a prediction of where the month ends. Bookings do not arrive at
# a constant rate; they crowd towards check-in, and measured across settled
# months this group picks up 1.5x to 4.6x more in each window than in the one
# before. A month read 107 days out therefore projects far below anything it has
# ever finished at — December 2026 lands at 24% occupancy on today's speed
# against the 67% December 2025 actually finished at. That is not the model
# failing; it is the model answering the question it was asked, which is "if the
# next hundred days look exactly like the last thirty, where do we get to". Read
# it as the floor under a month, and as the size of the acceleration the target
# is asking for.

def _days_left(year: int, month: int, as_of: date) -> int:
    """Booking days from `as_of` to the last night of the stay month."""
    return max(0, (date(year, month, _days_in_month(year, month)) - as_of).days + 1)


def _run_rate(cell: dict, book: dict, as_of: date,
              adr: Optional[float], target: Optional[float],
              deduction_pct: float = 0.0, other_revenue: float = 0.0) -> dict:
    """The month in points of occupancy: what is sold, what each source of
    nights adds on top, and what the target asks for.

    Four numbers in one unit, so they can be read against each other, and not
    one of them is a prediction:

      on the books      sold already, as a share of the house
      at this speed     room-nights a day now × days left, in points
      last year         what the same stretch actually delivered a year ago
      needed            the occupancy the revenue target implies at today's rate

    Nights are carried in the unit the target is set in; money is not
    converted, because revenue per reservation-night multiplied back over
    reservation-nights cancels the factor out.

    Money also carries the branch's two standing adjustments, because the
    target was set against a figure that already has them:

        revenue = (booked + nights still to come × rate) × (1 − deduct%)
                  + other revenue

    They are not cosmetic. Osaka runs a 6% deduction, and 1948, Oani and
    Saigon each add a fixed monthly amount — Saigon's is 48m VND. Left off,
    Osaka's October reads 101% of target where the KPI page would call the
    same month short.

    `needed` is the one that repays reading first. It comes out above 100% for
    a month whose target cannot be reached on rooms at the rate the branch is
    currently selling at — a full house would still be short — and that is a
    pricing problem wearing a pace problem's clothes. When it does,
    `adr_needed` says what rate would clear the target at a full house.
    """
    window = cell.get("window_days") or 0
    pickup = cell.get("pickup_nights") or 0.0
    days_left = _days_left(cell["year"], cell["month"], as_of)
    if not window or cell["status"] == "finished":
        days_left = 0

    per_day = pickup / window if window else 0.0
    window_adr = (cell["pickup_revenue"] / pickup) if pickup else None
    factor = cell["bed_factor"]
    otb = cell["otb_units"]
    capacity = cell["capacity"]
    ceiling = capacity * MAX_FORECAST_OCC

    added = per_day * days_left * factor
    ly_added = max(0.0, cell["ly_final_nights"] - cell["ly_otb_nights"]) * factor

    booked_revenue = book.get("revenue")
    # ONE rate for the whole card. `needed` divides the money still owed to the
    # target by the rate the nights beside it are being multiplied by, which is
    # the window's own — the rate this reading is named after. Dividing by a
    # different one put "4.7 points short" directly above "101% of target" on
    # the same card, both from the same inputs, which is how it was found.
    # `adr` (last year's rate moved by the trend) is only the fallback for a
    # window with no pickup to take a rate from.
    rate = window_adr or adr
    mult = 1 - deduction_pct / 100
    # Undo the adjustments to find the raw revenue the target implies, then ask
    # how many nights that is. Dividing the target itself by the rate would ask
    # the wrong question on any branch that carries either.
    raw_target = ((target - other_revenue) / mult) if (target and mult) else None
    needed = None
    adr_needed = None
    if raw_target and rate and booked_revenue is not None:
        needed = otb + max(0.0, (raw_target - booked_revenue) / rate)
        if needed > ceiling and ceiling > otb:
            adr_needed = (raw_target - booked_revenue) / (ceiling - otb)

    def pts(nights):
        return round(nights / capacity * 100, 2) if capacity and nights is not None else None

    reach = min(otb + added, ceiling)
    # The ceiling in money: every remaining room sold, at the rate the branch
    # is currently getting. It is the line between "go faster" and "no amount
    # of filling fixes this" — a target above it cannot be reached on rooms.
    room_to_sell = max(0.0, ceiling - otb)
    # What the gap is worth as a change of speed, which is the form it can be
    # acted on in: "three more room-nights a day", not "4.7 points".
    extra = max(0.0, needed - reach) if needed is not None else None
    extra_per_day = (extra / days_left) if (extra is not None and days_left) else None
    priceable = booked_revenue is not None and window_adr and factor
    revenue = (
        (booked_revenue + (reach - otb) / factor * window_adr) * mult + other_revenue
        if priceable else None
    )
    revenue_max = (
        (booked_revenue + room_to_sell / factor * window_adr) * mult + other_revenue
        if priceable else None
    )
    # And the rate that would clear the target with every room sold. Above the
    # rate being taken now, the target needs a price rise, not a push.
    adr_for_target = (
        ((raw_target - booked_revenue) / (room_to_sell / factor))
        if (raw_target and booked_revenue is not None and room_to_sell and factor) else None
    )
    return {
        "days_left": days_left,
        "window_days": window,
        "room_nights_per_day": round(per_day, 2),
        "adr": round(window_adr, 2) if window_adr else None,
        # nights, for the roll-ups to sum
        "otb_room_nights": round(otb, 1),
        "room_nights_added": round(added, 1),
        "ly_room_nights_added": round(ly_added, 1),
        "needed_room_nights": round(needed, 1) if needed is not None else None,
        "needed_extra_room_nights": round(extra, 1) if extra is not None else None,
        "needed_extra_per_day": round(extra_per_day, 2) if extra_per_day is not None else None,
        "room_nights": round(reach, 1),
        # and the same four as points of the house
        "otb_occ_pct": pts(otb),
        "points_added": pts(added),
        "ly_points_added": pts(ly_added),
        "needed_occ_pct": pts(needed),
        "occ_pct": pts(reach),
        "needed_over_capacity": bool(needed is not None and needed > ceiling),
        "adr_needed": round(adr_needed, 2) if adr_needed else None,
        "revenue_native": round(revenue, 2) if revenue is not None else None,
        "revenue_max_native": round(revenue_max, 2) if revenue_max is not None else None,
        "room_nights_to_sell": round(room_to_sell, 1),
        "adr_for_target": round(adr_for_target, 2) if adr_for_target else None,
        "capacity_capped": otb + added > ceiling,
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


def _own_adr(adr_map: dict, branch_id: str, ref_months: list[tuple[int, int]]) -> Optional[float]:
    """What this branch has actually been charging, over settled months.

    The fallback for a branch with no year-ago month to take a rate from. It is
    pooled over settled months on purpose: the alternative — the rate on the
    thin forward book — is whatever the first few bookings for a holiday month
    happened to pay. Oani's December book reads 7,955 a night at ten per cent
    sold, against the 3,753 it has actually averaged all year.
    """
    revenue = nights = 0.0
    for (y, m) in ref_months:
        row = adr_map.get((branch_id, y, m))
        if not row or not row["nights"]:
            continue
        revenue += row["revenue"]
        nights += row["nights"]
    return revenue / nights if nights else None


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
    room_category: Optional[str] = None,
) -> dict:
    """Where the selected stay months land, and what that is against target.

    `cells` is one row per (branch, stay month) — the numbers Fill Pace has
    already measured, handed over rather than re-queried:

        branch_id, year, month, city, days_out, capacity, status,
        otb_nights, ly_otb_nights, ly_final_nights

    `scoped_sources` and `room_category` say whether the caller narrowed the
    selection, and if either did, NOTHING is returned. Not a partial answer —
    none.

    The reason is that only half of this can be narrowed. The booking curve
    comes from `reservations`, which carries a source and a room type and
    filters cleanly. Everything the curve is measured against does not:
    `daily_metrics` has no source column at all and is not split by room type,
    so the book, the money in it and the ADR would stay whole-house while the
    pickup beside them was a slice; and the KPI target is set for the branch,
    not for Agoda, and not for dorms. Every figure would be a slice divided by
    a whole, which is not a small error — it is a plausible-looking wrong
    number, which is worse. The page says the projection is unavailable for
    that filter instead.
    """
    if not cells:
        return {"available": False, "reason": "no_months"}
    if scoped_sources or room_category:
        return {
            "available": False,
            "reason": "filtered",
            "filtered_by": ([] if not scoped_sources else ["source"])
                           + ([] if not room_category else ["room_category"]),
        }

    months = sorted({(c["year"], c["month"]) for c in cells})
    branch_ids = sorted({c["branch_id"] for c in cells})
    adr_months = months + [(y - 1, m) for (y, m) in months]
    ref_months = _settled_months(as_of, ADR_TREND_MONTHS)
    # One pass over daily_metrics serves three readers: the ADR a night still
    # to come is priced at, the trend that moves it, and the branch's own
    # recent occupancy where there is no year-ago month to read.
    adr_map = _monthly_adr(db, branch_ids, adr_months + ref_months
                           + [(y - 1, m) for (y, m) in ref_months])
    targets = _targets(db, branch_ids, months)
    factors = _bed_factors(cells, adr_map)
    forecast_cells = [_forecast_cell(c, adr_map, branch_meta, ref_months, factors)
                      for c in cells]
    adr_trend = {b: _adr_yoy(adr_map, b, ref_months) for b in branch_ids}

    priced = []
    for c in forecast_cells:
        bid, y, m = c["branch_id"], c["year"], c["month"]
        trend = adr_trend.get(bid)
        clipped = None
        if trend is not None:
            clipped = min(max(trend, ADR_YOY_MIN), ADR_YOY_MAX)
        # A year-ago month rejected as a volume base is rejected as a rate
        # base too. Oani's October 2025 ran 37 room-nights; whatever those few
        # bookings paid is not October's rate, and taking it would have priced
        # 789 nights at 6,562 against the 3,500 the branch actually averages.
        # Whatever the nights came from, the money follows the same judgement.
        ly_adr = ((adr_map.get((bid, y - 1, m)) or {}).get("adr")
                  if c["basis"] in ("ly_pickup", "actual") else None)
        book = adr_map.get((bid, y, m)) or {}
        # Nights already sold are already priced — they are in the book at
        # whatever they were sold for. Only what is still to come needs an ADR,
        # and it is looked for in descending order of how much it knows about
        # this month: last year's rate for it moved by this year's trend, then
        # last year's rate flat, then what this branch has actually been
        # charging over settled months, and only failing all three the rate on
        # a forward book that may be ten per cent sold.
        adr_remaining = (
            (ly_adr * clipped if (ly_adr and clipped) else None)
            or ly_adr
            or _own_adr(adr_map, bid, ref_months)
            or book.get("adr")
        )

        # The year-ago estimator's money. Nothing on the page reads it any
        # more — the card and the year projection are both run-rate — but it
        # is still in the payload, so it carries the same two adjustments as
        # everything else rather than sitting there on a second basis.
        mult = 1 - branch_meta.get(bid, {}).get("deduction_pct", 0.0) / 100
        other = branch_meta.get(bid, {}).get("other_revenue_native", 0.0)
        revenue = low = high = None
        if c["nights"] is not None and adr_remaining and book.get("nights") is not None:
            booked_nights = c["otb_units"]
            booked_revenue = book["revenue"]
            priced_at = lambda n: (
                (booked_revenue + max(0.0, n - booked_nights) * adr_remaining) * mult + other
            )
            revenue = priced_at(c["nights"])
            low = priced_at(c["low"])
            high = priced_at(c["high"])

        target = targets.get((bid, y, m), {})
        priced.append({
            **c,
            "run_rate": _run_rate(
                c, book, as_of, adr_remaining, target.get("native"),
                deduction_pct=branch_meta.get(bid, {}).get("deduction_pct", 0.0),
                other_revenue=branch_meta.get(bid, {}).get("other_revenue_native", 0.0),
            ),
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
        "cells": [_cell_row(c, branch_meta) for c in priced],
    }


def _cell_row(c: dict, branch_meta: dict) -> dict:
    """One (branch, stay month) row, flat enough to hand to another service.

    The full-year projection is built from these — settled months as they
    happened, plus one of these for every month still to come — and rebuilding
    them there would mean running the whole reservations query a second time.
    """
    return {
        "branch_id": c["branch_id"],
        "branch_name": branch_meta.get(c["branch_id"], {}).get("name"),
        "currency": branch_meta.get(c["branch_id"], {}).get("currency"),
        "year": c["year"],
        "month": c["month"],
        "stay_month": f"{c['year']:04d}-{c['month']:02d}",
        "days_out": c["days_out"],
        "basis": c["basis"],
        "room_nights": c["nights"],
        "room_nights_low": c["low"],
        "room_nights_high": c["high"],
        "capacity_capped": c["capacity_capped"],
        "revenue_native": c["revenue_native"],
        "revenue_low_native": c["revenue_low_native"],
        "revenue_high_native": c["revenue_high_native"],
        "target_native": c["target_native"],
        "run_rate": c["run_rate"],
        # Every input the arithmetic used, so the page can show its working
        # rather than assert a number. A projection nobody can reconstruct is
        # a projection nobody should act on.
        "otb_room_nights": c["otb_units"],
        "otb_reservation_nights": c["otb_nights"],
        "bed_factor": c["bed_factor"],
        "ly_otb_room_nights": c["ly_otb_nights"],
        "ly_final_room_nights": c["ly_final_nights"],
        "available_room_nights": c["capacity"],
        "run_rate_occ_pct": c.get("run_rate_occ_pct"),
        "adr_remaining": c["adr_remaining"],
        "adr_yoy": c["adr_yoy"],
        "booked_revenue_native": c["booked_revenue_native"],
        "booked_room_nights": c["booked_nights_metrics"],
    }


# ── aggregation ──────────────────────────────────────────────────────────────
#
# Every roll-up below sums nights and money and divides once at the end. A
# quarter's occupancy is not the mean of three months' percentages, and the
# group's is not the mean of five branches'.

def _sum(cells: list[dict], key: str) -> Optional[float]:
    """Sum a field over the cells that have one. None when none of them do."""
    values = [c[key] for c in cells if c.get(key) is not None]
    return round(sum(values), 2) if values else None


def _vnd(cells: list[dict], key: str, branch_meta: dict) -> Optional[float]:
    """Native amounts converted and summed — the only way to add TWD to JPY."""
    total = None
    for c in cells:
        value = c.get(key)
        if value is None:
            continue
        currency = branch_meta.get(c["branch_id"], {}).get("currency") or "VND"
        rate = get_cached_rate(currency, "VND") or 1.0
        total = (total or 0.0) + value * rate
    return round(total, 2) if total is not None else None


def _named(cells: list[dict], branch_meta: dict, **extra) -> list[dict]:
    return [
        {"branch_id": c["branch_id"],
         "branch_name": branch_meta.get(c["branch_id"], {}).get("name"),
         "stay_month": f"{c['year']:04d}-{c['month']:02d}",
         **{k: c.get(v) for k, v in extra.items()}}
        for c in cells
    ]


def _block(cells: list[dict], branch_meta: dict, capacity_basis: bool) -> dict:
    """One set of branch-months, summed.

    Two exclusions run through every figure here, and both are reported rather
    than absorbed:

      · a branch-month with no basis to project from contributes nothing — not
        its nights, and not its inventory to the denominator either, or the
        occupancy would read low by exactly the share of the house that was
        left out.
      · the money figures are built ONLY from branch-months that could be
        priced, and each one's target comes out with it. A forecast covering
        four branches against a target covering five is not an achievement
        percentage, it is a smaller number wearing one.
    """
    counted = [c for c in cells if c["nights"] is not None]
    no_base = [c for c in cells if c["nights"] is None]
    priced = [c for c in counted if c["revenue_native"] is not None]
    unpriced = [c for c in counted if c["revenue_native"] is None]

    capacity = sum(c["capacity"] for c in counted)
    nights = _sum(counted, "nights")
    # Units throughout, so occupancy here is the occupancy the KPI page shows.
    otb = round(sum(c["otb_units"] for c in counted), 2)
    ly_final = round(sum(c["ly_final_nights"] * c["bed_factor"] for c in counted), 2)
    revenue_native = _sum(priced, "revenue_native")
    target_native = _sum(priced, "target_native")
    revenue_vnd = _vnd(priced, "revenue_native", branch_meta)
    target_vnd = _vnd(priced, "target_native", branch_meta)

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
        "room_nights_low": _sum(counted, "low"),
        "room_nights_high": _sum(counted, "high"),
        "otb_room_nights": otb,
        "available_room_nights": capacity if capacity_basis else None,
        "occ_pct": occ(nights),
        "occ_pct_low": occ(_sum(counted, "low")),
        "occ_pct_high": occ(_sum(counted, "high")),
        "ly_final_room_nights": ly_final,
        "ly_final_occ_pct": occ(ly_final),
        "occ_pts_vs_ly": (
            round((nights - ly_final) / capacity * 100, 2)
            if nights is not None and capacity and capacity_basis else None
        ),
        "revenue_native": revenue_native,
        "revenue_low_native": _sum(priced, "revenue_low_native"),
        "revenue_high_native": _sum(priced, "revenue_high_native"),
        "revenue_vnd": revenue_vnd,
        "target_native": target_native,
        "target_vnd": target_vnd,
        "achievement_pct": hit(revenue_native, target_native),
        "achievement_low_pct": hit(_sum(priced, "revenue_low_native"), target_native),
        "achievement_high_pct": hit(_sum(priced, "revenue_high_native"), target_native),
        "achievement_vnd_pct": hit(revenue_vnd, target_vnd),
        "achievement_low_vnd_pct": hit(
            _vnd(priced, "revenue_low_native", branch_meta), target_vnd),
        "achievement_high_vnd_pct": hit(
            _vnd(priced, "revenue_high_native", branch_meta), target_vnd),
        "basis": sorted({c["basis"] for c in cells}),
        "capacity_capped": any(c["capacity_capped"] for c in counted),
        "months_counted": len(counted),
        "months_in_scope": len(cells),
        # No basis to project from: neither a year-ago month nor months of
        # their own. Out of the nights, out of the inventory, out of the money.
        "unforecastable": _named(no_base, branch_meta),
        # Projected, but with no rate to price the nights still to come at, so
        # out of the money figures and out of the target they are read against.
        "unpriced": _named(unpriced, branch_meta),
        # Projected from their own recent occupancy, carrying no seasonality.
        "run_rate_months": _named(
            [c for c in counted if c["basis"] == "own_run_rate"],
            branch_meta, occ_pct="run_rate_occ_pct"),
    }


def _blended_adr(cells: list[dict], branch_meta: dict) -> Optional[float]:
    """One rate over a set of branch-months, or None where one cannot exist."""
    currencies = {branch_meta.get(c["branch_id"], {}).get("currency") for c in cells}
    if len(currencies) != 1:
        return None
    nights = sum(c["run_rate"]["room_nights_added"] for c in cells
                 if c["run_rate"]["adr"])
    if not nights:
        return None
    return round(sum(c["run_rate"]["room_nights_added"] * c["run_rate"]["adr"]
                     for c in cells if c["run_rate"]["adr"]) / nights, 2)


def _run_rate_block(cells: list[dict], branch_meta: dict, capacity_basis: bool) -> dict:
    """The run-rate reading, summed. Nights add; percentages are divided out of
    the totals once at the end, never averaged across branch-months."""
    counted = [c for c in cells if c["run_rate"]["room_nights"] is not None]
    priced = [c for c in counted if c["run_rate"]["revenue_native"] is not None]
    needed_rows = [c for c in counted if c["run_rate"]["needed_room_nights"] is not None]
    capacity = sum(c["capacity"] for c in counted)

    def nights(rows, key):
        return round(sum(r["run_rate"][key] for r in rows), 1) if rows else None

    def pts(value):
        if value is None or not capacity or not capacity_basis:
            return None
        return round(value / capacity * 100, 2)

    def money(rows, key, conv=False):
        if not rows:
            return None
        total = 0.0
        for c in rows:
            v = c["run_rate"].get(key, c.get(key))
            if v is None:
                continue
            rate = (get_cached_rate(branch_meta.get(c["branch_id"], {}).get("currency")
                                    or "VND", "VND") or 1.0) if conv else 1.0
            total += v * rate
        return round(total, 2)

    reach = nights(counted, "room_nights")
    needed = nights(needed_rows, "needed_room_nights")
    revenue = money(priced, "revenue_native")
    revenue_max = money(priced, "revenue_max_native")
    revenue_max_vnd = money(priced, "revenue_max_native", conv=True)
    target = money(priced, "target_native")
    revenue_vnd = money(priced, "revenue_native", conv=True)
    target_vnd = money(priced, "target_native", conv=True)
    over = [c for c in counted if c["run_rate"]["needed_over_capacity"]]
    return {
        "room_nights": reach,
        "room_nights_per_day": round(sum(c["run_rate"]["room_nights_per_day"]
                                         for c in counted), 2),
        # Summed across the selection: every one of these months is being sold
        # on the same booking days, so the shortfalls add.
        "needed_extra_per_day": round(sum(c["run_rate"]["needed_extra_per_day"] or 0
                                          for c in counted), 2),
        "otb_occ_pct": pts(nights(counted, "otb_room_nights")),
        "points_added": pts(nights(counted, "room_nights_added")),
        "ly_points_added": pts(nights(counted, "ly_room_nights_added")),
        "needed_occ_pct": pts(needed),
        "occ_pct": pts(reach),
        "revenue_native": revenue,
        "revenue_vnd": revenue_vnd,
        "target_native": target,
        "target_vnd": target_vnd,
        "achievement_pct": (round(revenue / target * 100, 1)
                            if revenue is not None and target else None),
        "achievement_vnd_pct": (round(revenue_vnd / target_vnd * 100, 1)
                                if revenue_vnd is not None and target_vnd else None),
        "revenue_max_native": revenue_max,
        "revenue_max_vnd": revenue_max_vnd,
        "achievement_max_pct": (round(revenue_max / target * 100, 1)
                                if revenue_max is not None and target else None),
        "achievement_max_vnd_pct": (round(revenue_max_vnd / target_vnd * 100, 1)
                                    if revenue_max_vnd is not None and target_vnd else None),
        "capacity_capped": any(c["run_rate"]["capacity_capped"] for c in counted),
        "window_days": counted[0]["run_rate"]["window_days"] if counted else None,
        # The rate the nights still to come are priced at, so the page can name
        # it rather than leaving the reader to trust a revenue figure whose
        # price nobody showed them. Weighted by the nights each cell adds, and
        # only where one currency can carry it.
        "adr": _blended_adr(counted, branch_meta),
        # Months whose target cannot be reached on rooms at today's rate. These
        # are not pace problems and must not be read as ones.
        "over_capacity": [
            {"branch_id": c["branch_id"],
             "branch_name": branch_meta.get(c["branch_id"], {}).get("name"),
             "stay_month": f"{c['year']:04d}-{c['month']:02d}",
             "currency": branch_meta.get(c["branch_id"], {}).get("currency"),
             "needed_occ_pct": c["run_rate"]["needed_occ_pct"],
             # The same rate `needed` was computed with, not the window's own —
             # quoting one and dividing by the other reads as a contradiction.
             "adr_now": c["adr_remaining"],
             "adr_needed": c["run_rate"]["adr_needed"]}
            for c in over
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
    block["run_rate"] = _run_rate_block(cells, branch_meta, capacity_basis)
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
            "run_rate": _run_rate_block(rows, branch_meta, capacity_basis),
            **_block(rows, branch_meta, capacity_basis),
        })
    out.sort(key=lambda r: -(r["room_nights"] or 0))
    return out
