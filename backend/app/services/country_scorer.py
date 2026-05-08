"""
Country Scorer — v1.4
Scores countries by booking trend signals → Hot / Warm / Cold tiers,
plus a plain-language trend label (Doubled / Surging / Growing / Stable
/ Declining) that's easier to read than a raw score number.

Scoring formula (score 0–100, kept for sorting):
  WoW booking growth    40%  (this_week - last_week) / last_week
  MoM booking growth    30%  (this_month - last_month) / last_month
  Revenue/booking trend 20%  avg ADR this week vs 4-week average
  Booking recency       10%  days since last booking (inverted)

Volume filter (v1.4): skip countries with <= MIN_WEEKLY_BOOKINGS this
week. Without this, markets with 1–2 bookings get score-inflated by
WoW growth (1 → 2 = +100%) and crowd out higher-volume markets that
matter operationally.

Tiers: Hot >= 70 | Warm 40–69 | Cold < 40

Trend labels (driven by WoW growth, with first-appearance handling):
  - last_week == 0           → "📍 New market"
  - WoW >= +100%             → "🚀 Doubled+"
  - WoW +50% to +99%         → "🔥 Surging"
  - WoW +20% to +49%         → "📈 Growing"
  - WoW -10% to +19%         → "➡️ Stable"
  - WoW -50% to -11%         → "📉 Declining"
  - WoW < -50%               → "🆘 Crashing"
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.reservation import Reservation

logger = logging.getLogger(__name__)

# Tier thresholds (score 0–100)
TIER_HOT  = 70
TIER_WARM = 40

# Volume filter — countries with this_week_count > MIN_WEEKLY_BOOKINGS get
# scored. Below this, WoW % becomes statistical noise (going from 2 → 4
# bookings is a +100% boost but operationally not worth a campaign shift).
MIN_WEEKLY_BOOKINGS = 10


def _get_tier(score: float) -> str:
    if score >= TIER_HOT:
        return "Hot"
    elif score >= TIER_WARM:
        return "Warm"
    return "Cold"


def _trend_label(wow_growth: float, last_week_count: int) -> str:
    """Convert raw WoW growth into a plain-language tag.

    last_week_count is needed because the default growth=1.0 (assigned when
    last_week=0) covers two distinct cases — "first time appearing" vs
    "really doubled" — and they should read differently in the email.
    """
    if last_week_count == 0:
        return "📍 New market"
    pct = wow_growth * 100
    if pct >= 100:
        return "🚀 Doubled+"
    if pct >= 50:
        return "🔥 Surging"
    if pct >= 20:
        return "📈 Growing"
    if pct >= -10:
        return "➡️ Stable"
    if pct >= -50:
        return "📉 Declining"
    return "🆘 Crashing"


def _safe_div(a: float, b: float, default: float = 0.0) -> float:
    return a / b if b else default


def _normalize_growth(growth_rate: float) -> float:
    """Map unbounded growth rate → 0.0–1.0. +100% maps to 1.0, -100% maps to 0.0."""
    clamped = max(-1.0, min(1.0, growth_rate))
    return (clamped + 1.0) / 2.0


def score_countries(
    db: Session,
    branch_id: Optional[UUID] = None,
    reference_date: Optional[date] = None,
    top_n: int = 50,
) -> list[dict]:
    """
    Score and rank countries by booking potential (v1.3 formula).
    Returns list of dicts sorted by score descending.
    """
    if reference_date is None:
        reference_date = datetime.now(timezone.utc).date()

    # Date windows
    week_start      = reference_date - timedelta(days=7)
    last_week_start = reference_date - timedelta(days=14)
    month_start     = reference_date.replace(day=1)
    last_month_end  = month_start - timedelta(days=1)
    last_month_start = last_month_end.replace(day=1)
    four_weeks_ago  = reference_date - timedelta(days=28)

    def _base_q():
        q = db.query(Reservation).filter(
            Reservation.status.notin_(["cancelled", "canceled", "no_show"]),
            Reservation.guest_country_code.isnot(None),
        )
        if branch_id:
            q = q.filter(Reservation.branch_id == branch_id)
        return q

    # ── This week bookings per country ──────────────────────────────────────
    this_week = (
        _base_q()
        .filter(Reservation.reservation_date >= week_start,
                Reservation.reservation_date <= reference_date)
        .with_entities(
            Reservation.guest_country_code,
            Reservation.guest_country,
            func.count(Reservation.id).label("count"),
            func.coalesce(func.sum(Reservation.grand_total_native), 0).label("revenue"),
        )
        .group_by(Reservation.guest_country_code, Reservation.guest_country)
        .all()
    )

    if not this_week:
        return []

    # ── Last week bookings per country ───────────────────────────────────────
    last_week_dict = {
        r.guest_country_code: r.count
        for r in _base_q()
        .filter(Reservation.reservation_date >= last_week_start,
                Reservation.reservation_date < week_start)
        .with_entities(
            Reservation.guest_country_code,
            func.count(Reservation.id).label("count"),
        )
        .group_by(Reservation.guest_country_code)
        .all()
    }

    # ── This month bookings per country ──────────────────────────────────────
    this_month_dict = {
        r.guest_country_code: r.count
        for r in _base_q()
        .filter(Reservation.reservation_date >= month_start,
                Reservation.reservation_date <= reference_date)
        .with_entities(
            Reservation.guest_country_code,
            func.count(Reservation.id).label("count"),
        )
        .group_by(Reservation.guest_country_code)
        .all()
    }

    # ── Last month bookings per country ──────────────────────────────────────
    last_month_dict = {
        r.guest_country_code: r.count
        for r in _base_q()
        .filter(Reservation.reservation_date >= last_month_start,
                Reservation.reservation_date <= last_month_end)
        .with_entities(
            Reservation.guest_country_code,
            func.count(Reservation.id).label("count"),
        )
        .group_by(Reservation.guest_country_code)
        .all()
    }

    # ── 4-week avg ADR per country ────────────────────────────────────────────
    four_week_adr = {
        r.guest_country_code: float(r.avg_adr)
        for r in _base_q()
        .filter(Reservation.reservation_date >= four_weeks_ago,
                Reservation.reservation_date <= reference_date,
                Reservation.nights > 0)
        .with_entities(
            Reservation.guest_country_code,
            (func.coalesce(func.sum(Reservation.grand_total_native), 0) /
             func.nullif(func.sum(Reservation.nights), 0)).label("avg_adr"),
        )
        .group_by(Reservation.guest_country_code)
        .all()
        if r.avg_adr is not None
    }

    # ── Last reservation date per country ─────────────────────────────────────
    last_booking = {
        r.guest_country_code: r.last_date
        for r in _base_q()
        .with_entities(
            Reservation.guest_country_code,
            func.max(Reservation.reservation_date).label("last_date"),
        )
        .group_by(Reservation.guest_country_code)
        .all()
    }

    # ── Score each country ────────────────────────────────────────────────────
    scored = []
    for row in this_week:
        code = row.guest_country_code
        this_week_count  = row.count

        # Volume filter — skip noise from tiny markets. See module docstring.
        if this_week_count <= MIN_WEEKLY_BOOKINGS:
            continue

        last_week_count  = last_week_dict.get(code, 0)
        this_month_count = this_month_dict.get(code, 0)
        last_month_count = last_month_dict.get(code, 0)

        # WoW growth (40%)
        wow_growth = _safe_div(this_week_count - last_week_count, last_week_count, default=1.0)
        wow_score  = _normalize_growth(wow_growth)

        # MoM growth (30%)
        mom_growth = _safe_div(this_month_count - last_month_count, last_month_count, default=1.0)
        mom_score  = _normalize_growth(mom_growth)

        # Revenue/booking trend (20%) — this week ADR vs 4-week avg ADR
        this_week_revenue = float(row.revenue)
        this_week_adr = _safe_div(this_week_revenue, this_week_count)
        avg_adr = four_week_adr.get(code, this_week_adr)
        if avg_adr > 0:
            adr_ratio = this_week_adr / avg_adr  # 1.0 = on-par, >1 = improving
            adr_score = min(1.0, adr_ratio / 1.5)  # cap at 1.5x improvement
        else:
            adr_score = 0.5

        # Booking recency (10%) — days since last booking, inverted
        last_date = last_booking.get(code)
        if last_date:
            days_ago = (reference_date - last_date).days
            recency_score = max(0.0, 1.0 - (days_ago / 90))  # 0 days → 1.0, 90+ days → 0.0
        else:
            recency_score = 0.0

        raw_score = (
            0.40 * wow_score
            + 0.30 * mom_score
            + 0.20 * adr_score
            + 0.10 * recency_score
        )
        score = round(raw_score * 100, 1)  # convert to 0–100

        scored.append({
            "country_code": code,
            "country": row.guest_country or code,
            "score": score,
            "tier": _get_tier(score),
            "trend_label": _trend_label(wow_growth, last_week_count),
            "booking_count_this_week": this_week_count,
            "booking_count_last_week": last_week_count,
            "wow_growth_pct": round(wow_growth * 100, 1) if last_week_count > 0 else None,
            "booking_count_this_month": this_month_count,
            "booking_count_last_month": last_month_count,
            "mom_growth_pct": round(mom_growth * 100, 1) if last_month_count > 0 else None,
            "revenue_native": round(this_week_revenue, 2),
        })

    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:top_n]
