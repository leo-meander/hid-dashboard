"""
Weekly Report router
- GET  /report/weekly            → report data (JSON)
- GET  /report/preview           → HTML email preview (for iframe)
- POST /report/send-weekly       → generate + send email to team (frontend)
- POST /report/send-weekly-cron  → cron-triggered send (X-Sync-Token auth)
- GET  /report/schedule          → current email schedule config
- PATCH /report/schedule         → update email schedule
"""
import calendar
import json
import logging
import textwrap
from datetime import datetime, date, timedelta, timezone
from typing import Optional
from uuid import UUID
from zoneinfo import ZoneInfo

_ICT_TZ = ZoneInfo("Asia/Ho_Chi_Minh")


def _ict_today() -> date:
    # Weekly cache cron fires 20:00 UTC Sunday = 03:00 ICT Monday. The server
    # is UTC, so date.today() there returns Sunday and last_week_range rolls
    # two weeks back. Always resolve "today" in ICT so the report tracks the
    # team's calendar week.
    return datetime.now(_ICT_TZ).date()

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import case, func, or_
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.routers.auth import get_current_user
from app.routers.sync import verify_sync_token
from app.models.branch import Branch
from app.models.daily_metrics import DailyMetrics
from app.models.kpi import KPITarget
from app.models.reservation import Reservation
from app.models.user import User
from app.models.weekly_report_archive import WeeklyReportArchive
from app.models.weekly_report_cache import WeeklyReportCache
from app.models.weekly_report_comment import WeeklyReportComment
from app.services.country_scorer import score_countries
from app.services.email_sender import send_email_html
from app.services.kpi_engine import (
    compute_kpi_summary,
    compute_next_month_forecast,
    _EXCLUDED_STATUSES,
    _EXCLUDED_SOURCES,
)
from app.services.weekly_report_builder import build_branch_analytics, last_week_range
from app.models.gov_visitor import GovVisitorData

router = APIRouter()

logger = logging.getLogger(__name__)

MONTHS_EN = ["", "January", "February", "March", "April", "May", "June",
             "July", "August", "September", "October", "November", "December"]


def _week_start(today: date) -> date:
    """Monday of the week `today` falls in. Used as the discussion-thread
    scope key — every comment on the Weekly Report page is tagged with
    this date so threads stay anchored to the week the user was viewing.
    """
    return today - timedelta(days=today.weekday())


def _cell_attrs(branch_id, metric_key: str, label: Optional[str] = None) -> str:
    """Render the data-* attributes the frontend uses to detect a
    clickable metric cell or row. Email clients ignore `class` and
    `data-*` attributes that don't have inline styles backing them, so
    this is safe to include in the same HTML the email send pipeline
    produces.

    `label`, when provided, is read by the frontend as a human-readable
    drawer title — useful for dynamic keys (per-country, per-source rows)
    where the static frontend label map can't enumerate every variation.
    """
    bid = f' data-branch-id="{branch_id}"' if branch_id else ''
    lbl = f' data-metric-label="{_attr_escape(label)}"' if label else ''
    return f' class="hid-metric-cell" data-metric-key="{metric_key}"{bid}{lbl}'


def _attr_escape(value: str) -> str:
    """Escape HTML attribute special characters so values containing
    quotes/ampersands don't break the rendered tag.
    """
    return (
        str(value)
        .replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _safe_section(db: Session, label: str, fn, default):
    """Run `fn()` and degrade to `default` on failure so one slow query
    can't kill the whole weekly report. After a Postgres statement_timeout
    the session is in an aborted-transaction state and every subsequent
    query would fail — rollback() here clears it so the next section can
    keep querying.
    """
    try:
        return fn()
    except Exception as e:
        logger.warning("Report section '%s' failed: %s: %s", label, type(e).__name__, e)
        try:
            db.rollback()
        except Exception:
            pass
        return default


def _envelope(data):
    return {"success": True, "data": data, "error": None,
            "timestamp": datetime.now(timezone.utc).isoformat()}


# ── Report cache (refreshed weekly Mon 03:00 ICT) ─────────────────────────────


def _load_cached_report(db: Session) -> Optional[tuple[list, datetime]]:
    """Return (payload, computed_at) from cache, or None if no cache yet."""
    row = db.query(WeeklyReportCache).filter_by(
        id=WeeklyReportCache.SINGLETON_ID
    ).first()
    if not row:
        return None
    return row.payload, row.computed_at


def _save_cached_report(db: Session, payload: list) -> datetime:
    """Upsert the singleton cache row. Returns the new computed_at."""
    now = datetime.now(timezone.utc)
    row = db.query(WeeklyReportCache).filter_by(
        id=WeeklyReportCache.SINGLETON_ID
    ).first()
    if row:
        row.payload = payload
        row.computed_at = now
    else:
        db.add(WeeklyReportCache(
            id=WeeklyReportCache.SINGLETON_ID,
            payload=payload,
            computed_at=now,
        ))
    db.commit()
    return now


def _get_report_with_cache(db: Session, force_fresh: bool = False) -> tuple[list, Optional[datetime]]:
    """Return (report payload, computed_at).

    Default path: serve the cached payload (refreshed Mon 03:00 ICT). The
    cache is single-row and never expires automatically — if it's empty
    (first deploy, or table was truncated) we build fresh and save.

    `force_fresh=True` rebuilds and overwrites the cache — used by the
    refresh-cache endpoint the cron hits, and the optional `?fresh=1`
    query param for admin debugging.
    """
    if not force_fresh:
        cached = _load_cached_report(db)
        if cached is not None:
            return cached
    payload = _build_report(db)
    computed_at = _save_cached_report(db, payload)
    return payload, computed_at


def _fmt(val, currency=""):
    """Full number with currency symbol, no K/M/B abbreviation (per team rule)."""
    if val is None:
        return "—"
    sym = {"VND": "₫", "TWD": "NT$", "JPY": "¥"}.get(currency, currency + " ")
    return f"{sym}{round(val):,}"


def _pct(val):
    """Percentage with 2 decimals (per team rule)."""
    if val is None:
        return "—"
    return f"{val:.2f}%"


def _num(val):
    """Integer with thousands separator."""
    if val is None:
        return "—"
    return f"{int(val):,}"


def _signed_pct(val):
    """Signed percentage with 2 decimals (e.g. +12.34% / -5.67%)."""
    if val is None:
        return "—"
    sign = "+" if val >= 0 else ""
    return f"{sign}{val:.2f}%"


def _adjustment_formula_html(raw_forecast, deduct_pct, other_rev, currency):
    """Sub-line under the Adjusted Forecast cell explaining the math.

    Renders nothing when there's no adjustment (deduct=0 AND other_rev=0)
    so the per-branch table doesn't get cluttered when the operator
    hasn't set any inputs.
    """
    deduct = deduct_pct or 0
    other = other_rev or 0
    if deduct == 0 and other == 0:
        return ""
    deduct_str = f"−{deduct:g}%" if deduct > 0 else "−0%"
    rev_str = f" + {_fmt(other, currency)}" if other > 0 else ""
    return (
        f"<div style='font-size:9px;font-weight:400;color:#9ca3af;line-height:1.3;margin-top:2px;'>"
        f"= {_fmt(raw_forecast, currency)} × (1{deduct_str}){rev_str}"
        f"</div>"
    )


def _split_subline(room_val, dorm_val, kind: str, currency: str = "") -> str:
    """Render a 'Room … · Dorm …' breakdown sub-line under a metric cell.

    Used on the per-branch card to split ADR and forecast OCC% into Room
    vs Dorm (team feedback: weekly report should always show both broken
    out). Renders nothing unless BOTH values are present, so rooms-only or
    dorms-only branches — and branches missing split data — don't show a
    half-empty 'Room — · Dorm —' line.

    kind="money" formats with the branch currency; kind="pct" as a percent.
    """
    if room_val is None or dorm_val is None:
        return ""
    fmt = (lambda v: _fmt(v, currency)) if kind == "money" else _pct
    return (
        f"<span style='font-weight:400;color:#6b7280;font-size:11px;display:block;'>"
        f"Room {fmt(room_val)} · Dorm {fmt(dorm_val)}</span>"
    )


def _top_countries(db: Session, branch_id, days: int = 90, limit: int = 5):
    # GROUP BY guest_country_code (indexed via idx_reservations_country_code)
    # — the previous GROUP BY on raw guest_country plus a LOWER(...) LIKE
    # '%unknown%' filter defeated every index and hit statement_timeout on
    # busier branches. MIN(guest_country) gives us a display name without
    # losing index-only scan on the GROUP BY column. "Unknown" rows are
    # filtered in Python since N is small post-aggregation.
    cutoff = _ict_today() - timedelta(days=days)
    rows = (
        db.query(
            func.min(Reservation.guest_country).label("country"),
            Reservation.guest_country_code,
            func.count(Reservation.id).label("cnt"),
        )
        .filter(
            Reservation.branch_id == branch_id,
            Reservation.check_in_date >= cutoff,
            Reservation.guest_country_code.isnot(None),
            or_(
                Reservation.status == None,
                Reservation.status.notin_(list(_EXCLUDED_STATUSES)),
            ),
        )
        .group_by(Reservation.guest_country_code)
        .order_by(func.count(Reservation.id).desc())
        .all()
    )
    out = []
    for r in rows:
        name = r.country or ""
        if "unknown" in name.lower():
            continue
        out.append({"country": name, "bookings": r.cnt})
        if len(out) >= limit:
            break
    return out


def _growth_countries(db: Session, branch_id, limit: int = 3):
    """Top countries with biggest booking growth (90d vs prior 90d).

    GROUPs by indexed guest_country_code and pulls MIN(guest_country) for a
    display name in one row. Both windows must key on the same value so
    countries match across periods — using the code (stable) is safer than
    the raw string (which may have spelling variations).
    """
    today = _ict_today()
    recent_start = today - timedelta(days=90)
    prev_start = today - timedelta(days=180)

    def _by_code(d_from, d_to=None):
        # d_to is exclusive; None preserves the original "no upper bound"
        # semantics for the recent window (includes future check-ins).
        filters = [
            Reservation.branch_id == branch_id,
            Reservation.check_in_date >= d_from,
            Reservation.guest_country_code.isnot(None),
            or_(Reservation.status == None,
                Reservation.status.notin_(list(_EXCLUDED_STATUSES))),
        ]
        if d_to is not None:
            filters.append(Reservation.check_in_date < d_to)
        q = db.query(
            func.min(Reservation.guest_country).label("country"),
            Reservation.guest_country_code,
            func.count(Reservation.id).label("cnt"),
        ).filter(*filters).group_by(Reservation.guest_country_code).all()
        return {r.guest_country_code: (r.country, r.cnt) for r in q}

    recent = _by_code(recent_start)
    prev = _by_code(prev_start, recent_start)

    results = []
    for code, (name, rec_cnt) in recent.items():
        # Min volume — under 50 bookings/90d the WoW % is statistical noise
        # (a country going from 1→5 bookings is a "+400%" Hottest Market
        # that nobody should actually act on). Feedback 2026-05-18.
        if rec_cnt < 50:
            continue
        prv_name, prv_cnt = prev.get(code, (name, 0))
        if prv_cnt == 0:
            continue
        display = name or prv_name or ""
        if "unknown" in display.lower():
            continue
        growth = round((rec_cnt - prv_cnt) / prv_cnt * 100, 1)
        if growth > 0:
            results.append({"country": display, "recent": rec_cnt,
                            "prev": prv_cnt, "growth_pct": growth})

    results.sort(key=lambda x: x["growth_pct"], reverse=True)
    return results[:limit]


# ── Branch → Gov destination mapping ──────────────────────────────────────────
_BRANCH_NAME_DEST_MAP = {
    "meander taipei": "Taiwan",
    "meander 1948": "Taiwan",
    "meander oani": "Taiwan",
    "oani": "Taiwan",
    "meander osaka": "Japan",
    "meander saigon": "Vietnam",
}

MONTH_COLS = ["jan", "feb", "mar", "apr", "may", "jun",
              "jul", "aug", "sep", "oct", "nov", "dec"]


def _resolve_branch_dest(branch_name: str) -> Optional[str]:
    bn = branch_name.lower().strip()
    for key, dest in _BRANCH_NAME_DEST_MAP.items():
        if key in bn or bn in key:
            return dest
    return None


def _gov_top_countries(db: Session, destination: str, month_num: int, limit: int = 5):
    """Top source countries by gov visitor count for a specific month and destination."""
    col_attr = getattr(GovVisitorData, MONTH_COLS[month_num - 1])
    rows = (
        db.query(
            GovVisitorData.source_country,
            GovVisitorData.rank,
            col_attr.label("visitor_count"),
            GovVisitorData.total,
        )
        .filter(
            func.lower(GovVisitorData.destination) == destination.lower(),
            col_attr > 0,
        )
        .order_by(col_attr.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "source_country": r.source_country,
            "gov_rank": r.rank,
            "visitor_count": int(r.visitor_count or 0),
            "yearly_total": int(r.total or 0),
        }
        for r in rows
    ]


def _gov_growth_countries(db: Session, destination: str, target_month: int, prior_month: int, limit: int = 5):
    """Countries with highest MoM growth in gov visitor data (target vs prior month)."""
    col_target = getattr(GovVisitorData, MONTH_COLS[target_month - 1])
    col_prior = getattr(GovVisitorData, MONTH_COLS[prior_month - 1])
    rows = (
        db.query(
            GovVisitorData.source_country,
            col_target.label("target_visitors"),
            col_prior.label("prior_visitors"),
        )
        .filter(
            func.lower(GovVisitorData.destination) == destination.lower(),
            col_target > 0,
            col_prior > 0,
        )
        .all()
    )
    results = []
    for r in rows:
        target_v = int(r.target_visitors or 0)
        prior_v = int(r.prior_visitors or 0)
        if prior_v == 0:
            continue
        growth = round((target_v - prior_v) / prior_v * 100, 1)
        if growth > 0:
            results.append({
                "source_country": r.source_country,
                "target_visitors": target_v,
                "prior_visitors": prior_v,
                "growth_pct": growth,
            })
    results.sort(key=lambda x: x["growth_pct"], reverse=True)
    return results[:limit]


def _actual_occ_pct(db: Session, branch_id, year: int, month: int, total_rooms: int) -> Optional[float]:
    """Compute MTD OCC% the right way: total room-nights sold / (rooms × days).

    Was previously using AVG(daily_metrics.occ_pct), which gave skewed
    numbers when one day had 0 sold (closed days, sync gaps) — that 0
    dragged the simple average down even when the rest of the month was
    healthy. The room-nights formula matches metrics-rules.md and never
    weighs a low-data day against a normal day.
    """
    if total_rooms <= 0:
        return None
    first_day = date(year, month, 1)
    today = _ict_today()
    last_day = min(today, date(year, month, calendar.monthrange(year, month)[1]))
    days = (last_day - first_day).days + 1
    if days <= 0:
        return None
    sold = db.query(
        func.coalesce(func.sum(DailyMetrics.total_sold), 0),
    ).filter(
        DailyMetrics.branch_id == branch_id,
        DailyMetrics.date >= first_day,
        DailyMetrics.date <= last_day,
    ).scalar()
    sold_int = int(sold or 0)
    if sold_int == 0:
        return None
    return sold_int / (total_rooms * days)


# Branch display order on the weekly report — tabs + cards follow this.
# Matched as case-insensitive substrings of Branch.name; any branch not
# listed falls to the end, alphabetically. Requested by growth team
# 2026-05-26: Taipei → 1948 → Oani → Osaka → Saigon.
_BRANCH_DISPLAY_ORDER = ("taipei", "1948", "oani", "osaka", "saigon")


def _branch_display_sort_key(b):
    name = (b.name or "").lower()
    for i, kw in enumerate(_BRANCH_DISPLAY_ORDER):
        if kw in name:
            return (i, name)
    return (len(_BRANCH_DISPLAY_ORDER), name)


def _build_report(db: Session):
    today = _ict_today()
    branches = sorted(
        db.query(Branch).filter_by(is_active=True).all(),
        key=_branch_display_sort_key,
    )
    report = []

    # No Cloudbeds pre-sync here — daily_metrics is already refreshed twice
    # daily (09:00 + 14:00 ICT) by cron-insights.yml, so the data this
    # report reads is at most ~7h stale. The previous in-line pre-sync
    # added 30-60s per build and was the main reason the report page took
    # 30-90s to load.

    for b in branches:
        total_rooms = b.total_rooms or 0
        # Pass split room/dorm counts so compute_kpi_summary uses the
        # split-based forecast (room_adr × pred_room + dorm_adr × pred_dorm)
        # — same path the Group Summary dashboard uses. Without these the
        # function silently falls back to a single-OCC × total_rooms × ADR
        # estimate and the email's Adjusted Forecast diverges from the
        # dashboard's Adjusted column (caught for Taipei: cache showed
        # NT$5.18M, dashboard NT$5.50M, same KPI inputs).
        total_room_count = b.total_room_count or 0
        total_dorm_count = b.total_dorm_count or 0
        # Each section wrapped in _safe_section so a Postgres statement_timeout
        # on a country GROUP BY (the usual culprit) downgrades that section to
        # empty defaults instead of crashing the entire weekly email.
        kpi = _safe_section(
            db, f"kpi[{b.name}]",
            lambda: compute_kpi_summary(
                db, b.id, today.year, today.month, total_rooms,
                total_room_count=total_room_count,
                total_dorm_count=total_dorm_count,
            ),
            {"actual_revenue_native": None, "target_revenue_native": None,
             "achievement_pct": None, "avg_adr_native": None,
             "predicted_occ_pct": None, "days_elapsed": None,
             "total_days": None, "occ_forecast_native": None},
        )
        nxt = _safe_section(
            db, f"next_forecast[{b.name}]",
            lambda: compute_next_month_forecast(
                db, b.id, total_rooms, today.year, today.month,
                total_room_count=total_room_count,
                total_dorm_count=total_dorm_count,
            ),
            {"next_month": None, "next_year": None,
             "next_month_forecast_native": None, "next_month_target_native": None,
             "next_month_adr": None, "next_month_booked_nights": None,
             "next_month_booked_revenue": None, "predicted_occ_next": None},
        )
        top = _safe_section(db, f"top_countries[{b.name}]",
                            lambda: _top_countries(db, b.id), [])
        growth = _safe_section(db, f"growth_countries[{b.name}]",
                               lambda: _growth_countries(db, b.id), [])

        # ── KPI Dashboard "Adjusted" inputs (Home page) ────────────────────
        # Adjusted Forecast = Forecast × (1 − deduction%) + Other Revenue.
        # Operators set deduction% + other_rev per month in /kpi to model
        # known headwinds (commissions, comps) and add-ons (F&B, parking).
        # Pull both for current month + next month so Exec Summary and
        # Next-Month columns show the same numbers as the dashboard.
        kt_cur = (db.query(KPITarget)
                    .filter_by(branch_id=b.id, year=today.year, month=today.month)
                    .first())
        nxt_year = nxt.get("next_year") or today.year
        nxt_month = nxt.get("next_month") or (today.month % 12 + 1)
        kt_nxt = (db.query(KPITarget)
                    .filter_by(branch_id=b.id, year=nxt_year, month=nxt_month)
                    .first())
        deduct_cur = float(kt_cur.deduction_pct or 0) if kt_cur else 0.0
        other_rev_cur = float(kt_cur.other_revenue_native or 0) if kt_cur else 0.0
        deduct_nxt = float(kt_nxt.deduction_pct or 0) if kt_nxt else 0.0
        other_rev_nxt = float(kt_nxt.other_revenue_native or 0) if kt_nxt else 0.0

        def _adjust(forecast, deduct_pct, other_rev):
            if forecast is None:
                return None
            return max(0.0, float(forecast) * (1 - deduct_pct / 100) + other_rev)

        adjusted_cur = _adjust(kpi["occ_forecast_native"], deduct_cur, other_rev_cur)
        adjusted_nxt = _adjust(nxt["next_month_forecast_native"], deduct_nxt, other_rev_nxt)

        # Country Intel scores (Hot / Warm / Cold)
        country_intel = _safe_section(
            db, f"country_intel[{b.name}]",
            lambda: score_countries(db, branch_id=b.id, top_n=10),
            [],
        )

        # Actual OCC from daily_metrics
        actual_occ = _safe_section(
            db, f"actual_occ[{b.name}]",
            lambda: _actual_occ_pct(db, b.id, today.year, today.month, total_rooms),
            None,
        )

        # Predicted/forecast OCC from KPI targets
        predicted_occ_current = kpi.get("predicted_occ_pct")
        predicted_occ_next = nxt.get("predicted_occ_next")
        # Room/Dorm forecast-OCC split (from KPITarget.predicted_room/dorm_occ_pct)
        predicted_room_occ_current = kpi.get("predicted_room_occ_pct")
        predicted_dorm_occ_current = kpi.get("predicted_dorm_occ_pct")
        predicted_room_occ_next = nxt.get("predicted_room_occ_next")
        predicted_dorm_occ_next = nxt.get("predicted_dorm_occ_next")

        # Gov visitor forecast for recommendations
        dest = _resolve_branch_dest(b.name)
        # Paid Ads → next month (demand arriving soon, act now)
        # KOLs → month+2 (peak travel — need KOL lead time to recruit/activate)
        ads_month = today.month % 12 + 1           # next month
        kol_month = (today.month + 1) % 12 + 1     # month after next
        ads_prior = today.month                      # current month (for growth calc)
        gov_ads_top = _safe_section(
            db, f"gov_ads_top[{b.name}]",
            lambda: _gov_top_countries(db, dest, ads_month, limit=5) if dest else [],
            [],
        )
        gov_ads_growth = _safe_section(
            db, f"gov_ads_growth[{b.name}]",
            lambda: _gov_growth_countries(db, dest, ads_month, ads_prior, limit=5) if dest else [],
            [],
        )
        gov_kol_top = _safe_section(
            db, f"gov_kol_top[{b.name}]",
            lambda: _gov_top_countries(db, dest, kol_month, limit=5) if dest else [],
            [],
        )

        # Analytical sections (summary, outliers, behavior, channel mix,
        # country insights, ad budget optimizer). This is the heaviest call
        # — runs ~10 country GROUP BYs per branch — most likely to hit
        # statement_timeout. Wrap so failure degrades to empty analytics
        # block instead of killing the email.
        analytics = _safe_section(
            db, f"analytics[{b.name}]",
            lambda: build_branch_analytics(db, b, today),
            {},
        )

        report.append({
            "branch_id": str(b.id),
            "branch_name": b.name,
            "branch_city": b.city,
            "currency": b.currency,
            "analytics": analytics,
            # This month
            "actual_revenue": kpi["actual_revenue_native"],
            "target_revenue": kpi["target_revenue_native"],
            "achievement_pct": round(kpi["achievement_pct"] * 100, 1) if kpi["achievement_pct"] else None,
            "avg_adr": kpi["avg_adr_native"],
            "room_adr": kpi.get("room_adr_native"),
            "dorm_adr": kpi.get("dorm_adr_native"),
            "avg_occ_pct": round(actual_occ * 100, 1) if actual_occ else None,
            "predicted_occ_pct": round(predicted_occ_current * 100, 1) if predicted_occ_current else None,
            "predicted_room_occ_pct": round(predicted_room_occ_current * 100, 1) if predicted_room_occ_current else None,
            "predicted_dorm_occ_pct": round(predicted_dorm_occ_current * 100, 1) if predicted_dorm_occ_current else None,
            "days_elapsed": kpi["days_elapsed"],
            "total_days": kpi["total_days"],
            "occ_forecast": kpi["occ_forecast_native"],
            "occ_forecast_pct": round(kpi["occ_forecast_native"] / kpi["target_revenue_native"] * 100, 1)
                if (kpi["occ_forecast_native"] and kpi["target_revenue_native"]) else None,
            # KPI Dashboard "Adjusted" inputs + computed Adjusted Forecast
            "deduction_pct": deduct_cur,
            "other_revenue_native": other_rev_cur,
            "adjusted_forecast": round(adjusted_cur, 2) if adjusted_cur is not None else None,
            "adjusted_forecast_pct": (
                round(adjusted_cur / kpi["target_revenue_native"] * 100, 1)
                if (adjusted_cur is not None and kpi["target_revenue_native"]) else None
            ),
            # Next month
            "next_month": nxt["next_month"],
            "next_year": nxt["next_year"],
            "next_forecast": nxt["next_month_forecast_native"],
            "next_target": nxt["next_month_target_native"],
            "next_forecast_pct": round(nxt["next_month_forecast_native"] / nxt["next_month_target_native"] * 100, 1)
                if (nxt["next_month_forecast_native"] and nxt["next_month_target_native"]) else None,
            # Next month adjustment fields
            "next_deduction_pct": deduct_nxt,
            "next_other_revenue_native": other_rev_nxt,
            "next_adjusted_forecast": round(adjusted_nxt, 2) if adjusted_nxt is not None else None,
            "next_adjusted_forecast_pct": (
                round(adjusted_nxt / nxt["next_month_target_native"] * 100, 1)
                if (adjusted_nxt is not None and nxt["next_month_target_native"]) else None
            ),
            "next_adr": nxt["next_month_adr"],
            "next_room_adr": nxt.get("next_month_room_adr"),
            "next_dorm_adr": nxt.get("next_month_dorm_adr"),
            "next_booked_nights": nxt["next_month_booked_nights"],
            "next_booked_revenue": nxt.get("next_month_booked_revenue"),
            "predicted_occ_next": round(predicted_occ_next * 100, 1) if predicted_occ_next else None,
            "predicted_room_occ_next": round(predicted_room_occ_next * 100, 1) if predicted_room_occ_next else None,
            "predicted_dorm_occ_next": round(predicted_dorm_occ_next * 100, 1) if predicted_dorm_occ_next else None,
            # Country intel
            "top_countries": top,
            "growth_countries": growth,
            "country_intel": country_intel,
            # Gov forecast for recommendations
            "gov_ads_top": gov_ads_top,
            "gov_ads_growth": gov_ads_growth,
            "gov_kol_top": gov_kol_top,
        })

    return report


# ── Analytical section renderers ─────────────────────────────────────────────

_TABLE_TH = (
    "padding:6px 10px;text-align:left;color:#6b7280;font-weight:500;"
    "font-size:11px;text-transform:uppercase;background:#f9fafb;"
    "border-bottom:1px solid #e5e7eb;"
)
_TABLE_TD = "padding:6px 10px;color:#374151;font-size:12px;border-bottom:1px solid #f3f4f6;"


def _render_exec_summary(report: list, today: date) -> str:
    """Top-of-email pacing table — one row per branch.

    Revenue / OCC / ADR pulled from `b` (kpi_engine via Cloudbeds Insights
    filtered API) — same source the dashboard's Group Summary page uses.
    The previously-shown `mtd.revenue` came from daily_metrics cache which
    excludes nothing and could lag the live Insights pull, so the email
    showed lower numbers than the dashboard. RevPAR computed as ADR × OCC
    so it stays consistent with the same source.
    """
    rows_html = []
    for b in report:
        cur = b["currency"]
        ach = b["achievement_pct"]
        a = b.get("analytics", {})
        wow = a.get("summary", {}).get("wow_revenue_pct")
        yoy = a.get("summary", {}).get("yoy_revenue_pct")
        wow_html = f"<span style='color:{'#16a34a' if (wow or 0)>=0 else '#dc2626'}'>{_signed_pct(wow)}</span>" if wow is not None else "—"
        yoy_html = f"<span style='color:{'#16a34a' if (yoy or 0)>=0 else '#dc2626'}'>{_signed_pct(yoy)}</span>" if yoy is not None else "—"

        # RevPAR = ADR × OCC% (per metrics-rules)
        adr = b.get("avg_adr") or 0
        occ_pct = b.get("avg_occ_pct") or 0  # 0..100 scale (already %)
        revpar = round(adr * occ_pct / 100, 2) if (adr and occ_pct) else None

        # Forecast cell shows the ADJUSTED value from the KPI Dashboard
        # (same as Group Summary's Adjusted column). Operators set
        # deduction% + other_rev per month to model commissions / comps /
        # ancillary on top of the raw ADR×predicted-nights forecast.
        # The formula is shown as a sub-line so the math is auditable.
        raw_fcst = b.get("occ_forecast")
        adj_fcst = b.get("adjusted_forecast")
        adj_fcst_pct = b.get("adjusted_forecast_pct")
        deduct = b.get("deduction_pct") or 0
        other_rev = b.get("other_revenue_native") or 0

        if adj_fcst_pct is not None:
            fcst_color = (
                "#16a34a" if adj_fcst_pct >= 100 else
                "#ca8a04" if adj_fcst_pct >= 90 else
                "#ea580c" if adj_fcst_pct >= 75 else
                "#dc2626"
            )
            # Formula sub-line — only show non-trivial adjustments
            if deduct > 0 or other_rev > 0:
                deduct_str = f"−{deduct:g}%" if deduct > 0 else "−0%"
                rev_str = f" + {_fmt(other_rev, cur)}" if other_rev > 0 else ""
                formula_line = (
                    f"<div style='font-size:9px;color:#9ca3af;line-height:1.3;'>"
                    f"= {_fmt(raw_fcst, cur)} × (1{deduct_str}){rev_str}"
                    f"</div>"
                )
            else:
                formula_line = (
                    f"<div style='font-size:9px;color:#9ca3af;'>no adjustment</div>"
                )
            fcst_html = (
                f"<div style='font-weight:700;color:{fcst_color};'>{_fmt(adj_fcst, cur)}</div>"
                f"<div style='font-size:10px;color:{fcst_color};'>{_pct(adj_fcst_pct)} of target</div>"
                f"{formula_line}"
            )
        else:
            fcst_html = "<span style='color:#9ca3af;'>not set</span>"

        # Pacing color now follows ADJUSTED forecast % (matches dashboard
        # color). MTD pacing is naturally low early in the month so we
        # color by the forward-looking forecast instead.
        if adj_fcst_pct is not None:
            ach_color = (
                "#16a34a" if adj_fcst_pct >= 100 else
                "#ca8a04" if adj_fcst_pct >= 90 else
                "#ea580c" if adj_fcst_pct >= 75 else
                "#dc2626"
            )
        else:
            ach_color = "#6b7280"

        bid = b['branch_id']
        rows_html.append(f"""
          <tr>
            <td style="{_TABLE_TD}"><strong>{b['branch_name']}</strong></td>
            <td style="{_TABLE_TD};text-align:right;"{_cell_attrs(bid, 'revenue_mtd')}>{_fmt(b.get('actual_revenue'), cur)}</td>
            <td style="{_TABLE_TD};text-align:right;"{_cell_attrs(bid, 'target')}>{_fmt(b.get('target_revenue'), cur)}</td>
            <td style="{_TABLE_TD};text-align:right;color:{ach_color};font-weight:700;"{_cell_attrs(bid, 'pacing')}>{_pct(ach)}</td>
            <td style="{_TABLE_TD};text-align:right;vertical-align:top;"{_cell_attrs(bid, 'forecast')}>{fcst_html}</td>
            <td style="{_TABLE_TD};text-align:right;"{_cell_attrs(bid, 'occ')}>{_pct(b.get('avg_occ_pct'))}</td>
            <td style="{_TABLE_TD};text-align:right;"{_cell_attrs(bid, 'adr')}>{_fmt(b.get('avg_adr'), cur)}</td>
            <td style="{_TABLE_TD};text-align:right;"{_cell_attrs(bid, 'revpar')}>{_fmt(revpar, cur)}</td>
            <td style="{_TABLE_TD};text-align:right;"{_cell_attrs(bid, 'wow_revenue')}>{wow_html}</td>
            <td style="{_TABLE_TD};text-align:right;"{_cell_attrs(bid, 'yoy_revenue')}>{yoy_html}</td>
          </tr>""")

    ws_iso = _week_start(today).isoformat()
    return f"""
    <div id="exec-summary" class="hid-exec-summary" data-week-start="{ws_iso}" style="background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:20px;margin-bottom:16px;">
      <h3 style="margin:0 0 12px;font-size:15px;font-weight:700;color:#111827;">📊 Executive Summary</h3>
      <div style="overflow-x:auto;">
        <table style="width:100%;border-collapse:collapse;font-size:12px;">
          <tr>
            <th style="{_TABLE_TH}">Branch</th>
            <th style="{_TABLE_TH};text-align:right;">Revenue MTD</th>
            <th style="{_TABLE_TH};text-align:right;">Target</th>
            <th style="{_TABLE_TH};text-align:right;">Pacing</th>
            <th style="{_TABLE_TH};text-align:right;" title="ADR × predicted nights for the month">Forecast</th>
            <th style="{_TABLE_TH};text-align:right;">OCC</th>
            <th style="{_TABLE_TH};text-align:right;">ADR</th>
            <th style="{_TABLE_TH};text-align:right;">RevPAR</th>
            <th style="{_TABLE_TH};text-align:right;">WoW Rev</th>
            <th style="{_TABLE_TH};text-align:right;">YoY Rev</th>
          </tr>
          {''.join(rows_html)}
        </table>
      </div>
      <p style="margin:10px 0 0;font-size:11px;color:#6b7280;">
        Revenue / OCC / ADR = Cloudbeds Insights filtered (excl. Blogger / House Use / KOL / Special Case / Work Exchange) — same source as the Group Summary dashboard.<br/>
        Forecast = <strong>Adjusted</strong> column from Home: <code>Adjusted = Forecast × (1 − Deduction%) + Other Revenue</code>
        (raw Forecast = ADR × predicted-nights; set Deduction% + Other Rev in KPI Dashboard). Pacing color follows Adjusted vs target:
        <span style="color:#16a34a;">green ≥100%</span> ·
        <span style="color:#ca8a04;">yellow 90-99%</span> ·
        <span style="color:#ea580c;">orange 75-89%</span> ·
        <span style="color:#dc2626;">red &lt;75%</span>.<br/>
        RevPAR = ADR × OCC. WoW Rev = last calendar week vs prev calendar week. YoY Rev = MTD this year vs same MTD last year (— if no prior-year data for this window).
      </p>
    </div>"""


def _render_outliers(b: dict) -> str:
    out = b.get("analytics", {}).get("outliers", [])
    if not out:
        return ""
    cur = b["currency"]
    bid = b["branch_id"]
    rows = []
    for o in out:
        arrow = "▲" if o["direction"] == "spike" else "▼"
        color = "#16a34a" if o["direction"] == "spike" else "#dc2626"
        reasons = " · ".join(o["reasons"]) if o["reasons"] else "no tagged cause"
        attrs = _cell_attrs(bid, f"outlier.{o['date']}", f"Outlier — {o['date']} ({o['direction']})")
        rows.append(
            f"<tr{attrs}><td style='{_TABLE_TD}'>{o['date']}</td>"
            f"<td style='{_TABLE_TD};text-align:right;color:{color};font-weight:600;'>{arrow} {o['direction']}</td>"
            f"<td style='{_TABLE_TD};text-align:right;'>{_fmt(o['revenue'], cur)}</td>"
            f"<td style='{_TABLE_TD};text-align:right;'>{o['occ_pct']:.2f}%</td>"
            f"<td style='{_TABLE_TD};color:#6b7280;'>{reasons}</td></tr>"
        )
    return f"""
    <div style="margin-top:14px;padding-top:12px;border-top:1px solid #f3f4f6;">
      <p style="margin:0 0 6px;font-size:12px;font-weight:600;color:#374151;">⚡ Outliers (last week, vs 30d baseline σ)</p>
      <p style="margin:0 0 4px;font-size:11px;color:#9ca3af;">
        Source: daily_metrics (Cloudbeds Insights overlay) by stay date.
      </p>
      <table style="width:100%;border-collapse:collapse;">
        <tr><th style="{_TABLE_TH}">Date</th><th style="{_TABLE_TH};text-align:right;">Type</th>
            <th style="{_TABLE_TH};text-align:right;">Revenue</th><th style="{_TABLE_TH};text-align:right;">OCC</th>
            <th style="{_TABLE_TH}">Reasons</th></tr>
        {''.join(rows)}
      </table>
    </div>"""


def _render_behavior(b: dict) -> str:
    beh = b.get("analytics", {}).get("behavior")
    if not beh:
        return ""
    bid = b["branch_id"]

    def _pp_delta_html(val):
        if val is None:
            return "<span style='color:#9ca3af;'>n/a</span>"
        # Higher cancel rate is bad — flip the color convention
        color = "#16a34a" if val < 0 else "#dc2626" if val > 0 else "#6b7280"
        sign = "+" if val >= 0 else ""
        return f"<span style='color:{color}'>{sign}{val:.2f}pp</span>"

    def _wow_pct_html(val, lower_is_better=False):
        if val is None:
            return "<span style='color:#9ca3af;'>n/a</span>"
        good = (val < 0) if lower_is_better else (val >= 0)
        color = "#16a34a" if good else "#dc2626"
        return f"<span style='color:{color}'>{_signed_pct(val)}</span>"

    cxl_rows = []
    for c in beh["cancellation_by_source"][:6]:
        cat = c['source_category']
        attrs = _cell_attrs(bid, f"behavior.cancellation.{cat}", f"Cancellation — {cat}")
        cxl_rows.append(
            f"<tr{attrs}><td style='{_TABLE_TD}'>{cat}</td>"
            f"<td style='{_TABLE_TD};text-align:right;'>{c['total']:,}</td>"
            f"<td style='{_TABLE_TD};text-align:right;'>{c['cancelled']:,}</td>"
            f"<td style='{_TABLE_TD};text-align:right;font-weight:600;'>{_pct(c['pct'])}</td>"
            f"<td style='{_TABLE_TD};text-align:right;color:#9ca3af;font-size:10px;'>"
            f"{_pct(c.get('prev_pct'))}</td>"
            f"<td style='{_TABLE_TD};text-align:right;'>{_pp_delta_html(c.get('pp_delta'))}</td></tr>"
        )

    def _bucket_row(label, val, total, prefix: str, label_prefix: str):
        pct = f"{val/total*100:.2f}%" if total > 0 else "—"
        attrs = _cell_attrs(bid, f"{prefix}.{label}", f"{label_prefix} — {label}")
        return (
            f"<tr{attrs}><td style='{_TABLE_TD}'>{label}</td>"
            f"<td style='{_TABLE_TD};text-align:right;'>{val:,}</td>"
            f"<td style='{_TABLE_TD};text-align:right;color:#6b7280;'>{pct}</td></tr>"
        )

    lt_total = sum(beh["lead_time_buckets"].values())
    lt_rows = "".join(
        _bucket_row(k, v, lt_total, "behavior.lead_time", "Lead time")
        for k, v in beh["lead_time_buckets"].items()
    )

    los_total = sum(beh["los_buckets"].values())
    los_rows = "".join(
        _bucket_row(k, v, los_total, "behavior.los", "LOS")
        for k, v in beh["los_buckets"].items()
    )

    cxl_overall = beh.get("cancellation_overall_pct")
    cxl_overall_pp = beh.get("cancellation_overall_pp_delta")
    cxl_overall_html = (
        f"{_pct(cxl_overall)} {_pp_delta_html(cxl_overall_pp)} WoW"
        if cxl_overall is not None else "—"
    )
    lead_avg = beh.get("lead_time_avg_days")
    lead_prev = beh.get("lead_time_avg_days_prev")
    lead_wow_html = _wow_pct_html(beh.get("lead_time_wow_pct"))
    lead_label = (
        f"avg {lead_avg}d (prev {lead_prev or '—'}d, {lead_wow_html} WoW)"
        if lead_avg is not None else "—"
    )
    los_avg = beh.get("los_avg_nights")
    los_prev = beh.get("los_avg_nights_prev")
    los_wow_html = _wow_pct_html(beh.get("los_wow_pct"))
    los_label = (
        f"avg {los_avg}n (prev {los_prev or '—'}n, {los_wow_html} WoW)"
        if los_avg is not None else "—"
    )

    return f"""
    <div style="margin-top:14px;padding-top:12px;border-top:1px solid #f3f4f6;">
      <p style="margin:0 0 4px;font-size:12px;font-weight:600;color:#374151;">
        🧭 Booking Behavior (last week · {beh['window_start']} → {beh['window_end']})
      </p>
      <p style="margin:0 0 6px;font-size:11px;color:#9ca3af;">
        Source: reservations by check-in date (last week's stays). Compared to prev week ({beh['prev_window_start']} → {beh['prev_window_end']}).
        Overall cancel: <strong style="color:#374151;">{cxl_overall_html}</strong>.
      </p>
      <table style="width:48%;display:inline-table;border-collapse:collapse;margin-right:2%;vertical-align:top;">
        <tr><th style="{_TABLE_TH}" colspan="6">Cancellation by source · vs prev week</th></tr>
        <tr><th style="{_TABLE_TH}">Cat.</th>
            <th style="{_TABLE_TH};text-align:right;">Total</th>
            <th style="{_TABLE_TH};text-align:right;">Cxl</th>
            <th style="{_TABLE_TH};text-align:right;">%</th>
            <th style="{_TABLE_TH};text-align:right;">Prev %</th>
            <th style="{_TABLE_TH};text-align:right;">Δ pp</th></tr>
        {''.join(cxl_rows) or '<tr><td style="'+_TABLE_TD+'" colspan="6">No data</td></tr>'}
      </table>
      <table style="width:24%;display:inline-table;border-collapse:collapse;margin-right:2%;vertical-align:top;">
        <tr><th style="{_TABLE_TH}" colspan="3">Lead time · {lead_label}</th></tr>
        <tr><th style="{_TABLE_TH}">Bucket</th><th style="{_TABLE_TH};text-align:right;">Count</th><th style="{_TABLE_TH};text-align:right;">%</th></tr>
        {lt_rows}
      </table>
      <table style="width:22%;display:inline-table;border-collapse:collapse;vertical-align:top;">
        <tr><th style="{_TABLE_TH}" colspan="3">LOS · {los_label}</th></tr>
        <tr><th style="{_TABLE_TH}">Bucket</th><th style="{_TABLE_TH};text-align:right;">Count</th><th style="{_TABLE_TH};text-align:right;">%</th></tr>
        {los_rows}
      </table>
    </div>"""


def _render_channel_mix(b: dict) -> str:
    mix = b.get("analytics", {}).get("channel_mix")
    if not mix or mix["total_nights"] == 0:
        return ""
    cur = b["currency"]
    bid = b["branch_id"]

    def _wow(val):
        if val is None:
            return "<span style='color:#9ca3af;'>n/a</span>"
        color = "#16a34a" if val >= 0 else "#dc2626"
        return f"<span style='color:{color}'>{_signed_pct(val)}</span>"

    cat_rows = []
    for c in mix["categories"]:
        cat = c['source_category']
        attrs = _cell_attrs(bid, f"channel.category.{cat}", f"Channel category — {cat}")
        cat_rows.append(
            f"<tr{attrs}>"
            f"<td style='{_TABLE_TD}'>{cat}</td>"
            f"<td style='{_TABLE_TD};text-align:right;'>{c['room_nights']:,}</td>"
            f"<td style='{_TABLE_TD};text-align:right;color:#9ca3af;font-size:10px;'>{c.get('prev_room_nights', 0):,}</td>"
            f"<td style='{_TABLE_TD};text-align:right;'>{_wow(c.get('wow_nights_pct'))}</td>"
            f"<td style='{_TABLE_TD};text-align:right;'>{_pct(c['nights_share_pct'])}</td>"
            f"<td style='{_TABLE_TD};text-align:right;'>{_fmt(c['revenue_native'], cur)}</td>"
            f"<td style='{_TABLE_TD};text-align:right;'>{_wow(c.get('wow_revenue_pct'))}</td>"
            f"</tr>"
        )

    src_rows = []
    for s in mix["top_sources"]:
        src = s['source']
        attrs = _cell_attrs(bid, f"channel.source.{src}", f"Top source — {src}")
        src_rows.append(
            f"<tr{attrs}>"
            f"<td style='{_TABLE_TD}'>{src}</td>"
            f"<td style='{_TABLE_TD};text-align:right;'>{s['room_nights']:,}</td>"
            f"<td style='{_TABLE_TD};text-align:right;color:#9ca3af;font-size:10px;'>{s.get('prev_room_nights', 0):,}</td>"
            f"<td style='{_TABLE_TD};text-align:right;'>{_wow(s.get('wow_nights_pct'))}</td>"
            f"<td style='{_TABLE_TD};text-align:right;'>{_pct(s['nights_share_pct'])}</td>"
            f"<td style='{_TABLE_TD};text-align:right;'>{_fmt(s['revenue_native'], cur)}</td>"
            f"</tr>"
        )

    trend_rows = []
    for t in mix["direct_trend"]:
        attrs = _cell_attrs(bid, f"channel.direct_trend.{t['label']}", f"Direct trend — {t['label']}")
        trend_rows.append(
            f"<tr{attrs}><td style='{_TABLE_TD}'>{t['label']}</td>"
            f"<td style='{_TABLE_TD};text-align:right;'>{t['direct_nights']:,}</td>"
            f"<td style='{_TABLE_TD};text-align:right;'>{t['total_nights']:,}</td>"
            f"<td style='{_TABLE_TD};text-align:right;font-weight:600;'>{_pct(t['direct_pct'])}</td></tr>"
        )

    return f"""
    <div style="margin-top:14px;padding-top:12px;border-top:1px solid #f3f4f6;">
      <p style="margin:0 0 4px;font-size:12px;font-weight:600;color:#374151;">
        📡 Channel Mix (last week · {mix['window_start']} → {mix['window_end']})
      </p>
      <p style="margin:0 0 6px;font-size:11px;color:#9ca3af;">
        Source: room-nights occupied last week · {mix['total_nights']:,} nights ({_wow(mix.get('wow_total_nights_pct'))} WoW) ·
        {_fmt(mix['total_revenue_native'], cur)} revenue ({_wow(mix.get('wow_total_revenue_pct'))} WoW) ·
        compared to prev week ({mix['prev_window_start']} → {mix['prev_window_end']})
      </p>
      <table style="width:100%;border-collapse:collapse;">
        <tr><th style="{_TABLE_TH}" colspan="7">By Category · vs prev week</th></tr>
        <tr>
          <th style="{_TABLE_TH}">Category</th>
          <th style="{_TABLE_TH};text-align:right;">Nights</th>
          <th style="{_TABLE_TH};text-align:right;">Prev</th>
          <th style="{_TABLE_TH};text-align:right;">WoW</th>
          <th style="{_TABLE_TH};text-align:right;">Share</th>
          <th style="{_TABLE_TH};text-align:right;">Revenue</th>
          <th style="{_TABLE_TH};text-align:right;">Rev WoW</th>
        </tr>
        {''.join(cat_rows)}
      </table>
      <table style="width:100%;border-collapse:collapse;margin-top:8px;">
        <tr><th style="{_TABLE_TH}" colspan="6">Top Sources · vs prev week</th></tr>
        <tr>
          <th style="{_TABLE_TH}">Source</th>
          <th style="{_TABLE_TH};text-align:right;">Nights</th>
          <th style="{_TABLE_TH};text-align:right;">Prev</th>
          <th style="{_TABLE_TH};text-align:right;">WoW</th>
          <th style="{_TABLE_TH};text-align:right;">Share</th>
          <th style="{_TABLE_TH};text-align:right;">Revenue</th>
        </tr>
        {''.join(src_rows)}
      </table>
      <table style="width:100%;border-collapse:collapse;margin-top:8px;">
        <tr><th style="{_TABLE_TH}" colspan="4">Direct booking trend (last 4 months)</th></tr>
        <tr><th style="{_TABLE_TH}">Month</th><th style="{_TABLE_TH};text-align:right;">Direct nights</th>
            <th style="{_TABLE_TH};text-align:right;">Total nights</th><th style="{_TABLE_TH};text-align:right;">Direct %</th></tr>
        {''.join(trend_rows)}
      </table>
    </div>"""


def _render_country_detail(b: dict) -> str:
    """Country Insights — 2 tables (booking date / check-in date) × top 10
    countries × 3 deltas (WoW / 30d / YoY).

    Replaces the previous "Top markets / Growing / Country Intel" chip
    line + "Growing / Shrinking / Emerging" chip lists. Per feedback
    (2026-05-04) those duplicated each other; consolidating into one
    block with explicit deltas is clearer.
    """
    ci = b.get("analytics", {}).get("countries")
    if not ci:
        return ""
    by_book = ci.get("by_booking_date") or []
    by_stay = ci.get("by_checkin_date") or []
    if not by_book and not by_stay:
        return ""

    cur = b["currency"]
    bid = b["branch_id"]
    windows = ci.get("windows") or {}

    def _delta(val, neutral_color="#9ca3af"):
        if val is None:
            return f"<span style='color:{neutral_color};'>n/a</span>"
        color = "#16a34a" if val >= 0 else "#dc2626"
        return f"<span style='color:{color};font-weight:600;'>{_signed_pct(val)}</span>"

    def _build_rows(rows: list, prefix: str, label_prefix: str) -> str:
        out = []
        for c in rows:
            country = c["country"]
            attrs = _cell_attrs(bid, f"{prefix}.{country}", f"{label_prefix} — {country}")
            out.append(
                f"<tr{attrs}>"
                f"<td style='{_TABLE_TD}'>{country}</td>"
                f"<td style='{_TABLE_TD};text-align:right;'>{c['bookings']:,}</td>"
                f"<td style='{_TABLE_TD};text-align:right;'>{c['nights']:,}</td>"
                f"<td style='{_TABLE_TD};text-align:right;'>{_fmt(c['revenue_native'], cur)}</td>"
                f"<td style='{_TABLE_TD};text-align:right;'>{_fmt(c['adr_native'], cur)}</td>"
                f"<td style='{_TABLE_TD};text-align:right;'>{_delta(c.get('wow_pct'))}</td>"
                f"<td style='{_TABLE_TD};text-align:right;'>{_delta(c.get('d30_pct'))}</td>"
                f"<td style='{_TABLE_TD};text-align:right;'>{_delta(c.get('yoy_pct'))}</td>"
                f"</tr>"
            )
        return "".join(out)

    book_rows = _build_rows(by_book, "country.book", "Country (booked)") or (
        f"<tr><td colspan='8' style='{_TABLE_TD};color:#9ca3af;'>No bookings in last 30d</td></tr>"
    )
    stay_rows = _build_rows(by_stay, "country.stay", "Country (check-in)") or (
        f"<tr><td colspan='8' style='{_TABLE_TD};color:#9ca3af;'>No stays in last 30d</td></tr>"
    )

    last_30 = windows.get("last_30") or ["", ""]
    last_7 = windows.get("last_7") or ["", ""]

    def _table(title: str, subtitle: str, rows_html: str) -> str:
        return f"""
      <p style="margin:10px 0 4px;font-size:12px;font-weight:600;color:#374151;">{title}</p>
      <p style="margin:0 0 4px;font-size:11px;color:#9ca3af;">{subtitle}</p>
      <table style="width:100%;border-collapse:collapse;">
        <tr>
          <th style="{_TABLE_TH}">Country</th>
          <th style="{_TABLE_TH};text-align:right;">Bookings</th>
          <th style="{_TABLE_TH};text-align:right;">Nights</th>
          <th style="{_TABLE_TH};text-align:right;">Revenue</th>
          <th style="{_TABLE_TH};text-align:right;">ADR</th>
          <th style="{_TABLE_TH};text-align:right;" title="last 7d vs prev 7d">WoW</th>
          <th style="{_TABLE_TH};text-align:right;" title="last 30d vs prior 30d">30d Δ</th>
          <th style="{_TABLE_TH};text-align:right;" title="last 30d vs same window prior year">YoY</th>
        </tr>
        {rows_html}
      </table>"""

    booking_table = _table(
        "📅 By Date Booked (campaign signal)",
        f"Top {len(by_book)} countries by reservation_date — when the booking landed.",
        book_rows,
    )
    checkin_table = _table(
        "🏨 By Check-in Date (stays)",
        f"Top {len(by_stay)} countries by check_in_date — when guests actually arrive.",
        stay_rows,
    )

    return f"""
    <div style="margin-top:14px;padding-top:12px;border-top:1px solid #f3f4f6;">
      <p style="margin:0 0 4px;font-size:12px;font-weight:600;color:#374151;">
        🌏 Country Insights — last 30d ({last_30[0]} → {last_30[1]})
      </p>
      <p style="margin:0 0 4px;font-size:11px;color:#9ca3af;">
        WoW = last 7d ({last_7[0]} → {last_7[1]}) vs prev 7d ·
        30d Δ = last 30d vs prior 30d (60..30 days ago) ·
        YoY = last 30d vs same window {_ict_today().year - 1}
      </p>
      {booking_table}
      {checkin_table}
    </div>"""


def _render_ad_optimizer(b: dict) -> str:
    ads = b.get("analytics", {}).get("ad_optimizer", [])
    if not ads:
        return ""
    cur = b["currency"]

    country_blocks = []
    for c in ads:
        action_color = {
            "BOOST": "#dc2626", "STABILIZE": "#ca8a04",
            "MAINTAIN": "#16a34a", "REVIEW": "#6b7280",
        }.get(c["action"], "#6b7280")

        holidays_html = (
            " · ".join(
                f"{h['name']} [{h['propensity']}]" for h in c["holidays"][:3]
            ) or "none in window"
        )

        camp_rows = []
        for cp in c["campaigns"]["campaigns"][:5]:
            roas_cell = f"{cp['roas']:.2f}x" if cp["roas"] is not None else "—"
            audience_chip = (
                f"<span style='background:#eef2ff;color:#4338ca;padding:1px 6px;border-radius:8px;font-size:10px;margin-right:4px;'>{cp['target_audience']}</span>"
                if cp.get("target_audience") else ""
            )
            funnel_chip = (
                f"<span style='background:#fef3c7;color:#92400e;padding:1px 6px;border-radius:8px;font-size:10px;margin-right:4px;'>{cp['funnel_stage']}</span>"
                if cp.get("funnel_stage") else ""
            )
            angle_chip = (
                f"<span style='background:#dcfce7;color:#166534;padding:1px 6px;border-radius:8px;font-size:10px;'>{cp['angle_name']}{' · '+cp['hook_type'] if cp.get('hook_type') else ''}</span>"
                if cp.get("angle_name") else ""
            )
            chips = audience_chip + funnel_chip + angle_chip
            usp_html = (
                f"<div style='font-size:11px;color:#6b7280;margin-top:2px;'><strong>USP:</strong> {cp['usp']}</div>"
                if cp.get("usp") else ""
            )
            body_html = (
                f"<div style='font-size:11px;color:#9ca3af;margin-top:2px;font-style:italic;'>“{cp['ad_body_excerpt']}”</div>"
                if cp.get("ad_body_excerpt") else ""
            )
            name_cell = (
                f"<div>{(cp['ad_name'] or cp['adset'] or cp['campaign'] or '')[:60]}</div>"
                f"<div style='margin-top:3px;'>{chips}</div>"
                f"{usp_html}{body_html}"
            )
            camp_rows.append(
                f"<tr><td style='{_TABLE_TD};vertical-align:top;'>{cp['channel'] or '—'}</td>"
                f"<td style='{_TABLE_TD};vertical-align:top;'>{name_cell}</td>"
                f"<td style='{_TABLE_TD};text-align:right;vertical-align:top;'>{_fmt(cp['cost'], cur)}</td>"
                f"<td style='{_TABLE_TD};text-align:right;vertical-align:top;'>{cp['impressions']:,}</td>"
                f"<td style='{_TABLE_TD};text-align:right;vertical-align:top;'>{_pct(cp['ctr_pct'])}</td>"
                f"<td style='{_TABLE_TD};text-align:right;vertical-align:top;'>{_pct(cp['cvr_pct'])}</td>"
                f"<td style='{_TABLE_TD};text-align:right;vertical-align:top;'>{cp['bookings']}</td>"
                f"<td style='{_TABLE_TD};text-align:right;vertical-align:top;'>{_fmt(cp['cpa'], cur)}</td>"
                f"<td style='{_TABLE_TD};text-align:right;vertical-align:top;font-weight:600;'>{roas_cell}</td></tr>"
            )

        recs_items = "".join(f"<li style='margin:2px 0;'>{r}</li>" for r in c["recommendations"])

        overall = c["campaigns"]
        country_blocks.append(f"""
        <div style="background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:14px;margin-bottom:10px;">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
            <div>
              <strong style="font-size:14px;color:#111827;">{c['country']}</strong>
              <span style="font-size:11px;color:#6b7280;margin-left:8px;">
                Spend MTD: {_fmt(c['spend_mtd'], cur)} · Lead time: {c['lead_time_days']}d
              </span>
            </div>
            <span style="background:{action_color};color:#fff;padding:3px 10px;border-radius:10px;font-size:11px;font-weight:700;">
              {c['action']}
            </span>
          </div>
          <p style="margin:0 0 8px;font-size:11px;color:#6b7280;">
            Target window: {c['window_start']} → {c['window_end']} ·
            Window OCC: {_pct(c['window_occ_pct'])} vs Predicted: {_pct(c['predicted_occ_pct'])}<br/>
            {c['action_reason']}<br/>
            <strong>Holidays:</strong> {holidays_html}
          </p>
          <table style="width:100%;border-collapse:collapse;margin-bottom:8px;">
            <tr><th style="{_TABLE_TH}">Ch.</th><th style="{_TABLE_TH}">Name</th>
                <th style="{_TABLE_TH};text-align:right;">Cost</th><th style="{_TABLE_TH};text-align:right;">Impr</th>
                <th style="{_TABLE_TH};text-align:right;">CTR</th><th style="{_TABLE_TH};text-align:right;">CVR</th>
                <th style="{_TABLE_TH};text-align:right;">Bk</th><th style="{_TABLE_TH};text-align:right;">CPA</th>
                <th style="{_TABLE_TH};text-align:right;">ROAS</th></tr>
            {''.join(camp_rows) or '<tr><td colspan="9" style="'+_TABLE_TD+'">No campaign rows</td></tr>'}
            <tr style="background:#f9fafb;font-weight:600;">
              <td style="{_TABLE_TD}" colspan="2">Overall</td>
              <td style="{_TABLE_TD};text-align:right;">{_fmt(overall['total_cost'], cur)}</td>
              <td style="{_TABLE_TD};text-align:right;">{overall['total_impressions']:,}</td>
              <td style="{_TABLE_TD};text-align:right;">{_pct(overall['overall_ctr_pct'])}</td>
              <td style="{_TABLE_TD};text-align:right;">{_pct(overall['overall_cvr_pct'])}</td>
              <td style="{_TABLE_TD};text-align:right;">{overall['total_bookings']}</td>
              <td style="{_TABLE_TD};text-align:right;">{_fmt(overall['overall_cpa'], cur)}</td>
              <td style="{_TABLE_TD};text-align:right;">{overall['overall_roas']:.2f}x</td>
            </tr>
          </table>
          <ul style="margin:0;padding-left:18px;font-size:12px;color:#374151;">{recs_items}</ul>
        </div>""")

    return f"""
    <div id="ad-optimizer-{b['branch_id']}" data-branch-id="{b['branch_id']}" class="hid-ad-optimizer" style="background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:20px;margin-bottom:16px;">
      <h3 style="margin:0 0 4px;font-size:15px;font-weight:700;color:#111827;">🎯 Ad Budget Optimizer — {b['branch_name']}</h3>
      <p style="margin:0 0 12px;font-size:12px;color:#6b7280;">
        6-step workflow per country running ads this month. Window OCC compared to predicted OCC
        drives BOOST / MAINTAIN / STABILIZE.
      </p>
      {''.join(country_blocks)}
    </div>"""


# ── Per-branch executive narrative ───────────────────────────────────────────


def _branch_narrative(b: dict) -> list[str]:
    """3-5 plain-English bullets summarizing where the branch stands.

    Reads precomputed analytics + KPI fields and stitches a top-of-branch
    briefing so a reader knows the headline story before scrolling into
    the detailed tables.

    Order of consideration (only includes a bullet when the data is real):
      1. Pacing vs target (this month).
      2. Last-week revenue / OCC / ADR + WoW delta.
      3. Strongest growth market.
      4. Biggest concern (worst ROAS / drop day / stuck KOL).
      5. Next-month on-the-books status.

    All "week" framing refers to the most recently completed Mon–Sun
    (see last_week_range() in weekly_report_builder).
    """
    bullets: list[str] = []
    cur = b["currency"]
    a = b.get("analytics", {}) or {}
    summary = a.get("summary") or {}
    lw = summary.get("last_week") or {}
    pa_lw = (a.get("paid_ads") or {}).get("last_week") or {}

    # 1. Pacing — judged by ADJUSTED FORECAST vs target (will the month
    #    hit KPI after deductions/other-rev?), not by actual MTD vs target.
    #    Early-month MTD is naturally low — flagging "🔴 At risk" when
    #    MTD < 80% would fire on day 5 of every month even if forecast is
    #    110%. Using adjusted (not raw) so this matches the Group Summary
    #    dashboard's "Adjusted" column color.
    fcst_pct = b.get("adjusted_forecast_pct") or b.get("occ_forecast_pct")
    ach = b.get("achievement_pct")  # current actual/target — shown as context
    month_name = MONTHS_EN[_ict_today().month]
    if fcst_pct is not None:
        ach_str = f"{ach:.1f}% MTD" if ach is not None else "MTD n/a"
        if fcst_pct >= 100:
            bullets.append(
                f"🟢 <strong>On track to hit {month_name} target</strong> — "
                f"forecast {fcst_pct:.1f}% of target ({ach_str})."
            )
        elif fcst_pct >= 90:
            bullets.append(
                f"🟡 <strong>Forecast just under target</strong> — projected {fcst_pct:.1f}% "
                f"({ach_str}); needs +{100-fcst_pct:.1f}pp lift to close."
            )
        elif fcst_pct >= 75:
            bullets.append(
                f"🟠 <strong>Forecast behind target</strong> — projected {fcst_pct:.1f}% "
                f"({ach_str}); push pricing + ads to recover."
            )
        else:
            bullets.append(
                f"🔴 <strong>At risk of missing target</strong> — forecast only {fcst_pct:.1f}% "
                f"({ach_str}); review pricing + promo immediately."
            )
    elif ach is not None:
        # Forecast unavailable — fall back to MTD-only signal but be explicit
        bullets.append(
            f"📊 MTD pacing {ach:.1f}% — forecast unavailable. "
            f"Set predicted OCC% in KPI Dashboard for a real projection."
        )

    # 2. Last-week revenue movement
    wow = summary.get("wow_revenue_pct")
    if wow is not None:
        direction = "up" if wow >= 0 else "down"
        emoji = "📈" if wow >= 0 else "📉"
        bullets.append(
            f"{emoji} Revenue last week {_fmt(lw.get('revenue'), cur)} ({direction} {abs(wow):.1f}% WoW), "
            f"OCC {_pct((lw.get('occ_pct') or 0)*100) if lw.get('occ_pct') is not None else '—'}, "
            f"ADR {_fmt(lw.get('adr'), cur)}."
        )

    # 3. Top growth country
    growth = b.get("growth_countries") or []
    if growth:
        g = growth[0]
        bullets.append(
            f"🚀 Hottest market: <strong>{g['country']}</strong> "
            f"+{g['growth_pct']:.0f}% bookings vs prior 90d."
        )

    # 4. Concerns — pick the most urgent of: paid ads ROAS<1, outliers drop, KOL stuck, email open<15%
    concerns: list[str] = []
    if pa_lw.get("cost") and pa_lw.get("roas") is not None and pa_lw["roas"] < 1.0:
        concerns.append(f"⚠️ Paid Ads ROAS {pa_lw['roas']:.2f}x — under break-even")
    out = a.get("outliers") or []
    drops = [o for o in out if o["direction"] == "drop"]
    if drops:
        worst = max(drops, key=lambda o: abs(o.get("rev_z", 0)))
        concerns.append(f"⚠️ Drop day {worst['date']} (rev z={worst['rev_z']})")
    kol = a.get("kol") or {}
    if kol.get("stuck"):
        concerns.append(f"⚠️ {len(kol['stuck'])} KOL deliverable(s) stuck >14d")
    crm = a.get("crm") or {}
    em = crm.get("email") or {}
    if em.get("sent", 0) > 500 and (em.get("open_rate_pct") or 100) < 15:
        concerns.append(f"⚠️ Email open rate only {em['open_rate_pct']:.1f}%")
    if concerns:
        bullets.append(concerns[0])  # one most-urgent

    # 5. Next-month status
    nxt_pct = b.get("next_forecast_pct")
    nxt_booked = b.get("next_booked_revenue")
    if nxt_pct is not None and nxt_pct >= 60:
        bullets.append(
            f"📅 {b.get('next_month') and MONTHS_EN[b['next_month']] or 'Next month'} "
            f"on-the-books {_fmt(nxt_booked, cur)} ({nxt_pct}% of target) — solid pipeline."
        )
    elif nxt_pct is not None and nxt_pct < 30:
        bullets.append(
            f"⏰ {b.get('next_month') and MONTHS_EN[b['next_month']] or 'Next month'} "
            f"only {nxt_pct}% of target on-the-books — push ad spend + CRM activation now."
        )

    return bullets[:5]


# ── Per-branch combined action block ─────────────────────────────────────────


def _render_branch_actions(
    country_actions: list[str],
    ads_recs: list[str],
    kol_recs: list[str],
    ads_actions: list[str],
    kol_actions: list[str],
    crm_actions: list[str],
    ads_rec_month: str,
    kol_rec_month: str,
) -> str:
    """Combined action block — folds Country Intel actions, gov-visitor
    recommendations (Paid Ads next month + KOLs month+2), and channel-level
    next actions into one panel attached to a branch card.

    Replaces the three separate end-of-email panels (Country Intel
    Actions / Next Week Recommendations / Channel Actions) — feedback
    (2026-05-03) said the standalone panels duplicated context already
    presented per-branch.
    """
    # Combine all action lists by topic. Order: Country Intel first, then
    # gov-driven Paid Ads (next month), then gov-driven KOL (month+2),
    # then channel-level next actions per channel.
    sections: list[tuple[str, str, list[str]]] = []
    if country_actions:
        sections.append(("🎯 Country Intel — this week", "#dc2626", country_actions))
    if ads_recs:
        sections.append(
            (f"📣 Paid Ads — {ads_rec_month} demand (gov forecast)", "#4f46e5", ads_recs)
        )
    if kol_recs:
        sections.append(
            (f"🎥 KOLs — {kol_rec_month} peak (gov forecast)", "#7c3aed", kol_recs)
        )
    if ads_actions:
        sections.append(("📣 Paid Ads — channel signals", "#4f46e5", ads_actions))
    # KOL — channel signals temporarily hidden from weekly report (2026-05-18, user request).
    # Re-enable by uncommenting the block below; kol_actions is still computed upstream.
    # if kol_actions:
    #     sections.append(("🎥 KOL — channel signals", "#7c3aed", kol_actions))
    if crm_actions:
        sections.append(("✉️ CRM — channel signals", "#059669", crm_actions))

    if not sections:
        return ""

    blocks = []
    for label, color, items in sections:
        list_html = "".join(f"<li style='margin:2px 0;'>{a}</li>" for a in items)
        blocks.append(
            f"<div style='margin-top:8px;'>"
            f"<span style='font-size:11px;font-weight:700;color:{color};text-transform:uppercase;letter-spacing:0.4px;'>{label}</span>"
            f"<ul style='margin:3px 0 0;padding-left:20px;color:#374151;font-size:13px;line-height:1.5;'>{list_html}</ul>"
            f"</div>"
        )

    return f"""
    <div style="margin-top:14px;padding-top:12px;border-top:1px solid #f3f4f6;">
      <p style="margin:0 0 4px;font-size:12px;font-weight:600;color:#374151;">🎯 Action Items</p>
      {''.join(blocks)}
    </div>"""


# ── Paid Ads / KOL / CRM section renderers ───────────────────────────────────


def _render_paid_ads(b: dict) -> str:
    pa = b.get("analytics", {}).get("paid_ads")
    if not pa:
        return ""
    cur = b["currency"]
    bid = b["branch_id"]
    lw = pa["last_week"]
    pw = pa["prev_week"]

    def _wow(val):
        if val is None:
            return "—"
        color = "#16a34a" if val >= 0 else "#dc2626"
        return f"<span style='color:{color}'>{_signed_pct(val)}</span>"

    if lw["cost"] == 0 and pw["cost"] == 0:
        return f"""
        <div style="margin-top:14px;padding-top:12px;border-top:1px solid #f3f4f6;">
          <p style="margin:0 0 6px;font-size:12px;font-weight:600;color:#374151;">📣 Paid Ads (last week · {pa['window_start']} → {pa['window_end']})</p>
          <p style="margin:0;font-size:12px;color:#9ca3af;">No ad spend last week or the week before.</p>
        </div>"""

    # Channel rows — each metric paired with its WoW delta. % deltas are
    # used for absolute counts (cost / impressions / bookings); pp deltas
    # for rates (CTR / CVR) since pct-changes-of-pcts read confusingly.
    def _ch_cell_pct(value_html: str, wow_pct):
        if wow_pct is None:
            return f"<td style='{_TABLE_TD};text-align:right;vertical-align:top;'>{value_html}</td>"
        color = "#16a34a" if wow_pct >= 0 else "#dc2626"
        return (
            f"<td style='{_TABLE_TD};text-align:right;vertical-align:top;'>"
            f"<div>{value_html}</div>"
            f"<div style='font-size:10px;color:{color};'>{_signed_pct(wow_pct)}</div>"
            f"</td>"
        )

    def _ch_cell_pp(value_html: str, pp_delta):
        if pp_delta is None:
            return f"<td style='{_TABLE_TD};text-align:right;vertical-align:top;'>{value_html}</td>"
        color = "#16a34a" if pp_delta >= 0 else "#dc2626"
        sign = "+" if pp_delta >= 0 else ""
        return (
            f"<td style='{_TABLE_TD};text-align:right;vertical-align:top;'>"
            f"<div>{value_html}</div>"
            f"<div style='font-size:10px;color:{color};'>{sign}{pp_delta:.2f}pp</div>"
            f"</td>"
        )

    ch_rows_parts = []
    for c in pa["by_channel"]:
        roas_html = f"{c['roas']:.2f}x" if c["roas"] is not None else "—"
        cost_html = _fmt(c["cost"], cur)
        bk_html = f"{c['bookings']}"
        impr_html = f"{c['impressions']:,}"
        ctr_html = _pct(c["ctr_pct"])
        cvr_html = _pct(c["cvr_pct"])
        attrs = _cell_attrs(bid, f"paid_ads.channel.{c['channel']}", f"Paid Ads — {c['channel']}")
        ch_rows_parts.append(
            f"<tr{attrs}>"
            f"<td style='{_TABLE_TD};vertical-align:top;'>{c['channel']}</td>"
            + _ch_cell_pct(cost_html, c.get("wow_cost_pct"))
            + _ch_cell_pct(impr_html, c.get("wow_impressions_pct"))
            + _ch_cell_pp(ctr_html, c.get("ctr_pp_delta"))
            + _ch_cell_pp(cvr_html, c.get("cvr_pp_delta"))
            + _ch_cell_pct(bk_html, c.get("wow_bookings_pct"))
            + _ch_cell_pct(f"<strong>{roas_html}</strong>", c.get("wow_roas_pct"))
            + "</tr>"
        )
    ch_rows = "".join(ch_rows_parts)

    # By Country rows — full ads metrics from Ads Platform's
    # /api/export/spend/daily-by-country endpoint. Same _ch_cell_*
    # helpers as the By Channel table so the visual treatment is
    # consistent across the section.
    country_rows_parts = []
    for cr in (pa.get("by_country") or []):
        spend_html = _fmt(cr["spend"], cur)
        rev_html = _fmt(cr["revenue"], cur)
        roas_str = f"{cr['roas']:.2f}x" if cr["roas"] is not None else "—"
        if cr["roas"] is not None and cr["roas"] >= 1.0:
            roas_color = "#16a34a"
        elif cr["roas"] is not None:
            roas_color = "#dc2626"
        else:
            roas_color = "#9ca3af"
        bk_html = f"{cr['bookings']}"
        ctr_html = _pct(cr["ctr_pct"])
        cpa_html = _fmt(cr["cpa"], cur) if cr["cpa"] is not None else "—"
        country_attrs = _cell_attrs(bid, f"paid_ads.country.{cr['country']}", f"Paid Ads country — {cr['country']}")
        country_rows_parts.append(
            f"<tr{country_attrs}>"
            f"<td style='{_TABLE_TD};vertical-align:top;'>{cr['country']}</td>"
            + _ch_cell_pct(spend_html, cr.get("wow_spend_pct"))
            + _ch_cell_pct(rev_html, cr.get("wow_revenue_pct"))
            + _ch_cell_pct(
                f"<strong style='color:{roas_color};'>{roas_str}</strong>",
                cr.get("wow_roas_pct"),
            )
            + _ch_cell_pp(ctr_html, cr.get("ctr_pp_delta"))
            + f"<td style='{_TABLE_TD};text-align:right;vertical-align:top;'>{cpa_html}</td>"
            + _ch_cell_pct(bk_html, cr.get("wow_bookings_pct"))
            + "</tr>"
        )
    country_rows_html = "".join(country_rows_parts)

    roas_str = f"{lw['roas']:.2f}x" if lw["roas"] is not None else "—"
    summary_attrs = _cell_attrs(bid, "paid_ads.summary", "Paid Ads — summary")
    return f"""
    <div style="margin-top:14px;padding-top:12px;border-top:1px solid #f3f4f6;">
      <p{summary_attrs} style="margin:0 0 4px;font-size:12px;font-weight:600;color:#374151;">📣 Paid Ads (last week · {pa['window_start']} → {pa['window_end']})</p>
      <p style="margin:0 0 6px;font-size:11px;color:#9ca3af;">
        Source: ads_performance (Ads Platform sync) by ad's daily date_from. Bookings/revenue attributed by Ads Platform.
      </p>
      <p style="margin:0 0 10px;font-size:12px;color:#6b7280;">
        Spend {_fmt(lw['cost'], cur)} · Bookings {lw['bookings']} · Revenue {_fmt(lw['revenue'], cur)} ·
        ROAS {roas_str} ({_wow(pa['wow_roas_pct'])} WoW) · CTR {_pct(lw['ctr_pct'])} · CVR {_pct(lw['cvr_pct'])} · CPA {_fmt(lw['cpa'], cur)}<br/>
        WoW: spend {_wow(pa['wow_cost_pct'])} · revenue {_wow(pa['wow_revenue_pct'])}
      </p>

      <table style="width:100%;border-collapse:collapse;">
        <tr><th style="{_TABLE_TH}" colspan="7">By Channel (last week, with WoW deltas)</th></tr>
        <tr><th style="{_TABLE_TH}">Channel</th>
            <th style="{_TABLE_TH};text-align:right;">Cost</th>
            <th style="{_TABLE_TH};text-align:right;">Impr</th>
            <th style="{_TABLE_TH};text-align:right;">CTR</th>
            <th style="{_TABLE_TH};text-align:right;">CVR</th>
            <th style="{_TABLE_TH};text-align:right;">Bk</th>
            <th style="{_TABLE_TH};text-align:right;">ROAS</th></tr>
        {ch_rows or '<tr><td colspan="7" style="'+_TABLE_TD+';color:#9ca3af;">No channel data</td></tr>'}
      </table>

      <table style="width:100%;border-collapse:collapse;margin-top:12px;">
        <tr><th style="{_TABLE_TH}" colspan="7">🌏 By Country — last week, with WoW deltas</th></tr>
        <tr>
          <th style="{_TABLE_TH}">Country</th>
          <th style="{_TABLE_TH};text-align:right;">Spend</th>
          <th style="{_TABLE_TH};text-align:right;">Revenue</th>
          <th style="{_TABLE_TH};text-align:right;">ROAS</th>
          <th style="{_TABLE_TH};text-align:right;">CTR</th>
          <th style="{_TABLE_TH};text-align:right;">CPA</th>
          <th style="{_TABLE_TH};text-align:right;">Conv.</th>
        </tr>
        {country_rows_html or '<tr><td colspan="7" style="'+_TABLE_TD+';color:#9ca3af;">No country-level data — verify ADS_PLATFORM_API_KEY is set and /api/export/spend/daily-by-country is reachable.</td></tr>'}
      </table>
      <p style="margin:6px 0 0;font-size:10px;color:#9ca3af;">
        Source: Ads Platform /api/export/spend/daily-by-country (ad_country_metrics, post-allocation per-country breakdown).
        Revenue + Conv. = website + offline summed. ROAS = Revenue / Spend; CTR = Clicks / Impr; CPA = Spend / Conv.
      </p>
    </div>"""


def _render_kol(b: dict) -> str:
    """Render the KOL section as a monthly progress table.

    Layout (per feedback 2026-05-18, after KOL Engine migrated Invited
    targets from per-branch to per-country):
      - Invited (Proactive): org-wide total + per-country breakdown table
        (same on every branch email — Invited is no longer a per-branch concept)
      - Collaborated / Posted: per-branch values

    Source = KOL Engine public API GET /api/public/kol-targets/{slug}.
    """
    k = b.get("analytics", {}).get("kol") or {}
    targets = k.get("targets")
    bid = b["branch_id"]

    if not targets:
        reason = k.get("targets_unavailable_reason") or (
            "KOL Engine targets not configured. Set KOL_PUBLIC_API_KEY "
            "and KOL_TARGETS_ORG_SLUG on Zeabur, then verify this branch "
            "exists in KOL Engine."
        )
        return f"""
        <div style="margin-top:14px;padding-top:12px;border-top:1px solid #f3f4f6;">
          <p style="margin:0 0 6px;font-size:12px;font-weight:600;color:#374151;">🎥 KOL — Monthly Progress</p>
          <p style="margin:0;font-size:12px;color:#9ca3af;">{reason}</p>
        </div>"""

    period_label = targets.get("period_label") or (
        f"{MONTHS_EN[targets.get('period_month') or _ict_today().month]} "
        f"{targets.get('period_year') or _ict_today().year}"
    )

    def _pct_color(pct_val):
        if pct_val is None:
            return ("#9ca3af", 0.0)
        v = float(pct_val)
        bar = max(0.0, min(100.0, v))
        if v >= 100:
            return ("#16a34a", bar)
        if v >= 75:
            return ("#ca8a04", bar)
        if v >= 50:
            return ("#ea580c", bar)
        return ("#dc2626", bar)

    def _pct_cell(pct_val) -> str:
        color, _ = _pct_color(pct_val)
        if pct_val is None:
            return f"<span style='color:{color};'>n/a</span>"
        return f"<span style='color:{color};font-weight:700;'>{float(pct_val):.1f}%</span>"

    def _bar(pct_val) -> str:
        color, bar_pct = _pct_color(pct_val)
        return (
            f"<div style='background:#e5e7eb;border-radius:6px;height:6px;width:120px;display:inline-block;vertical-align:middle;'>"
            f"<div style='background:{color};height:6px;border-radius:6px;width:{bar_pct:.1f}%;'></div>"
            f"</div>"
        )

    def _progress_row(label: str, metric: dict, metric_key: str) -> str:
        actual = int(metric.get("actual") or 0)
        target = int(metric.get("target") or 0)
        pct = metric.get("pct")
        attrs = _cell_attrs(bid, metric_key, f"KOL — {label}")
        return (
            f"<tr{attrs}>"
            f"<td style='{_TABLE_TD};'><strong>{label}</strong></td>"
            f"<td style='{_TABLE_TD};text-align:right;font-weight:600;'>{actual:,}</td>"
            f"<td style='{_TABLE_TD};text-align:right;color:#6b7280;'>/ {target:,}</td>"
            f"<td style='{_TABLE_TD};text-align:right;'>{_pct_cell(pct)}</td>"
            f"<td style='{_TABLE_TD};text-align:right;'>{_bar(pct)}</td>"
            f"</tr>"
        )

    # Branch-level Collaborated + Posted
    branch_rows = (
        _progress_row("🤝 Collaborated", targets.get("collaborated") or {}, "kol.collaborated")
        + _progress_row("🎬 Posted", targets.get("posted") or {}, "kol.posted")
    )

    # Org-wide Invited (Proactive) headline
    org_invited = targets.get("org_invited") or {}
    invited_actual = int(org_invited.get("actual") or 0)
    invited_target = int(org_invited.get("target") or 0)
    invited_pct = org_invited.get("pct")
    # Org-wide thread — no branch_id so every branch's email shares
    # the same comment thread for Invited.
    invited_header_attrs = _cell_attrs(None, "kol.invited.org", "KOL — Invited (Org-wide)")
    invited_header = (
        f"<span{invited_header_attrs}>📨 Invited (Proactive) — Org-wide: "
        f"<strong style='color:#111827;'>{invited_actual:,}</strong>"
        f"<span style='color:#6b7280;'> / {invited_target:,}</span> · "
        f"{_pct_cell(invited_pct)}</span>"
    )

    # Country breakdown table
    country_rows_html = ""
    for row in (targets.get("invite_by_country") or []):
        country = row.get("country") or ""
        if country == "__unknown__":
            country_label = "<em style='color:#9ca3af;'>Unknown country</em>"
            country_key = "unknown"
            country_label_plain = "Unknown"
        else:
            country_label = country
            country_key = country
            country_label_plain = country
        actual = int(row.get("actual") or 0)
        target = int(row.get("target") or 0)
        pct = row.get("pct")
        # Invited-by-country thread is org-wide (same data shown on every
        # branch's email), so we omit branch_id from the cell attrs to
        # ensure all branches share one thread per country.
        country_attrs = _cell_attrs(None, f"kol.invited.country.{country_key}", f"KOL invited — {country_label_plain}")
        country_rows_html += (
            f"<tr{country_attrs}>"
            f"<td style='{_TABLE_TD};'>{country_label}</td>"
            f"<td style='{_TABLE_TD};text-align:right;font-weight:600;'>{actual:,}</td>"
            f"<td style='{_TABLE_TD};text-align:right;color:#6b7280;'>/ {target:,}</td>"
            f"<td style='{_TABLE_TD};text-align:right;'>{_pct_cell(pct)}</td>"
            f"<td style='{_TABLE_TD};text-align:right;'>{_bar(pct)}</td>"
            f"</tr>"
        )

    country_table_html = ""
    if country_rows_html:
        country_table_html = f"""
      <p style="margin:10px 0 4px;font-size:11px;font-weight:600;color:#6b7280;">Invited breakdown by KOL nationality</p>
      <table style="width:100%;border-collapse:collapse;">
        <tr>
          <th style="{_TABLE_TH}">Country</th>
          <th style="{_TABLE_TH};text-align:right;">Actual</th>
          <th style="{_TABLE_TH};text-align:right;">Target</th>
          <th style="{_TABLE_TH};text-align:right;">Pacing</th>
          <th style="{_TABLE_TH};text-align:right;">Progress</th>
        </tr>
        {country_rows_html}
      </table>"""

    return f"""
    <div style="margin-top:14px;padding-top:12px;border-top:1px solid #f3f4f6;">
      <p style="margin:0 0 6px;font-size:12px;font-weight:600;color:#374151;">
        🎥 KOL — Monthly Progress · {period_label}
      </p>
      <p style="margin:0 0 8px;font-size:12px;color:#374151;">{invited_header}</p>
      <table style="width:100%;border-collapse:collapse;">
        <tr>
          <th style="{_TABLE_TH}">Metric</th>
          <th style="{_TABLE_TH};text-align:right;">Actual</th>
          <th style="{_TABLE_TH};text-align:right;">Target</th>
          <th style="{_TABLE_TH};text-align:right;">Pacing</th>
          <th style="{_TABLE_TH};text-align:right;">Progress</th>
        </tr>
        {branch_rows}
      </table>
      {country_table_html}
      <p style="margin:6px 0 0;font-size:11px;color:#9ca3af;">
        Source: KOL Engine targets API. Invited (Proactive) is org-wide (bucketed by KOL nationality); Collaborated/Posted are per-branch. Pacing color: ≥100% green · 75-99% yellow · 50-74% orange · &lt;50% red.
      </p>
    </div>"""


def _render_crm(b: dict) -> str:
    """CRM section — revenue only, sourced from CRM-tagged reservations.

    Data source: reservations where room_type or rate_plan_name contains
    'CRM' / "MEANDER'S FRIEND" / 'Travel guide' / 'Grand Open' / 'Extension Promotion'. Filtered
    on reservation_date (Date Booked) per the team rule. Excludes
    Blogger / House Use / KOL / Special Case / Work Exchange from revenue.

    Email-stats / workflow / bulk-send tables intentionally omitted —
    feedback (2026-05-03) wanted CRM down to a single revenue line.
    """
    c = b.get("analytics", {}).get("crm")
    if not c:
        return f"""
        <div style="margin-top:14px;padding-top:12px;border-top:1px solid #f3f4f6;">
          <p style="margin:0 0 6px;font-size:12px;font-weight:600;color:#374151;">✉️ CRM</p>
          <p style="margin:0;font-size:12px;color:#9ca3af;">No CRM data available — analytics not computed.</p>
        </div>"""
    cur = b["currency"]
    rev_t = c["crm_revenue_this"]

    def _wow(val):
        if val is None:
            return "—"
        color = "#16a34a" if val >= 0 else "#dc2626"
        return f"<span style='color:{color}'>{_signed_pct(val)}</span>"

    if rev_t["bookings"] == 0:
        return f"""
        <div style="margin-top:14px;padding-top:12px;border-top:1px solid #f3f4f6;">
          <p style="margin:0 0 6px;font-size:12px;font-weight:600;color:#374151;">✉️ CRM (last week · {c['window_start']} → {c['window_end']})</p>
          <p style="margin:0;font-size:12px;color:#9ca3af;">
            No CRM-tagged reservations in window. Source: room_type / rate_plan_name
            containing "CRM" / "MEANDER'S FRIEND" / "Travel guide" / "Grand Open" / "Extension Promotion".
          </p>
        </div>"""

    # Per-rate-plan breakdown — grouped by Rate Plan Name to mirror the
    # Marketing Activity → CRM Reservations tab (one row per rate plan, not
    # per room_type × rate_plan combo). Sorted by revenue desc, with a
    # Total row matching that page.
    by_rate_plan = c.get("by_rate_plan") or []
    if by_rate_plan:
        rp_rows = "".join(
            f"<tr>"
            f"<td style='{_TABLE_TD}'>{_attr_escape(rp.get('rate_plan_name') or '—')}</td>"
            f"<td style='{_TABLE_TD};text-align:right;'>{rp['bookings']}</td>"
            f"<td style='{_TABLE_TD};text-align:right;color:#6b7280;'>{rp['nights']}</td>"
            f"<td style='{_TABLE_TD};text-align:right;font-weight:600;'>{_fmt(rp['revenue'], cur)}</td>"
            f"<td style='{_TABLE_TD};text-align:right;'>{_fmt(rp.get('adr'), cur)}</td>"
            f"</tr>"
            for rp in by_rate_plan
        )
        tot_bookings = sum(rp["bookings"] for rp in by_rate_plan)
        tot_nights = sum(rp["nights"] for rp in by_rate_plan)
        tot_revenue = sum(rp["revenue"] for rp in by_rate_plan)
        tot_adr = round(tot_revenue / tot_nights, 2) if tot_nights > 0 else None
        total_row = (
            f"<tr style='background:#f9fafb;font-weight:700;color:#111827;'>"
            f"<td style='{_TABLE_TD}'>Total</td>"
            f"<td style='{_TABLE_TD};text-align:right;'>{tot_bookings}</td>"
            f"<td style='{_TABLE_TD};text-align:right;'>{tot_nights}</td>"
            f"<td style='{_TABLE_TD};text-align:right;'>{_fmt(tot_revenue, cur)}</td>"
            f"<td style='{_TABLE_TD};text-align:right;'>{_fmt(tot_adr, cur)}</td>"
            f"</tr>"
        )
        rp_table = f"""
      <table style="width:100%;border-collapse:collapse;margin-top:8px;">
        <tr>
          <th style="{_TABLE_TH}">Rate Plan Name</th>
          <th style="{_TABLE_TH};text-align:right;">Bookings</th>
          <th style="{_TABLE_TH};text-align:right;">Nights</th>
          <th style="{_TABLE_TH};text-align:right;">Revenue</th>
          <th style="{_TABLE_TH};text-align:right;">ADR</th>
        </tr>
        {rp_rows}
        {total_row}
      </table>"""
    else:
        rp_table = ""

    return f"""
    <div style="margin-top:14px;padding-top:12px;border-top:1px solid #f3f4f6;">
      <p style="margin:0 0 4px;font-size:12px;font-weight:600;color:#374151;">
        ✉️ CRM (last week · {c['window_start']} → {c['window_end']})
      </p>
      <p style="margin:0 0 6px;font-size:11px;color:#9ca3af;">
        Source: CRM-tagged reservations (room_type/rate_plan contains CRM / MEANDER'S FRIEND / Travel guide / Grand Open / Extension Promotion) by reservation_date (Date Booked).
      </p>
      <p style="margin:0;font-size:12px;color:#374151;">
        Bookings: <strong>{rev_t['bookings']}</strong> ({rev_t['nights']} nights) ·
        Revenue: <strong style="color:#111827;">{_fmt(rev_t['revenue'], cur)}</strong> ·
        WoW {_wow(c['wow_revenue_pct'])}
      </p>
      {rp_table}
    </div>"""


# ── Next-action helpers (Paid Ads / KOL / CRM) ────────────────────────────────


def _ads_next_actions(b: dict) -> list[str]:
    pa = b.get("analytics", {}).get("paid_ads") or {}
    lw = pa.get("last_week") or {}
    cur = b["currency"]
    actions = []
    if lw.get("cost", 0) == 0:
        actions.append("⚠️ No ad spend last week — restart campaigns or confirm tracking is wired")
        return actions
    roas = lw.get("roas")
    if roas is not None and roas < 1.0:
        actions.append(f"🔴 Last-week ROAS {roas:.2f}x — pause underperformers + reallocate to top-ROAS ads")
    elif roas is not None and roas >= 3.0:
        actions.append(f"🚀 Last-week ROAS {roas:.2f}x — scale top-channel budget +20%")
    if (lw.get("ctr_pct") or 100) < 1.0 and lw.get("impressions", 0) >= 10_000:
        actions.append("📉 CTR < 1% — refresh creatives/headlines (test 2-3 new angles)")
    if pa.get("bottom_campaigns"):
        actions.append(f"🛑 {len(pa['bottom_campaigns'])} ad(s) flagged as underperformer — pause or replace")
    wow_rev = pa.get("wow_revenue_pct")
    if wow_rev is not None and wow_rev <= -25:
        actions.append(f"📉 Revenue down {wow_rev:.1f}% WoW — diagnose attribution + lead-time shift")
    return actions


def _kol_next_actions(b: dict) -> list[str]:
    k = b.get("analytics", {}).get("kol") or {}
    actions = []
    if k.get("total_kols", 0) == 0:
        return actions
    if k.get("stuck"):
        names = ", ".join(s["kol_name"] for s in k["stuck"][:3])
        actions.append(f"⏳ Follow up stuck KOLs (>14d): {names}")
    if k.get("expiring"):
        urgent = [e for e in k["expiring"] if e["days_left"] <= 14]
        if urgent:
            actions.append(f"⏰ Renew/replace {len(urgent)} usage right(s) expiring within 14d")
        else:
            actions.append(f"📅 {len(k['expiring'])} KOL usage right(s) expiring in next 60d — plan renewal")
    if k.get("available_for_ads"):
        names = ", ".join(a["kol_name"] for a in k["available_for_ads"][:3])
        actions.append(f"✅ Activate ads-eligible KOLs available: {names}")
    if k.get("contract_open", 0) > 0:
        actions.append(f"📝 {k['contract_open']} contract(s) Draft/Negotiating — close this week")
    roi = k.get("roi")
    if roi is not None and roi < 0.5 and k.get("cost_mtd_native", 0) > 0:
        actions.append(f"⚠️ KOL ROI MTD {roi:.2f}x — review which KOLs are driving organic bookings")
    return actions


def _crm_next_actions(b: dict) -> list[str]:
    c = b.get("analytics", {}).get("crm") or {}
    em = c.get("email") or {}
    actions = []
    if not c.get("ghl_branch_name"):
        actions.append("⚠️ CRM email stats unavailable — branch not mapped to GHL location")
        return actions
    if em.get("sent", 0) == 0:
        actions.append("📭 No bulk emails sent last 30d — schedule re-engagement campaign")
    elif (em.get("open_rate_pct") or 100) < 15:
        actions.append(f"📬 Open rate {em.get('open_rate_pct'):.2f}% — A/B test new subject lines")
    if (em.get("click_rate_pct") or 100) < 1.0 and em.get("sent", 0) > 500:
        actions.append("🔗 Click rate < 1% — review CTAs and offer relevance")
    if (em.get("unsub_rate_pct") or 0) > 1.0:
        actions.append(f"🚫 Unsub rate {em.get('unsub_rate_pct'):.2f}% — segmentation/cadence is too aggressive")
    # Underperforming workflows: bookings=0 after >1000 sent
    weak = [w for w in em.get("top_workflows", [])
            if w["sent"] >= 1000 and w["bookings"] == 0]
    if weak:
        actions.append(f"💤 {len(weak)} workflow(s) with 0 bookings after 1K+ sent — pause or rewrite")
    wow_rev = c.get("wow_revenue_pct")
    if wow_rev is not None and wow_rev <= -25:
        actions.append(f"📉 CRM revenue down {wow_rev:.1f}% WoW — investigate cancellations / cohort drop")
    return actions


def _build_html(report: list, today: date) -> str:
    month_name = MONTHS_EN[today.month]
    next_month_name = MONTHS_EN[today.month % 12 + 1]

    # Month names for recommendations
    ads_rec_month = MONTHS_EN[today.month % 12 + 1]           # next month
    kol_rec_month = MONTHS_EN[(today.month + 1) % 12 + 1]     # month+2

    sections = []
    next_actions_all = []

    for b in report:
        cur = b["currency"]
        ach = b["achievement_pct"]
        ach_color = "#16a34a" if ach and ach >= 100 else "#ca8a04" if ach and ach >= 80 else "#dc2626"
        ach_str = _pct(ach)

        # Country intel next actions — driven by country_scorer tiers.
        # Drop "Unknown" rows — no actionable country to scale ads for.
        # Feedback 2026-05-18.
        actions = []
        intel = [
            c for c in b.get("country_intel", [])
            if (c.get("country") or "").strip().lower() != "unknown"
        ]
        hot_countries = [c for c in intel if c["tier"] == "Hot"]
        warm_countries = [c for c in intel if c["tier"] == "Warm"]
        cold_countries = [c for c in intel if c["tier"] == "Cold"]

        for c in hot_countries[:3]:
            wow = f" (WoW {c['wow_growth_pct']:+.0f}%)" if c.get("wow_growth_pct") is not None else ""
            label = c.get("trend_label", "Hot")
            actions.append(
                f"🔥 {c['country']} — {label}{wow} — scale ad spend & prioritize OTA rates"
            )
        for c in warm_countries[:2]:
            label = c.get("trend_label", "Warm")
            actions.append(
                f"📈 {c['country']} — {label} — test new ad creatives & increase visibility"
            )
        for c in cold_countries[:1]:
            if c.get("booking_count_this_week", 0) > 0:
                label = c.get("trend_label", "Cold")
                actions.append(
                    f"❄️ {c['country']} — {label} — review content relevance & consider pausing low-ROAS ads"
                )

        if not b["occ_forecast"]:
            actions.append("⚠️ Predicted OCC% not set — go to KPI Dashboard to input")
        if b["achievement_pct"] and b["achievement_pct"] < 80:
            actions.append(f"🔴 KPI at {ach_str} — review pricing and promotions")

        # ── Paid Ads recommendations (April demand) ───────────────────────
        ads_recs = []
        gov_ads_top = b.get("gov_ads_top", [])
        gov_ads_growth = b.get("gov_ads_growth", [])

        # High-volume markets to scale ads for next month
        for g in gov_ads_top[:3]:
            ads_recs.append(
                f"📣 {g['source_country']} — {g['visitor_count']:,} visitors in {ads_rec_month[:3]} (gov #{g['gov_rank']}) → scale ad spend"
            )
        # High-growth markets (next month vs current month)
        prior_month_short = MONTHS_EN[today.month][:3]
        for g in gov_ads_growth[:2]:
            if g["source_country"] not in [a["source_country"] for a in gov_ads_top[:3]]:
                ads_recs.append(
                    f"🚀 {g['source_country']} — +{g['growth_pct']}% growth {ads_rec_month[:3]} vs {prior_month_short} ({g['target_visitors']:,} visitors) → test new campaigns"
                )

        # ── KOL recommendations (month+2 peak travel) ─────────────────────
        kol_recs = []
        gov_kol_top = b.get("gov_kol_top", [])
        for g in gov_kol_top[:3]:
            kol_recs.append(
                f"🎥 {g['source_country']} — {g['visitor_count']:,} visitors in {kol_rec_month[:3]} (gov #{g['gov_rank']}) → activate/recruit KOLs now"
            )

        # Per-channel actionables driven by Paid Ads / KOL / CRM analytics
        ads_actions = _ads_next_actions(b)
        kol_actions = _kol_next_actions(b)
        crm_actions = _crm_next_actions(b)

        next_actions_all.append({
            "branch": b["branch_name"],
            "city": b["branch_city"],
            "actions": actions,
            "ads_recs": ads_recs,
            "kol_recs": kol_recs,
            "ads_actions": ads_actions,
            "kol_actions": kol_actions,
            "crm_actions": crm_actions,
        })

        # Top markets / Growing / Country Intel chip lines were removed
        # 2026-05-04 — overlapping with the Country Insights detail block.
        # `intel` (from country_scorer) is still used by the Hot/Warm/Cold
        # action bullets above.

        narrative_bullets = _branch_narrative(b)
        narrative_html = (
            "<div style='background:#f9fafb;border-left:3px solid #4f46e5;padding:12px 14px;"
            "margin-bottom:14px;border-radius:6px;'>"
            "<p style='margin:0 0 6px;font-size:11px;font-weight:700;color:#4f46e5;text-transform:uppercase;letter-spacing:0.5px;'>Last week at a glance</p>"
            "<ul style='margin:0;padding-left:18px;color:#374151;font-size:13px;line-height:1.5;'>"
            + "".join(f"<li style='margin:2px 0;'>{x}</li>" for x in narrative_bullets)
            + "</ul></div>"
        ) if narrative_bullets else ""

        # ── Combined per-branch action block ─────────────────────────────
        # Folds together: Country Intel actions (`actions`), gov-visitor-driven
        # Paid Ads / KOL recommendations, and channel-level next actions.
        combined_action_html = _render_branch_actions(
            country_actions=actions,
            ads_recs=ads_recs,
            kol_recs=kol_recs,
            ads_actions=ads_actions,
            kol_actions=kol_actions,
            crm_actions=crm_actions,
            ads_rec_month=ads_rec_month,
            kol_rec_month=kol_rec_month,
        )

        paid_ads_block = _render_paid_ads(b)
        kol_block = _render_kol(b)
        crm_block = _render_crm(b)

        bid = b['branch_id']
        ws_iso = _week_start(today).isoformat()
        sections.append(f"""
        <div id="branch-card-{b['branch_id']}" data-branch-id="{b['branch_id']}" data-branch-name="{b['branch_name']}" data-week-start="{ws_iso}" class="hid-branch-card" style="background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:20px;margin-bottom:16px;">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px;">
            <div>
              <h3 style="margin:0;font-size:16px;font-weight:700;color:#111827;">{b['branch_name']}</h3>
              <p style="margin:2px 0 0;font-size:12px;color:#6b7280;">{b['branch_city']} · {cur}</p>
            </div>
            <span style="background:#f3f4f6;padding:4px 12px;border-radius:20px;font-size:13px;font-weight:700;color:{ach_color};">
              {ach_str} of target
            </span>
          </div>

          {narrative_html}

          <table style="width:100%;border-collapse:collapse;font-size:13px;">
            <tr style="background:#f9fafb;">
              <th style="padding:8px 12px;text-align:left;color:#6b7280;font-weight:500;font-size:11px;text-transform:uppercase;">Metric</th>
              <th style="padding:8px 12px;text-align:right;color:#6b7280;font-weight:500;font-size:11px;text-transform:uppercase;">{month_name} (MTD {b['days_elapsed']}/{b['total_days']}d)</th>
              <th style="padding:8px 12px;text-align:right;color:#6b7280;font-weight:500;font-size:11px;text-transform:uppercase;">{next_month_name}</th>
            </tr>
            <tr style="border-top:1px solid #f3f4f6;">
              <td style="padding:8px 12px;color:#374151;">Revenue</td>
              <td style="padding:8px 12px;text-align:right;font-weight:600;color:#111827;"{_cell_attrs(bid, 'branch.revenue')}>{_fmt(b['actual_revenue'], cur)}</td>
              <td style="padding:8px 12px;text-align:right;color:#111827;font-weight:600;"{_cell_attrs(bid, 'branch.next_revenue')}>
                {_fmt(b.get('next_booked_revenue'), cur)}
                <span style="font-weight:400;color:#6b7280;font-size:11px;display:block;">on-the-books · {_num(b.get('next_booked_nights'))} nights</span>
              </td>
            </tr>
            <tr style="border-top:1px solid #f3f4f6;background:#f9fafb;">
              <td style="padding:8px 12px;color:#374151;">Target</td>
              <td style="padding:8px 12px;text-align:right;color:#6b7280;"{_cell_attrs(bid, 'branch.target')}>{_fmt(b['target_revenue'], cur)}</td>
              <td style="padding:8px 12px;text-align:right;color:#6b7280;"{_cell_attrs(bid, 'branch.next_target')}>{_fmt(b['next_target'], cur)}</td>
            </tr>
            <tr style="border-top:1px solid #f3f4f6;">
              <td style="padding:8px 12px;color:#374151;">ADR</td>
              <td style="padding:8px 12px;text-align:right;font-weight:600;color:#111827;"{_cell_attrs(bid, 'branch.adr')}>
                {_fmt(b['avg_adr'], cur)}
                {_split_subline(b.get('room_adr'), b.get('dorm_adr'), 'money', cur)}
              </td>
              <td style="padding:8px 12px;text-align:right;font-weight:600;color:#111827;"{_cell_attrs(bid, 'branch.next_adr')}>
                {_fmt(b['next_adr'], cur)}
                {_split_subline(b.get('next_room_adr'), b.get('next_dorm_adr'), 'money', cur)}
              </td>
            </tr>
            <tr style="border-top:1px solid #f3f4f6;background:#f9fafb;">
              <td style="padding:8px 12px;color:#374151;">OCC% (actual)</td>
              <td style="padding:8px 12px;text-align:right;font-weight:600;color:#111827;"{_cell_attrs(bid, 'branch.occ_actual')}>{_pct(b['avg_occ_pct'])}</td>
              <td style="padding:8px 12px;text-align:right;color:#6b7280;">—</td>
            </tr>
            <tr style="border-top:1px solid #f3f4f6;">
              <td style="padding:8px 12px;color:#374151;">OCC% (forecast)</td>
              <td style="padding:8px 12px;text-align:right;font-weight:600;color:#4f46e5;"{_cell_attrs(bid, 'branch.occ_forecast')}>
                {_pct(b['predicted_occ_pct'])}
                {_split_subline(b.get('predicted_room_occ_pct'), b.get('predicted_dorm_occ_pct'), 'pct')}
              </td>
              <td style="padding:8px 12px;text-align:right;font-weight:600;color:#059669;"{_cell_attrs(bid, 'branch.next_occ_forecast')}>
                {_pct(b['predicted_occ_next'])}
                {_split_subline(b.get('predicted_room_occ_next'), b.get('predicted_dorm_occ_next'), 'pct')}
              </td>
            </tr>
            <tr style="border-top:1px solid #f3f4f6;">
              <td style="padding:8px 12px;color:#374151;font-weight:600;">Forecast (Adjusted)</td>
              <td style="padding:8px 12px;text-align:right;font-weight:700;color:#4f46e5;"{_cell_attrs(bid, 'branch.forecast')}>
                {_fmt(b.get('adjusted_forecast') or b['occ_forecast'], cur)}
                {f"<span style='font-weight:400;color:#6b7280;'> ({b.get('adjusted_forecast_pct') or b['occ_forecast_pct']}%)</span>" if (b.get('adjusted_forecast_pct') or b['occ_forecast_pct']) else ""}
                {_adjustment_formula_html(b.get('occ_forecast'), b.get('deduction_pct'), b.get('other_revenue_native'), cur)}
              </td>
              <td style="padding:8px 12px;text-align:right;font-weight:700;color:#059669;"{_cell_attrs(bid, 'branch.next_forecast')}>
                {_fmt(b.get('next_adjusted_forecast') or b['next_forecast'], cur)}
                {f"<span style='font-weight:400;color:#6b7280;'> ({b.get('next_adjusted_forecast_pct') or b['next_forecast_pct']}%)</span>" if (b.get('next_adjusted_forecast_pct') or b['next_forecast_pct']) else ""}
                {_adjustment_formula_html(b.get('next_forecast'), b.get('next_deduction_pct'), b.get('next_other_revenue_native'), cur)}
              </td>
            </tr>
          </table>

          {_render_outliers(b)}
          {_render_behavior(b)}
          {_render_channel_mix(b)}
          {_render_country_detail(b)}
          {paid_ads_block}
          {kol_block}
          {crm_block}
          {combined_action_html}
        </div>""")

    ad_optimizer_html = "".join(_render_ad_optimizer(b) for b in report)
    exec_summary_html = _render_exec_summary(report, today)

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f3f4f6;margin:0;padding:24px;">
  <div style="max-width:960px;margin:0 auto;">

    <!-- Header -->
    <div style="background:linear-gradient(135deg,#4f46e5,#7c3aed);border-radius:12px;padding:24px;margin-bottom:20px;color:#fff;">
      <h1 style="margin:0;font-size:22px;font-weight:700;">HiD Weekly Report</h1>
      <p style="margin:6px 0 0;opacity:0.85;font-size:14px;">
        {today.strftime('%A, %d %B %Y')} · {month_name} {today.year}
      </p>
    </div>

    <!-- Executive Summary (all branches at a glance) -->
    {exec_summary_html}

    <!-- Per-branch sections — each branch self-contained with its own
         KPI / outliers / behavior / channel mix / countries / Paid Ads /
         KOL / CRM / actions in one card -->
    {''.join(sections)}

    <!-- Ad Budget Optimizer (6-step framework per country, drill-down) -->
    {ad_optimizer_html}

    <!-- Footer -->
    <p style="text-align:center;font-size:11px;color:#9ca3af;margin-top:16px;">
      Generated by HiD Dashboard · {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}
    </p>
  </div>
</body>
</html>"""


def _build_compact_email_html(report: list, today: date) -> str:
    """Compact email = header + Executive Summary + CTA to full UI report.

    Operators wanted the email to be a glance-able digest rather than a
    multi-screen scroll. The full per-branch detail (KPI tables, outliers,
    behavior, channel mix, country insights, paid ads, KOL, CRM, actions,
    ad-budget optimizer) lives on the dashboard at FRONTEND_URL/report
    instead. This function renders the compact version.
    """
    month_name = MONTHS_EN[today.month]
    exec_summary_html = _render_exec_summary(report, today)

    frontend_url = (settings.FRONTEND_URL or "").rstrip("/")
    full_report_url = f"{frontend_url}/report?view=full" if frontend_url else "#"

    # Branch quick-jump links — operators can deep-link to a specific
    # branch tab in the UI via ?view=full&branch={id}.
    branch_links_html = ""
    if frontend_url and report:
        chips = []
        for b in report:
            chips.append(
                f"<a href='{frontend_url}/report?view=full&branch={b['branch_id']}' "
                f"style='display:inline-block;background:#f3f4f6;color:#374151;"
                f"padding:6px 12px;border-radius:16px;font-size:12px;margin:3px;"
                f"text-decoration:none;border:1px solid #e5e7eb;'>"
                f"{b['branch_name']}</a>"
            )
        branch_links_html = (
            "<div style='margin-top:12px;text-align:center;'>"
            "<p style='margin:0 0 8px;font-size:11px;color:#6b7280;'>"
            "Or jump to a specific branch:</p>"
            + "".join(chips) + "</div>"
        )

    cta_html = f"""
    <div style="background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:24px;margin-bottom:16px;text-align:center;">
      <h3 style="margin:0 0 8px;font-size:16px;font-weight:700;color:#111827;">📋 Full Report on Dashboard</h3>
      <p style="margin:0 0 16px;font-size:13px;color:#6b7280;line-height:1.5;">
        Per-branch drill-downs, Paid Ads / KOL / CRM detail, Country Insights, Outliers,
        Booking Behavior, Channel Mix, and Ad Budget Optimizer are all in the full report on the dashboard.
      </p>
      <a href="{full_report_url}"
         style="display:inline-block;background:#4f46e5;color:#fff;padding:12px 28px;
                border-radius:8px;text-decoration:none;font-weight:600;font-size:14px;">
        View Full Report &rarr;
      </a>
      {branch_links_html}
    </div>"""

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f3f4f6;margin:0;padding:24px;">
  <div style="max-width:960px;margin:0 auto;">

    <!-- Header -->
    <div style="background:linear-gradient(135deg,#4f46e5,#7c3aed);border-radius:12px;padding:24px;margin-bottom:20px;color:#fff;">
      <h1 style="margin:0;font-size:22px;font-weight:700;">HiD Weekly Report</h1>
      <p style="margin:6px 0 0;opacity:0.85;font-size:14px;">
        {today.strftime('%A, %d %B %Y')} · {month_name} {today.year}
      </p>
    </div>

    <!-- Executive Summary (the only data the email itself shows) -->
    {exec_summary_html}

    <!-- CTA — full report on dashboard -->
    {cta_html}

    <!-- Footer -->
    <p style="text-align:center;font-size:11px;color:#9ca3af;margin-top:16px;">
      Generated by HiD Dashboard · {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}<br/>
      To stop receiving this, ask the marketing team to remove your address from <code>EMAIL_RECIPIENTS</code>.
    </p>
  </div>
</body>
</html>"""


@router.get("/weekly")
def weekly_report(
    fresh: int = 0,
    db: Session = Depends(get_db),
):
    """Return weekly report data as JSON. Reads from cache (Mon 03:00 ICT
    refresh) by default — pass ?fresh=1 to force rebuild + cache overwrite.
    """
    today = _ict_today()
    report, computed_at = _get_report_with_cache(db, force_fresh=bool(fresh))
    return _envelope({
        "generated_at": (computed_at or datetime.now(timezone.utc)).isoformat(),
        "cache_computed_at": computed_at.isoformat() if computed_at else None,
        "from_cache": not bool(fresh),
        "month": today.month,
        "year": today.year,
        "branches": report,
    })


@router.post("/send-weekly")
def send_weekly_email(
    to: Optional[str] = None,
    user_ids: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Generate and send weekly HTML email via SendGrid (Gmail SMTP fallback).

    Recipient resolution (in order):
      - ?user_ids=uuid1,uuid2 — look up emails from `users` table
      - ?to=a@x.com,b@y.com   — raw email override (useful for testing)
      - env EMAIL_RECIPIENTS  — fallback default list
    """
    recipients_raw = getattr(settings, "EMAIL_RECIPIENTS", "") or ""

    recipients: list[str] = []
    if user_ids:
        id_list = [i.strip() for i in user_ids.split(",") if i.strip()]
        users = db.query(User).filter(User.id.in_(id_list)).all()
        recipients.extend(u.email for u in users if u.email)
    if to:
        recipients.extend(t.strip() for t in to.split(",") if t.strip())
    if not recipients:
        recipients = [r.strip() for r in recipients_raw.split(",") if r.strip()]

    # Dedupe while preserving order
    seen = set()
    recipients = [r for r in recipients if not (r in seen or seen.add(r))]

    if not recipients:
        raise HTTPException(
            status_code=400,
            detail="No recipients — pass ?user_ids=… or ?to=… or set EMAIL_RECIPIENTS"
        )

    today = _ict_today()
    report = _build_report(db)
    # Email gets the compact version (Exec Summary + CTA → UI). Full
    # per-branch detail lives on the dashboard.
    html = _build_compact_email_html(report, today)
    subject = f"HiD Weekly Report — {today.strftime('%d %b %Y')}"

    if not send_email_html(subject, html, recipients):
        raise HTTPException(
            status_code=502,
            detail="Email send failed — check Zeabur logs and GET /api/report/email-config",
        )

    return _envelope({
        "sent_to": recipients,
        "subject": subject,
        "branches_included": len(report),
    })


# ── Email config diagnostic (no secrets) ─────────────────────────────────────


def _mask_email(addr: str) -> str:
    """Mask the local part: 'mason@staymeander.com' → 'ma***@staymeander.com'."""
    if not addr or "@" not in addr:
        return addr or ""
    local, _, domain = addr.partition("@")
    if len(local) <= 2:
        masked = local + "***"
    else:
        masked = local[:2] + "***"
    return f"{masked}@{domain}"


@router.get("/email-config")
def get_email_config():
    """Diagnose which email provider is active without exposing secrets.

    Useful for verifying Zeabur env vars are set correctly without trawling
    logs. Returns the selected provider, masked EMAIL_FROM, list of
    recipients (masked), and which keys are present/missing per provider.
    """
    rs_key = bool((getattr(settings, "RESEND_API_KEY", "") or "").strip())
    sg_key = bool((getattr(settings, "SENDGRID_API_KEY", "") or "").strip())
    gmail_user = (getattr(settings, "GMAIL_USER", "") or "").strip()
    gmail_pass = bool((getattr(settings, "GMAIL_APP_PASSWORD", "") or "").strip())
    email_from = (getattr(settings, "EMAIL_FROM", "") or "").strip()
    recipients_raw = (getattr(settings, "EMAIL_RECIPIENTS", "") or "").strip()
    recipients = [r.strip() for r in recipients_raw.split(",") if r.strip()]

    # Mirror the provider-selection logic in email_sender.send_email_html
    if rs_key and email_from:
        provider = "resend"
    elif sg_key and email_from:
        provider = "sendgrid"
    elif gmail_user and gmail_pass:
        provider = "gmail-smtp"
    else:
        provider = "none"

    return _envelope({
        "active_provider": provider,
        "email_from": _mask_email(email_from) if email_from else None,
        "recipients_count": len(recipients),
        "recipients_masked": [_mask_email(r) for r in recipients],
        "keys": {
            "RESEND_API_KEY": rs_key,
            "SENDGRID_API_KEY": sg_key,
            "EMAIL_FROM": bool(email_from),
            "GMAIL_USER": bool(gmail_user),
            "GMAIL_APP_PASSWORD": gmail_pass,
            "EMAIL_RECIPIENTS": bool(recipients_raw),
            "SYNC_TRIGGER_TOKEN": bool(
                (getattr(settings, "SYNC_TRIGGER_TOKEN", "") or "").strip()
            ),
        },
        "hints": _email_config_hints(provider, email_from, recipients, rs_key, sg_key),
    })


def _email_config_hints(provider, email_from, recipients, rs_key, sg_key):
    hints = []
    if provider == "none":
        hints.append("No email provider configured. Set RESEND_API_KEY+EMAIL_FROM "
                     "(preferred) or SENDGRID_API_KEY+EMAIL_FROM on Zeabur.")
    if (rs_key or sg_key) and not email_from:
        hints.append("API key set but EMAIL_FROM is empty — both required for HTTP providers.")
    if not recipients:
        hints.append("EMAIL_RECIPIENTS is empty — cron send will 400 (no default recipients).")
    if email_from and "@" in email_from:
        domain = email_from.split("@", 1)[1]
        if provider == "resend":
            hints.append(f"Resend will reject if domain '{domain}' is not verified — "
                         "check Resend dashboard → Domains.")
        elif provider == "sendgrid":
            hints.append(f"SendGrid will reject if sender '{email_from}' is not "
                         "verified — Settings → Sender Authentication.")
    return hints


# ── Cron-triggered weekly send (auth via X-Sync-Token) ────────────────────────


def _send_weekly_email_to_default_recipients(db: Session) -> dict:
    """Shared internal: render report and send to env EMAIL_RECIPIENTS.
    Used by the cron-triggered endpoint. Frontend still uses /send-weekly
    which allows ad-hoc recipients via ?to= or ?user_ids=.

    Reads from the report cache (refreshed Mon 03:00 ICT, ~4h before this
    Mon 07:00 send). If the cache is missing (first deploy / table empty)
    `_get_report_with_cache` builds fresh and populates it.
    """
    recipients_raw = getattr(settings, "EMAIL_RECIPIENTS", "") or ""

    recipients = [r.strip() for r in recipients_raw.split(",") if r.strip()]
    seen = set()
    recipients = [r for r in recipients if not (r in seen or seen.add(r))]
    if not recipients:
        raise HTTPException(400, "EMAIL_RECIPIENTS env var is empty — set it on Zeabur")

    today = _ict_today()
    report, _ = _get_report_with_cache(db)
    # Email gets the compact version (Exec Summary + CTA → UI). Full
    # per-branch detail lives on the dashboard.
    html = _build_compact_email_html(report, today)
    subject = f"HiD Weekly Report — {today.strftime('%d %b %Y')}"

    if not send_email_html(subject, html, recipients):
        raise HTTPException(
            status_code=502,
            detail="Email send failed — check Zeabur logs and GET /api/report/email-config",
        )

    return {
        "sent_to": recipients,
        "subject": subject,
        "branches_included": len(report),
    }


@router.post("/send-weekly-cron", dependencies=[Depends(verify_sync_token)])
def send_weekly_cron(db: Session = Depends(get_db)):
    """Cron-triggered weekly email send. Auth: X-Sync-Token header.

    Always sends to env EMAIL_RECIPIENTS — no recipient overrides accepted
    (intentional: removes the chance of cron leaking to a wrong list).
    """
    return _envelope(_send_weekly_email_to_default_recipients(db))


def _archive_week_start(today: date) -> date:
    """Monday of the week the report's data covers (= last completed Mon-Sun).

    Used as the archive primary key so every click within a single "data
    week" overwrites the same row. Cron runs Mon 03:00 ICT and reports on
    last Mon-Sun; a manual click on Wed of the same week reports on the
    same data and therefore the same archive row gets overwritten. Only
    when a new Mon-Sun completes does a fresh archive row get created.
    """
    return last_week_range(today)[0]


def _upsert_weekly_archive(
    db: Session,
    payload: list,
    week_start_date: date,
    *,
    source: str = "cron",
    archived_by: Optional[UUID] = None,
) -> WeeklyReportArchive:
    """Save (or refresh) the snapshot for `week_start_date`. Used by the
    Monday cron refresh and by the manual archive endpoint. Same-week
    re-runs overwrite the row so the most recent build wins.
    """
    row = db.query(WeeklyReportArchive).filter_by(week_start=week_start_date).first()
    if row:
        row.payload = payload
        row.archived_at = datetime.now(timezone.utc)
        row.source = source
        if archived_by is not None:
            row.archived_by = archived_by
    else:
        row = WeeklyReportArchive(
            week_start=week_start_date,
            payload=payload,
            source=source,
            archived_by=archived_by,
        )
        db.add(row)
    db.commit()
    db.refresh(row)
    return row


@router.post("/refresh-cache", dependencies=[Depends(verify_sync_token)])
def refresh_cache(db: Session = Depends(get_db)):
    """Cron-triggered cache rebuild. Auth: X-Sync-Token header.

    Hit by GitHub Actions every Monday 03:00 ICT — pre-warms the report
    cache so the Weekly Report page loads instantly all week and the
    Mon 07:00 email send doesn't have to rebuild. Also snapshots the
    payload into `weekly_report_archives` keyed by the Monday of the
    DATA week (the just-closed Mon-Sun the report covers), so every
    click within that week's lifecycle overwrites the same row instead
    of creating duplicates per click day.
    """
    payload, computed_at = _get_report_with_cache(db, force_fresh=True)
    today = _ict_today()
    archive_row = _upsert_weekly_archive(db, payload, _archive_week_start(today), source="cron")
    return _envelope({
        "computed_at": computed_at.isoformat() if computed_at else None,
        "branches_included": len(payload),
        "archived_week_start": archive_row.week_start.isoformat(),
    })


# ── Email preview ─────────────────────────────────────────────────────────────

@router.get("/preview", response_class=HTMLResponse)
def preview_email(
    fresh: int = 0,
    db: Session = Depends(get_db),
):
    """Return rendered HTML email for iframe preview (no sending). Reads
    from cache (Mon 03:00 ICT refresh) by default — pass ?fresh=1 to
    force rebuild + cache overwrite.
    """
    today = _ict_today()
    report, _ = _get_report_with_cache(db, force_fresh=bool(fresh))
    html = _build_html(report, today)
    return HTMLResponse(content=html)


# ── Email schedule management ─────────────────────────────────────────────────

_schedule_logger = logging.getLogger(__name__)

# In-memory schedule config (loaded from env on startup, updated via API)
_email_schedule: dict = {
    "enabled": False,
    "day_of_week": "mon",  # mon,tue,wed,thu,fri,sat,sun
    "hour": 7,
    "minute": 0,
    "recipients": [],
}

_DAY_MAP = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
_DAY_NAMES = {v: k for k, v in _DAY_MAP.items()}


def _init_schedule():
    """Initialize schedule from env var EMAIL_RECIPIENTS."""
    recipients_raw = getattr(settings, "EMAIL_RECIPIENTS", "") or ""
    _email_schedule["recipients"] = [
        r.strip() for r in recipients_raw.split(",") if r.strip()
    ]


_init_schedule()


class ScheduleUpdate(BaseModel):
    enabled: Optional[bool] = None
    day_of_week: Optional[str] = None  # mon-sun
    hour: Optional[int] = None         # 0-23
    minute: Optional[int] = None       # 0-59
    recipients: Optional[list[str]] = None


def _apply_schedule_to_scheduler():
    """Create or update the APScheduler job based on current _email_schedule."""
    from app.scheduler import scheduler
    from apscheduler.triggers.cron import CronTrigger

    job_id = "weekly_email_send"

    if not _email_schedule["enabled"]:
        try:
            scheduler.remove_job(job_id)
            _schedule_logger.info("Weekly email job removed (disabled)")
        except Exception:
            pass
        return

    day = _email_schedule["day_of_week"]
    hour = _email_schedule["hour"]
    minute = _email_schedule["minute"]

    def _send_weekly_job():
        from app.database import SessionLocal
        db = SessionLocal()
        try:
            recipients = _email_schedule.get("recipients", [])
            if not recipients:
                _schedule_logger.warning("Weekly email job: no recipients configured")
                return
            report = _build_report(db)
            today = _ict_today()
            html = _build_compact_email_html(report, today)
            subject = f"HiD Weekly Report — {today.strftime('%d %b %Y')}"
            if send_email_html(subject, html, recipients):
                _schedule_logger.info("Weekly email sent to %s", recipients)
            else:
                _schedule_logger.error("Weekly email job: send_email_html returned False")
        except Exception as e:
            _schedule_logger.error("Weekly email job failed: %s", e)
        finally:
            db.close()

    scheduler.add_job(
        _send_weekly_job,
        trigger=CronTrigger(day_of_week=day, hour=hour, minute=minute),
        id=job_id,
        replace_existing=True,
    )
    _schedule_logger.info(
        "Weekly email job scheduled: %s at %02d:%02d ICT", day, hour, minute
    )


@router.get("/schedule")
def get_schedule():
    """Return current email schedule configuration."""
    from app.scheduler import scheduler

    job = scheduler.get_job("weekly_email_send")
    next_run = str(job.next_run_time) if job else None

    return _envelope({
        **_email_schedule,
        "next_run": next_run,
    })


@router.patch("/schedule")
def update_schedule(body: ScheduleUpdate):
    """Update email schedule and reschedule the APScheduler job."""
    if body.enabled is not None:
        _email_schedule["enabled"] = body.enabled
    if body.day_of_week is not None:
        if body.day_of_week not in _DAY_MAP:
            raise HTTPException(400, f"Invalid day_of_week: {body.day_of_week}")
        _email_schedule["day_of_week"] = body.day_of_week
    if body.hour is not None:
        if not (0 <= body.hour <= 23):
            raise HTTPException(400, "hour must be 0-23")
        _email_schedule["hour"] = body.hour
    if body.minute is not None:
        if not (0 <= body.minute <= 59):
            raise HTTPException(400, "minute must be 0-59")
        _email_schedule["minute"] = body.minute
    if body.recipients is not None:
        _email_schedule["recipients"] = [r.strip() for r in body.recipients if r.strip()]

    _apply_schedule_to_scheduler()

    return _envelope(_email_schedule)


# ── Per-metric discussion threads + weekly archives ──────────────────────────
#
# Two collaboration features layered on top of the Weekly Report page:
#   - Click any KPI cell → comment thread scoped to (week_start, branch_id,
#     metric_key). Any logged-in user can post; authors / admins can edit
#     or delete. Soft-delete keeps reply context intact.
#   - Week selector — comments are queried by week_start so threads stay
#     attached to the data point they were about, even when viewing past
#     weeks. The report payload itself is snapshotted into
#     `weekly_report_archives` on every Monday cron refresh.


class CommentCreateIn(BaseModel):
    week_start: date
    branch_id: Optional[UUID] = None
    metric_key: str
    body: str
    parent_comment_id: Optional[UUID] = None


class CommentPatchIn(BaseModel):
    body: Optional[str] = None
    is_action_item: Optional[bool] = None
    is_resolved: Optional[bool] = None


def _comment_out(c: WeeklyReportComment, author: Optional[User]) -> dict:
    return {
        "id": str(c.id),
        "week_start": c.week_start.isoformat() if c.week_start else None,
        "branch_id": str(c.branch_id) if c.branch_id else None,
        "metric_key": c.metric_key,
        "parent_comment_id": str(c.parent_comment_id) if c.parent_comment_id else None,
        "author_id": str(c.author_id) if c.author_id else None,
        "author_name": (author.name or author.email) if author else None,
        "author_email": author.email if author else None,
        "author_role": author.role if author else None,
        "body": c.body,
        "is_action_item": bool(c.is_action_item),
        "is_resolved": bool(c.is_resolved),
        "resolved_by": str(c.resolved_by) if c.resolved_by else None,
        "resolved_at": c.resolved_at.isoformat() if c.resolved_at else None,
        "created_at": c.created_at.isoformat() if c.created_at else None,
        "updated_at": c.updated_at.isoformat() if c.updated_at else None,
    }


def _hydrate_comments(db: Session, comments: list[WeeklyReportComment]) -> list[dict]:
    """Bulk-load authors so the list response includes display names
    without N+1 queries.
    """
    author_ids = {c.author_id for c in comments if c.author_id}
    authors: dict = {}
    if author_ids:
        rows = db.query(User).filter(User.id.in_(author_ids)).all()
        authors = {u.id: u for u in rows}
    return [_comment_out(c, authors.get(c.author_id)) for c in comments]


@router.get("/comments")
def list_comments(
    week_start: date,
    branch_id: Optional[UUID] = None,
    metric_key: Optional[str] = None,
    include_resolved: bool = True,
    _current: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List discussion threads for a given week. Optionally narrow to a
    branch + metric for the side-drawer view, or fetch all for badge
    counts.
    """
    q = db.query(WeeklyReportComment).filter(
        WeeklyReportComment.week_start == week_start,
        WeeklyReportComment.is_deleted == False,  # noqa: E712
    )
    if branch_id is not None:
        q = q.filter(WeeklyReportComment.branch_id == branch_id)
    if metric_key is not None:
        q = q.filter(WeeklyReportComment.metric_key == metric_key)
    if not include_resolved:
        q = q.filter(WeeklyReportComment.is_resolved == False)  # noqa: E712
    comments = q.order_by(WeeklyReportComment.created_at.asc()).all()
    return _envelope(_hydrate_comments(db, comments))


@router.get("/comments/counts")
def comment_counts(
    week_start: date,
    _current: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return active-thread counts grouped by (branch_id, metric_key) for
    badge rendering on the report page. Skips resolved + deleted threads
    so the UI badge only highlights what's still open.
    """
    action_item_int = case((WeeklyReportComment.is_action_item == True, 1), else_=0)  # noqa: E712
    rows = (
        db.query(
            WeeklyReportComment.branch_id,
            WeeklyReportComment.metric_key,
            func.count(WeeklyReportComment.id).label("count"),
            func.sum(action_item_int).label("action_items"),
        )
        .filter(
            WeeklyReportComment.week_start == week_start,
            WeeklyReportComment.is_deleted == False,  # noqa: E712
            WeeklyReportComment.is_resolved == False,  # noqa: E712
        )
        .group_by(WeeklyReportComment.branch_id, WeeklyReportComment.metric_key)
        .all()
    )
    out = []
    for r in rows:
        out.append({
            "branch_id": str(r.branch_id) if r.branch_id else None,
            "metric_key": r.metric_key,
            "count": int(r.count or 0),
            "action_items": int(r.action_items or 0),
        })
    return _envelope(out)


@router.post("/comments", status_code=201)
def create_comment(
    body: CommentCreateIn,
    current: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create a comment. Any authenticated user can post — admin / editor
    / viewer alike. Replies pass `parent_comment_id`.
    """
    text = (body.body or "").strip()
    if not text:
        raise HTTPException(400, "Comment body cannot be empty")
    if len(text) > 5000:
        raise HTTPException(400, "Comment body too long (max 5000 chars)")
    if not body.metric_key:
        raise HTTPException(400, "metric_key is required")
    if body.parent_comment_id is not None:
        parent = db.query(WeeklyReportComment).filter_by(
            id=body.parent_comment_id, is_deleted=False,
        ).first()
        if not parent:
            raise HTTPException(404, "Parent comment not found")
    c = WeeklyReportComment(
        week_start=body.week_start,
        branch_id=body.branch_id,
        metric_key=body.metric_key,
        parent_comment_id=body.parent_comment_id,
        author_id=current.id,
        body=text,
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    return _envelope(_comment_out(c, current))


@router.patch("/comments/{comment_id}")
def update_comment(
    comment_id: UUID,
    body: CommentPatchIn,
    current: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Edit a comment. Author can change body / mark action item / resolve.
    Resolving is open to any user (a thread reaching consensus is a team
    decision, not just the author's). Admins can edit anyone's body.
    """
    c = db.query(WeeklyReportComment).filter_by(id=comment_id, is_deleted=False).first()
    if not c:
        raise HTTPException(404, "Comment not found")

    is_author = c.author_id == current.id
    is_admin = current.role == "admin"

    if body.body is not None:
        if not (is_author or is_admin):
            raise HTTPException(403, "Only the author or an admin can edit the body")
        text = body.body.strip()
        if not text:
            raise HTTPException(400, "Comment body cannot be empty")
        if len(text) > 5000:
            raise HTTPException(400, "Comment body too long (max 5000 chars)")
        c.body = text
    if body.is_action_item is not None:
        c.is_action_item = bool(body.is_action_item)
    if body.is_resolved is not None:
        c.is_resolved = bool(body.is_resolved)
        if c.is_resolved:
            c.resolved_by = current.id
            c.resolved_at = datetime.now(timezone.utc)
        else:
            c.resolved_by = None
            c.resolved_at = None

    db.commit()
    db.refresh(c)
    author = db.query(User).filter_by(id=c.author_id).first() if c.author_id else None
    return _envelope(_comment_out(c, author))


@router.delete("/comments/{comment_id}")
def delete_comment(
    comment_id: UUID,
    current: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Soft-delete a comment. Author or admin only. Replies stay visible
    with a placeholder so the thread context isn't lost.
    """
    c = db.query(WeeklyReportComment).filter_by(id=comment_id, is_deleted=False).first()
    if not c:
        raise HTTPException(404, "Comment not found")
    if not (c.author_id == current.id or current.role == "admin"):
        raise HTTPException(403, "Only the author or an admin can delete")
    c.is_deleted = True
    db.commit()
    return _envelope({"deleted": str(comment_id)})


# ── Weekly Report archives (snapshots per Monday) ────────────────────────────


@router.get("/archives")
def list_archives(
    _current: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List archived weeks with metadata for the UI week selector.
    Returns one row per stored snapshot, newest first, with the comment
    count for that week so the selector can flag weeks with discussion.
    """
    archives = (
        db.query(WeeklyReportArchive)
        .order_by(WeeklyReportArchive.week_start.desc())
        .all()
    )
    # Comment counts per week (open + total)
    open_int = case((WeeklyReportComment.is_resolved == False, 1), else_=0)  # noqa: E712
    counts_rows = (
        db.query(
            WeeklyReportComment.week_start,
            func.count(WeeklyReportComment.id).label("total"),
            func.sum(open_int).label("open"),
        )
        .filter(WeeklyReportComment.is_deleted == False)  # noqa: E712
        .group_by(WeeklyReportComment.week_start)
        .all()
    )
    counts = {r.week_start: (int(r.total or 0), int(r.open or 0)) for r in counts_rows}

    out = []
    for a in archives:
        total, open_ = counts.get(a.week_start, (0, 0))
        out.append({
            "week_start": a.week_start.isoformat(),
            "archived_at": a.archived_at.isoformat() if a.archived_at else None,
            "source": a.source,
            "branches_included": len(a.payload or []),
            "comment_count": total,
            "open_comment_count": open_,
        })
    return _envelope(out)


@router.get("/archives/{week_start}")
def get_archive(
    week_start: date,
    _current: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return the snapshot payload for a specific week. Same shape as
    /api/report/weekly so the UI can render it with the same components.
    """
    a = db.query(WeeklyReportArchive).filter_by(week_start=week_start).first()
    if not a:
        raise HTTPException(404, f"No archive found for week_start={week_start.isoformat()}")
    return _envelope({
        "week_start": a.week_start.isoformat(),
        "archived_at": a.archived_at.isoformat() if a.archived_at else None,
        "source": a.source,
        "branches": a.payload,
    })


@router.get("/archives/{week_start}/preview", response_class=HTMLResponse)
def preview_archive(
    week_start: date,
    db: Session = Depends(get_db),
):
    """Render the archived snapshot as the same HTML email format the
    live `/preview` endpoint returns, so the frontend can re-use its
    existing parser/renderer for past weeks.

    Note: this endpoint intentionally has no auth wrapper so the
    rendered HTML can be embedded directly via `fetch()` from the
    dashboard (which is itself behind login). Update if exposing this
    publicly becomes a concern.
    """
    a = db.query(WeeklyReportArchive).filter_by(week_start=week_start).first()
    if not a:
        raise HTTPException(404, f"No archive found for week_start={week_start.isoformat()}")
    # Rebuild HTML using the archived payload. We pass `week_start` as
    # the `today` arg so all the date-derived labels (Monday header,
    # week_start data attributes) reflect the archived week, not now.
    html = _build_html(a.payload, a.week_start)
    return HTMLResponse(content=html)


@router.post("/archives", status_code=201)
def create_archive(
    current: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Manually snapshot the current cached report into the archive
    table. Useful for forcing a snapshot mid-week (e.g. after a fresh
    rebuild) without waiting for the Monday cron. Admin-only since this
    overwrites the current week's archive.
    """
    if current.role != "admin":
        raise HTTPException(403, "Admin only")
    payload, _ = _get_report_with_cache(db)
    row = _upsert_weekly_archive(
        db, payload, _archive_week_start(_ict_today()),
        source="manual", archived_by=current.id,
    )
    return _envelope({
        "week_start": row.week_start.isoformat(),
        "archived_at": row.archived_at.isoformat(),
        "branches_included": len(payload),
    })
