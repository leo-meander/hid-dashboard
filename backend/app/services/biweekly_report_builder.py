"""
Bi-Weekly Branch Manager Report — analytical payload.

Audience is a branch manager, not an analyst: the question is "how did my
branch do over this half of the month, versus the same dates last month and
the same dates last year, and what should I do next". That framing drives
three differences from the weekly report:

  - **Single branch at a time.** Every section is scoped to one branch.
  - **Two fixed comparisons, both on the same calendar dates**: the previous
    month (MoM) and the previous year (YoY). Never the immediately preceding
    period — 15–31 Aug against 1–14 Aug compares unequal windows with
    different weekend and pay-cycle shapes.
  - **Every number carries a traffic light and a plain-English "why".**

Reuses the weekly report's section code wherever the shape matches — those
functions accept an explicit `window`, so the same tested queries run over
whatever the period's date range is. Sections with a genuinely different
shape (markets by revenue, target achievement, recommendations) are built
here.

See `biweekly_period.py` for how a period is defined.
"""
from __future__ import annotations

import calendar
import logging
from datetime import date, timedelta
from typing import Optional

from sqlalchemy import extract, func
from sqlalchemy.orm import Session

from app.models.ads import AdsPerformance
from app.models.branch import Branch
from app.models.daily_metrics import DailyMetrics
from app.models.holiday_intel import HolidayCalendar
from app.models.kol import KOLRecord
from app.models.kpi import KPITarget
from app.models.reservation import Reservation
from app.models.reservation_daily import ReservationDaily
from app.services.biweekly_period import (
    Period,
    comparable_as_totals,
    mom_window,
    shift_month,
    window_days,
    yoy_window,
)
from app.services.kol_engine import HOTEL_TO_BRANCH_KEY
from app.services.kpi_engine import (
    _EXCLUDED_STATUSES,
    compute_period_achievement,
    month_actual_and_target,
)
from app.services.report_common import safe_section
from app.services.weekly_report_builder import (
    _crm_revenue_by_rate_plan,
    channel_mix,
    crm_section,
    kol_section,
    paid_ads_section,
    pct_change,
    range_metrics,
)

logger = logging.getLogger(__name__)


# ── Traffic-light thresholds ─────────────────────────────────────────────────
#
# One place to tune. `verdict()` returns "g" / "w" / "b", which the renderer
# maps to the green / amber / red border and dot on each KPI card.

REVENUE_GOOD_PCT = 5.0        # ≥ +5% vs comparison → green
REVENUE_BAD_PCT = -5.0        # < −5% → red
OCC_GOOD_PTS = 3.0            # OCC moves in percentage POINTS, not percent
OCC_BAD_PTS = -3.0
ROAS_GOOD = 4.0               # ≥ 4× → green
ROAS_BAD = 2.0                # < 2× → red, near or below break-even
TARGET_GOOD_PCT = 100.0       # hit target
TARGET_BAD_PCT = 80.0
HIGHLIGHT_PCT = 10.0          # |Δ| worth calling out in Highlights/Watch-outs
MARKET_BOOM_PCT = 100.0       # market growth that earns a Highlight …
MARKET_MIN_BOOKINGS = 5       # … but only above this volume, else it's noise
UNKNOWN_MARKET_WARN_SHARE = 10.0   # % of revenue with no source market

# Branch tab order — matches the Weekly Report. The repo carries a second,
# conflicting order in team_kpi_service.BRANCH_KEYS; this is the one the
# reports use, and operators read the two side by side.
_BRANCH_DISPLAY_ORDER = ("taipei", "1948", "oani", "osaka", "saigon")


def _branch_display_sort_key(b: Branch):
    name = (b.name or "").lower()
    for i, token in enumerate(_BRANCH_DISPLAY_ORDER):
        if token in name:
            return (i, name)
    return (len(_BRANCH_DISPLAY_ORDER), name)


def verdict(delta: Optional[float], good: float, bad: float) -> str:
    """Map a delta onto a traffic light. Unknown data is amber, never green."""
    if delta is None:
        return "w"
    if delta >= good:
        return "g"
    if delta < bad:
        return "b"
    return "w"


# ── Comparison helpers ───────────────────────────────────────────────────────
#
# Every section carries the same two comparisons, both on the same calendar
# dates as the period itself: one month back (MoM) and one year back (YoY).
#
# NEITHER window is guaranteed to be the same length as the period. A second
# half runs 15–EOM, so 15–31 Mar (17 days) meets 15–28 Feb (14) one month
# back, and a leap February shifts the year-ago window by a day. Comparing
# those totals head-on would invent an ~18% decline out of the calendar, so
# EVERY comparison of a TOTAL — MoM and YoY alike — goes through `_pct_norm`,
# which falls back to per-day averages when the lengths differ.
#
# Rate metrics (ADR, OCC, RevPAR, ROAS, engagement rate) are already
# normalised by their own denominator and are compared directly either way.


def _pct_norm(cur, cmp_, cur_days: int, cmp_days: int) -> Optional[float]:
    """Percent change between two totals, per-day when the windows differ."""
    if cur is None or cmp_ is None:
        return None
    if cur_days != cmp_days:
        if cur_days <= 0 or cmp_days <= 0:
            return None
        return pct_change(cur / cur_days, cmp_ / cmp_days)
    return pct_change(cur, cmp_)


def _yoy_days(p: Period) -> tuple[tuple[date, date], int, bool]:
    """The year-ago window, its length, and whether it needs per-day framing."""
    yoy = yoy_window(p)
    days = window_days(yoy)
    return yoy, days, days != p.days


def _mom_days(p: Period) -> tuple[tuple[date, date], int, bool]:
    """The same-dates-last-month window, its length, and whether it needs
    per-day framing. Same shape as `_yoy_days` so the two read alike at every
    call site.
    """
    mom = mom_window(p)
    days = window_days(mom)
    return mom, days, days != p.days


def _stay_overlaps(d_from: date, d_to: date):
    """SQL predicate for "this reservation has at least one night inside the
    window" — the occupancy basis, read straight off `reservations`.

    This is the same population `reservation_daily` gives via
    COUNT(DISTINCT reservation_id) over its per-night rows, without needing
    that table. `populate_reservation_daily` writes one row per date in
    `[check_in_date, check_out_date)` — the loop is `while current <
    check_out_date` — so a night lands in the window exactly when
    `check_in_date <= d_to` and `check_out_date > d_from`.

    Why this matters: `reservation_daily` has no rows before 2026, which is
    why the year-over-year columns in Markets and Channel Mix were blank. A
    booking COUNT does not need the one thing that table uniquely holds
    (Cloudbeds' actual per-night rate — see `nightly_rate` in
    cloudbeds.populate_reservation_daily), so counts can be compared against
    last year today, from data already on disk. Revenue still cannot: a
    year-ago figure derived from `grand_total / nights` would be a different
    measurement wearing the same label.

    The third clause is not redundant. A zero-night booking — check_out equal
    to check_in, which the data does carry — writes NO reservation_daily rows
    (that loop never runs, and `populate_reservation_daily` skips `nights <= 0`
    outright), yet satisfies both range comparisons whenever it sits inside the
    window. Without it, such a booking is counted here and not there, and the
    year-over-year percentage inherits the difference.
    """
    return (
        Reservation.check_in_date <= d_to,
        Reservation.check_out_date > d_from,
        Reservation.check_out_date > Reservation.check_in_date,
    )


def _stay_bookings_by_country(db: Session, branch_id, d_from: date,
                              d_to: date) -> dict:
    """Bookings per source market with a night inside the window, from
    `reservations`. Keyed by `guest_country_code` to match `markets_block`.
    """
    rows = (
        db.query(
            Reservation.guest_country_code,
            func.count(func.distinct(Reservation.id)),
        )
        .filter(
            Reservation.branch_id == branch_id,
            *_stay_overlaps(d_from, d_to),
            ~func.lower(func.coalesce(Reservation.status, "")).in_(
                list(_EXCLUDED_STATUSES)
            ),
        )
        .group_by(Reservation.guest_country_code)
        .all()
    )
    return {(r[0] or "??"): int(r[1] or 0) for r in rows}


def _stay_bookings_by_source(db: Session, branch_id, d_from: date,
                             d_to: date) -> dict:
    """Same count, grouped by booking source — for `channel_bookings_block`."""
    rows = (
        db.query(
            Reservation.source,
            func.count(func.distinct(Reservation.id)),
        )
        .filter(
            Reservation.branch_id == branch_id,
            *_stay_overlaps(d_from, d_to),
            ~func.lower(func.coalesce(Reservation.status, "")).in_(
                list(_EXCLUDED_STATUSES)
            ),
        )
        .group_by(Reservation.source)
        .all()
    )
    out: dict = {}
    for src, n in rows:
        name = (src or "Unknown").strip() or "Unknown"
        out[name] = out.get(name, 0) + int(n or 0)
    return out


# ── 1. KPI block ─────────────────────────────────────────────────────────────


def _delta_block(
    cur: dict, cmp_: dict, cur_days: int, cmp_days: int
) -> dict:
    """Compare two `range_metrics` results.

    Totals (revenue, room-nights) are only compared as totals when both
    windows are the same length. When they differ — a 17-day 15–31 Mar
    against a 14-day 15–28 Feb, or a leap February a year back — they are
    compared on a per-day basis instead, and `per_day` is set so the renderer
    can label the chip "/day". Rate metrics (ADR, OCC, RevPAR) are already
    normalised and are compared directly either way.
    """
    per_day = cur_days != cmp_days

    def _tot(key):
        a, b = cur.get(key), cmp_.get(key)
        if a is None or b is None:
            return None
        if per_day:
            if cur_days <= 0 or cmp_days <= 0:
                return None
            return pct_change(a / cur_days, b / cmp_days)
        return pct_change(a, b)

    occ_cur, occ_cmp = cur.get("occ_pct"), cmp_.get("occ_pct")
    occ_pts = (
        round((occ_cur - occ_cmp) * 100, 1)
        if (occ_cur is not None and occ_cmp is not None) else None
    )

    return {
        "per_day": per_day,
        "revenue_pct": _tot("revenue"),
        "sold_pct": _tot("sold"),
        "adr_pct": pct_change(cur.get("adr"), cmp_.get("adr")),
        "revpar_pct": pct_change(cur.get("revpar"), cmp_.get("revpar")),
        "occ_pts": occ_pts,
        "revenue_abs": (
            round(cur["revenue"] - cmp_["revenue"], 2)
            if (cur.get("revenue") is not None and cmp_.get("revenue") is not None
                and not per_day)
            else None
        ),
    }


def kpi_block(db: Session, branch: Branch, p: Period) -> dict:
    """Headline metrics for the period, vs the same dates last year and the
    same dates last month.
    """
    total_rooms = branch.total_rooms or 0
    yoy = yoy_window(p)
    mom = mom_window(p)

    this = range_metrics(db, branch.id, total_rooms, p.start, p.end)
    prior = range_metrics(db, branch.id, total_rooms, mom[0], mom[1])
    last_year = range_metrics(db, branch.id, total_rooms, yoy[0], yoy[1])

    d_yoy = _delta_block(this, last_year, p.days, window_days(yoy))
    d_prior = _delta_block(this, prior, p.days, window_days(mom))

    return {
        "this": this,
        "prior": prior,
        "yoy": last_year,
        "vs_yoy": d_yoy,
        "vs_prior": d_prior,
        "yoy_window": [yoy[0].isoformat(), yoy[1].isoformat()],
        "prior_window": [mom[0].isoformat(), mom[1].isoformat()],
        "yoy_comparable": comparable_as_totals(p, yoy),
        "mom_comparable": comparable_as_totals(p, mom),
        "yoy_has_data": (last_year.get("sold") or 0) > 0,
        "lights": {
            "revenue": verdict(d_yoy["revenue_pct"], REVENUE_GOOD_PCT, REVENUE_BAD_PCT),
            "adr": verdict(d_yoy["adr_pct"], REVENUE_GOOD_PCT, REVENUE_BAD_PCT),
            "occ": verdict(d_yoy["occ_pts"], OCC_GOOD_PTS, OCC_BAD_PTS),
            "revpar": verdict(d_yoy["revpar_pct"], REVENUE_GOOD_PCT, REVENUE_BAD_PCT),
        },
    }


# ── 1b. Room type split ──────────────────────────────────────────────────────


def _segment_totals(db: Session, branch_id, d_from: date, d_to: date) -> dict:
    """Revenue and units sold per room-type segment over a window.

    Reads the Cloudbeds Insights columns on `daily_metrics` — `rooms_sold` /
    `dorms_sold` and the revenue split beside them. NOT `room_occ_pct` /
    `dorm_occ_pct`: no production sync writes those any more (they are only
    touched by `recompute_occ_and_bookings`, which the `/api/sync/insights`
    pipeline never calls), and they count dorm ROOMS against a bed capacity.
    """
    if d_to < d_from:
        return {"room_rev": 0.0, "dorm_rev": 0.0, "room_nights": 0, "dorm_nights": 0}

    row = db.query(
        func.coalesce(func.sum(DailyMetrics.room_revenue_native), 0),
        func.coalesce(func.sum(DailyMetrics.dorm_revenue_native), 0),
        func.coalesce(func.sum(DailyMetrics.rooms_sold), 0),
        func.coalesce(func.sum(DailyMetrics.dorms_sold), 0),
    ).filter(
        DailyMetrics.branch_id == branch_id,
        DailyMetrics.date >= d_from,
        DailyMetrics.date <= d_to,
    ).one()

    return {
        "room_rev": float(row[0]), "dorm_rev": float(row[1]),
        "room_nights": int(row[2]), "dorm_nights": int(row[3]),
    }


def _segment_rates(totals: dict, capacity: int, days: int,
                   rev_key: str, nights_key: str) -> dict:
    """ADR / OCC / RevPAR for one segment against its own inventory."""
    rev, nights = totals[rev_key], totals[nights_key]
    denom = capacity * days
    return {
        "revenue": round(rev, 2),
        "nights": nights,
        "occ_pct": round(nights / denom, 4) if denom > 0 else None,
        "adr": round(rev / nights, 2) if nights > 0 else None,
        "revpar": round(rev / denom, 2) if denom > 0 else None,
    }


def room_type_block(db: Session, branch: Branch, p: Period) -> dict:
    """ADR / OCC / RevPAR broken out by private room vs dorm bed.

    Each segment divides by its OWN inventory: a private room is one unit,
    a dorm bed is one unit. `branches.total_rooms` mixes the two, so the
    blended RevPAR on the cards above is a capacity-weighted average of these
    two figures — never their sum, and never something either segment can be
    ranked against. The renderer says so in the panel footer.

    Rooms-only properties (Osaka) return `has_split=False`: the split would
    only restate the blended cards.
    """
    room_cap = branch.total_room_count or 0
    dorm_cap = branch.total_dorm_count or 0
    if room_cap <= 0 or dorm_cap <= 0:
        return {"has_split": False, "segments": []}

    yoy, yoy_days, _ = _yoy_days(p)
    cur = _segment_totals(db, branch.id, p.start, p.end)
    ago = _segment_totals(db, branch.id, yoy[0], yoy[1])

    segments = []
    for key, label, unit, cap, rev_key, nights_key in (
        ("room", "Private room", "rooms", room_cap, "room_rev", "room_nights"),
        ("dorm", "Dorm bed", "beds", dorm_cap, "dorm_rev", "dorm_nights"),
    ):
        now = _segment_rates(cur, cap, p.days, rev_key, nights_key)
        then = _segment_rates(ago, cap, yoy_days, rev_key, nights_key)
        occ_pts = (
            round((now["occ_pct"] - then["occ_pct"]) * 100, 1)
            if (now["occ_pct"] is not None and then["occ_pct"] is not None)
            else None
        )
        segments.append({
            "key": key, "label": label, "unit": unit, "capacity": cap,
            **now,
            "adr_vs_yoy_pct": pct_change(now["adr"], then["adr"]),
            "occ_vs_yoy_pts": occ_pts,
            "revpar_vs_yoy_pct": pct_change(now["revpar"], then["revpar"]),
        })

    return {
        "has_split": True,
        "days": p.days,
        "yoy_has_data": (ago["room_nights"] + ago["dorm_nights"]) > 0,
        "segments": segments,
    }


# ── 2. Target achievement ────────────────────────────────────────────────────


def _month_achievement(db: Session, branch: Branch, year: int, month: int,
                       as_of: date) -> dict:
    """Standalone achievement for one calendar month.

    Uses `kpi_engine.month_actual_and_target` — the exact math the KPI
    Targets grid shows — rather than the period-window proration this module
    uses elsewhere. That proration caps the actual-revenue sum at "today",
    which discards `daily_metrics` rows for nights later in the month that
    are already on the books (Cloudbeds reports revenue as reservations are
    confirmed, not backfilled day by day). Capping it there made this report
    show a fraction of what the KPI grid showed for the exact same month —
    e.g. Taipei August read 85% here against 70% on the grid, for numbers
    that should have been identical. This also picks up a manual accounting
    override when one is set, which the old proration never read at all.
    """
    month_end = date(year, month, calendar.monthrange(year, month)[1])

    target_row = (
        db.query(KPITarget)
        .filter_by(branch_id=branch.id, year=year, month=month)
        .first()
    )
    target_native = float(target_row.target_revenue_native or 0) if target_row else 0.0
    override_native = (
        float(target_row.actual_revenue_override)
        if target_row and target_row.actual_revenue_override is not None
        else None
    )
    cloudbeds_actual = db.query(
        func.coalesce(func.sum(DailyMetrics.revenue_native), 0)
    ).filter(
        DailyMetrics.branch_id == branch.id,
        extract("year", DailyMetrics.date) == year,
        extract("month", DailyMetrics.date) == month,
    ).scalar() or 0

    result = month_actual_and_target(branch, target_native, override_native,
                                     float(cloudbeds_actual))

    month_start = date(year, month, 1)
    if as_of >= month_end:
        status = "closed"
    elif as_of >= month_start:
        status = "in_progress"
    else:
        status = "upcoming"

    return {
        "year": year, "month": month,
        "label": month_start.strftime("%B %Y"),
        "achievement": {
            "actual_revenue": result["actual_revenue"],
            "target_revenue": result["target_revenue"],
        },
        "pct": result["achievement_pct"],
        "closed": status == "closed",
        "status": status,
        # An upcoming month has no history at all, so its "actual" is purely
        # nights already on the books for dates that have not happened yet —
        # a pickup figure, not performance. The renderer must not colour it
        # like one, and `verdict` is never applied to it here.
        "has_target": target_native > 0,
        "is_override": result["is_override"],
    }


#: How many months past the reporting month the target gauges look ahead.
#: One. Two was tried and cut (2026-08-17): the second month out reads as
#: noise — its pickup is so thin that the gauge says nothing a manager can
#: act on this fortnight, and it pushed the month that IS actionable into a
#: row of three.
TARGET_LOOKAHEAD_MONTHS = 1


def target_block(db: Session, branch: Branch, p: Period) -> dict:
    """Prorated target for the period, its own calendar month, and the months
    ahead of it.

    Managers are held to a monthly number, so the period figure alone is not
    enough. Half-month periods never straddle a month boundary, so the month
    the period sits in is always the first gauge.

    The look-ahead months are the point of this block for planning: an
    upcoming month's "actual" is revenue already on the books for nights that
    have not happened yet, so a gauge reading 20% in mid-August against
    October is not a failure — it is the pickup so far, and the gap is what
    there is still time to sell. That is the difference the report has to
    make visible, which is why `status` travels with every month and
    `verdict` is only ever applied to the period itself.

    A future month with no target set and nothing on the books is dropped
    rather than drawn as an empty 0/0 gauge.
    """
    period_ach = compute_period_achievement(db, branch, p.start, p.end)
    period_pct = period_ach.get("achievement_pct")

    months = [_month_achievement(db, branch, p.year, p.month, as_of=p.end)]
    for offset in range(1, TARGET_LOOKAHEAD_MONTHS + 1):
        year, month = shift_month(p.year, p.month, offset)
        ahead = _month_achievement(db, branch, year, month, as_of=p.end)
        if not ahead["has_target"] and not (ahead["achievement"]["actual_revenue"] or 0):
            continue
        months.append(ahead)

    # Mirror the REPORTING month at the top level too, so anything reading
    # this payload from before `months` existed keeps working unchanged. It
    # is deliberately not the last entry any more — that is now a forecast
    # month, and every legacy consumer of `month_pct` means "how did the
    # month this report covers do".
    current = months[0]

    return {
        "period": period_ach,
        "period_pct": round(period_pct * 100, 1) if period_pct is not None else None,
        "months": months,
        "month": current["achievement"],
        "month_pct": current["pct"],
        "month_label": current["label"],
        "month_closed": current["closed"],
        "light": verdict(
            round(period_pct * 100, 1) if period_pct is not None else None,
            TARGET_GOOD_PCT, TARGET_BAD_PCT,
        ),
    }


# ── 3. Markets (source countries by revenue) ─────────────────────────────────


def markets_block(db: Session, branch: Branch, p: Period, limit: int = 8) -> dict:
    """Source markets ranked by revenue in the period.

    Counted on an **occupancy basis** from `reservation_daily` — one row per
    reservation per night — so this reconciles with Channel Mix and with the
    OCC/ADR engine. A check-in cohort would credit the whole of a 6-night stay
    to whichever period the guest arrived in.

    `Unknown` (no guest country recorded) is kept as its own row rather than
    dropped: its size is itself a reportable data-quality problem, and hiding
    it would make the market shares silently wrong.
    """
    def _by_country(d_from: date, d_to: date) -> dict:
        rows = (
            db.query(
                Reservation.guest_country_code,
                func.min(Reservation.guest_country),
                func.count(func.distinct(ReservationDaily.reservation_id)),
                func.count(ReservationDaily.id),
                func.coalesce(func.sum(ReservationDaily.nightly_rate), 0),
            )
            .join(Reservation, ReservationDaily.reservation_id == Reservation.id)
            .filter(
                ReservationDaily.branch_id == branch.id,
                ReservationDaily.date >= d_from,
                ReservationDaily.date <= d_to,
                ~func.lower(func.coalesce(Reservation.status, "")).in_(
                    list(_EXCLUDED_STATUSES)
                ),
            )
            .group_by(Reservation.guest_country_code)
            .all()
        )
        return {
            (r[0] or "??"): {
                "country_code": r[0],
                "country": (r[1] or "Unknown"),
                "bookings": int(r[2] or 0),
                "nights": int(r[3] or 0),
                "revenue": float(r[4] or 0),
            }
            for r in rows
        }

    mom, mom_days, mom_per_day = _mom_days(p)
    yoy, yoy_days, yoy_per_day = _yoy_days(p)
    cur = _by_country(p.start, p.end)
    prior = _by_country(mom[0], mom[1])
    last_year = _by_country(yoy[0], yoy[1])

    # Booking COUNTS for the year-over-year comparison come from
    # `reservations`, not `reservation_daily` — see `_stay_overlaps`. Both
    # sides of the percentage are counted the same way, deliberately: taking
    # "this period" from reservation_daily and "last year" from reservations
    # would turn that table's known staleness (nothing refreshes it on a
    # schedule) into a fake year-on-year decline. `bookings` as DISPLAYED stays
    # on reservation_daily so it keeps agreeing with the revenue beside it; the
    # two counts are identical whenever that table is complete, which is the
    # normal case and the case the data notes flag when it is not.
    cur_stay = _stay_bookings_by_country(db, branch.id, p.start, p.end)
    yoy_stay = _stay_bookings_by_country(db, branch.id, yoy[0], yoy[1])

    total_rev = sum(v["revenue"] for v in cur.values())
    unknown_rev = sum(
        v["revenue"] for k, v in cur.items()
        if k == "??" or "unknown" in (v["country"] or "").lower()
    )
    unknown_bookings = sum(
        v["bookings"] for k, v in cur.items()
        if k == "??" or "unknown" in (v["country"] or "").lower()
    )

    rows = []
    for code, v in cur.items():
        is_unknown = code == "??" or "unknown" in (v["country"] or "").lower()
        prior_rev = prior.get(code, {}).get("revenue", 0.0)
        prior_bookings = prior.get(code, {}).get("bookings", 0)
        yoy_rev = last_year.get(code, {}).get("revenue", 0.0)
        yoy_bookings = last_year.get(code, {}).get("bookings", 0)
        rows.append({
            **v,
            "is_unknown": is_unknown,
            "share_pct": round(v["revenue"] / total_rev * 100, 1) if total_rev else None,
            "prior_revenue": prior_rev,
            "vs_prior_pct": _pct_norm(v["revenue"], prior_rev, p.days, mom_days),
            "prior_bookings": prior_bookings,
            "bookings_vs_prior_pct": _pct_norm(
                v["bookings"], prior_bookings, p.days, mom_days),
            "yoy_revenue": yoy_rev,
            # Stays None until `reservation_daily` holds year-ago nights: the
            # only year-ago revenue derivable from `reservations` alone is
            # grand_total/nights, a different measurement under the same label.
            "vs_yoy_pct": _pct_norm(v["revenue"], yoy_rev, p.days, yoy_days),
            "yoy_bookings": yoy_stay.get(code, 0),
            "bookings_vs_yoy_pct": _pct_norm(
                cur_stay.get(code, 0), yoy_stay.get(code, 0), p.days, yoy_days),
            # Kept for auditing: the reservation_daily count this row displays
            # vs the reservations count the percentage above is built from.
            "yoy_bookings_rd": yoy_bookings,
            "bookings_stay_basis": cur_stay.get(code, 0),
        })

    rows.sort(key=lambda r: -r["revenue"])
    known = [r for r in rows if not r["is_unknown"]]

    return {
        "rows": known[:limit],
        "total_revenue": round(total_rev, 2),
        "yoy_total_revenue": round(sum(v["revenue"] for v in last_year.values()), 2),
        "yoy_per_day": yoy_per_day,
        "mom_per_day": mom_per_day,
        "unknown_revenue": round(unknown_rev, 2),
        "unknown_bookings": unknown_bookings,
        "unknown_share_pct": (
            round(unknown_rev / total_rev * 100, 1) if total_rev else None
        ),
        "market_count": len(known),
    }


# ── 3b. Channel mix by booking count ─────────────────────────────────────────


def channel_bookings_block(db: Session, branch: Branch, p: Period,
                           limit: int = 7) -> dict:
    """Bookings per source channel in the period.

    Deliberately NOT a change to `weekly_report_builder.channel_mix`, which
    counts room-nights and is shared with the weekly report — editing it
    would silently move the weekly numbers too.

    A booking is counted once per period if any of its nights fall inside it
    (`COUNT(DISTINCT reservation_id)` over `reservation_daily`), the same
    basis as revenue and the markets table. A check-in cohort would put a
    stay that started the day before the period entirely outside it.
    """
    def _by_source(d_from: date, d_to: date) -> dict:
        rows = (
            db.query(
                Reservation.source,
                Reservation.source_category,
                func.count(func.distinct(ReservationDaily.reservation_id)),
            )
            .join(Reservation, ReservationDaily.reservation_id == Reservation.id)
            .filter(
                ReservationDaily.branch_id == branch.id,
                ReservationDaily.date >= d_from,
                ReservationDaily.date <= d_to,
                ~func.lower(func.coalesce(Reservation.status, "")).in_(
                    list(_EXCLUDED_STATUSES)
                ),
            )
            .group_by(Reservation.source, Reservation.source_category)
            .all()
        )
        out = {}
        for src, cat, n in rows:
            name = (src or "Unknown").strip() or "Unknown"
            entry = out.setdefault(
                name, {"source": name, "category": cat or "", "bookings": 0}
            )
            entry["bookings"] += int(n or 0)
        return out

    mom, mom_days, mom_per_day = _mom_days(p)
    yoy, yoy_days, yoy_per_day = _yoy_days(p)
    cur = _by_source(p.start, p.end)
    prior = _by_source(mom[0], mom[1])
    last_year = _by_source(yoy[0], yoy[1])

    # Year-over-year counts read `reservations` for both sides — same reasoning
    # as `markets_block`; `reservation_daily` holds no year-ago nights, and a
    # booking count does not need the per-night rates that table uniquely has.
    cur_stay = _stay_bookings_by_source(db, branch.id, p.start, p.end)
    yoy_stay = _stay_bookings_by_source(db, branch.id, yoy[0], yoy[1])

    total = sum(v["bookings"] for v in cur.values())
    rows = []
    for name, v in cur.items():
        prior_n = prior.get(name, {}).get("bookings", 0)
        rows.append({
            **v,
            "share_pct": round(v["bookings"] / total * 100, 1) if total else None,
            "prior_bookings": prior_n,
            "vs_prior_pct": _pct_norm(v["bookings"], prior_n, p.days, mom_days),
            "yoy_bookings": yoy_stay.get(name, 0),
            "vs_yoy_pct": _pct_norm(cur_stay.get(name, 0), yoy_stay.get(name, 0),
                                    p.days, yoy_days),
            "yoy_bookings_rd": last_year.get(name, {}).get("bookings", 0),
            "bookings_stay_basis": cur_stay.get(name, 0),
            "is_direct": (v["category"] or "").strip().lower() == "direct",
        })
    rows.sort(key=lambda r: -r["bookings"])

    direct_bookings = sum(r["bookings"] for r in rows if r["is_direct"])
    yoy_total = sum(yoy_stay.values())
    return {
        "rows": rows[:limit],
        "total_bookings": total,
        "yoy_total_bookings": yoy_total,
        "total_vs_yoy_pct": _pct_norm(sum(cur_stay.values()), yoy_total,
                                      p.days, yoy_days),
        "yoy_per_day": yoy_per_day,
        "mom_per_day": mom_per_day,
        "direct_bookings": direct_bookings,
        "direct_share_pct": (
            round(direct_bookings / total * 100, 1) if total else None
        ),
    }


# ── 3c. KOL reach / engagement (external) ────────────────────────────────────


def kol_reach_block(branch: Branch, p: Period) -> dict:
    """Reach + engagement for the period, from the KOL Engine, plus the
    MoM and YoY deltas every other metric in this report carries.

    HiD's `kol_records` has no reach/engagement columns, so this is the only
    source. It degrades to `available: False` on any failure — see
    `kol_engine.fetch_kol_insights` for why that is not reported as zero.

    The month-back call looks like a second network round trip but isn't:
    `fetch_kol_insights` caches the org's full record set for
    `_KOL_INSIGHTS_TTL_SEC` and filters in memory, so this reuses whatever
    the first call (this period, or another branch in the same report run)
    already fetched.
    """
    from app.config import settings
    from app.services.kol_engine import fetch_kol_insights, resolve_hotel_id_from_branch_name

    hotel_id = resolve_hotel_id_from_branch_name(branch.name or "")
    branch_key = HOTEL_TO_BRANCH_KEY.get(hotel_id) if hotel_id else None
    if not branch_key:
        return {"available": False, "posts": 0, "reach": 0, "engagements": 0,
                "engagement_rate_pct": None, "reason": "branch_not_mapped"}

    kwargs = dict(
        base_url=settings.KOL_ENGINE_URL,
        org_id=settings.KOL_ENGINE_ORG_ID,
        api_key=settings.KOL_SYNC_API_KEY,
        branch_key=branch_key,
    )
    this = fetch_kol_insights(**kwargs, date_from=p.start, date_to=p.end)
    if not this.get("available"):
        return this

    mom, mom_days, mom_per_day = _mom_days(p)
    prior = fetch_kol_insights(**kwargs, date_from=mom[0], date_to=mom[1])
    prior_reach = prior.get("reach") if prior.get("available") else None
    prior_engagements = prior.get("engagements") if prior.get("available") else None

    # Same-period-last-year, from the same in-memory cache as the two calls
    # above — a branch that was not posting a year ago simply reports 0, and
    # `_pct_norm` returns None off a zero base rather than a fake +100%.
    yoy, yoy_days, yoy_per_day = _yoy_days(p)
    last_year = fetch_kol_insights(**kwargs, date_from=yoy[0], date_to=yoy[1])
    yoy_reach = last_year.get("reach") if last_year.get("available") else None
    yoy_engagements = last_year.get("engagements") if last_year.get("available") else None

    return {
        **this,
        "reach_vs_prior_pct": _pct_norm(this.get("reach"), prior_reach,
                                        p.days, mom_days),
        "engagements_vs_prior_pct": _pct_norm(this.get("engagements"),
                                              prior_engagements, p.days, mom_days),
        "reach_vs_yoy_pct": _pct_norm(this.get("reach"), yoy_reach, p.days, yoy_days),
        "engagements_vs_yoy_pct": _pct_norm(this.get("engagements"), yoy_engagements,
                                            p.days, yoy_days),
        "yoy_per_day": yoy_per_day,
        "mom_per_day": mom_per_day,
    }


# ── 3d. Cost / ROAS for the CRM and KOL channels ─────────────────────────────
#
# `paid_ads_section` already carries cost + ROAS per channel from daily-grain
# `ads_performance` rows. CRM and KOL have no equivalent daily spend feed, so
# each gets its own exact-dated-where-possible cost query rather than reusing
# a shared helper — see the docstrings below for what each one actually reads.


def _kol_period_cost_roas(db: Session, branch: Branch, p: Period, kol: dict) -> dict:
    """Cost + ROAS for the KOL channel, dated to the exact period window.

    `weekly_report_builder.kol_section`'s `cost_mtd_native` is calendar
    month-to-date — right for a Monday digest, wrong for a half-month window
    that starts on the 15th. This sums `kol_records.cost_native` for KOLs
    invited inside `[p.start, p.end]` instead, the exact-dated counterpart
    already used for organic bookings/revenue.

    Also computes the same dates one month back, so the renderer can show a
    MoM arrow next to Cost, Revenue and ROAS, the same as every other channel
    in this report.
    """
    mom, mom_days, mom_per_day = _mom_days(p)
    prev_start, prev_end = mom

    def _cost(d_from: date, d_to: date) -> float:
        total = db.query(func.coalesce(func.sum(KOLRecord.cost_native), 0)).filter(
            KOLRecord.branch_id == branch.id,
            KOLRecord.invitation_date >= d_from,
            KOLRecord.invitation_date <= d_to,
        ).scalar()
        return float(total or 0)

    def _organic(d_from: date, d_to: date) -> tuple[int, float]:
        row = db.query(
            func.count(Reservation.id),
            func.coalesce(func.sum(Reservation.grand_total_native), 0),
        ).filter(
            Reservation.branch_id == branch.id,
            Reservation.room_type.ilike("%KOL_%"),
            Reservation.check_in_date >= d_from,
            Reservation.check_in_date <= d_to,
            ~func.lower(func.coalesce(Reservation.status, "")).in_(list(_EXCLUDED_STATUSES)),
        ).one()
        return int(row[0] or 0), float(row[1] or 0)

    def _posts(d_from: date, d_to: date) -> int:
        return db.query(func.count(KOLRecord.id)).filter(
            KOLRecord.branch_id == branch.id,
            KOLRecord.published_date >= d_from,
            KOLRecord.published_date <= d_to,
        ).scalar() or 0

    yoy, yoy_days, yoy_per_day = _yoy_days(p)

    cost = _cost(p.start, p.end)
    prior_cost = _cost(prev_start, prev_end)
    prior_bookings, prior_revenue = _organic(prev_start, prev_end)
    prior_posts = _posts(prev_start, prev_end)
    yoy_cost = _cost(yoy[0], yoy[1])
    yoy_bookings, yoy_revenue = _organic(yoy[0], yoy[1])
    yoy_posts = _posts(yoy[0], yoy[1])

    revenue = float(kol.get("organic_revenue_native") or 0)
    bookings = kol.get("organic_bookings")
    roas = round(revenue / cost, 2) if cost > 0 else None
    prior_roas = round(prior_revenue / prior_cost, 2) if prior_cost > 0 else None
    yoy_roas = round(yoy_revenue / yoy_cost, 2) if yoy_cost > 0 else None

    return {
        "period_cost_native": round(cost, 2),
        "period_roas": roas,
        "prior_cost_native": round(prior_cost, 2),
        "prior_bookings": prior_bookings,
        "prior_revenue_native": round(prior_revenue, 2),
        "prior_roas": prior_roas,
        "prior_posts": prior_posts,
        # ROAS is a ratio and compares directly; cost, revenue, bookings and
        # posts are totals, so they go through `_pct_norm` for the case where
        # the month-back window is a different length (15–31 vs 15–28).
        "cost_vs_prior_pct": _pct_norm(cost, prior_cost, p.days, mom_days),
        "revenue_vs_prior_pct": _pct_norm(revenue, prior_revenue, p.days, mom_days),
        "roas_vs_prior_pct": pct_change(roas, prior_roas),
        "bookings_vs_prior_pct": _pct_norm(bookings, prior_bookings, p.days, mom_days),
        "posts_vs_prior_pct": _pct_norm(kol.get("posts_this_week"), prior_posts,
                                        p.days, mom_days),
        "mom_per_day": mom_per_day,
        # Same dates last year, on the same rules.
        "yoy_cost_native": round(yoy_cost, 2),
        "yoy_revenue_native": round(yoy_revenue, 2),
        "yoy_bookings": yoy_bookings,
        "yoy_posts": yoy_posts,
        "yoy_roas": yoy_roas,
        "cost_vs_yoy_pct": _pct_norm(cost, yoy_cost, p.days, yoy_days),
        "revenue_vs_yoy_pct": _pct_norm(revenue, yoy_revenue, p.days, yoy_days),
        "roas_vs_yoy_pct": pct_change(roas, yoy_roas),
        "bookings_vs_yoy_pct": _pct_norm(bookings, yoy_bookings, p.days, yoy_days),
        "posts_vs_yoy_pct": _pct_norm(kol.get("posts_this_week"), yoy_posts,
                                      p.days, yoy_days),
        "yoy_per_day": yoy_per_day,
    }


def _crm_period_cost_roas(db: Session, branch: Branch, p: Period, crm: dict) -> dict:
    """Cost + ROAS for the CRM channel, prorated from Budget Planner's
    monthly manual-actual figure.

    CRM has no upstream spend feed at all (see `marketing_budget.ActualsCache`
    — CRM cost is a number an operator types into Budget Planner once a
    month), so the window gets that month's figure weighted by the days of
    the month it actually covers. Half-month periods never straddle a month
    boundary, so this is now always a single month's proration — the loop is
    kept because the comparison windows are computed the same way and the
    arithmetic costs nothing.
    """
    from app.routers.marketing_budget import ActualsCache, _get_rate_to_vnd, _vnd_to_native

    mom, mom_days, mom_per_day = _mom_days(p)
    prev_start, prev_end = mom
    currency = branch.currency or "VND"
    rate = _get_rate_to_vnd(currency)
    cache = ActualsCache(db)

    def _cost(d_from: date, d_to: date) -> float:
        total = 0.0
        cur = d_from
        while cur <= d_to:
            month_end = date(cur.year, cur.month, calendar.monthrange(cur.year, cur.month)[1])
            span_end = min(d_to, month_end)
            days_in_window = (span_end - cur).days + 1
            days_in_month = month_end.day
            monthly_native = _vnd_to_native(
                cache.get(branch, cur.year, cur.month, "crm"), currency, rate,
            )
            total += monthly_native * (days_in_window / days_in_month)
            cur = month_end + timedelta(days=1)
        return round(total, 2)

    cost = _cost(p.start, p.end)
    prior_cost = _cost(prev_start, prev_end)

    this_rev = (crm.get("crm_revenue_this") or {})
    prev_rev = (crm.get("crm_revenue_prev") or {})
    revenue = float(this_rev.get("revenue") or 0)
    prior_revenue = float(prev_rev.get("revenue") or 0)
    bookings = this_rev.get("bookings")
    prior_bookings = prev_rev.get("bookings")

    roas = round(revenue / cost, 2) if cost > 0 else None
    prior_roas = round(prior_revenue / prior_cost, 2) if prior_cost > 0 else None

    return {
        "period_cost_native": cost,
        "period_roas": roas,
        "prior_cost_native": prior_cost,
        "prior_roas": prior_roas,
        "cost_vs_prior_pct": _pct_norm(cost, prior_cost, p.days, mom_days),
        "revenue_vs_prior_pct": _pct_norm(revenue, prior_revenue, p.days, mom_days),
        "roas_vs_prior_pct": pct_change(roas, prior_roas),
        "bookings_vs_prior_pct": _pct_norm(bookings, prior_bookings, p.days, mom_days),
        "mom_per_day": mom_per_day,
    }


def _crm_rate_plan_deltas(db: Session, branch: Branch, p: Period, crm: dict) -> list[dict]:
    """CRM revenue by rate plan/campaign, the same grouping the Marketing
    Activity → CRM Reservations "By Rate Plan" tab uses, with both of the
    report's deltas per row.

    `crm_section` already computes `by_rate_plan` for the current window —
    this only adds the month-back and year-back numbers, matched by rate plan
    name. A campaign that didn't run in a comparison window gets no row there,
    so its delta is None rather than a false "+100%" — which matters more for
    the year-ago match than the month-ago one, since campaign names churn far
    more over twelve months than over one.
    """
    this_rows = crm.get("by_rate_plan") or []
    if not this_rows:
        return []
    mom, mom_days, mom_per_day = _mom_days(p)
    prev_start, prev_end = mom
    yoy, yoy_days, yoy_per_day = _yoy_days(p)
    prior_by_name = {
        r["rate_plan_name"]: r
        for r in _crm_revenue_by_rate_plan(db, branch.id, prev_start, prev_end)
    }
    yoy_by_name = {
        r["rate_plan_name"]: r
        for r in _crm_revenue_by_rate_plan(db, branch.id, yoy[0], yoy[1])
    }
    out = []
    for r in this_rows:
        prior = prior_by_name.get(r["rate_plan_name"]) or {}
        last_year = yoy_by_name.get(r["rate_plan_name"]) or {}
        out.append({
            **r,
            "prior_revenue": prior.get("revenue"),
            "prior_bookings": prior.get("bookings"),
            "revenue_vs_prior_pct": _pct_norm(r.get("revenue"), prior.get("revenue"),
                                              p.days, mom_days),
            "bookings_vs_prior_pct": _pct_norm(r.get("bookings"), prior.get("bookings"),
                                               p.days, mom_days),
            "yoy_revenue": last_year.get("revenue"),
            "yoy_bookings": last_year.get("bookings"),
            "revenue_vs_yoy_pct": _pct_norm(r.get("revenue"), last_year.get("revenue"),
                                            p.days, yoy_days),
            "bookings_vs_yoy_pct": _pct_norm(r.get("bookings"), last_year.get("bookings"),
                                             p.days, yoy_days),
            "yoy_per_day": yoy_per_day,
            "mom_per_day": mom_per_day,
        })
    return out


def _crm_rate_plan_totals(p: Period, rows: list[dict]) -> dict:
    """Totals for the CRM rate-plan table, with both comparisons.

    Computed here rather than summed in the renderer because both percentages
    need the two window lengths to normalise correctly, and the renderer has
    no business knowing how long February was.

    A campaign missing from a comparison window contributes 0 to that total:
    it is genuinely new, so its whole revenue is real growth, unlike a missing
    per-row comparison which is an absence of data.
    """
    _, yoy_days, yoy_per_day = _yoy_days(p)
    _, mom_days, mom_per_day = _mom_days(p)

    def s(key):
        return sum(r.get(key) or 0 for r in rows)

    bookings, revenue = s("bookings"), s("revenue")
    return {
        "bookings": bookings,
        "revenue": round(revenue, 2),
        "bookings_vs_prior_pct": _pct_norm(bookings, s("prior_bookings"),
                                           p.days, mom_days),
        "revenue_vs_prior_pct": _pct_norm(revenue, s("prior_revenue"),
                                          p.days, mom_days),
        "bookings_vs_yoy_pct": _pct_norm(bookings, s("yoy_bookings"), p.days, yoy_days),
        "revenue_vs_yoy_pct": _pct_norm(revenue, s("yoy_revenue"), p.days, yoy_days),
        "yoy_per_day": yoy_per_day,
        "mom_per_day": mom_per_day,
    }


def _ads_yoy(db: Session, branch: Branch, p: Period, ads: dict) -> dict:
    """Year-over-year cost / revenue / bookings / ROAS per ad channel, merged
    onto the `paid_ads` payload alongside the `wow_*` prior-period deltas it
    already carries.

    Queries `ads_performance` directly rather than re-running
    `paid_ads_section` over the year-ago window: that function also calls the
    Ads Platform aggregator for its By-Country table, and this report never
    shows last year's country split — one extra network round trip per branch
    for numbers nothing renders.

    A channel that ran no ads a year ago (or did not exist yet) has a zero
    base, so `_pct_norm` returns None and the renderer draws no year-ago arrow
    at all. That is the honest answer: "new channel", not "+100%".
    """
    channels = ads.get("by_channel") or []
    if not channels:
        return ads

    yoy, yoy_days, yoy_per_day = _yoy_days(p)
    rows = db.query(
        AdsPerformance.channel,
        func.coalesce(func.sum(AdsPerformance.cost_native), 0),
        func.coalesce(func.sum(AdsPerformance.bookings), 0),
        func.coalesce(func.sum(AdsPerformance.revenue_native), 0),
    ).filter(
        AdsPerformance.branch_id == branch.id,
        AdsPerformance.grain == "daily",
        AdsPerformance.date_from >= yoy[0],
        AdsPerformance.date_from <= yoy[1],
    ).group_by(AdsPerformance.channel).all()

    last_year = {
        (r[0] or "Unknown"): {
            "cost": float(r[1] or 0),
            "bookings": int(r[2] or 0),
            "revenue": float(r[3] or 0),
        }
        for r in rows
    }
    blank = {"cost": 0.0, "bookings": 0, "revenue": 0.0}

    out_channels = []
    for c in channels:
        ly = last_year.get(c.get("channel"), blank)
        ly_roas = round(ly["revenue"] / ly["cost"], 2) if ly["cost"] > 0 else None
        out_channels.append({
            **c,
            "yoy_cost": round(ly["cost"], 2),
            "yoy_revenue": round(ly["revenue"], 2),
            "yoy_bookings": ly["bookings"],
            "yoy_roas": ly_roas,
            "yoy_cost_pct": _pct_norm(c.get("cost"), ly["cost"], p.days, yoy_days),
            "yoy_revenue_pct": _pct_norm(c.get("revenue"), ly["revenue"], p.days, yoy_days),
            "yoy_bookings_pct": _pct_norm(c.get("bookings"), ly["bookings"],
                                          p.days, yoy_days),
            "yoy_roas_pct": pct_change(c.get("roas"), ly_roas),
        })

    tot_cost = sum(v["cost"] for v in last_year.values())
    tot_rev = sum(v["revenue"] for v in last_year.values())
    tot_roas = round(tot_rev / tot_cost, 2) if tot_cost > 0 else None
    this_tot = ads.get("last_week") or {}
    this_roas = (
        round((this_tot.get("revenue") or 0) / this_tot["cost"], 2)
        if this_tot.get("cost") else None
    )

    return {
        **ads,
        "by_channel": out_channels,
        "yoy_total": {"cost": round(tot_cost, 2), "revenue": round(tot_rev, 2),
                      "roas": tot_roas},
        "yoy_cost_pct": _pct_norm(this_tot.get("cost"), tot_cost, p.days, yoy_days),
        "yoy_revenue_pct": _pct_norm(this_tot.get("revenue"), tot_rev, p.days, yoy_days),
        "yoy_roas_pct": pct_change(this_roas, tot_roas),
        "yoy_per_day": yoy_per_day,
    }


# ── 4. Recommended actions ───────────────────────────────────────────────────


def _spending_channels(ads: dict) -> list[dict]:
    """Ad channels that actually spent money and have a computable ROAS.

    `paid_ads_section` returns `by_channel` as a LIST of dicts keyed by a
    `channel` field. Channels with zero spend are dropped rather than treated
    as 0× — a channel that ran no ads has not "performed badly".
    """
    out = []
    for c in (ads or {}).get("by_channel") or []:
        if not isinstance(c, dict):
            continue
        if c.get("roas") is None or not (c.get("cost") or 0):
            continue
        out.append(c)
    return out


def _category_row(channel: dict, name: str) -> dict:
    """Look up one source_category row from `channel_mix`'s `categories` list."""
    for row in (channel or {}).get("categories") or []:
        if (row.get("source_category") or "").lower() == name.lower():
            return row
    return {}


def _upcoming_holidays(
    db: Session, country_code: str, horizon_start: date, horizon_end: date
) -> list[dict]:
    """Holidays for a country inside a forward-looking window.

    `holiday_calendars` rows are mostly recurring (year IS NULL) and stored as
    month/day ranges, so the year is applied from the horizon. Matched on
    country_code — the indexed column, and more reliable than the free-text
    country name.
    """
    rows = db.query(HolidayCalendar).filter(
        func.upper(HolidayCalendar.country_code) == (country_code or "").upper(),
    ).all()

    out = []
    for h in rows:
        for yr in {horizon_start.year, horizon_end.year}:
            try:
                h_start = date(yr, h.month_start, h.day_start or 1)
                h_end = date(yr, h.month_end, h.day_end or h.day_start or 1)
            except ValueError:
                continue
            if h_end < h_start:          # range wrapping the new year
                h_end = date(yr + 1, h.month_end, h.day_end or h.day_start or 1)
            if h_end < horizon_start or h_start > horizon_end:
                continue
            out.append({
                "name": h.holiday_name,
                "type": h.holiday_type,
                "propensity": h.travel_propensity,
                "start": h_start.isoformat(),
                "end": h_end.isoformat(),
                "label": f"{h_start.strftime('%b %d')}–{h_end.strftime('%b %d')}",
            })
            break
    out.sort(key=lambda x: x["start"])
    return out


def recommendations_block(
    db: Session, p: Period, markets: dict, ads: dict, kol: dict, target: dict
) -> list[dict]:
    """Date-anchored actions for the next period.

    Built by intersecting the markets that are actually growing with holidays
    coming up in those markets — the same reasoning a manager would do by
    hand, which is why the output reads as "push Malaysia Aug 17–25, school
    holidays, market up +236%" rather than a generic nudge.
    """
    actions: list[dict] = []
    horizon_start = p.end + timedelta(days=1)
    horizon_end = p.end + timedelta(days=60)

    growing = [
        r for r in markets.get("rows", [])
        if (r.get("vs_prior_pct") or 0) > 0 and r["bookings"] >= MARKET_MIN_BOOKINGS
    ]
    growing.sort(key=lambda r: -(r.get("vs_prior_pct") or 0))

    for r in growing[:4]:
        for h in _upcoming_holidays(db, r.get("country_code") or "", horizon_start, horizon_end):
            if (h.get("propensity") or "").upper() == "LOW":
                continue
            actions.append({
                "key": f"act.market.{r.get('country_code') or r['country']}",
                "title": f"Push the {r['country']} market",
                "when": h["label"],
                "body": (
                    f"{h['name']} ({h['type'].replace('_', ' ')}) — high travel demand. "
                    f"This market is up {r['vs_prior_pct']:+.0f}% vs the prior period "
                    f"on {r['bookings']} bookings."
                ),
            })
            break
        if len(actions) >= 3:
            break

    # Paid ads — shift budget away from anything near break-even.
    weak, strong = [], []
    for c in _spending_channels(ads):
        if c["roas"] < ROAS_BAD:
            weak.append(c)
        elif c["roas"] >= ROAS_GOOD:
            strong.append(c)
    if weak:
        worst = min(weak, key=lambda c: c["roas"])
        best = max(strong, key=lambda c: c["roas"]) if strong else None
        actions.append({
            "key": f"act.ads.{worst['channel']}",
            "title": f"Review {worst['channel']} Ads",
            "when": "This period",
            "body": (
                f"{worst['channel']} is returning {worst['roas']:.2f}× — at or below "
                "break-even. "
                + (f"Shift budget to {best['channel']} ({best['roas']:.1f}×), which is "
                   "carrying the results."
                   if best else "Pause or rebuild before spending further.")
            ),
        })

    if (kol or {}).get("posts_this_week") == 0:
        actions.append({
            "key": "act.kol_posts",
            "title": "No KOL posts went live this period",
            "when": "Next period",
            "body": "Chase the pipeline — KOL-driven bookings lag posting by weeks, "
                    "so an empty period here shows up as a gap two periods out.",
        })

    if target.get("period_pct") is not None and target["period_pct"] < TARGET_BAD_PCT:
        actions.append({
            "key": "act.target",
            "title": "Period landed under target",
            "when": "Immediate",
            "body": f"Achievement was {target['period_pct']:.0f}% of the prorated goal. "
                    f"Review pricing and promotions before the month closes.",
        })

    return actions[:6]


# ── 5. Highlights / watch-outs ───────────────────────────────────────────────


def highlights_block(kpi: dict, target: dict, markets: dict, ads: dict,
                     channel: dict) -> dict:
    """Rule-driven "what went well / what to watch", derived only from
    numbers already computed above — no extra queries.

    Every line carries a `key` naming the RULE that produced it, not the text
    it produced. That is what lets an operator correct a line and have the
    correction survive a rebuild: the numbers in the sentence change, the rule
    that fired does not. Keys are also stable across the two boxes on purpose —
    revenue up 12% is `flag.revenue` in Highlights and revenue down 12% is
    `flag.revenue` in Watch-outs, so an edit follows the finding rather than
    the box it happened to land in.
    """
    good: list[dict] = []
    watch: list[dict] = []
    d = kpi.get("vs_yoy", {})
    suffix = " vs same period last year"

    def _add(delta, label, key, unit="%"):
        if delta is None:
            return
        if unit == "pts":
            # Still a percentage-POINT move under the hood — only the label
            # reads "%" now, matching every other delta in the report.
            item = {"key": key, "text": f"<b>{label} {delta:+.1f}%</b>{suffix}."}
            if delta >= OCC_GOOD_PTS:
                good.append(item)
            elif delta < OCC_BAD_PTS:
                watch.append(item)
            return
        if abs(delta) < HIGHLIGHT_PCT:
            return
        item = {"key": key, "text": f"<b>{label} {delta:+.0f}%</b>{suffix}."}
        (good if delta > 0 else watch).append(item)

    _add(d.get("revenue_pct"), "Room revenue", "flag.revenue")
    _add(d.get("adr_pct"), "Average room rate", "flag.adr")
    _add(d.get("occ_pts"), "Occupancy", "flag.occ", unit="pts")

    if target.get("period_pct") is not None:
        if target["period_pct"] >= TARGET_GOOD_PCT:
            good.append({"key": "flag.target", "text": (
                f"<b>Beat the period target</b> — {target['period_pct']:.0f}% of goal.")})
        elif target["period_pct"] < TARGET_BAD_PCT:
            watch.append({"key": "flag.target", "text": (
                f"<b>Under target</b> — {target['period_pct']:.0f}% of the prorated goal.")})

    booming = [
        r for r in markets.get("rows", [])
        if (r.get("vs_prior_pct") or 0) >= MARKET_BOOM_PCT
        and r["bookings"] >= MARKET_MIN_BOOKINGS
    ]
    if booming:
        names = ", ".join(r["country"] for r in booming[:3])
        best = max(booming, key=lambda r: r["vs_prior_pct"])
        good.append({"key": "flag.markets", "text": (
            f"<b>{names} growing fast</b> — up to {best['vs_prior_pct']:+.0f}% "
            "vs the prior period.")})

    for c in _spending_channels(ads):
        name, roas = c["channel"], c["roas"]
        if roas >= ROAS_GOOD:
            good.append({"key": f"flag.ads.{name}", "text": (
                f"<b>{name} Ads at {roas:.1f}×</b> — a profitable channel.")})
        elif roas < ROAS_BAD:
            watch.append({"key": f"flag.ads.{name}", "text": (
                f"<b>{name} Ads only {roas:.2f}×</b> — near break-even; "
                "optimise or reallocate.")})

    direct_share = _category_row(channel, "Direct").get("revenue_share_pct")
    if direct_share is not None and direct_share >= 25:
        good.append({"key": "flag.direct", "text": (
            f"<b>Direct is {direct_share:.0f}% of revenue</b> — saves OTA "
            "commission (~15–18%).")})

    unk = markets.get("unknown_share_pct")
    if unk is not None and unk >= UNKNOWN_MARKET_WARN_SHARE:
        watch.append({"key": "flag.unknown_market", "text": (
            f"<b>{unk:.0f}% of revenue has no source market recorded</b> — "
            "market shares below are understated until guest-source data is tagged.")})

    return {"highlights": good[:5], "watchouts": watch[:5]}


# ── 6. Data quality notes ────────────────────────────────────────────────────


def data_notes_block(db: Session, branch: Branch, p: Period,
                     markets: dict, ads: dict) -> list[dict]:
    """Caveats a manager needs in order to read the numbers correctly.

    The `reservation_daily` check is the important one: nothing refreshes
    that table on a schedule, and Channel Mix + Markets are both computed
    from it. A half-populated table looks exactly like a collapse in
    bookings, so if coverage does not reach the end of the period we say so
    rather than letting the charts imply a story.
    """
    notes: list[dict] = []

    latest = db.query(func.max(ReservationDaily.date)).filter(
        ReservationDaily.branch_id == branch.id
    ).scalar()
    # Audience is a branch manager, so these read as "what is wrong with the
    # numbers and who fixes it", not as the table name behind the problem.
    if latest is None:
        notes.append({
            "level": "bad",
            "text": "Nightly booking data is missing for this branch, so Channels "
                    "and Markets are empty below. Ask the data team to run a full "
                    "booking sync.",
        })
    elif latest < p.end:
        notes.append({
            "level": "bad",
            "text": (
                f"Nightly booking data stops at {latest:%d %b %Y}, "
                f"{(p.end - latest).days} day(s) before this period ends. "
                "Channels and Markets are undercounted for those last days — "
                "don't read a drop there as real yet."
            ),
        })

    unk = markets.get("unknown_share_pct")
    if unk is not None and unk >= UNKNOWN_MARKET_WARN_SHARE:
        notes.append({
            "level": "warn",
            "text": (
                f"{markets['unknown_bookings']} booking(s) carry no source market "
                f"({unk:.0f}% of revenue). Market shares exclude them."
            ),
        })

    channel_names = {
        (c.get("channel") or "") for c in ((ads or {}).get("by_channel") or [])
        if isinstance(c, dict)
    }
    if "Google" not in channel_names:
        notes.append({
            "level": "warn",
            "text": "No Google Ads figures for this branch this period. That means "
                    "Google is not connected here, not that nothing was spent.",
        })

    notes.append({
        "level": "info",
        "text": "Affiliate commission from KOLs is not tracked anywhere in HiD, so "
                "it is not in any number above.",
    })
    return notes


# ── Assembly ─────────────────────────────────────────────────────────────────


def build_branch_biweekly(db: Session, branch: Branch, p: Period) -> dict:
    """Full payload for one branch over one period.

    Every section is wrapped in `safe_section`: a Postgres statement_timeout
    on one heavy GROUP BY degrades that section to a default instead of
    failing the whole report — the same resilience the weekly report has.

    Sections reused from the weekly builder are anchored on `today=p.end`,
    not the real today, so their internal "as of" logic (KOL monthly targets,
    expiring usage rights) lines up with the period being reported on rather
    than with whenever the report happens to be regenerated.
    """
    anchor = p.end
    window = (p.start, p.end)
    # The shared weekly sections default to "the equally-long window
    # immediately before", which for this report would be the other half of
    # the month. Naming the month-back window here is what keeps their
    # `wow_*` arrows pointing at the same reference as everything else on
    # the page.
    compare = mom_window(p)

    kpi = safe_section(db, f"bw.kpi[{branch.name}]",
                       lambda: kpi_block(db, branch, p), {})
    room_types = safe_section(db, f"bw.room_types[{branch.name}]",
                              lambda: room_type_block(db, branch, p),
                              {"has_split": False, "segments": []})
    target = safe_section(db, f"bw.target[{branch.name}]",
                          lambda: target_block(db, branch, p), {})
    channel = safe_section(db, f"bw.channel[{branch.name}]",
                           lambda: channel_mix(db, branch.id, anchor, window=window,
                                               compare=compare), {})
    markets = safe_section(db, f"bw.markets[{branch.name}]",
                           lambda: markets_block(db, branch, p),
                           {"rows": [], "total_revenue": 0})
    ads = safe_section(db, f"bw.ads[{branch.name}]",
                       lambda: paid_ads_section(db, branch, anchor, window=window,
                                                compare=compare), {})
    if ads:
        ads = safe_section(db, f"bw.ads_yoy[{branch.name}]",
                           lambda: _ads_yoy(db, branch, p, ads), ads)
    chan_bookings = safe_section(db, f"bw.channel_bookings[{branch.name}]",
                                 lambda: channel_bookings_block(db, branch, p),
                                 {"rows": [], "total_bookings": 0})
    kol = safe_section(db, f"bw.kol[{branch.name}]",
                       lambda: kol_section(db, branch.id, branch.name, anchor,
                                           window=window, compare=compare), {})
    # Network call, so it gets the same failure isolation as a slow query.
    kol_reach = safe_section(db, f"bw.kol_reach[{branch.name}]",
                             lambda: kol_reach_block(branch, p),
                             {"available": False})
    crm = safe_section(db, f"bw.crm[{branch.name}]",
                       lambda: crm_section(db, branch.id, branch.name, anchor,
                                           window=window, compare=compare), {})

    # Cost + ROAS, dated to this exact window — merged onto the section dicts
    # above rather than returned separately, so the renderer reads `kol` /
    # `crm` as one payload the same way it already reads `paid_ads`.
    if kol:
        kol_cost = safe_section(db, f"bw.kol_cost[{branch.name}]",
                                lambda: _kol_period_cost_roas(db, branch, p, kol), {})
        kol = {**kol, **kol_cost}
    if crm:
        crm_cost = safe_section(db, f"bw.crm_cost[{branch.name}]",
                                lambda: _crm_period_cost_roas(db, branch, p, crm), {})
        crm = {**crm, **crm_cost}
        crm["by_rate_plan"] = safe_section(
            db, f"bw.crm_rate_plan[{branch.name}]",
            lambda: _crm_rate_plan_deltas(db, branch, p, crm),
            crm.get("by_rate_plan") or [],
        )
        crm["rate_plan_totals"] = _crm_rate_plan_totals(p, crm["by_rate_plan"])

    flags = safe_section(db, f"bw.highlights[{branch.name}]",
                         lambda: highlights_block(kpi, target, markets, ads, channel),
                         {"highlights": [], "watchouts": []})
    actions = safe_section(db, f"bw.actions[{branch.name}]",
                           lambda: recommendations_block(db, p, markets, ads, kol, target), [])
    notes = safe_section(db, f"bw.notes[{branch.name}]",
                         lambda: data_notes_block(db, branch, p, markets, ads), [])

    return {
        "branch_id": str(branch.id),
        "branch_name": branch.name,
        "branch_city": branch.city,
        "currency": branch.currency,
        "kpi": kpi,
        "room_types": room_types,
        "target": target,
        "channel_mix": channel,
        "channel_bookings": chan_bookings,
        "markets": markets,
        "paid_ads": ads,
        "kol": kol,
        "kol_reach": kol_reach,
        "crm": crm,
        "highlights": flags.get("highlights", []),
        "watchouts": flags.get("watchouts", []),
        "actions": actions,
        "data_notes": notes,
    }


def build_biweekly_report(db: Session, p: Period) -> list[dict]:
    """One payload per active branch, in the reports' display order."""
    branches = sorted(
        db.query(Branch).filter_by(is_active=True).all(),
        key=_branch_display_sort_key,
    )
    return [build_branch_biweekly(db, b, p) for b in branches]
