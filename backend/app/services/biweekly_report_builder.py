"""
Bi-Weekly Branch Manager Report — analytical payload.

Audience is a branch manager, not an analyst: the question is "how did my
branch do over the last two weeks, versus the same two weeks last year, and
what should I do next". That framing drives three differences from the
weekly report:

  - **Single branch at a time.** Every section is scoped to one branch.
  - **Year-over-year is the headline comparison**, prior period is the
    secondary reference. Hotels are seasonal; week-over-week movement mostly
    measures the calendar.
  - **Every number carries a traffic light and a plain-English "why".**

Reuses the weekly report's section code wherever the shape matches — those
functions now accept an explicit `window`, so the same tested queries run
over a 14-day range. Sections with a genuinely different shape (markets by
revenue, target achievement, recommendations) are built here.

See `biweekly_period.py` for how a period is defined.
"""
from __future__ import annotations

import calendar
import logging
from datetime import date, timedelta
from typing import Optional

from sqlalchemy import extract, func
from sqlalchemy.orm import Session

from app.models.branch import Branch
from app.models.daily_metrics import DailyMetrics
from app.models.holiday_intel import HolidayCalendar
from app.models.kpi import KPITarget
from app.models.reservation import Reservation
from app.models.reservation_daily import ReservationDaily
from app.services.biweekly_period import (
    Period,
    comparable_as_totals,
    previous_window,
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


# ── 1. KPI block ─────────────────────────────────────────────────────────────


def _delta_block(
    cur: dict, cmp_: dict, cur_days: int, cmp_days: int
) -> dict:
    """Compare two `range_metrics` results.

    Totals (revenue, room-nights) are only compared as totals when both
    windows are the same length. When they differ — which happens when a
    21-day W51–W53 period meets a 14-day prior year — they are compared on a
    per-day basis instead, and `per_day` is set so the renderer can label the
    chip "/day". Rate metrics (ADR, OCC, RevPAR) are already normalised and
    are compared directly either way.
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
    """Headline metrics for the period, vs same period last year and vs the
    prior period.
    """
    total_rooms = branch.total_rooms or 0
    yoy = yoy_window(p)
    prev = previous_window(p)

    this = range_metrics(db, branch.id, total_rooms, p.start, p.end)
    prior = range_metrics(db, branch.id, total_rooms, prev[0], prev[1])
    last_year = range_metrics(db, branch.id, total_rooms, yoy[0], yoy[1])

    d_yoy = _delta_block(this, last_year, p.days, window_days(yoy))
    d_prior = _delta_block(this, prior, p.days, window_days(prev))

    return {
        "this": this,
        "prior": prior,
        "yoy": last_year,
        "vs_yoy": d_yoy,
        "vs_prior": d_prior,
        "yoy_window": [yoy[0].isoformat(), yoy[1].isoformat()],
        "prior_window": [prev[0].isoformat(), prev[1].isoformat()],
        "yoy_comparable": comparable_as_totals(p, yoy),
        "yoy_has_data": (last_year.get("sold") or 0) > 0,
        "lights": {
            "revenue": verdict(d_yoy["revenue_pct"], REVENUE_GOOD_PCT, REVENUE_BAD_PCT),
            "adr": verdict(d_yoy["adr_pct"], REVENUE_GOOD_PCT, REVENUE_BAD_PCT),
            "occ": verdict(d_yoy["occ_pts"], OCC_GOOD_PTS, OCC_BAD_PTS),
        },
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

    return {
        "year": year, "month": month,
        "label": date(year, month, 1).strftime("%B %Y"),
        "achievement": {
            "actual_revenue": result["actual_revenue"],
            "target_revenue": result["target_revenue"],
        },
        "pct": result["achievement_pct"],
        "closed": as_of >= month_end,
        "is_override": result["is_override"],
    }


def target_block(db: Session, branch: Branch, p: Period) -> dict:
    """Prorated target for the period, plus every calendar month it touches.

    Managers are held to a monthly number, so the period figure alone is not
    enough. A bi-weekly period can straddle a month boundary — e.g. Jul 25 –
    Aug 2 — and when it does, BOTH months get their own achievement, each
    computed from its own complete run of days rather than folded into
    whichever month the period happened to end in. A manager reading a report
    that closes on Aug 2 still sees how July actually finished, not just the
    two days of it the period overlapped.
    """
    period_ach = compute_period_achievement(db, branch, p.start, p.end)
    period_pct = period_ach.get("achievement_pct")

    months = []
    seen = set()
    for d in (p.start, p.end):
        key = (d.year, d.month)
        if key in seen:
            continue
        seen.add(key)
        months.append(_month_achievement(db, branch, d.year, d.month, as_of=p.end))

    # Mirror the LATEST month (the one the period ends in) at the top level
    # too, so anything reading this payload from before `months` existed —
    # including an already-cached period — keeps working unchanged.
    latest = months[-1]

    return {
        "period": period_ach,
        "period_pct": round(period_pct * 100, 1) if period_pct is not None else None,
        "months": months,
        "month": latest["achievement"],
        "month_pct": latest["pct"],
        "month_label": latest["label"],
        "month_closed": latest["closed"],
        "month_through": latest["through"],
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

    prev = previous_window(p)
    yoy = yoy_window(p)
    cur = _by_country(p.start, p.end)
    prior = _by_country(prev[0], prev[1])
    last_year = _by_country(yoy[0], yoy[1])

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
        yoy_rev = last_year.get(code, {}).get("revenue", 0.0)
        rows.append({
            **v,
            "is_unknown": is_unknown,
            "share_pct": round(v["revenue"] / total_rev * 100, 1) if total_rev else None,
            "prior_revenue": prior_rev,
            "vs_prior_pct": pct_change(v["revenue"], prior_rev),
            "yoy_revenue": yoy_rev,
            "vs_yoy_pct": pct_change(v["revenue"], yoy_rev),
        })

    rows.sort(key=lambda r: -r["revenue"])
    known = [r for r in rows if not r["is_unknown"]]

    return {
        "rows": known[:limit],
        "total_revenue": round(total_rev, 2),
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

    prev = previous_window(p)
    cur = _by_source(p.start, p.end)
    prior = _by_source(prev[0], prev[1])

    total = sum(v["bookings"] for v in cur.values())
    rows = []
    for name, v in cur.items():
        prior_n = prior.get(name, {}).get("bookings", 0)
        rows.append({
            **v,
            "share_pct": round(v["bookings"] / total * 100, 1) if total else None,
            "prior_bookings": prior_n,
            "vs_prior_pct": pct_change(v["bookings"], prior_n),
            "is_direct": (v["category"] or "").strip().lower() == "direct",
        })
    rows.sort(key=lambda r: -r["bookings"])

    direct_bookings = sum(r["bookings"] for r in rows if r["is_direct"])
    return {
        "rows": rows[:limit],
        "total_bookings": total,
        "direct_bookings": direct_bookings,
        "direct_share_pct": (
            round(direct_bookings / total * 100, 1) if total else None
        ),
    }


# ── 3c. KOL reach / engagement (external) ────────────────────────────────────


def kol_reach_block(branch: Branch, p: Period) -> dict:
    """Reach + engagement for the period, from the KOL Engine.

    HiD's `kol_records` has no reach/engagement columns, so this is the only
    source. It degrades to `available: False` on any failure — see
    `kol_engine.fetch_kol_insights` for why that is not reported as zero.
    """
    from app.config import settings
    from app.services.kol_engine import fetch_kol_insights, resolve_hotel_id_from_branch_name

    hotel_id = resolve_hotel_id_from_branch_name(branch.name or "")
    branch_key = HOTEL_TO_BRANCH_KEY.get(hotel_id) if hotel_id else None
    if not branch_key:
        return {"available": False, "posts": 0, "reach": 0, "engagements": 0,
                "engagement_rate_pct": None, "reason": "branch_not_mapped"}

    return fetch_kol_insights(
        base_url=settings.KOL_ENGINE_URL,
        org_id=settings.KOL_ENGINE_ORG_ID,
        api_key=settings.KOL_SYNC_API_KEY,
        branch_key=branch_key,
        date_from=p.start,
        date_to=p.end,
    )


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
            "title": "No KOL posts went live this period",
            "when": "Next period",
            "body": "Chase the pipeline — KOL-driven bookings lag posting by weeks, "
                    "so an empty period here shows up as a gap two periods out.",
        })

    if target.get("period_pct") is not None and target["period_pct"] < TARGET_BAD_PCT:
        actions.append({
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
    """
    good: list[str] = []
    watch: list[str] = []
    d = kpi.get("vs_yoy", {})
    suffix = " vs same period last year"

    def _add(delta, label, unit="%"):
        if delta is None:
            return
        if unit == "pts":
            text = f"<b>{label} {delta:+.1f} pts</b>{suffix}."
            if delta >= OCC_GOOD_PTS:
                good.append(text)
            elif delta < OCC_BAD_PTS:
                watch.append(text)
            return
        if abs(delta) < HIGHLIGHT_PCT:
            return
        text = f"<b>{label} {delta:+.0f}%</b>{suffix}."
        (good if delta > 0 else watch).append(text)

    _add(d.get("revenue_pct"), "Room revenue")
    _add(d.get("adr_pct"), "Average room rate")
    _add(d.get("occ_pts"), "Occupancy", unit="pts")

    if target.get("period_pct") is not None:
        if target["period_pct"] >= TARGET_GOOD_PCT:
            good.append(f"<b>Beat the period target</b> — {target['period_pct']:.0f}% of goal.")
        elif target["period_pct"] < TARGET_BAD_PCT:
            watch.append(f"<b>Under target</b> — {target['period_pct']:.0f}% of the prorated goal.")

    booming = [
        r for r in markets.get("rows", [])
        if (r.get("vs_prior_pct") or 0) >= MARKET_BOOM_PCT
        and r["bookings"] >= MARKET_MIN_BOOKINGS
    ]
    if booming:
        names = ", ".join(r["country"] for r in booming[:3])
        best = max(booming, key=lambda r: r["vs_prior_pct"])
        good.append(
            f"<b>{names} growing fast</b> — up to {best['vs_prior_pct']:+.0f}% vs the prior period."
        )

    for c in _spending_channels(ads):
        name, roas = c["channel"], c["roas"]
        if roas >= ROAS_GOOD:
            good.append(f"<b>{name} Ads at {roas:.1f}×</b> — a profitable channel.")
        elif roas < ROAS_BAD:
            watch.append(f"<b>{name} Ads only {roas:.2f}×</b> — near break-even; optimise or reallocate.")

    direct_share = _category_row(channel, "Direct").get("revenue_share_pct")
    if direct_share is not None and direct_share >= 25:
        good.append(
            f"<b>Direct is {direct_share:.0f}% of revenue</b> — saves OTA commission (~15–18%)."
        )

    unk = markets.get("unknown_share_pct")
    if unk is not None and unk >= UNKNOWN_MARKET_WARN_SHARE:
        watch.append(
            f"<b>{unk:.0f}% of revenue has no source market recorded</b> — "
            "market shares below are understated until guest-source data is tagged."
        )

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
    if latest is None:
        notes.append({
            "level": "bad",
            "text": "No reservation_daily rows for this branch — Channel Mix and "
                    "Markets cannot be computed. Run a full Insights sync.",
        })
    elif latest < p.end:
        notes.append({
            "level": "bad",
            "text": (
                f"Nightly booking data only reaches {latest:%d %b %Y}, "
                f"{(p.end - latest).days} day(s) short of the period end. "
                "Channel Mix and Markets are understated for the tail of this "
                "period — refresh before acting on a decline."
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
            "text": "No Google Ads rows for this branch this period. HiD receives "
                    "Google spend only through the Ads Platform aggregator — an "
                    "absent row means not connected, not zero spend.",
        })

    notes.append({
        "level": "info",
        "text": "KOL reach and engagement (views, likes, engagement rate) are not "
                "tracked in HiD — only posts published and bookings attributed to "
                "KOLs. Affiliate commission is not recorded at all.",
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

    kpi = safe_section(db, f"bw.kpi[{branch.name}]",
                       lambda: kpi_block(db, branch, p), {})
    target = safe_section(db, f"bw.target[{branch.name}]",
                          lambda: target_block(db, branch, p), {})
    channel = safe_section(db, f"bw.channel[{branch.name}]",
                           lambda: channel_mix(db, branch.id, anchor, window=window), {})
    markets = safe_section(db, f"bw.markets[{branch.name}]",
                           lambda: markets_block(db, branch, p),
                           {"rows": [], "total_revenue": 0})
    ads = safe_section(db, f"bw.ads[{branch.name}]",
                       lambda: paid_ads_section(db, branch, anchor, window=window), {})
    chan_bookings = safe_section(db, f"bw.channel_bookings[{branch.name}]",
                                 lambda: channel_bookings_block(db, branch, p),
                                 {"rows": [], "total_bookings": 0})
    kol = safe_section(db, f"bw.kol[{branch.name}]",
                       lambda: kol_section(db, branch.id, branch.name, anchor, window=window), {})
    # Network call, so it gets the same failure isolation as a slow query.
    kol_reach = safe_section(db, f"bw.kol_reach[{branch.name}]",
                             lambda: kol_reach_block(branch, p),
                             {"available": False})
    crm = safe_section(db, f"bw.crm[{branch.name}]",
                       lambda: crm_section(db, branch.id, branch.name, anchor, window=window), {})

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
