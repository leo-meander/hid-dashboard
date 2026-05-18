"""
Weekly Report Builder — Phase 5.1

Produces the analytical payload used by the weekly email and Report page.
Each section is a pure function so sections can be unit-tested independently.

Conventions (honor user memory rules):
- OCC includes ALL sources (no source exclusions)
- Revenue EXCLUDES Blogger, House Use, KOL, Special Case, Work Exchange
- Percentages rendered with 2 decimals; money rendered as full numbers (no K/M/B)
"""
from __future__ import annotations

import calendar
import logging
import statistics
from datetime import date, timedelta
from typing import Optional
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.ads import AdsPerformance
from app.models.angle import AdAngle
from app.models.branch import Branch
from app.models.daily_metrics import DailyMetrics
from app.models.email_campaign_stats import EmailCampaignStats
from app.models.holiday_intel import HolidayCalendar
from app.models.kol import KOLRecord
from app.models.kpi import KPITarget
from app.models.reservation import Reservation
from app.services.crm_filters import crm_reservation_filter
from app.services.kpi_engine import _EXCLUDED_SOURCES, _EXCLUDED_STATUSES

logger = logging.getLogger(__name__)


# ── Calendar-week helper ────────────────────────────────────────────────────


def last_week_range(today: date) -> tuple[date, date]:
    """Return (Monday, Sunday) of the most recently completed calendar week.

    The weekly cron runs Monday 07:00 ICT — the email reports on the week
    that just ended (prior Mon–Sun), not the partial new week that's
    barely started. This helper is the single source of truth for that
    range so every section is aligned.

    Day-of-week handling:
      - Mon: returns last Mon..last Sun (full 7 days, freshly closed)
      - Tue–Sat (manual / late trigger): still returns the SAME prior
        Mon..Sun, not a rolling window — keeps "last week" stable
      - Sun: returns the Mon..Sun two weeks back (current week not yet
        complete, so "last week" still means the one before that)
    """
    dow = today.weekday()  # 0=Mon, 6=Sun
    last_sun = today - timedelta(days=dow + 1)
    last_mon = last_sun - timedelta(days=6)
    return last_mon, last_sun


# ── Reservation query helpers ────────────────────────────────────────────────

def _reservations_base(db: Session, branch_id: UUID):
    """All non-cancelled/no-show reservations for a branch."""
    return db.query(Reservation).filter(
        Reservation.branch_id == branch_id,
        ~func.lower(func.coalesce(Reservation.status, "")).in_(list(_EXCLUDED_STATUSES)),
    )


def _reservations_revenue_base(db: Session, branch_id: UUID):
    """Non-cancelled reservations, excluding Blogger/House Use/KOL/Special Case/Work Exchange."""
    return _reservations_base(db, branch_id).filter(
        ~func.lower(func.coalesce(Reservation.source, "")).in_(list(_EXCLUDED_SOURCES)),
    )


# ── 1. Summary metrics (KPI snapshot + WoW / MoM / YoY) ─────────────────────

def _range_metrics(db: Session, branch_id: UUID, total_rooms: int,
                   date_from: date, date_to: date) -> dict:
    """Aggregate daily_metrics over [date_from, date_to]."""
    if date_to < date_from:
        return {"revenue": 0.0, "sold": 0, "adr": None, "occ_pct": None, "revpar": None}

    row = db.query(
        func.coalesce(func.sum(DailyMetrics.revenue_native), 0),
        func.coalesce(func.sum(DailyMetrics.total_sold), 0),
    ).filter(
        DailyMetrics.branch_id == branch_id,
        DailyMetrics.date >= date_from,
        DailyMetrics.date <= date_to,
    ).one()

    rev = float(row[0])
    sold = int(row[1])
    days = (date_to - date_from).days + 1
    adr = round(rev / sold, 2) if sold > 0 else None
    occ_pct = round(sold / (total_rooms * days), 4) if (total_rooms > 0 and days > 0) else None
    revpar = round(rev / (total_rooms * days), 2) if (total_rooms > 0 and days > 0) else None

    return {
        "revenue": rev,
        "sold": sold,
        "adr": adr,
        "occ_pct": occ_pct,
        "revpar": revpar,
        "days": days,
    }


def _pct_change(cur: Optional[float], prev: Optional[float]) -> Optional[float]:
    if cur is None or prev is None or prev == 0:
        return None
    return round((cur - prev) / prev * 100, 2)


def summary_metrics(db: Session, branch_id: UUID, total_rooms: int, today: date) -> dict:
    """
    Reframed as a Monday-morning weekly digest:

    - last_week:  the week that just closed (Mon–Sun previous). Primary
                  number on the email — what the team should be reacting
                  to first thing Monday.
    - prev_week:  the Mon–Sun before last_week. Used to compute WoW.
                  Apples-to-apples (7d vs 7d) — unlike the old comparison
                  of "this calendar week so far" (often just 1 day on
                  Monday) vs "last week".
    - mtd / last_month / yoy: month-level context, unchanged.

    YoY renders None when last year has no data for the same MTD window.
    """
    last_mon, last_sun = last_week_range(today)
    prev_mon = last_mon - timedelta(days=7)
    prev_sun = last_mon - timedelta(days=1)

    # This month MTD
    mtd_start = today.replace(day=1)
    mtd_end = today
    # Last month (full)
    if mtd_start.month == 1:
        lm_start = date(mtd_start.year - 1, 12, 1)
    else:
        lm_start = date(mtd_start.year, mtd_start.month - 1, 1)
    lm_end = mtd_start - timedelta(days=1)
    # YoY — same MTD window last year
    try:
        yoy_start = date(today.year - 1, today.month, 1)
        yoy_end = date(today.year - 1, today.month, today.day)
    except ValueError:
        # leap-day edge case
        yoy_start = date(today.year - 1, today.month, 1)
        yoy_end = date(today.year - 1, today.month, 28)

    last_week = _range_metrics(db, branch_id, total_rooms, last_mon, last_sun)
    prev_week = _range_metrics(db, branch_id, total_rooms, prev_mon, prev_sun)
    mtd = _range_metrics(db, branch_id, total_rooms, mtd_start, mtd_end)
    last_month = _range_metrics(db, branch_id, total_rooms, lm_start, lm_end)
    yoy = _range_metrics(db, branch_id, total_rooms, yoy_start, yoy_end)

    return {
        "last_week": last_week,
        "prev_week": prev_week,
        "last_week_start": last_mon.isoformat(),
        "last_week_end": last_sun.isoformat(),
        "mtd": mtd,
        "last_month": last_month,
        "yoy": yoy,
        "wow_revenue_pct": _pct_change(last_week["revenue"], prev_week["revenue"]),
        "wow_occ_pct": _pct_change(last_week["occ_pct"], prev_week["occ_pct"]),
        "mom_revenue_pct": _pct_change(mtd["revenue"], last_month["revenue"]),
        "yoy_revenue_pct": _pct_change(mtd["revenue"], yoy["revenue"]) if yoy["sold"] > 0 else None,
        "yoy_occ_pct": _pct_change(mtd["occ_pct"], yoy["occ_pct"]) if yoy["sold"] > 0 else None,
    }


# ── 2. Outliers (days with unusual OCC or revenue) ──────────────────────────

def outliers(db: Session, branch_id: UUID, today: date,
             baseline_days: int = 30) -> list[dict]:
    """
    Flag days from last calendar week (Mon–Sun previous) where revenue or
    OCC deviates > 1.5σ from a `baseline_days` rolling mean.

    Why split: 7 days is too few to compute a reliable σ on its own, so we
    keep the 30-day window for the baseline statistic and slice the
    reporting window to last calendar week.

    Annotate each outlier with a probable cause:
      - weekend (Sat/Sun)
      - cancellation spike (cancellation_pct > 0.15)
    """
    last_mon, last_sun = last_week_range(today)
    baseline_start = last_sun - timedelta(days=baseline_days)
    rows = db.query(
        DailyMetrics.date,
        DailyMetrics.revenue_native,
        DailyMetrics.occ_pct,
        DailyMetrics.cancellation_pct,
    ).filter(
        DailyMetrics.branch_id == branch_id,
        DailyMetrics.date >= baseline_start,
        DailyMetrics.date <= last_sun,
    ).order_by(DailyMetrics.date).all()

    if len(rows) < 5:
        return []

    revs = [float(r.revenue_native or 0) for r in rows]
    occs = [float(r.occ_pct or 0) for r in rows]
    try:
        rev_mean = statistics.mean(revs)
        rev_sd = statistics.pstdev(revs) or 1
        occ_mean = statistics.mean(occs)
        occ_sd = statistics.pstdev(occs) or 1
    except statistics.StatisticsError:
        return []

    out = []
    for r in rows:
        # Only report events in last calendar week
        if r.date < last_mon:
            continue
        rev = float(r.revenue_native or 0)
        occ = float(r.occ_pct or 0)
        cxl = float(r.cancellation_pct or 0)
        rev_z = (rev - rev_mean) / rev_sd if rev_sd > 0 else 0
        occ_z = (occ - occ_mean) / occ_sd if occ_sd > 0 else 0

        if abs(rev_z) < 1.5 and abs(occ_z) < 1.5:
            continue

        reasons = []
        if r.date.weekday() in (5, 6):
            reasons.append("weekend")
        if cxl > 0.15:
            reasons.append(f"cancellation spike ({round(cxl*100, 2)}%)")

        direction = "spike" if (rev_z > 0 or occ_z > 0) else "drop"
        out.append({
            "date": r.date.isoformat(),
            "direction": direction,
            "revenue": round(rev, 2),
            "occ_pct": round(occ * 100, 2),
            "rev_z": round(rev_z, 2),
            "occ_z": round(occ_z, 2),
            "reasons": reasons,
        })

    # Keep the top 5 absolute deviations
    out.sort(key=lambda x: max(abs(x["rev_z"]), abs(x["occ_z"])), reverse=True)
    return out[:5]


# ── 3. Booking behavior (cancellation / lead time / LOS) ────────────────────

def _lead_time_buckets(values: list[int]) -> dict:
    buckets = {"0-3d": 0, "4-14d": 0, "15-30d": 0, "31-60d": 0, "60d+": 0}
    for v in values:
        if v <= 3:
            buckets["0-3d"] += 1
        elif v <= 14:
            buckets["4-14d"] += 1
        elif v <= 30:
            buckets["15-30d"] += 1
        elif v <= 60:
            buckets["31-60d"] += 1
        else:
            buckets["60d+"] += 1
    return buckets


def _los_buckets(values: list[int]) -> dict:
    buckets = {"1 night": 0, "2-3 nights": 0, "4-7 nights": 0, "8+ nights": 0}
    for v in values:
        if v <= 1:
            buckets["1 night"] += 1
        elif v <= 3:
            buckets["2-3 nights"] += 1
        elif v <= 7:
            buckets["4-7 nights"] += 1
        else:
            buckets["8+ nights"] += 1
    return buckets


def booking_behavior(db: Session, branch_id: UUID, today: date, days: int = 7) -> dict:
    """Cancellation % / lead time / LOS — last calendar week vs prev week.

    Filtered on **check_in_date** (i.e. cohort = "stays scheduled for
    last week"). That's deliberate: a cancellation rate measured by
    stay date tells operators how many of last week's beds actually got
    occupied. If you want "what % of bookings made last week got later
    cancelled" use the CRM/marketing-activity views instead — those
    filter on reservation_date.

    Returns this-week + prev-week buckets + their WoW deltas so the
    renderer can show side-by-side comparisons.
    """
    cutoff, end_date = last_week_range(today)
    prev_end = cutoff - timedelta(days=1)
    prev_start = prev_end - timedelta(days=6)

    def _aggregate(d_from: date, d_to: date) -> dict:
        # Cancellation % by source_category (compute in Python — small N)
        all_rows = db.query(
            Reservation.source_category, Reservation.status,
        ).filter(
            Reservation.branch_id == branch_id,
            Reservation.check_in_date >= d_from,
            Reservation.check_in_date <= d_to,
        ).all()

        by_source: dict = {}
        for r in all_rows:
            sc = r.source_category or "Unknown"
            d = by_source.setdefault(sc, {"total": 0, "cancelled": 0})
            d["total"] += 1
            if (r.status or "").strip().lower() in _EXCLUDED_STATUSES:
                d["cancelled"] += 1

        total_all = sum(d["total"] for d in by_source.values())
        cxl_all = sum(d["cancelled"] for d in by_source.values())
        overall_pct = round(cxl_all / total_all * 100, 2) if total_all > 0 else None

        # Lead time / LOS (non-cancelled only)
        rows = db.query(
            Reservation.reservation_date,
            Reservation.check_in_date,
            Reservation.check_out_date,
            Reservation.nights,
        ).filter(
            Reservation.branch_id == branch_id,
            Reservation.check_in_date >= d_from,
            Reservation.check_in_date <= d_to,
            ~func.lower(func.coalesce(Reservation.status, "")).in_(list(_EXCLUDED_STATUSES)),
        ).all()

        lead_times: list[int] = []
        los_values: list[int] = []
        for r in rows:
            if r.reservation_date and r.check_in_date:
                lt = (r.check_in_date - r.reservation_date).days
                if lt >= 0:
                    lead_times.append(lt)
            if r.nights:
                los_values.append(int(r.nights))
            elif r.check_in_date and r.check_out_date:
                los_values.append((r.check_out_date - r.check_in_date).days)

        return {
            "by_source": by_source,
            "overall_cxl_pct": overall_pct,
            "total_all": total_all,
            "cxl_all": cxl_all,
            "lead_times": lead_times,
            "los_values": los_values,
            "lead_time_avg": round(sum(lead_times) / len(lead_times), 1) if lead_times else None,
            "los_avg": round(sum(los_values) / len(los_values), 2) if los_values else None,
        }

    this_w = _aggregate(cutoff, end_date)
    prev_w = _aggregate(prev_start, prev_end)

    # cancellation_by_source — emit one row per source with this+prev pcts
    all_sources = sorted(
        set(this_w["by_source"].keys()) | set(prev_w["by_source"].keys()),
        key=lambda s: -(this_w["by_source"].get(s, {}).get("total", 0)),
    )
    cancellation_by_source = []
    for sc in all_sources:
        t = this_w["by_source"].get(sc, {"total": 0, "cancelled": 0})
        p = prev_w["by_source"].get(sc, {"total": 0, "cancelled": 0})
        t_pct = round(t["cancelled"] / t["total"] * 100, 2) if t["total"] > 0 else None
        p_pct = round(p["cancelled"] / p["total"] * 100, 2) if p["total"] > 0 else None
        cancellation_by_source.append({
            "source_category": sc,
            "total": t["total"],
            "cancelled": t["cancelled"],
            "pct": t_pct,
            "prev_total": p["total"],
            "prev_pct": p_pct,
            # pp delta: e.g. 38.54% this week - 32.10% prev = +6.44pp
            "pp_delta": (round(t_pct - p_pct, 2) if (t_pct is not None and p_pct is not None) else None),
        })

    return {
        "window_days": (end_date - cutoff).days + 1,
        "window_start": cutoff.isoformat(),
        "window_end": end_date.isoformat(),
        "prev_window_start": prev_start.isoformat(),
        "prev_window_end": prev_end.isoformat(),
        "date_filter_col": "check_in_date",
        "cancellation_overall_pct": this_w["overall_cxl_pct"],
        "cancellation_overall_pct_prev": prev_w["overall_cxl_pct"],
        "cancellation_overall_pp_delta": (
            round(this_w["overall_cxl_pct"] - prev_w["overall_cxl_pct"], 2)
            if (this_w["overall_cxl_pct"] is not None
                and prev_w["overall_cxl_pct"] is not None) else None
        ),
        "cancellation_by_source": cancellation_by_source,
        "lead_time_avg_days": this_w["lead_time_avg"],
        "lead_time_avg_days_prev": prev_w["lead_time_avg"],
        "lead_time_wow_pct": _pct_change(this_w["lead_time_avg"], prev_w["lead_time_avg"]),
        "lead_time_buckets": _lead_time_buckets(this_w["lead_times"]),
        "lead_time_samples": len(this_w["lead_times"]),
        "los_avg_nights": this_w["los_avg"],
        "los_avg_nights_prev": prev_w["los_avg"],
        "los_wow_pct": _pct_change(this_w["los_avg"], prev_w["los_avg"]),
        "los_buckets": _los_buckets(this_w["los_values"]),
        "los_samples": len(this_w["los_values"]),
    }


# ── 4. Channel mix ──────────────────────────────────────────────────────────

def channel_mix(db: Session, branch_id: UUID, today: date, days: int = 7) -> dict:
    """Room-nights + revenue share by source_category and by specific
    source — last calendar week vs prev calendar week.

    Filtered on **check_in_date** (cohort = stays last week). Same date
    column as booking_behavior to keep both views consistent. Revenue
    excludes the usual non-paying sources (Blogger / House Use /
    Special Case).
    """
    cutoff, end_date = last_week_range(today)
    prev_end = cutoff - timedelta(days=1)
    prev_start = prev_end - timedelta(days=6)

    def _category_aggregate(d_from: date, d_to: date) -> dict:
        rows = (
            _reservations_base(db, branch_id)
            .filter(
                Reservation.check_in_date >= d_from,
                Reservation.check_in_date <= d_to,
            )
            .with_entities(
                Reservation.source_category,
                func.coalesce(func.sum(Reservation.nights), 0),
                func.coalesce(func.sum(Reservation.grand_total_native), 0),
            )
            .group_by(Reservation.source_category)
            .all()
        )
        return {
            (r[0] or "Unknown"): {
                "nights": int(r[1] or 0),
                "revenue": float(r[2] or 0),
            }
            for r in rows
        }

    def _source_aggregate(d_from: date, d_to: date) -> dict:
        rows = (
            _reservations_base(db, branch_id)
            .filter(
                Reservation.check_in_date >= d_from,
                Reservation.check_in_date <= d_to,
            )
            .with_entities(
                Reservation.source,
                func.coalesce(func.sum(Reservation.nights), 0),
                func.coalesce(func.sum(Reservation.grand_total_native), 0),
            )
            .group_by(Reservation.source)
            .all()
        )
        return {
            (r[0] or "Unknown"): {
                "nights": int(r[1] or 0),
                "revenue": float(r[2] or 0),
            }
            for r in rows
        }

    cat_this = _category_aggregate(cutoff, end_date)
    cat_prev = _category_aggregate(prev_start, prev_end)
    src_this = _source_aggregate(cutoff, end_date)
    src_prev = _source_aggregate(prev_start, prev_end)

    total_nights = sum(d["nights"] for d in cat_this.values())
    total_rev = sum(d["revenue"] for d in cat_this.values())
    prev_total_nights = sum(d["nights"] for d in cat_prev.values())
    prev_total_rev = sum(d["revenue"] for d in cat_prev.values())

    blank = {"nights": 0, "revenue": 0}
    categories = []
    for cat in set(cat_this.keys()) | set(cat_prev.keys()):
        t = cat_this.get(cat, blank)
        p = cat_prev.get(cat, blank)
        categories.append({
            "source_category": cat,
            "room_nights": t["nights"],
            "revenue_native": round(t["revenue"], 2),
            "nights_share_pct": round(t["nights"] / total_nights * 100, 2) if total_nights else None,
            "revenue_share_pct": round(t["revenue"] / total_rev * 100, 2) if total_rev else None,
            "prev_room_nights": p["nights"],
            "prev_revenue_native": round(p["revenue"], 2),
            "wow_nights_pct": _pct_change(t["nights"], p["nights"]),
            "wow_revenue_pct": _pct_change(t["revenue"], p["revenue"]),
        })
    categories.sort(key=lambda x: -x["room_nights"])

    # Per-source breakdown (top 10 by current-week nights)
    sources_top = sorted(src_this.items(), key=lambda x: -x[1]["nights"])[:10]
    sources = []
    for src, t in sources_top:
        p = src_prev.get(src, blank)
        sources.append({
            "source": src,
            "room_nights": t["nights"],
            "revenue_native": round(t["revenue"], 2),
            "nights_share_pct": round(t["nights"] / total_nights * 100, 2) if total_nights else None,
            "prev_room_nights": p["nights"],
            "wow_nights_pct": _pct_change(t["nights"], p["nights"]),
        })

    # Direct-booking trend — last 3 full months vs this month MTD
    direct_trend = []
    for offset in range(3, -1, -1):
        ref = today.replace(day=1) - timedelta(days=offset * 30)
        month_start = ref.replace(day=1)
        month_end = date(month_start.year, month_start.month,
                         calendar.monthrange(month_start.year, month_start.month)[1])
        month_end = min(month_end, today)
        month_rows = _reservations_base(db, branch_id).filter(
            Reservation.check_in_date >= month_start,
            Reservation.check_in_date <= month_end,
        ).with_entities(
            Reservation.source_category,
            func.coalesce(func.sum(Reservation.nights), 0),
        ).group_by(Reservation.source_category).all()
        m_total = sum(int(r[1] or 0) for r in month_rows)
        m_direct = sum(int(r[1] or 0) for r in month_rows if (r[0] or "").lower() == "direct")
        direct_trend.append({
            "label": f"{month_start.strftime('%b %Y')}",
            "direct_nights": m_direct,
            "total_nights": m_total,
            "direct_pct": round(m_direct / m_total * 100, 2) if m_total > 0 else None,
        })

    return {
        "window_days": (end_date - cutoff).days + 1,
        "window_start": cutoff.isoformat(),
        "window_end": end_date.isoformat(),
        "prev_window_start": prev_start.isoformat(),
        "prev_window_end": prev_end.isoformat(),
        "date_filter_col": "check_in_date",
        "total_nights": total_nights,
        "total_revenue_native": round(total_rev, 2),
        "prev_total_nights": prev_total_nights,
        "prev_total_revenue_native": round(prev_total_rev, 2),
        "wow_total_nights_pct": _pct_change(total_nights, prev_total_nights),
        "wow_total_revenue_pct": _pct_change(total_rev, prev_total_rev),
        "categories": categories,
        "top_sources": sources,
        "direct_trend": direct_trend,
    }


# ── 5. Country insights ──────────────────────────────────────────────────────

def country_insights(db: Session, branch_id: UUID, today: date,
                     days: int = 30, limit: int = 10) -> dict:
    """Top-N countries with WoW / 30d / YoY deltas, in TWO views.

    Per feedback (2026-05-04) the email previously had three overlapping
    country sections (Top markets chip, Growing markets chip, Country
    Intel chip + Country Insights detail with growing/shrinking/emerging).
    They've been collapsed into a single block with two tables:

      - by_booking_date: filtered on reservation_date (Date Booked)
        — measures how marketing activity is converting RIGHT NOW
      - by_checkin_date: filtered on check_in_date (Stay Date)
        — measures actual stay volume coming in

    Each row shows three deltas:
      - wow_pct: last 7d vs prev 7d
      - d30_pct: last 30d vs prior 30d (30..60 days ago)
      - yoy_pct: last 30d vs same 30d window in prior year

    Sorted by last-30d bookings (booking-date table) — same ranking is
    used for the check-in table so the two read consistently.
    """
    last_7_end = today
    last_7_start = today - timedelta(days=6)
    prev_7_end = last_7_start - timedelta(days=1)
    prev_7_start = prev_7_end - timedelta(days=6)

    last_30_end = today
    last_30_start = today - timedelta(days=29)
    prev_30_end = last_30_start - timedelta(days=1)
    prev_30_start = prev_30_end - timedelta(days=29)

    # YoY: same 30-day window in the prior calendar year. replace(year=...)
    # may raise on Feb 29 — fall through to a clamped date when that hits.
    try:
        yoy_end = today.replace(year=today.year - 1)
    except ValueError:
        yoy_end = today.replace(year=today.year - 1, day=28)
    yoy_start = yoy_end - timedelta(days=29)

    def _stats(date_col, d_from: date, d_to: date) -> dict:
        # GROUP BY guest_country_code (indexed via idx_reservations_country_code)
        # so each of the 10 windows below can use an index scan instead of a
        # sort on raw guest_country. MIN(guest_country) supplies a display
        # name in the same row. "Unknown" rows are filtered in Python since
        # N is small post-aggregation — keeping that filter in SQL via
        # LOWER(...) LIKE '%unknown%' on raw text defeated every index and
        # was the main statement_timeout culprit.
        rows = db.query(
            func.min(Reservation.guest_country).label("country"),
            Reservation.guest_country_code,
            func.count(Reservation.id).label("bookings"),
            func.coalesce(func.sum(Reservation.nights), 0).label("nights"),
            func.coalesce(func.sum(Reservation.grand_total_native), 0).label("rev"),
        ).filter(
            Reservation.branch_id == branch_id,
            date_col >= d_from,
            date_col <= d_to,
            Reservation.guest_country_code.isnot(None),
            ~func.lower(func.coalesce(Reservation.status, "")).in_(list(_EXCLUDED_STATUSES)),
        ).group_by(Reservation.guest_country_code).all()
        out = {}
        for r in rows:
            name = r.country or ""
            if "unknown" in name.lower():
                continue
            out[r.guest_country_code] = {
                "display": name,
                "bookings": int(r.bookings or 0),
                "nights": int(r.nights or 0),
                "revenue": float(r.rev or 0),
            }
        return out

    def _build_table(date_col):
        # Six per-country dicts share `date_col` so booking-date and
        # check-in-date tables stay 100% comparable apples-to-apples.
        # Lookups key on guest_country_code (stable across windows); the
        # display name comes from whichever window first surfaced this code
        # in the last_30 ranking.
        last_30 = _stats(date_col, last_30_start, last_30_end)
        prev_30 = _stats(date_col, prev_30_start, prev_30_end)
        last_7 = _stats(date_col, last_7_start, last_7_end)
        prev_7 = _stats(date_col, prev_7_start, prev_7_end)
        yoy = _stats(date_col, yoy_start, yoy_end)

        out = []
        ranked = sorted(last_30.items(), key=lambda x: -x[1]["bookings"])[:limit]
        for code, cur in ranked:
            wow_pct = _pct_change(
                last_7.get(code, {}).get("bookings", 0),
                prev_7.get(code, {}).get("bookings", 0),
            )
            d30_pct = _pct_change(
                cur["bookings"],
                prev_30.get(code, {}).get("bookings", 0),
            )
            yoy_cur = yoy.get(code, {}).get("bookings", 0)
            yoy_pct = _pct_change(cur["bookings"], yoy_cur) if yoy_cur > 0 else None
            adr = round(cur["revenue"] / cur["nights"], 2) if cur["nights"] > 0 else None
            out.append({
                "country": cur["display"],
                "bookings": cur["bookings"],
                "nights": cur["nights"],
                "revenue_native": round(cur["revenue"], 2),
                "adr_native": adr,
                "wow_pct": wow_pct,
                "d30_pct": d30_pct,
                "yoy_pct": yoy_pct,
            })
        return out

    return {
        "window_days": (last_30_end - last_30_start).days + 1,
        "windows": {
            "last_7": [last_7_start.isoformat(), last_7_end.isoformat()],
            "prev_7": [prev_7_start.isoformat(), prev_7_end.isoformat()],
            "last_30": [last_30_start.isoformat(), last_30_end.isoformat()],
            "prev_30": [prev_30_start.isoformat(), prev_30_end.isoformat()],
            "yoy": [yoy_start.isoformat(), yoy_end.isoformat()],
        },
        "by_booking_date": _build_table(Reservation.reservation_date),
        "by_checkin_date": _build_table(Reservation.check_in_date),
    }


# ── 6. Ad Budget Optimizer (6-step workflow) ────────────────────────────────

def _country_lead_time(db: Session, branch_id: UUID, country: str, today: date,
                       days: int = 90) -> Optional[int]:
    """Average lead time (days) for a given country, last `days` days."""
    cutoff = today - timedelta(days=days)
    rows = db.query(
        Reservation.reservation_date, Reservation.check_in_date,
    ).filter(
        Reservation.branch_id == branch_id,
        func.lower(Reservation.guest_country) == country.lower(),
        Reservation.check_in_date >= cutoff,
        Reservation.reservation_date.isnot(None),
        ~func.lower(func.coalesce(Reservation.status, "")).in_(list(_EXCLUDED_STATUSES)),
    ).all()

    deltas = [(r.check_in_date - r.reservation_date).days
              for r in rows if r.reservation_date and r.check_in_date]
    deltas = [d for d in deltas if d >= 0]
    if not deltas:
        return None
    return int(round(sum(deltas) / len(deltas)))


def _window_occ(db: Session, branch_id: UUID, window_start: date, window_end: date) -> Optional[float]:
    """Average daily_metrics.occ_pct across the window (0..1), or None if no data."""
    row = db.query(func.avg(DailyMetrics.occ_pct)).filter(
        DailyMetrics.branch_id == branch_id,
        DailyMetrics.date >= window_start,
        DailyMetrics.date <= window_end,
    ).scalar()
    return float(row) if row else None


def _country_holidays(db: Session, country: str, window_start: date, window_end: date) -> list[dict]:
    """Holidays in the target window matching country (by name, case-insensitive)."""
    rows = db.query(HolidayCalendar).filter(
        func.lower(HolidayCalendar.country_name) == country.lower(),
    ).all()

    out = []
    for h in rows:
        # Compare month/day ranges against window (ignore year for recurring)
        try:
            h_start = date(window_start.year, h.month_start, h.day_start or 1)
            h_end = date(window_start.year, h.month_end, h.day_end or h.day_start or 1)
        except ValueError:
            continue
        if h_end < window_start or h_start > window_end:
            continue
        out.append({
            "name": h.holiday_name,
            "type": h.holiday_type,
            "propensity": h.travel_propensity,
            "start": h_start.isoformat(),
            "end": h_end.isoformat(),
        })
    return out


def _country_campaigns(db: Session, branch_id: UUID, country: str,
                       month_start: date, month_end: date) -> dict:
    """Summarize active campaigns/ads for this country this month.

    Step 5 of the 6-step framework — surfaces creative metadata so the
    email reader can see angle / audience / USP alongside performance:
      - target_audience  (Solo / Couple / Family / Business / High Intent)
      - funnel_stage     (TOF / MOF / BOF)
      - angle name + hook + first keypoint (USP) via ad_angle_id join
      - first 140 chars of ad_body (primary text from creative)
    """
    rows = db.query(
        AdsPerformance.campaign_name,
        AdsPerformance.adset_name,
        AdsPerformance.ad_name,
        AdsPerformance.channel,
        AdsPerformance.target_audience,
        AdsPerformance.funnel_stage,
        AdsPerformance.ad_body,
        AdsPerformance.ad_angle_id,
        AdAngle.name.label("angle_name"),
        AdAngle.hook_type.label("hook_type"),
        AdAngle.keypoint_1.label("usp"),
        AdAngle.verdict.label("angle_verdict"),
        func.coalesce(func.sum(AdsPerformance.cost_native), 0).label("cost"),
        func.coalesce(func.sum(AdsPerformance.impressions), 0).label("impr"),
        func.coalesce(func.sum(AdsPerformance.clicks), 0).label("clicks"),
        func.coalesce(func.sum(AdsPerformance.bookings), 0).label("bookings"),
        func.coalesce(func.sum(AdsPerformance.revenue_native), 0).label("rev"),
    ).outerjoin(
        AdAngle, AdAngle.id == AdsPerformance.ad_angle_id,
    ).filter(
        AdsPerformance.branch_id == branch_id,
        func.lower(AdsPerformance.target_country) == country.lower(),
        AdsPerformance.date_from >= month_start,
        AdsPerformance.date_from <= month_end,
        AdsPerformance.cost_native > 0,
    ).group_by(
        AdsPerformance.campaign_name, AdsPerformance.adset_name,
        AdsPerformance.ad_name, AdsPerformance.channel,
        AdsPerformance.target_audience, AdsPerformance.funnel_stage,
        AdsPerformance.ad_body, AdsPerformance.ad_angle_id,
        AdAngle.name, AdAngle.hook_type, AdAngle.keypoint_1, AdAngle.verdict,
    ).order_by(func.sum(AdsPerformance.cost_native).desc()).limit(5).all()

    campaigns = []
    total_cost = total_rev = 0.0
    total_impr = total_clicks = total_bookings = 0
    for r in rows:
        cost = float(r.cost or 0)
        impr = int(r.impr or 0)
        clicks = int(r.clicks or 0)
        bookings = int(r.bookings or 0)
        rev = float(r.rev or 0)
        total_cost += cost
        total_impr += impr
        total_clicks += clicks
        total_bookings += bookings
        total_rev += rev
        ad_body_excerpt = (r.ad_body or "").strip().replace("\n", " ")
        if len(ad_body_excerpt) > 140:
            ad_body_excerpt = ad_body_excerpt[:137] + "…"
        campaigns.append({
            "campaign": r.campaign_name,
            "adset": r.adset_name,
            "ad_name": r.ad_name,
            "channel": r.channel,
            "target_audience": r.target_audience,
            "funnel_stage": r.funnel_stage,
            "angle_name": r.angle_name,
            "hook_type": r.hook_type,
            "usp": (r.usp or "").strip()[:120] if r.usp else None,
            "angle_verdict": r.angle_verdict,
            "ad_body_excerpt": ad_body_excerpt or None,
            "cost": round(cost, 2),
            "impressions": impr,
            "clicks": clicks,
            "bookings": bookings,
            "revenue": round(rev, 2),
            "ctr_pct": round(clicks / impr * 100, 2) if impr > 0 else None,
            "cvr_pct": round(bookings / clicks * 100, 2) if clicks > 0 else None,
            "cpa": round(cost / bookings, 2) if bookings > 0 else None,
            "roas": round(rev / cost, 2) if cost > 0 else None,
        })

    return {
        "campaigns": campaigns,
        "total_cost": round(total_cost, 2),
        "total_impressions": total_impr,
        "total_clicks": total_clicks,
        "total_bookings": total_bookings,
        "total_revenue": round(total_rev, 2),
        "overall_ctr_pct": round(total_clicks / total_impr * 100, 2) if total_impr else None,
        "overall_cvr_pct": round(total_bookings / total_clicks * 100, 2) if total_clicks else None,
        "overall_cpa": round(total_cost / total_bookings, 2) if total_bookings else None,
        "overall_roas": round(total_rev / total_cost, 2) if total_cost > 0 else None,
    }


def ad_budget_optimizer(db: Session, branch_id: UUID, today: date,
                        total_rooms: int) -> list[dict]:
    """
    6-step per-country optimization. Only countries with ad spend this month
    (1st → yesterday).
    """
    month_start = today.replace(day=1)
    yesterday = today - timedelta(days=1)
    if yesterday < month_start:
        yesterday = month_start

    # Countries currently running ads
    country_rows = db.query(
        AdsPerformance.target_country,
        func.coalesce(func.sum(AdsPerformance.cost_native), 0).label("spend"),
    ).filter(
        AdsPerformance.branch_id == branch_id,
        AdsPerformance.target_country.isnot(None),
        AdsPerformance.target_country != "",
        AdsPerformance.date_from >= month_start,
        AdsPerformance.date_from <= yesterday,
        AdsPerformance.cost_native > 0,
    ).group_by(AdsPerformance.target_country).order_by(
        func.sum(AdsPerformance.cost_native).desc()
    ).all()

    if not country_rows:
        return []

    # Predicted OCC (manual input) — compare actual window OCC against this to
    # decide BOOST vs STABILIZE.
    kpi_row = db.query(KPITarget).filter_by(
        branch_id=branch_id, year=today.year, month=today.month,
    ).first()
    predicted_occ = float(kpi_row.predicted_occ_pct) if (kpi_row and kpi_row.predicted_occ_pct) else None

    results = []
    for cr in country_rows:
        country = cr.target_country
        spend = float(cr.spend or 0)

        # Step 1 — Lead time
        lead = _country_lead_time(db, branch_id, country, today, days=90)
        if lead is None:
            lead = 14  # default fallback

        # Step 2 — Target window = today + lead ± 7 days
        win_start = today + timedelta(days=max(lead - 7, 0))
        win_end = today + timedelta(days=lead + 7)

        # Step 3 — OCC in window + recommendation
        window_occ = _window_occ(db, branch_id, win_start, win_end)
        if window_occ is not None and predicted_occ is not None:
            gap = window_occ - predicted_occ  # negative → below target → boost
            if gap < -0.05:
                action = "BOOST"
                action_reason = f"Window OCC {round(window_occ*100,2)}% is {round(abs(gap)*100,2)}pp below predicted {round(predicted_occ*100,2)}%"
            elif gap > 0.05:
                action = "STABILIZE"
                action_reason = f"Window OCC {round(window_occ*100,2)}% is {round(gap*100,2)}pp above predicted — avoid over-selling"
            else:
                action = "MAINTAIN"
                action_reason = f"Window OCC {round(window_occ*100,2)}% is on pace with predicted {round(predicted_occ*100,2)}%"
        elif window_occ is not None:
            action = "REVIEW"
            action_reason = f"Window OCC {round(window_occ*100,2)}% — predicted OCC not set for this month"
        else:
            action = "REVIEW"
            action_reason = "No OCC data for window (future dates with no bookings yet)"

        # Step 4 — Holidays in window
        holidays = _country_holidays(db, country, win_start, win_end)

        # Step 5 — Current campaigns
        campaigns = _country_campaigns(db, branch_id, country, month_start, yesterday)

        # Step 6 — Rule-based recommendations
        recs = []
        if action == "BOOST":
            recs.append(f"Increase budget 15–25% on {country} campaigns with ROAS ≥ 2.0")
        elif action == "STABILIZE":
            recs.append(f"Hold budget flat; prioritize rate integrity over ad scaling")
        elif action == "MAINTAIN":
            recs.append(f"Keep current budget; monitor daily OCC trend in window")
        else:
            recs.append(f"Review {country} manually — data gap")

        high_prop_holiday = [h for h in holidays if h.get("propensity") == "HIGH"]
        if high_prop_holiday:
            names = ", ".join(h["name"] for h in high_prop_holiday[:2])
            recs.append(f"Leverage HIGH-propensity holiday(s) in window: {names}")
            # Audience-angle suggestions inferred from holiday name keywords
            joined = " ".join(h.get("name", "") for h in high_prop_holiday).lower()
            angle_hint = None
            if any(k in joined for k in ("valentine", "couple")):
                angle_hint = "Couple / Romantic getaway angle"
            elif any(k in joined for k in ("family", "children", "kids")):
                angle_hint = "Family / Multi-room angle"
            elif any(k in joined for k in ("national", "independence", "founding")):
                angle_hint = "Long weekend / Cultural travel angle"
            elif any(k in joined for k in ("new year", "lunar")):
                angle_hint = "Festive / Premium-stay angle"
            if angle_hint:
                recs.append(f"Test creative aligned to: {angle_hint}")

        # Flag under-performing campaigns (Step 6: refresh underperformers)
        lose_campaigns = [c for c in campaigns["campaigns"]
                          if c["roas"] is not None and c["roas"] < 1.0 and c["bookings"] < 2]
        if lose_campaigns:
            recs.append(f"Pause or refresh {len(lose_campaigns)} ads with ROAS<1.0 and <2 bookings")

        # Low CTR signal — Step 6: test new materials when current ones fail
        if (campaigns["overall_ctr_pct"] is not None
                and campaigns["overall_ctr_pct"] < 1.0
                and campaigns["total_impressions"] > 5000):
            recs.append("Overall CTR < 1.0% — test new creatives/angles for this market")

        # Reallocate-budget hint — Step 6: shift to top performer
        winners = [c for c in campaigns["campaigns"]
                   if c["roas"] is not None and c["roas"] >= 2.0 and c["bookings"] >= 2]
        if winners and lose_campaigns:
            top = max(winners, key=lambda c: c["roas"])
            short_id = (top["ad_name"] or top["adset"] or top["campaign"] or "")[:40]
            recs.append(f"Reallocate budget from losers → top performer "
                        f"({short_id}, ROAS {top['roas']:.2f}x)")

        # Audience diversification hint — when only one audience has spend
        audiences = {c.get("target_audience") for c in campaigns["campaigns"]
                     if c.get("target_audience")}
        if len(audiences) == 1 and campaigns["total_cost"] > 0:
            only_aud = next(iter(audiences))
            recs.append(f"Only '{only_aud}' audience active — test 1-2 alternative audiences")

        results.append({
            "country": country,
            "spend_mtd": round(spend, 2),
            "lead_time_days": lead,
            "window_start": win_start.isoformat(),
            "window_end": win_end.isoformat(),
            "window_occ_pct": round(window_occ * 100, 2) if window_occ is not None else None,
            "predicted_occ_pct": round(predicted_occ * 100, 2) if predicted_occ else None,
            "action": action,
            "action_reason": action_reason,
            "holidays": holidays,
            "campaigns": campaigns,
            "recommendations": recs,
        })

    return results


# ── 7. Paid Ads — last 7d vs prior 7d, by channel/country/funnel ────────────

# Sources to EXCLUDE when computing CRM/KOL revenue from reservations
# (non-paying segments — already filtered globally at ingest, but enforced
# here too because CRM_filter / KOL filter only check room_type/rate_plan).
_NON_PAYING_SOURCES = {"blogger", "house use", "houseuse", "special case"}


def _ads_window_aggregate(db: Session, branch_id: UUID,
                          d_from: date, d_to: date) -> dict:
    """Aggregate daily-grain ads rows for [d_from, d_to]."""
    row = db.query(
        func.coalesce(func.sum(AdsPerformance.cost_native), 0),
        func.coalesce(func.sum(AdsPerformance.impressions), 0),
        func.coalesce(func.sum(AdsPerformance.clicks), 0),
        func.coalesce(func.sum(AdsPerformance.leads), 0),
        func.coalesce(func.sum(AdsPerformance.bookings), 0),
        func.coalesce(func.sum(AdsPerformance.revenue_native), 0),
    ).filter(
        AdsPerformance.branch_id == branch_id,
        AdsPerformance.grain == "daily",
        AdsPerformance.date_from >= d_from,
        AdsPerformance.date_from <= d_to,
    ).one()

    cost = float(row[0] or 0)
    impr = int(row[1] or 0)
    clicks = int(row[2] or 0)
    leads = int(row[3] or 0)
    bookings = int(row[4] or 0)
    rev = float(row[5] or 0)

    return {
        "cost": round(cost, 2),
        "impressions": impr,
        "clicks": clicks,
        "leads": leads,
        "bookings": bookings,
        "revenue": round(rev, 2),
        "ctr_pct": round(clicks / impr * 100, 2) if impr > 0 else None,
        "cvr_pct": round(bookings / clicks * 100, 2) if clicks > 0 else None,
        "cpa": round(cost / bookings, 2) if bookings > 0 else None,
        "roas": round(rev / cost, 2) if cost > 0 else None,
    }


def _build_country_perf(db: Session, branch: Branch,
                        week_start: date, week_end: date,
                        prev_start: date, prev_end: date,
                        top_n: int = 10) -> list[dict]:
    """Per-country full ads performance with WoW deltas.

    Sourced from Ads Platform's ``/api/export/spend/daily-by-country``
    endpoint (added 2026-05-08, mirrors ``/spend/daily`` pattern).
    Server-side aggregation at (date × platform × account × country) so
    a single HTTP call covers the whole week for the branch. We then
    aggregate per-country in Python.

    Each row carries: spend, impressions, clicks, conversions (= bookings),
    revenue, plus computed ROAS / CTR / CPA, with WoW deltas for spend /
    revenue / ROAS / bookings and pp delta for CTR.

    Sorted by current-week spend desc, capped at top_n. Returns [] when
    the upstream call fails so the email shows the empty-state message
    rather than crashing.
    """
    from app.services.ads_platform import (
        AdsPlatformClient, AdsPlatformError, branch_slug_for,
    )

    slug = branch_slug_for(branch)

    def _aggregate(d_from: date, d_to: date) -> dict:
        try:
            client = AdsPlatformClient()
            rows = client.get_spend_daily_by_country(
                date_from=d_from.isoformat(),
                date_to=d_to.isoformat(),
                branch=slug,
            )
        except (AdsPlatformError, Exception) as e:
            logger.warning(
                "fetch country spend failed for branch=%s window=%s..%s: %s",
                slug, d_from, d_to, e,
            )
            return {}

        # Sum across (date, platform, account) per country
        by_country: dict = {}
        for r in rows or []:
            country = (r.get("country") or "").strip().upper() or "UNKNOWN"
            d = by_country.setdefault(country, {
                "spend": 0.0, "impressions": 0, "clicks": 0,
                "conversions": 0, "revenue": 0.0,
            })
            d["spend"] += float(r.get("spend") or 0)
            d["impressions"] += int(r.get("impressions") or 0)
            d["clicks"] += int(r.get("clicks") or 0)
            d["conversions"] += int(r.get("conversions") or 0)
            d["revenue"] += float(r.get("revenue") or 0)
        return by_country

    this = _aggregate(week_start, week_end)
    prev = _aggregate(prev_start, prev_end)

    blank = {"spend": 0, "impressions": 0, "clicks": 0, "conversions": 0, "revenue": 0}
    rows: list[dict] = []
    for country in set(this.keys()) | set(prev.keys()):
        t = this.get(country, blank)
        p = prev.get(country, blank)

        roas = round(t["revenue"] / t["spend"], 2) if t["spend"] > 0 else None
        prev_roas = (p["revenue"] / p["spend"]) if p["spend"] > 0 else None
        ctr = round(t["clicks"] / t["impressions"] * 100, 2) if t["impressions"] > 0 else None
        prev_ctr = (p["clicks"] / p["impressions"] * 100) if p["impressions"] > 0 else None
        cpa = round(t["spend"] / t["conversions"], 2) if t["conversions"] > 0 else None

        rows.append({
            "country": country,
            "spend": round(t["spend"], 2),
            "impressions": t["impressions"],
            "clicks": t["clicks"],
            "bookings": t["conversions"],  # conversions = bookings in Ads Platform
            "revenue": round(t["revenue"], 2),
            "roas": roas,
            "ctr_pct": ctr,
            "cpa": cpa,
            "prev_spend": round(p["spend"], 2),
            "prev_revenue": round(p["revenue"], 2),
            "prev_bookings": p["conversions"],
            "wow_spend_pct": _pct_change(t["spend"], p["spend"]),
            "wow_revenue_pct": _pct_change(t["revenue"], p["revenue"]),
            "wow_roas_pct": _pct_change(roas, prev_roas),
            "wow_bookings_pct": _pct_change(t["conversions"], p["conversions"]),
            "ctr_pp_delta": (round(ctr - prev_ctr, 2)
                              if (ctr is not None and prev_ctr is not None) else None),
        })
    # Sort by current-week spend desc (matches Ads Platform UI default)
    rows.sort(key=lambda x: -x["spend"])
    return rows[:top_n]


def paid_ads_section(db: Session, branch: Branch, today: date,
                     days: int = 7) -> dict:
    """Paid Ads snapshot — last calendar week vs the week before it
    (apples-to-apples 7d vs 7d), plus channel breakdown, By Country
    table, and activity log. Sourced from local ads_performance for
    channel totals (grain='daily') and Ads Platform's
    /api/export/spend/daily-by-country for the country breakdown.
    """
    branch_id = branch.id
    week_start, week_end = last_week_range(today)
    prev_end = week_start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=6)

    last_week = _ads_window_aggregate(db, branch_id, week_start, week_end)
    prev_week = _ads_window_aggregate(db, branch_id, prev_start, prev_end)

    # By channel — last week + prev week so we can show WoW deltas
    def _channel_aggregate(d_from, d_to):
        rows = db.query(
            AdsPerformance.channel,
            func.coalesce(func.sum(AdsPerformance.cost_native), 0),
            func.coalesce(func.sum(AdsPerformance.impressions), 0),
            func.coalesce(func.sum(AdsPerformance.clicks), 0),
            func.coalesce(func.sum(AdsPerformance.bookings), 0),
            func.coalesce(func.sum(AdsPerformance.revenue_native), 0),
        ).filter(
            AdsPerformance.branch_id == branch_id,
            AdsPerformance.grain == "daily",
            AdsPerformance.date_from >= d_from,
            AdsPerformance.date_from <= d_to,
        ).group_by(AdsPerformance.channel).all()
        return {
            (r[0] or "Unknown"): {
                "cost": float(r[1] or 0),
                "impressions": int(r[2] or 0),
                "clicks": int(r[3] or 0),
                "bookings": int(r[4] or 0),
                "revenue": float(r[5] or 0),
            }
            for r in rows
        }

    ch_this = _channel_aggregate(week_start, week_end)
    ch_prev = _channel_aggregate(prev_start, prev_end)
    blank = {"cost": 0, "impressions": 0, "clicks": 0, "bookings": 0, "revenue": 0}

    by_channel = []
    for channel in set(ch_this.keys()) | set(ch_prev.keys()):
        t = ch_this.get(channel, blank)
        p = ch_prev.get(channel, blank)
        roas = round(t["revenue"] / t["cost"], 2) if t["cost"] > 0 else None
        prev_roas = (p["revenue"] / p["cost"]) if p["cost"] > 0 else None
        ctr = round(t["clicks"] / t["impressions"] * 100, 2) if t["impressions"] > 0 else None
        prev_ctr = (p["clicks"] / p["impressions"] * 100) if p["impressions"] > 0 else None
        cvr = round(t["bookings"] / t["clicks"] * 100, 2) if t["clicks"] > 0 else None
        prev_cvr = (p["bookings"] / p["clicks"] * 100) if p["clicks"] > 0 else None
        by_channel.append({
            "channel": channel,
            "cost": round(t["cost"], 2),
            "impressions": t["impressions"],
            "clicks": t["clicks"],
            "bookings": t["bookings"],
            "revenue": round(t["revenue"], 2),
            "ctr_pct": ctr,
            "cvr_pct": cvr,
            "roas": roas,
            "wow_cost_pct": _pct_change(t["cost"], p["cost"]),
            "wow_impressions_pct": _pct_change(t["impressions"], p["impressions"]),
            # CTR / CVR are rates — pp deltas read more naturally than %
            "ctr_pp_delta": (round(ctr - prev_ctr, 2)
                              if (ctr is not None and prev_ctr is not None) else None),
            "cvr_pp_delta": (round(cvr - prev_cvr, 2)
                              if (cvr is not None and prev_cvr is not None) else None),
            "wow_bookings_pct": _pct_change(t["bookings"], p["bookings"]),
            "wow_roas_pct": _pct_change(roas, prev_roas),
        })
    by_channel.sort(key=lambda x: -x["cost"])

    # NOTE: by_funnel removed per feedback (2026-05-04) — funnel-stage
    # breakdown wasn't driving decisions and added clutter to the per-branch
    # card. Funnel data is still surfaced inside Ad Budget Optimizer.
    #
    # NOTE: activity_log removed per feedback (2026-05-18) — local diff on
    # ads_performance was broken (grain='ad' rows are metadata-only, no cost)
    # and the canonical Activity Log already lives in the Ads Platform
    # dashboard. Duplicating it into the email added no value.

    # By Country — replaces the old Top by ROAS / Underperformers tables.
    # Per feedback (2026-05-04): operators want a country-level slice that
    # mirrors the Ads Platform "By Country" view, not campaign-grain rows.
    by_country = _build_country_perf(
        db, branch, week_start, week_end, prev_start, prev_end, top_n=10,
    )

    return {
        "window_days": (week_end - week_start).days + 1,
        "window_start": week_start.isoformat(),
        "window_end": week_end.isoformat(),
        "last_week": last_week,
        "prev_week": prev_week,
        "wow_cost_pct": _pct_change(last_week["cost"], prev_week["cost"]),
        "wow_revenue_pct": _pct_change(last_week["revenue"], prev_week["revenue"]),
        "wow_roas_pct": _pct_change(last_week["roas"], prev_week["roas"]),
        "by_channel": by_channel,
        "by_country": by_country,
    }


# ── 8. KOL — pipeline, ROI, expiring usage rights ───────────────────────────

def kol_section(db: Session, branch_id: UUID, branch_name: str,
                today: date, days: int = 7) -> dict:
    """KOL monthly progress (Invited / Collaborated / Posted vs target).

    The email previously showed pipeline counts, stuck deliverables,
    expiring usage rights, ads-eligible KOLs, cost MTD, organic ROI etc.
    Per feedback (2026-05-04) all of that was stripped out — operators
    only want to see the monthly progress vs target from the KOL Engine
    public API.

    Targets fetched from KOL Engine: GET /api/public/kol-targets/{slug}
    (Bearer auth via KOL_PUBLIC_API_KEY). Cached 10 minutes so the 5
    per-branch passes within one report build hit the network once.

    The legacy fields (pipeline / stuck / expiring / available_for_ads /
    cost_mtd / organic_*) are still computed for backward compat with
    any other consumer, but the email no longer renders them.
    """
    week_start, week_end = last_week_range(today)
    expiry_horizon = today + timedelta(days=60)

    # KOL records for this branch
    kols = db.query(KOLRecord).filter(KOLRecord.branch_id == branch_id).all()

    # Posts published in last calendar week
    posts_this_week = sum(
        1 for k in kols
        if k.published_date and week_start <= k.published_date <= week_end
    )

    # Pipeline counts (deliverable_status)
    pipeline = {"Not Started": 0, "In Progress": 0, "Editing": 0, "Done": 0, "Other": 0}
    contract_open = 0
    for k in kols:
        s = (k.deliverable_status or "").strip()
        if s in pipeline:
            pipeline[s] += 1
        else:
            pipeline["Other"] += 1
        if (k.contract_status or "").strip().lower() in ("draft", "negotiating"):
            contract_open += 1

    # Stuck — In Progress for more than 14 days (using updated_at as proxy)
    stuck_threshold = today - timedelta(days=14)
    stuck = []
    for k in kols:
        if (k.deliverable_status or "").strip() == "In Progress":
            updated_dt = k.updated_at.date() if k.updated_at else None
            if updated_dt and updated_dt < stuck_threshold:
                stuck.append({
                    "kol_name": k.kol_name,
                    "nationality": k.kol_nationality,
                    "updated_at": updated_dt.isoformat(),
                    "days_stuck": (today - updated_dt).days,
                })
    stuck.sort(key=lambda x: -x["days_stuck"])

    # Expiring usage rights (within 60 days)
    expiring = []
    for k in kols:
        if k.usage_rights_expiry_date and today <= k.usage_rights_expiry_date <= expiry_horizon:
            expiring.append({
                "kol_name": k.kol_name,
                "expiry_date": k.usage_rights_expiry_date.isoformat(),
                "days_left": (k.usage_rights_expiry_date - today).days,
                "channel": k.paid_ads_channel,
            })
    expiring.sort(key=lambda x: x["days_left"])

    # Ads-eligible KOLs available (paid_ads_eligible=True AND ads_usage_status='Available')
    available_for_ads = []
    for k in kols:
        if not k.paid_ads_eligible:
            continue
        status = (k.ads_usage_status or "").strip().lower()
        if status == "available":
            available_for_ads.append({
                "kol_name": k.kol_name,
                "nationality": k.kol_nationality,
                "channel": k.paid_ads_channel,
                "expiry": k.usage_rights_expiry_date.isoformat() if k.usage_rights_expiry_date else None,
            })

    # Cost MTD (sum of cost_native for KOLs invited this month)
    month_start = today.replace(day=1)
    cost_row = db.query(
        func.coalesce(func.sum(KOLRecord.cost_native), 0),
    ).filter(
        KOLRecord.branch_id == branch_id,
        KOLRecord.invitation_date >= month_start,
        KOLRecord.invitation_date <= today,
    ).one()
    cost_mtd = float(cost_row[0] or 0)

    # Organic KOL bookings + revenue from reservations (room_type ILIKE '%KOL_%')
    # — last calendar week, by check_in_date.
    res_rows = db.query(
        func.count(Reservation.id),
        func.coalesce(func.sum(Reservation.nights), 0),
        func.coalesce(func.sum(Reservation.grand_total_native), 0),
    ).filter(
        Reservation.branch_id == branch_id,
        Reservation.room_type.ilike("%KOL_%"),
        Reservation.check_in_date >= week_start,
        Reservation.check_in_date <= week_end,
        ~func.lower(func.coalesce(Reservation.status, "")).in_(list(_EXCLUDED_STATUSES)),
        ~func.lower(func.coalesce(Reservation.source, "")).in_(list(_NON_PAYING_SOURCES)),
    ).one()

    organic_bookings = int(res_rows[0] or 0)
    organic_nights = int(res_rows[1] or 0)
    organic_revenue = float(res_rows[2] or 0)
    roi = round(organic_revenue / cost_mtd, 2) if cost_mtd > 0 else None

    # ── Monthly targets from KOL Engine public API ──────────────────────
    # Fetch the org-level payload (cached 10min) and pull out the row for
    # this branch via hotel_id. month=today.month so we always show the
    # current calendar month's progress.
    from app.config import settings as _settings
    from app.services.kol_engine import (
        fetch_kol_targets, resolve_hotel_id_from_branch_name,
    )
    targets_payload = fetch_kol_targets(
        base_url=_settings.KOL_ENGINE_URL,
        org_slug=_settings.KOL_TARGETS_ORG_SLUG,
        api_key=_settings.KOL_PUBLIC_API_KEY,
        year=today.year,
        month=today.month,
    )
    targets = None
    if targets_payload:
        hotel_id = resolve_hotel_id_from_branch_name(branch_name)
        period = targets_payload.get("period") or {}
        # Invited (Proactive) is org-wide (bucketed by KOL nationality), not
        # per-branch — pulled from invite_by_country. totals.invited_proactive
        # is the org total (also matches invite_by_country[country=null]).
        org_totals = targets_payload.get("totals") or {}
        invite_rows = targets_payload.get("invite_by_country") or []
        org_invited = org_totals.get("invited_proactive") or {}
        # Country breakdown rows = invite_by_country minus the org-total row
        # (country=null) since we surface that separately as org_invited.
        invite_by_country = [
            r for r in invite_rows if r.get("country") is not None
        ]
        if hotel_id:
            for br in (targets_payload.get("branches") or []):
                if br.get("hotel_id") == hotel_id:
                    targets = {
                        "period_label": period.get("label"),
                        "period_year": period.get("year") or today.year,
                        "period_month": period.get("month") or today.month,
                        "hotel_id": hotel_id,
                        "hotel_name": br.get("hotel_name"),
                        # Invited (Proactive) — org-wide, same on every branch
                        # email; per-branch invited from the API is stale and
                        # double-counts so we don't render it.
                        "org_invited": org_invited,
                        "invite_by_country": invite_by_country,
                        "collaborated": br.get("collaborated") or {},
                        "posted": br.get("posted") or {},
                    }
                    break
        # If hotel_id resolution failed, leave targets = None — the
        # renderer will show a "branch not mapped" hint.

    return {
        "window_days": (week_end - week_start).days + 1,
        "window_start": week_start.isoformat(),
        "window_end": week_end.isoformat(),
        "total_kols": len(kols),
        "posts_this_week": posts_this_week,
        "pipeline": pipeline,
        "contract_open": contract_open,
        "stuck": stuck[:5],
        "expiring": expiring[:5],
        "available_for_ads": available_for_ads[:5],
        "cost_mtd_native": round(cost_mtd, 2),
        "organic_bookings": organic_bookings,
        "organic_nights": organic_nights,
        "organic_revenue_native": round(organic_revenue, 2),
        "roi": roi,
        # NEW (2026-05-04): monthly progress from KOL Engine public API
        "targets": targets,
        "targets_unavailable_reason": None if targets else (
            "KOL Engine returned no targets for this branch — check "
            "KOL_PUBLIC_API_KEY / KOL_TARGETS_ORG_SLUG, or that the branch "
            "is mapped in KOL Engine."
        ),
    }


# ── 9. CRM (Email Marketing) — workflows + bulk + revenue attribution ───────

def _crm_revenue(db: Session, branch_id: UUID, d_from: date, d_to: date) -> dict:
    """Aggregate CRM bookings/revenue from reservations.

    Uses reservation_date (Date Booked) per team rule for marketing-activity
    measurement. Excludes non-paying sources.
    """
    row = db.query(
        func.count(Reservation.id),
        func.coalesce(func.sum(Reservation.nights), 0),
        func.coalesce(func.sum(Reservation.grand_total_native), 0),
    ).filter(
        Reservation.branch_id == branch_id,
        crm_reservation_filter(),
        Reservation.reservation_date >= d_from,
        Reservation.reservation_date <= d_to,
        ~func.lower(func.coalesce(Reservation.status, "")).in_(list(_EXCLUDED_STATUSES)),
        ~func.lower(func.coalesce(Reservation.source, "")).in_(list(_NON_PAYING_SOURCES)),
    ).one()
    return {
        "bookings": int(row[0] or 0),
        "nights": int(row[1] or 0),
        "revenue": round(float(row[2] or 0), 2),
    }


def _crm_revenue_by_rate_plan(
    db: Session, branch_id: UUID, d_from: date, d_to: date,
) -> list[dict]:
    """Per-rate-plan breakdown of CRM bookings/revenue in window.

    Groups by (rate_plan_name, room_type) so reservations with NULL
    rate_plan_name (CRM signal lives on room_type instead) still get
    bucketed correctly. The renderer prefers rate_plan_name for the
    label, falling back to room_type.

    Sorted by revenue desc — usually 1-3 rows per branch per week
    because each branch has only a handful of CRM rate plans.
    """
    rows = db.query(
        Reservation.rate_plan_name,
        Reservation.room_type,
        func.count(Reservation.id),
        func.coalesce(func.sum(Reservation.nights), 0),
        func.coalesce(func.sum(Reservation.grand_total_native), 0),
    ).filter(
        Reservation.branch_id == branch_id,
        crm_reservation_filter(),
        Reservation.reservation_date >= d_from,
        Reservation.reservation_date <= d_to,
        ~func.lower(func.coalesce(Reservation.status, "")).in_(list(_EXCLUDED_STATUSES)),
        ~func.lower(func.coalesce(Reservation.source, "")).in_(list(_NON_PAYING_SOURCES)),
    ).group_by(
        Reservation.rate_plan_name,
        Reservation.room_type,
    ).all()

    out = []
    for r in rows:
        rate_plan = r[0]
        room_type = r[1]
        out.append({
            "rate_plan_name": rate_plan,
            "room_type": room_type,
            "label": rate_plan or room_type or "—",
            "bookings": int(r[2] or 0),
            "nights": int(r[3] or 0),
            "revenue": round(float(r[4] or 0), 2),
        })
    out.sort(key=lambda x: -x["revenue"])
    return out


# Branch UUID / name → GHL EmailCampaignStats.branch_name string
_GHL_BRANCH_NAME_MAP = {
    "meander saigon": "Saigon",
    "meander taipei": "Taipei",
    "meander 1948": "1948",
    "meander osaka": "Osaka",
    "meander oani": "Oani",
    "oani": "Oani",
}


def _resolve_ghl_branch_name(branch_name: str) -> Optional[str]:
    bn = (branch_name or "").lower().strip()
    for key, ghl_name in _GHL_BRANCH_NAME_MAP.items():
        if key in bn or bn in key:
            return ghl_name
    return None


def crm_section(db: Session, branch_id: UUID, branch_name: str,
                today: date, days: int = 7) -> dict:
    """CRM revenue from CRM-tagged reservations — last calendar week
    vs the week before (apples-to-apples)."""
    week_start, week_end = last_week_range(today)
    prev_end = week_start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=6)

    # CRM revenue from reservations (last week vs prev week)
    rev_this = _crm_revenue(db, branch_id, week_start, week_end)
    rev_prev = _crm_revenue(db, branch_id, prev_start, prev_end)
    by_rate_plan = _crm_revenue_by_rate_plan(db, branch_id, week_start, week_end)

    # Email stats — by GHL branch name
    ghl_name = _resolve_ghl_branch_name(branch_name)
    email_summary = {
        "sent": 0, "delivered": 0, "opened": 0, "clicked": 0,
        "bounced": 0, "unsubscribed": 0,
        "open_rate_pct": None, "click_rate_pct": None,
        "bounce_rate_pct": None, "unsub_rate_pct": None,
        "attributed_bookings": 0, "attributed_revenue_native": 0.0,
        "top_workflows": [], "bulk_recent": [],
    }

    common_return = {
        "window_days": (week_end - week_start).days + 1,
        "window_start": week_start.isoformat(),
        "window_end": week_end.isoformat(),
        "crm_revenue_this": rev_this,
        "crm_revenue_prev": rev_prev,
        "wow_revenue_pct": _pct_change(rev_this["revenue"], rev_prev["revenue"]),
        "by_rate_plan": by_rate_plan,
    }

    if not ghl_name:
        return {
            **common_return,
            "ghl_branch_name": None,
            "email": email_summary,
        }

    # Workflows — latest snapshot rows (sentinel-dated, lifetime cumulative).
    # We treat them as "always include" because each row is overwritten on sync.
    workflow_rows = db.query(EmailCampaignStats).filter(
        EmailCampaignStats.branch_name == ghl_name,
        EmailCampaignStats.campaign_type == "workflow",
    ).all()

    # Bulk — only sends in this window
    bulk_rows = db.query(EmailCampaignStats).filter(
        EmailCampaignStats.branch_name == ghl_name,
        EmailCampaignStats.campaign_type == "bulk",
        EmailCampaignStats.stat_date >= week_start,
        EmailCampaignStats.stat_date <= week_end,
    ).all()

    all_rows = workflow_rows + bulk_rows

    sent = sum(int(r.total_sent or 0) for r in all_rows)
    delivered = sum(int(r.total_delivered or 0) for r in all_rows)
    opened = sum(int(r.unique_opened or 0) for r in all_rows)
    clicked = sum(int(r.unique_clicked or 0) for r in all_rows)
    bounced = sum(int(r.total_bounced or 0) for r in all_rows)
    unsub = sum(int(r.total_unsubscribed or 0) for r in all_rows)
    attr_bookings = sum(int(r.attributed_bookings or 0) for r in all_rows)
    attr_rev = sum(float(r.attributed_revenue_native or 0) for r in all_rows)

    email_summary.update({
        "sent": sent,
        "delivered": delivered,
        "opened": opened,
        "clicked": clicked,
        "bounced": bounced,
        "unsubscribed": unsub,
        "open_rate_pct": round(opened / sent * 100, 2) if sent > 0 else None,
        "click_rate_pct": round(clicked / sent * 100, 2) if sent > 0 else None,
        "bounce_rate_pct": round(bounced / sent * 100, 2) if sent > 0 else None,
        "unsub_rate_pct": round(unsub / sent * 100, 2) if sent > 0 else None,
        "attributed_bookings": attr_bookings,
        "attributed_revenue_native": round(attr_rev, 2),
    })

    # Top 5 workflows by attributed revenue (lifetime)
    top_wf = []
    for r in workflow_rows:
        s = int(r.total_sent or 0)
        op = int(r.unique_opened or 0)
        cl = int(r.unique_clicked or 0)
        bk = int(r.attributed_bookings or 0)
        rv = float(r.attributed_revenue_native or 0)
        if s == 0:
            continue
        top_wf.append({
            "name": r.workflow_name or r.workflow_id,
            "sent": s,
            "open_rate_pct": round(op / s * 100, 2),
            "click_rate_pct": round(cl / s * 100, 2),
            "bookings": bk,
            "revenue": round(rv, 2),
            "rev_per_email": round(rv / s, 2),
        })
    top_wf.sort(key=lambda x: -x["revenue"])

    # Bulk sends in window — most recent first
    bulk_recent = []
    for r in bulk_rows:
        s = int(r.total_sent or 0)
        op = int(r.unique_opened or 0)
        cl = int(r.unique_clicked or 0)
        bulk_recent.append({
            "name": r.workflow_name or r.workflow_id,
            "stat_date": r.stat_date.isoformat(),
            "sent": s,
            "open_rate_pct": round(op / s * 100, 2) if s > 0 else None,
            "click_rate_pct": round(cl / s * 100, 2) if s > 0 else None,
            "bookings": int(r.attributed_bookings or 0),
            "revenue": round(float(r.attributed_revenue_native or 0), 2),
        })
    bulk_recent.sort(key=lambda x: x["stat_date"], reverse=True)

    email_summary["top_workflows"] = top_wf[:5]
    email_summary["bulk_recent"] = bulk_recent[:5]

    return {
        **common_return,
        "ghl_branch_name": ghl_name,
        "email": email_summary,
    }


# ── Public orchestrator ──────────────────────────────────────────────────────

def build_branch_analytics(db: Session, branch: Branch, today: date) -> dict:
    """Combine all analytical sections for a single branch.

    Window choices (Monday-morning weekly digest):
      - summary:     last calendar week + prev week for WoW (apples-to-apples)
      - outliers:    30-day baseline σ, only events in last calendar week
      - behavior:    last calendar week (cancel %, lead time, LOS)
      - channel_mix: last calendar week
      - countries:   top-10 by last-30d bookings, in two views (booking
                      date / check-in date), each with WoW + 30d + YoY
      - paid_ads:    last calendar week vs prev calendar week
      - kol:         last calendar week (posts published; expiring is forward-looking 60d)
      - crm:         last calendar week (revenue only; from CRM-tagged reservations)

    "Last calendar week" = the most recently completed Mon–Sun. See
    last_week_range() docstring for day-of-week edge cases.
    """
    total_rooms = branch.total_rooms or 0
    return {
        "summary": summary_metrics(db, branch.id, total_rooms, today),
        "outliers": outliers(db, branch.id, today, baseline_days=30),
        "behavior": booking_behavior(db, branch.id, today, days=7),
        "channel_mix": channel_mix(db, branch.id, today, days=7),
        "countries": country_insights(db, branch.id, today, days=30, limit=10),
        "ad_optimizer": ad_budget_optimizer(db, branch.id, today, total_rooms),
        "paid_ads": paid_ads_section(db, branch, today, days=7),
        "kol": kol_section(db, branch.id, branch.name, today, days=7),
        "crm": crm_section(db, branch.id, branch.name, today, days=7),
    }
