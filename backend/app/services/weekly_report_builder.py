"""
Weekly Report Builder — Phase 5.1

Produces the analytical payload used by the weekly email and Report page.
Each section is a pure function so sections can be unit-tested independently.

Conventions (honor user memory rules):
- OCC includes ALL sources (no source exclusions)
- Revenue EXCLUDES Blogger, House Use, KOL, Special Case
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

from sqlalchemy import or_

from app.models.ads import AdsPerformance
from app.models.angle import AdAngle
from app.models.branch import Branch
from app.models.daily_metrics import DailyMetrics
from app.models.email_campaign_stats import EmailCampaignStats
from app.models.holiday_intel import HolidayCalendar
from app.models.kol import KOLRecord
from app.models.kpi import KPITarget
from app.models.reservation import Reservation
from app.services.kpi_engine import _EXCLUDED_SOURCES, _EXCLUDED_STATUSES

logger = logging.getLogger(__name__)


# ── Reservation query helpers ────────────────────────────────────────────────

def _reservations_base(db: Session, branch_id: UUID):
    """All non-cancelled/no-show reservations for a branch."""
    return db.query(Reservation).filter(
        Reservation.branch_id == branch_id,
        ~func.lower(func.coalesce(Reservation.status, "")).in_(list(_EXCLUDED_STATUSES)),
    )


def _reservations_revenue_base(db: Session, branch_id: UUID):
    """Non-cancelled reservations, excluding Blogger/House Use/KOL/Special Case."""
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
    Current week / last week / this month / last month / YoY (same month last year).
    YoY renders None when no data exists for last year (graceful).
    """
    # This week (Mon..Sun containing today)
    this_week_start = today - timedelta(days=today.weekday())
    this_week_end = this_week_start + timedelta(days=6)
    last_week_start = this_week_start - timedelta(days=7)
    last_week_end = this_week_start - timedelta(days=1)

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

    this_week = _range_metrics(db, branch_id, total_rooms, this_week_start, min(this_week_end, today))
    last_week = _range_metrics(db, branch_id, total_rooms, last_week_start, last_week_end)
    mtd = _range_metrics(db, branch_id, total_rooms, mtd_start, mtd_end)
    last_month = _range_metrics(db, branch_id, total_rooms, lm_start, lm_end)
    yoy = _range_metrics(db, branch_id, total_rooms, yoy_start, yoy_end)

    return {
        "this_week": this_week,
        "last_week": last_week,
        "mtd": mtd,
        "last_month": last_month,
        "yoy": yoy,
        "wow_revenue_pct": _pct_change(this_week["revenue"], last_week["revenue"]),
        "wow_occ_pct": _pct_change(this_week["occ_pct"], last_week["occ_pct"]),
        "mom_revenue_pct": _pct_change(mtd["revenue"], last_month["revenue"]),
        "yoy_revenue_pct": _pct_change(mtd["revenue"], yoy["revenue"]) if yoy["sold"] > 0 else None,
        "yoy_occ_pct": _pct_change(mtd["occ_pct"], yoy["occ_pct"]) if yoy["sold"] > 0 else None,
    }


# ── 2. Outliers (days with unusual OCC or revenue) ──────────────────────────

def outliers(db: Session, branch_id: UUID, today: date,
             baseline_days: int = 30, report_days: int = 7) -> list[dict]:
    """
    Flag days where revenue or OCC deviates > 1.5σ from a `baseline_days`
    rolling mean, but only report events that fell within the last
    `report_days` (default 7) — this is a weekly digest.

    Why split: 7 days is too few to compute a reliable σ on its own, so we
    keep the 30-day window for the baseline statistic and slice the
    reporting window separately.

    Annotate each outlier with a probable cause:
      - weekend (Sat/Sun)
      - cancellation spike (cancellation_pct > 0.15)
    """
    baseline_start = today - timedelta(days=baseline_days)
    report_start = today - timedelta(days=report_days)
    rows = db.query(
        DailyMetrics.date,
        DailyMetrics.revenue_native,
        DailyMetrics.occ_pct,
        DailyMetrics.cancellation_pct,
    ).filter(
        DailyMetrics.branch_id == branch_id,
        DailyMetrics.date >= baseline_start,
        DailyMetrics.date <= today,
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
        # Only report events in the past `report_days`
        if r.date < report_start:
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
    """
    Cancellation %, lead time distribution, LOS distribution — last `days` days
    based on check_in_date.
    """
    cutoff = today - timedelta(days=days)

    # ── Cancellation % by source_category (compute in Python) ─────────
    all_rows = db.query(
        Reservation.source_category, Reservation.status,
    ).filter(
        Reservation.branch_id == branch_id,
        Reservation.check_in_date >= cutoff,
        Reservation.check_in_date <= today,
    ).all()

    by_source = {}
    for r in all_rows:
        sc = r.source_category or "Unknown"
        d = by_source.setdefault(sc, {"total": 0, "cancelled": 0})
        d["total"] += 1
        if (r.status or "").strip().lower() in _EXCLUDED_STATUSES:
            d["cancelled"] += 1

    cancellation_by_source = []
    total_all = sum(d["total"] for d in by_source.values())
    cxl_all = sum(d["cancelled"] for d in by_source.values())
    for sc, d in sorted(by_source.items(), key=lambda x: -x[1]["total"]):
        pct = round(d["cancelled"] / d["total"] * 100, 2) if d["total"] > 0 else None
        cancellation_by_source.append({
            "source_category": sc,
            "total": d["total"],
            "cancelled": d["cancelled"],
            "pct": pct,
        })
    cancellation_overall_pct = round(cxl_all / total_all * 100, 2) if total_all > 0 else None

    # ── Lead time & LOS (non-cancelled only) ──────────────────────────
    rows = db.query(
        Reservation.reservation_date,
        Reservation.check_in_date,
        Reservation.check_out_date,
        Reservation.nights,
    ).filter(
        Reservation.branch_id == branch_id,
        Reservation.check_in_date >= cutoff,
        Reservation.check_in_date <= today,
        ~func.lower(func.coalesce(Reservation.status, "")).in_(list(_EXCLUDED_STATUSES)),
    ).all()

    lead_times = []
    los_values = []
    for r in rows:
        if r.reservation_date and r.check_in_date:
            lt = (r.check_in_date - r.reservation_date).days
            if lt >= 0:
                lead_times.append(lt)
        if r.nights:
            los_values.append(int(r.nights))
        elif r.check_in_date and r.check_out_date:
            los_values.append((r.check_out_date - r.check_in_date).days)

    lead_time_avg = round(sum(lead_times) / len(lead_times), 1) if lead_times else None
    los_avg = round(sum(los_values) / len(los_values), 2) if los_values else None

    return {
        "window_days": days,
        "cancellation_overall_pct": cancellation_overall_pct,
        "cancellation_by_source": cancellation_by_source,
        "lead_time_avg_days": lead_time_avg,
        "lead_time_buckets": _lead_time_buckets(lead_times),
        "lead_time_samples": len(lead_times),
        "los_avg_nights": los_avg,
        "los_buckets": _los_buckets(los_values),
        "los_samples": len(los_values),
    }


# ── 4. Channel mix ──────────────────────────────────────────────────────────

def channel_mix(db: Session, branch_id: UUID, today: date, days: int = 7) -> dict:
    """
    Room-nights and revenue share by source_category (OTA/Direct/LTA) and by specific source.
    Revenue excludes the usual non-paying sources.
    """
    cutoff = today - timedelta(days=days)
    base = _reservations_base(db, branch_id).filter(
        Reservation.check_in_date >= cutoff,
        Reservation.check_in_date <= today,
    )

    # By category
    cat_rows = base.with_entities(
        Reservation.source_category,
        func.coalesce(func.sum(Reservation.nights), 0),
        func.coalesce(func.sum(Reservation.grand_total_native), 0),
    ).group_by(Reservation.source_category).all()

    total_nights = sum(int(r[1] or 0) for r in cat_rows)
    total_rev = sum(float(r[2] or 0) for r in cat_rows)

    categories = []
    for r in cat_rows:
        nights = int(r[1] or 0)
        rev = float(r[2] or 0)
        categories.append({
            "source_category": r[0] or "Unknown",
            "room_nights": nights,
            "revenue_native": round(rev, 2),
            "nights_share_pct": round(nights / total_nights * 100, 2) if total_nights else None,
            "revenue_share_pct": round(rev / total_rev * 100, 2) if total_rev else None,
        })
    categories.sort(key=lambda x: -x["room_nights"])

    # Per-source breakdown (top 10)
    src_rows = base.with_entities(
        Reservation.source,
        func.coalesce(func.sum(Reservation.nights), 0),
        func.coalesce(func.sum(Reservation.grand_total_native), 0),
    ).group_by(Reservation.source).order_by(func.sum(Reservation.nights).desc()).limit(10).all()

    sources = []
    for r in src_rows:
        nights = int(r[1] or 0)
        rev = float(r[2] or 0)
        sources.append({
            "source": r[0] or "Unknown",
            "room_nights": nights,
            "revenue_native": round(rev, 2),
            "nights_share_pct": round(nights / total_nights * 100, 2) if total_nights else None,
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
        "window_days": days,
        "total_nights": total_nights,
        "total_revenue_native": round(total_rev, 2),
        "categories": categories,
        "top_sources": sources,
        "direct_trend": direct_trend,
    }


# ── 5. Country insights ──────────────────────────────────────────────────────

def country_insights(db: Session, branch_id: UUID, today: date,
                     days: int = 90, limit: int = 8) -> dict:
    """
    Top countries + YoY change + growing/shrinking/emerging + per-country ADR/LOS.
    """
    cutoff = today - timedelta(days=days)
    prev_cutoff = cutoff - timedelta(days=days)
    yoy_start = today.replace(year=today.year - 1) - timedelta(days=days)
    yoy_end = today.replace(year=today.year - 1)

    def _country_stats(date_from: date, date_to: date):
        rows = db.query(
            Reservation.guest_country,
            func.count(Reservation.id).label("bookings"),
            func.coalesce(func.sum(Reservation.nights), 0).label("nights"),
            func.coalesce(func.sum(Reservation.grand_total_native), 0).label("rev"),
        ).filter(
            Reservation.branch_id == branch_id,
            Reservation.check_in_date >= date_from,
            Reservation.check_in_date <= date_to,
            Reservation.guest_country.isnot(None),
            ~func.lower(func.coalesce(Reservation.guest_country, "")).contains("unknown"),
            ~func.lower(func.coalesce(Reservation.status, "")).in_(list(_EXCLUDED_STATUSES)),
        ).group_by(Reservation.guest_country).all()
        return {r.guest_country: {"bookings": int(r.bookings or 0),
                                  "nights": int(r.nights or 0),
                                  "revenue": float(r.rev or 0)} for r in rows}

    recent = _country_stats(cutoff, today)
    prev = _country_stats(prev_cutoff, cutoff - timedelta(days=1))
    yoy = _country_stats(yoy_start, yoy_end)

    # Top countries with YoY
    top = []
    for country, cur in sorted(recent.items(), key=lambda x: -x[1]["bookings"])[:limit]:
        yoy_cur = yoy.get(country, {}).get("bookings", 0)
        yoy_pct = _pct_change(cur["bookings"], yoy_cur) if yoy_cur > 0 else None
        adr = round(cur["revenue"] / cur["nights"], 2) if cur["nights"] > 0 else None
        los = round(cur["nights"] / cur["bookings"], 2) if cur["bookings"] > 0 else None
        top.append({
            "country": country,
            "bookings": cur["bookings"],
            "nights": cur["nights"],
            "revenue_native": round(cur["revenue"], 2),
            "adr_native": adr,
            "avg_los": los,
            "yoy_bookings_pct": yoy_pct,
        })

    # Growing / shrinking / emerging
    growing, shrinking, emerging = [], [], []
    for country, cur in recent.items():
        if cur["bookings"] < 2:
            continue
        p = prev.get(country, {}).get("bookings", 0)
        if p == 0:
            emerging.append({"country": country, "bookings": cur["bookings"]})
            continue
        change = _pct_change(cur["bookings"], p)
        entry = {"country": country, "bookings": cur["bookings"], "prev": p, "change_pct": change}
        if change is not None and change >= 20:
            growing.append(entry)
        elif change is not None and change <= -20:
            shrinking.append(entry)

    growing.sort(key=lambda x: -(x["change_pct"] or 0))
    shrinking.sort(key=lambda x: (x["change_pct"] or 0))
    emerging.sort(key=lambda x: -x["bookings"])

    return {
        "window_days": days,
        "top": top,
        "growing": growing[:5],
        "shrinking": shrinking[:5],
        "emerging": emerging[:5],
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


def paid_ads_section(db: Session, branch_id: UUID, today: date,
                     days: int = 7) -> dict:
    """Paid Ads weekly snapshot — totals (this week vs prior), by channel,
    by funnel, top/bottom campaigns, and country breakdown.
    """
    week_end = today
    week_start = today - timedelta(days=days - 1)
    prev_end = week_start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=days - 1)

    this_week = _ads_window_aggregate(db, branch_id, week_start, week_end)
    last_week = _ads_window_aggregate(db, branch_id, prev_start, prev_end)

    # By channel (this week)
    ch_rows = db.query(
        AdsPerformance.channel,
        func.coalesce(func.sum(AdsPerformance.cost_native), 0),
        func.coalesce(func.sum(AdsPerformance.impressions), 0),
        func.coalesce(func.sum(AdsPerformance.clicks), 0),
        func.coalesce(func.sum(AdsPerformance.bookings), 0),
        func.coalesce(func.sum(AdsPerformance.revenue_native), 0),
    ).filter(
        AdsPerformance.branch_id == branch_id,
        AdsPerformance.grain == "daily",
        AdsPerformance.date_from >= week_start,
        AdsPerformance.date_from <= week_end,
    ).group_by(AdsPerformance.channel).all()

    by_channel = []
    for r in ch_rows:
        cost = float(r[1] or 0)
        impr = int(r[2] or 0)
        clicks = int(r[3] or 0)
        bookings = int(r[4] or 0)
        rev = float(r[5] or 0)
        by_channel.append({
            "channel": r[0] or "Unknown",
            "cost": round(cost, 2),
            "impressions": impr,
            "clicks": clicks,
            "bookings": bookings,
            "revenue": round(rev, 2),
            "ctr_pct": round(clicks / impr * 100, 2) if impr > 0 else None,
            "cvr_pct": round(bookings / clicks * 100, 2) if clicks > 0 else None,
            "roas": round(rev / cost, 2) if cost > 0 else None,
        })
    by_channel.sort(key=lambda x: -x["cost"])

    # By funnel stage
    fn_rows = db.query(
        AdsPerformance.funnel_stage,
        func.coalesce(func.sum(AdsPerformance.cost_native), 0),
        func.coalesce(func.sum(AdsPerformance.bookings), 0),
        func.coalesce(func.sum(AdsPerformance.revenue_native), 0),
    ).filter(
        AdsPerformance.branch_id == branch_id,
        AdsPerformance.grain == "daily",
        AdsPerformance.date_from >= week_start,
        AdsPerformance.date_from <= week_end,
    ).group_by(AdsPerformance.funnel_stage).all()

    by_funnel = []
    for r in fn_rows:
        cost = float(r[1] or 0)
        bookings = int(r[2] or 0)
        rev = float(r[3] or 0)
        by_funnel.append({
            "funnel": r[0] or "—",
            "cost": round(cost, 2),
            "bookings": bookings,
            "revenue": round(rev, 2),
            "roas": round(rev / cost, 2) if cost > 0 else None,
        })
    by_funnel.sort(key=lambda x: -x["cost"])

    # Top / bottom campaigns by ROAS (need impressions ≥ 1000 to qualify)
    camp_rows = db.query(
        AdsPerformance.campaign_name,
        AdsPerformance.adset_name,
        AdsPerformance.ad_name,
        AdsPerformance.channel,
        func.coalesce(func.sum(AdsPerformance.cost_native), 0),
        func.coalesce(func.sum(AdsPerformance.impressions), 0),
        func.coalesce(func.sum(AdsPerformance.clicks), 0),
        func.coalesce(func.sum(AdsPerformance.bookings), 0),
        func.coalesce(func.sum(AdsPerformance.revenue_native), 0),
    ).filter(
        AdsPerformance.branch_id == branch_id,
        AdsPerformance.grain == "ad",
        AdsPerformance.cost_native > 0,
        or_(
            AdsPerformance.date_from.is_(None),
            AdsPerformance.date_from <= week_end,
        ),
    ).group_by(
        AdsPerformance.campaign_name, AdsPerformance.adset_name,
        AdsPerformance.ad_name, AdsPerformance.channel,
    ).all()

    qualified = []
    for r in camp_rows:
        cost = float(r[4] or 0)
        impr = int(r[5] or 0)
        clicks = int(r[6] or 0)
        bookings = int(r[7] or 0)
        rev = float(r[8] or 0)
        if impr < 1000 or cost < 1:
            continue
        qualified.append({
            "campaign": r[0],
            "adset": r[1],
            "ad_name": r[2],
            "channel": r[3] or "—",
            "cost": round(cost, 2),
            "impressions": impr,
            "clicks": clicks,
            "bookings": bookings,
            "revenue": round(rev, 2),
            "ctr_pct": round(clicks / impr * 100, 2) if impr > 0 else None,
            "cvr_pct": round(bookings / clicks * 100, 2) if clicks > 0 else None,
            "cpa": round(cost / bookings, 2) if bookings > 0 else None,
            "roas": round(rev / cost, 2) if cost > 0 else 0,
        })

    top_campaigns = sorted(qualified, key=lambda x: -(x["roas"] or 0))[:5]
    # Underperformers: ROAS < 1.0 AND bookings < 2 AND ≥ 5K impressions
    bottom_campaigns = sorted(
        [c for c in qualified if c["roas"] < 1.0 and c["bookings"] < 2
         and c["impressions"] >= 5000],
        key=lambda x: x["roas"],
    )[:5]

    return {
        "window_days": days,
        "this_week": this_week,
        "last_week": last_week,
        "wow_cost_pct": _pct_change(this_week["cost"], last_week["cost"]),
        "wow_revenue_pct": _pct_change(this_week["revenue"], last_week["revenue"]),
        "wow_roas_pct": _pct_change(this_week["roas"], last_week["roas"]),
        "by_channel": by_channel,
        "by_funnel": by_funnel,
        "top_campaigns": top_campaigns,
        "bottom_campaigns": bottom_campaigns,
    }


# ── 8. KOL — pipeline, ROI, expiring usage rights ───────────────────────────

def kol_section(db: Session, branch_id: UUID, today: date,
                days: int = 7) -> dict:
    """KOL weekly snapshot.

    Default window 7d (was 30d). "Total KOLs" replaced with
    `posts_published_this_week` — count of KOL records whose
    `published_date` falls in the window. One KOL record represents one
    collaboration / case, so it maps to one post.

    Still surfaces:
      - Pipeline counts (across all KOLs for the branch — not windowed,
        because deliverable_status is current state, not history)
      - Stuck deliverables (>14d in In Progress, branch-wide)
      - Expiring usage rights (next 60d, branch-wide)
      - Ads-eligible KOLs available (branch-wide)
      - Cost MTD + organic bookings/revenue/ROI from KOL_* room types
    """
    cutoff = today - timedelta(days=days)
    expiry_horizon = today + timedelta(days=60)

    # KOL records for this branch
    kols = db.query(KOLRecord).filter(KOLRecord.branch_id == branch_id).all()

    # Posts published in the last `days` window
    posts_this_week = sum(
        1 for k in kols
        if k.published_date and cutoff <= k.published_date <= today
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
    res_rows = db.query(
        func.count(Reservation.id),
        func.coalesce(func.sum(Reservation.nights), 0),
        func.coalesce(func.sum(Reservation.grand_total_native), 0),
    ).filter(
        Reservation.branch_id == branch_id,
        Reservation.room_type.ilike("%KOL_%"),
        Reservation.check_in_date >= cutoff,
        Reservation.check_in_date <= today,
        ~func.lower(func.coalesce(Reservation.status, "")).in_(list(_EXCLUDED_STATUSES)),
        ~func.lower(func.coalesce(Reservation.source, "")).in_(list(_NON_PAYING_SOURCES)),
    ).one()

    organic_bookings = int(res_rows[0] or 0)
    organic_nights = int(res_rows[1] or 0)
    organic_revenue = float(res_rows[2] or 0)
    roi = round(organic_revenue / cost_mtd, 2) if cost_mtd > 0 else None

    return {
        "window_days": days,
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
    }


# ── 9. CRM (Email Marketing) — workflows + bulk + revenue attribution ───────

def _crm_reservation_filter():
    """Match same set the CRM router uses."""
    return or_(
        Reservation.room_type.ilike("%CRM%"),
        Reservation.rate_plan_name.ilike("%CRM%"),
        Reservation.room_type.ilike("%MEANDER'S FRIEND%"),
        Reservation.rate_plan_name.ilike("%MEANDER'S FRIEND%"),
        Reservation.room_type.ilike("%Travel guide%"),
        Reservation.rate_plan_name.ilike("%Travel guide%"),
        Reservation.room_type.ilike("%Grand Open%"),
        Reservation.rate_plan_name.ilike("%Grand Open%"),
    )


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
        _crm_reservation_filter(),
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
                today: date, days: int = 30) -> dict:
    """CRM summary: email workflow performance + CRM revenue attribution."""
    week_end = today
    week_start = today - timedelta(days=days - 1)
    prev_end = week_start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=days - 1)

    # CRM revenue from reservations (this period vs prior)
    rev_this = _crm_revenue(db, branch_id, week_start, week_end)
    rev_prev = _crm_revenue(db, branch_id, prev_start, prev_end)

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

    if not ghl_name:
        return {
            "window_days": days,
            "ghl_branch_name": None,
            "crm_revenue_this": rev_this,
            "crm_revenue_prev": rev_prev,
            "wow_revenue_pct": _pct_change(rev_this["revenue"], rev_prev["revenue"]),
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
        "window_days": days,
        "ghl_branch_name": ghl_name,
        "crm_revenue_this": rev_this,
        "crm_revenue_prev": rev_prev,
        "wow_revenue_pct": _pct_change(rev_this["revenue"], rev_prev["revenue"]),
        "email": email_summary,
    }


# ── Public orchestrator ──────────────────────────────────────────────────────

def build_branch_analytics(db: Session, branch: Branch, today: date) -> dict:
    """Combine all analytical sections for a single branch.

    Window choices (weekly digest mindset):
      - summary:     this-week + WoW deltas (computed inside summary_metrics)
      - outliers:    30-day baseline σ, only events in last 7 days
      - behavior:    7 days (cancel %, lead time, LOS distributions)
      - channel_mix: 7 days
      - countries:   90 days for top + YoY comparison (need volume)
      - paid_ads:    7 days
      - kol:         7 days (count of posts published in window)
      - crm:         7 days (revenue only — simplified)
    """
    total_rooms = branch.total_rooms or 0
    return {
        "summary": summary_metrics(db, branch.id, total_rooms, today),
        "outliers": outliers(db, branch.id, today, baseline_days=30, report_days=7),
        "behavior": booking_behavior(db, branch.id, today, days=7),
        "channel_mix": channel_mix(db, branch.id, today, days=7),
        "countries": country_insights(db, branch.id, today, days=90, limit=8),
        "ad_optimizer": ad_budget_optimizer(db, branch.id, today, total_rooms),
        "paid_ads": paid_ads_section(db, branch.id, today, days=7),
        "kol": kol_section(db, branch.id, today, days=7),
        "crm": crm_section(db, branch.id, branch.name, today, days=7),
    }
