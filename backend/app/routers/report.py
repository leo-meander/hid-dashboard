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

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.routers.sync import verify_sync_token
from app.models.branch import Branch
from app.models.daily_metrics import DailyMetrics
from app.models.kpi import KPITarget
from app.models.reservation import Reservation
from app.models.user import User
from app.services.cloudbeds import sync_cloudbeds_occupancy
from app.services.country_scorer import score_countries
from app.services.email_sender import send_email_html
from app.services.kpi_engine import (
    compute_kpi_summary,
    compute_next_month_forecast,
    _EXCLUDED_STATUSES,
    _EXCLUDED_SOURCES,
)
from app.services.weekly_report_builder import build_branch_analytics
from app.models.gov_visitor import GovVisitorData

router = APIRouter()

MONTHS_EN = ["", "January", "February", "March", "April", "May", "June",
             "July", "August", "September", "October", "November", "December"]


def _envelope(data):
    return {"success": True, "data": data, "error": None,
            "timestamp": datetime.now(timezone.utc).isoformat()}


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


def _top_countries(db: Session, branch_id, days: int = 90, limit: int = 5):
    cutoff = date.today() - timedelta(days=days)
    rows = (
        db.query(
            Reservation.guest_country,
            func.count(Reservation.id).label("cnt"),
        )
        .filter(
            Reservation.branch_id == branch_id,
            Reservation.check_in_date >= cutoff,
            Reservation.guest_country.isnot(None),
            ~func.lower(func.coalesce(Reservation.guest_country, "")).contains("unknown"),
            or_(
                Reservation.status == None,
                Reservation.status.notin_(list(_EXCLUDED_STATUSES)),
            ),
        )
        .group_by(Reservation.guest_country)
        .order_by(func.count(Reservation.id).desc())
        .limit(limit)
        .all()
    )
    return [{"country": r.guest_country, "bookings": r.cnt} for r in rows]


def _growth_countries(db: Session, branch_id, limit: int = 3):
    """Top countries with biggest booking growth (90d vs prior 90d)."""
    today = date.today()
    recent_start = today - timedelta(days=90)
    prev_start = today - timedelta(days=180)

    recent = {
        r.guest_country: r.cnt
        for r in db.query(
            Reservation.guest_country,
            func.count(Reservation.id).label("cnt"),
        ).filter(
            Reservation.branch_id == branch_id,
            Reservation.check_in_date >= recent_start,
            Reservation.guest_country.isnot(None),
            ~func.lower(func.coalesce(Reservation.guest_country, "")).contains("unknown"),
            or_(Reservation.status == None,
                Reservation.status.notin_(list(_EXCLUDED_STATUSES))),
        ).group_by(Reservation.guest_country).all()
    }

    prev = {
        r.guest_country: r.cnt
        for r in db.query(
            Reservation.guest_country,
            func.count(Reservation.id).label("cnt"),
        ).filter(
            Reservation.branch_id == branch_id,
            Reservation.check_in_date >= prev_start,
            Reservation.check_in_date < recent_start,
            Reservation.guest_country.isnot(None),
            ~func.lower(func.coalesce(Reservation.guest_country, "")).contains("unknown"),
            or_(Reservation.status == None,
                Reservation.status.notin_(list(_EXCLUDED_STATUSES))),
        ).group_by(Reservation.guest_country).all()
    }

    results = []
    for country, rec_cnt in recent.items():
        if rec_cnt < 2:
            continue
        prv_cnt = prev.get(country, 0)
        if prv_cnt == 0:
            continue
        growth = round((rec_cnt - prv_cnt) / prv_cnt * 100, 1)
        if growth > 0:
            results.append({"country": country, "recent": rec_cnt,
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
    today = date.today()
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


def _sync_fresh_insights(db: Session, branches):
    """Pull latest Cloudbeds Insights data into daily_metrics before report.

    Always syncs the FULL current month (1st → last day) to ensure
    revenue_native in daily_metrics matches the Cloudbeds dashboard exactly.
    """
    import logging
    logger = logging.getLogger(__name__)
    today = date.today()
    month_start = today.replace(day=1)
    month_end = today.replace(day=calendar.monthrange(today.year, today.month)[1])

    for b in branches:
        pid = b.cloudbeds_property_id
        if not pid:
            continue
        api_key = settings.get_api_key_for_property(str(pid))
        if not api_key:
            continue
        try:
            sync_cloudbeds_occupancy(
                db, str(b.id), pid, b.currency, api_key,
                date_from=month_start, date_to=month_end,
            )
            db.flush()  # ensure fresh data is visible to subsequent queries
            logger.info("Report pre-sync OK branch=%s [%s..%s]", b.name, month_start, month_end)
        except Exception as e:
            logger.warning("Report pre-sync FAIL branch=%s: %s", b.name, e)


def _build_report(db: Session):
    today = date.today()
    branches = db.query(Branch).filter_by(is_active=True).all()
    report = []

    # Sync fresh Cloudbeds Insights before building report
    _sync_fresh_insights(db, branches)

    for b in branches:
        total_rooms = b.total_rooms or 0
        kpi = compute_kpi_summary(db, b.id, today.year, today.month, total_rooms)
        nxt = compute_next_month_forecast(db, b.id, total_rooms, today.year, today.month)
        top = _top_countries(db, b.id)
        growth = _growth_countries(db, b.id)

        # Country Intel scores (Hot / Warm / Cold)
        country_intel = score_countries(db, branch_id=b.id, top_n=10)

        # Actual OCC from daily_metrics
        actual_occ = _actual_occ_pct(db, b.id, today.year, today.month, total_rooms)

        # Predicted/forecast OCC from KPI targets
        predicted_occ_current = kpi.get("predicted_occ_pct")
        predicted_occ_next = nxt.get("predicted_occ_next")

        # Gov visitor forecast for recommendations
        dest = _resolve_branch_dest(b.name)
        # Paid Ads → next month (demand arriving soon, act now)
        # KOLs → month+2 (peak travel — need KOL lead time to recruit/activate)
        ads_month = today.month % 12 + 1           # next month
        kol_month = (today.month + 1) % 12 + 1     # month after next
        ads_prior = today.month                      # current month (for growth calc)
        gov_ads_top = _gov_top_countries(db, dest, ads_month, limit=5) if dest else []
        gov_ads_growth = _gov_growth_countries(db, dest, ads_month, ads_prior, limit=5) if dest else []
        gov_kol_top = _gov_top_countries(db, dest, kol_month, limit=5) if dest else []

        # Analytical sections (summary, outliers, behavior, channel mix,
        # country insights, ad budget optimizer)
        analytics = build_branch_analytics(db, b, today)

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
            "avg_occ_pct": round(actual_occ * 100, 1) if actual_occ else None,
            "predicted_occ_pct": round(predicted_occ_current * 100, 1) if predicted_occ_current else None,
            "days_elapsed": kpi["days_elapsed"],
            "total_days": kpi["total_days"],
            "occ_forecast": kpi["occ_forecast_native"],
            "occ_forecast_pct": round(kpi["occ_forecast_native"] / kpi["target_revenue_native"] * 100, 1)
                if (kpi["occ_forecast_native"] and kpi["target_revenue_native"]) else None,
            # Next month
            "next_month": nxt["next_month"],
            "next_year": nxt["next_year"],
            "next_forecast": nxt["next_month_forecast_native"],
            "next_target": nxt["next_month_target_native"],
            "next_forecast_pct": round(nxt["next_month_forecast_native"] / nxt["next_month_target_native"] * 100, 1)
                if (nxt["next_month_forecast_native"] and nxt["next_month_target_native"]) else None,
            "next_adr": nxt["next_month_adr"],
            "next_booked_nights": nxt["next_month_booked_nights"],
            "next_booked_revenue": nxt.get("next_month_booked_revenue"),
            "predicted_occ_next": round(predicted_occ_next * 100, 1) if predicted_occ_next else None,
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
    """Top-of-email pacing table — one row per branch."""
    rows_html = []
    for b in report:
        cur = b["currency"]
        ach = b["achievement_pct"]
        ach_color = "#16a34a" if ach and ach >= 100 else "#ca8a04" if ach and ach >= 80 else "#dc2626"
        a = b.get("analytics", {})
        mtd = a.get("summary", {}).get("mtd", {})
        wow = a.get("summary", {}).get("wow_revenue_pct")
        yoy = a.get("summary", {}).get("yoy_revenue_pct")
        wow_html = f"<span style='color:{'#16a34a' if (wow or 0)>=0 else '#dc2626'}'>{_signed_pct(wow)}</span>" if wow is not None else "—"
        yoy_html = f"<span style='color:{'#16a34a' if (yoy or 0)>=0 else '#dc2626'}'>{_signed_pct(yoy)}</span>" if yoy is not None else "—"
        rows_html.append(f"""
          <tr>
            <td style="{_TABLE_TD}"><strong>{b['branch_name']}</strong></td>
            <td style="{_TABLE_TD};text-align:right;">{_fmt(mtd.get('revenue'), cur)}</td>
            <td style="{_TABLE_TD};text-align:right;">{_fmt(b['target_revenue'], cur)}</td>
            <td style="{_TABLE_TD};text-align:right;color:{ach_color};font-weight:700;">{_pct(ach)}</td>
            <td style="{_TABLE_TD};text-align:right;">{_pct((mtd.get('occ_pct') or 0)*100) if mtd.get('occ_pct') is not None else '—'}</td>
            <td style="{_TABLE_TD};text-align:right;">{_fmt(b['avg_adr'], cur)}</td>
            <td style="{_TABLE_TD};text-align:right;">{_fmt(mtd.get('revpar'), cur)}</td>
            <td style="{_TABLE_TD};text-align:right;">{wow_html}</td>
            <td style="{_TABLE_TD};text-align:right;">{yoy_html}</td>
          </tr>""")

    return f"""
    <div style="background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:20px;margin-bottom:16px;">
      <h3 style="margin:0 0 12px;font-size:15px;font-weight:700;color:#111827;">📊 Executive Summary</h3>
      <div style="overflow-x:auto;">
        <table style="width:100%;border-collapse:collapse;font-size:12px;">
          <tr>
            <th style="{_TABLE_TH}">Branch</th>
            <th style="{_TABLE_TH};text-align:right;">Revenue MTD</th>
            <th style="{_TABLE_TH};text-align:right;">Target</th>
            <th style="{_TABLE_TH};text-align:right;">Pacing</th>
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
        Revenue = accommodation-only (excl. Blogger / House Use / KOL / Special Case). OCC counts all sources.<br/>
        WoW Rev = current week vs prior week. YoY Rev = MTD this year vs same MTD last year (— if no 2025 data for this window).
      </p>
    </div>"""


def _render_outliers(b: dict) -> str:
    out = b.get("analytics", {}).get("outliers", [])
    if not out:
        return ""
    cur = b["currency"]
    rows = []
    for o in out:
        arrow = "▲" if o["direction"] == "spike" else "▼"
        color = "#16a34a" if o["direction"] == "spike" else "#dc2626"
        reasons = " · ".join(o["reasons"]) if o["reasons"] else "no tagged cause"
        rows.append(
            f"<tr><td style='{_TABLE_TD}'>{o['date']}</td>"
            f"<td style='{_TABLE_TD};text-align:right;color:{color};font-weight:600;'>{arrow} {o['direction']}</td>"
            f"<td style='{_TABLE_TD};text-align:right;'>{_fmt(o['revenue'], cur)}</td>"
            f"<td style='{_TABLE_TD};text-align:right;'>{o['occ_pct']:.2f}%</td>"
            f"<td style='{_TABLE_TD};color:#6b7280;'>{reasons}</td></tr>"
        )
    return f"""
    <div style="margin-top:14px;padding-top:12px;border-top:1px solid #f3f4f6;">
      <p style="margin:0 0 6px;font-size:12px;font-weight:600;color:#374151;">⚡ Outliers (last 7d, vs 30d baseline)</p>
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

    cxl_rows = []
    for c in beh["cancellation_by_source"][:6]:
        cxl_rows.append(
            f"<tr><td style='{_TABLE_TD}'>{c['source_category']}</td>"
            f"<td style='{_TABLE_TD};text-align:right;'>{c['total']:,}</td>"
            f"<td style='{_TABLE_TD};text-align:right;'>{c['cancelled']:,}</td>"
            f"<td style='{_TABLE_TD};text-align:right;font-weight:600;'>{_pct(c['pct'])}</td></tr>"
        )

    def _bucket_row(label, val, total):
        pct = f"{val/total*100:.2f}%" if total > 0 else "—"
        return (
            f"<tr><td style='{_TABLE_TD}'>{label}</td>"
            f"<td style='{_TABLE_TD};text-align:right;'>{val:,}</td>"
            f"<td style='{_TABLE_TD};text-align:right;color:#6b7280;'>{pct}</td></tr>"
        )

    lt_total = sum(beh["lead_time_buckets"].values())
    lt_rows = "".join(_bucket_row(k, v, lt_total) for k, v in beh["lead_time_buckets"].items())

    los_total = sum(beh["los_buckets"].values())
    los_rows = "".join(_bucket_row(k, v, los_total) for k, v in beh["los_buckets"].items())

    return f"""
    <div style="margin-top:14px;padding-top:12px;border-top:1px solid #f3f4f6;">
      <p style="margin:0 0 6px;font-size:12px;font-weight:600;color:#374151;">
        🧭 Booking Behavior ({beh['window_days']}d · cancel overall {_pct(beh['cancellation_overall_pct'])})
      </p>
      <table style="width:32%;display:inline-table;border-collapse:collapse;margin-right:2%;vertical-align:top;">
        <tr><th style="{_TABLE_TH}" colspan="4">Cancellation by source</th></tr>
        <tr><th style="{_TABLE_TH}">Cat.</th><th style="{_TABLE_TH};text-align:right;">Total</th>
            <th style="{_TABLE_TH};text-align:right;">Cxl</th><th style="{_TABLE_TH};text-align:right;">%</th></tr>
        {''.join(cxl_rows) or '<tr><td style="'+_TABLE_TD+'" colspan="4">No data</td></tr>'}
      </table>
      <table style="width:32%;display:inline-table;border-collapse:collapse;margin-right:2%;vertical-align:top;">
        <tr><th style="{_TABLE_TH}" colspan="3">Lead time (avg {beh['lead_time_avg_days'] or '—'}d, n={beh['lead_time_samples']})</th></tr>
        <tr><th style="{_TABLE_TH}">Bucket</th><th style="{_TABLE_TH};text-align:right;">Count</th><th style="{_TABLE_TH};text-align:right;">%</th></tr>
        {lt_rows}
      </table>
      <table style="width:32%;display:inline-table;border-collapse:collapse;vertical-align:top;">
        <tr><th style="{_TABLE_TH}" colspan="3">LOS (avg {beh['los_avg_nights'] or '—'}n, n={beh['los_samples']})</th></tr>
        <tr><th style="{_TABLE_TH}">Bucket</th><th style="{_TABLE_TH};text-align:right;">Count</th><th style="{_TABLE_TH};text-align:right;">%</th></tr>
        {los_rows}
      </table>
    </div>"""


def _render_channel_mix(b: dict) -> str:
    mix = b.get("analytics", {}).get("channel_mix")
    if not mix or mix["total_nights"] == 0:
        return ""
    cur = b["currency"]

    cat_rows = []
    for c in mix["categories"]:
        cat_rows.append(
            f"<tr><td style='{_TABLE_TD}'>{c['source_category']}</td>"
            f"<td style='{_TABLE_TD};text-align:right;'>{c['room_nights']:,}</td>"
            f"<td style='{_TABLE_TD};text-align:right;'>{_pct(c['nights_share_pct'])}</td>"
            f"<td style='{_TABLE_TD};text-align:right;'>{_fmt(c['revenue_native'], cur)}</td>"
            f"<td style='{_TABLE_TD};text-align:right;'>{_pct(c['revenue_share_pct'])}</td></tr>"
        )

    src_rows = []
    for s in mix["top_sources"]:
        src_rows.append(
            f"<tr><td style='{_TABLE_TD}'>{s['source']}</td>"
            f"<td style='{_TABLE_TD};text-align:right;'>{s['room_nights']:,}</td>"
            f"<td style='{_TABLE_TD};text-align:right;'>{_pct(s['nights_share_pct'])}</td>"
            f"<td style='{_TABLE_TD};text-align:right;'>{_fmt(s['revenue_native'], cur)}</td></tr>"
        )

    trend_rows = []
    for t in mix["direct_trend"]:
        trend_rows.append(
            f"<tr><td style='{_TABLE_TD}'>{t['label']}</td>"
            f"<td style='{_TABLE_TD};text-align:right;'>{t['direct_nights']:,}</td>"
            f"<td style='{_TABLE_TD};text-align:right;'>{t['total_nights']:,}</td>"
            f"<td style='{_TABLE_TD};text-align:right;font-weight:600;'>{_pct(t['direct_pct'])}</td></tr>"
        )

    return f"""
    <div style="margin-top:14px;padding-top:12px;border-top:1px solid #f3f4f6;">
      <p style="margin:0 0 6px;font-size:12px;font-weight:600;color:#374151;">
        📡 Channel Mix (last {mix['window_days']}d · {mix['total_nights']:,} nights · {_fmt(mix['total_revenue_native'], cur)})
      </p>
      <table style="width:48%;display:inline-table;border-collapse:collapse;margin-right:2%;vertical-align:top;">
        <tr><th style="{_TABLE_TH}" colspan="5">By Category</th></tr>
        <tr><th style="{_TABLE_TH}">Category</th><th style="{_TABLE_TH};text-align:right;">Nights</th>
            <th style="{_TABLE_TH};text-align:right;">N%</th><th style="{_TABLE_TH};text-align:right;">Revenue</th>
            <th style="{_TABLE_TH};text-align:right;">R%</th></tr>
        {''.join(cat_rows)}
      </table>
      <table style="width:48%;display:inline-table;border-collapse:collapse;vertical-align:top;">
        <tr><th style="{_TABLE_TH}" colspan="4">Top Sources</th></tr>
        <tr><th style="{_TABLE_TH}">Source</th><th style="{_TABLE_TH};text-align:right;">Nights</th>
            <th style="{_TABLE_TH};text-align:right;">N%</th><th style="{_TABLE_TH};text-align:right;">Revenue</th></tr>
        {''.join(src_rows)}
      </table>
      <table style="width:100%;border-collapse:collapse;margin-top:10px;">
        <tr><th style="{_TABLE_TH}" colspan="4">Direct booking trend</th></tr>
        <tr><th style="{_TABLE_TH}">Month</th><th style="{_TABLE_TH};text-align:right;">Direct nights</th>
            <th style="{_TABLE_TH};text-align:right;">Total nights</th><th style="{_TABLE_TH};text-align:right;">Direct %</th></tr>
        {''.join(trend_rows)}
      </table>
    </div>"""


def _render_country_detail(b: dict) -> str:
    ci = b.get("analytics", {}).get("countries")
    if not ci or not ci["top"]:
        return ""
    cur = b["currency"]

    top_rows = []
    yoy_present_count = 0
    for c in ci["top"]:
        yoy = c["yoy_bookings_pct"]
        if yoy is not None:
            yoy_present_count += 1
            yoy_html = (
                f"<span style='color:{'#16a34a' if yoy >= 0 else '#dc2626'};font-weight:600;'>"
                f"{_signed_pct(yoy)}</span>"
            )
        else:
            yoy_html = "<span style='color:#9ca3af;' title='No data for same window in 2025'>n/a</span>"
        top_rows.append(
            f"<tr><td style='{_TABLE_TD}'>{c['country']}</td>"
            f"<td style='{_TABLE_TD};text-align:right;'>{c['bookings']:,}</td>"
            f"<td style='{_TABLE_TD};text-align:right;'>{c['nights']:,}</td>"
            f"<td style='{_TABLE_TD};text-align:right;'>{_fmt(c['adr_native'], cur)}</td>"
            f"<td style='{_TABLE_TD};text-align:right;'>{c['avg_los']:.2f}n</td>"
            f"<td style='{_TABLE_TD};text-align:right;'>{yoy_html}</td></tr>"
        )

    def _chips(lst, color):
        if not lst:
            return "<span style='color:#9ca3af;font-size:12px;'>none</span>"
        chips = []
        for x in lst:
            label = x.get("country", "")
            detail = (f" {_signed_pct(x.get('change_pct'))}"
                      if x.get("change_pct") is not None else
                      f" ({x.get('bookings')} bookings)")
            chips.append(
                f"<span style='background:{color};color:#fff;padding:2px 8px;border-radius:12px;"
                f"font-size:11px;margin-right:4px;display:inline-block;margin-bottom:3px;'>"
                f"{label}{detail}</span>"
            )
        return "".join(chips)

    yoy_coverage_note = (
        f" · YoY data: {yoy_present_count}/{len(ci['top'])} countries"
        if yoy_present_count < len(ci["top"]) else ""
    )

    return f"""
    <div style="margin-top:14px;padding-top:12px;border-top:1px solid #f3f4f6;">
      <p style="margin:0 0 6px;font-size:12px;font-weight:600;color:#374151;">
        🌏 Country Insights (last {ci['window_days']}d){yoy_coverage_note}
      </p>
      <table style="width:100%;border-collapse:collapse;">
        <tr><th style="{_TABLE_TH}">Country</th><th style="{_TABLE_TH};text-align:right;">Bookings</th>
            <th style="{_TABLE_TH};text-align:right;">Nights</th><th style="{_TABLE_TH};text-align:right;">ADR</th>
            <th style="{_TABLE_TH};text-align:right;">Avg LOS</th>
            <th style="{_TABLE_TH};text-align:right;" title="bookings this window vs same window in 2025">YoY</th></tr>
        {''.join(top_rows)}
      </table>
      <p style="margin:6px 0 0;font-size:11px;color:#9ca3af;">
        YoY = bookings (last {ci['window_days']}d) vs same window in {date.today().year - 1}.
        "n/a" = no data for that country in 2025.
      </p>
      <div style="margin-top:10px;font-size:12px;color:#374151;">
        <div style="margin-bottom:4px;"><strong>Growing (vs prior 90d):</strong> {_chips(ci['growing'], '#16a34a')}</div>
        <div style="margin-bottom:4px;"><strong>Shrinking (vs prior 90d):</strong> {_chips(ci['shrinking'], '#dc2626')}</div>
        <div><strong>Emerging (new countries):</strong> {_chips(ci['emerging'], '#6366f1')}</div>
      </div>
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
    <div style="background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:20px;margin-bottom:16px;">
      <h3 style="margin:0 0 4px;font-size:15px;font-weight:700;color:#111827;">🎯 Ad Budget Optimizer</h3>
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
      2. WoW revenue / OCC delta.
      3. Strongest channel or country this week.
      4. Biggest concern (worst ROAS / cancel spike / low CTR).
      5. Next-month on-the-books status.
    """
    bullets: list[str] = []
    cur = b["currency"]
    a = b.get("analytics", {}) or {}
    summary = a.get("summary") or {}
    tw = summary.get("this_week") or {}
    pa_tw = (a.get("paid_ads") or {}).get("this_week") or {}

    # 1. Pacing
    ach = b.get("achievement_pct")
    if ach is not None:
        if ach >= 100:
            bullets.append(f"🟢 <strong>On track</strong> — {ach:.1f}% of {MONTHS_EN[date.today().month]} target hit.")
        elif ach >= 80:
            bullets.append(f"🟡 <strong>Behind target</strong> — {ach:.1f}% pacing; needs +{100-ach:.0f}pp this week.")
        else:
            bullets.append(f"🔴 <strong>At risk</strong> — only {ach:.1f}% of monthly target; review pricing + promo immediately.")

    # 2. WoW revenue movement
    wow = summary.get("wow_revenue_pct")
    if wow is not None:
        direction = "up" if wow >= 0 else "down"
        emoji = "📈" if wow >= 0 else "📉"
        bullets.append(
            f"{emoji} Revenue this week {_fmt(tw.get('revenue'), cur)} ({direction} {abs(wow):.1f}% WoW), "
            f"OCC {_pct((tw.get('occ_pct') or 0)*100) if tw.get('occ_pct') is not None else '—'}, "
            f"ADR {_fmt(tw.get('adr'), cur)}."
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
    if pa_tw.get("cost") and pa_tw.get("roas") is not None and pa_tw["roas"] < 1.0:
        concerns.append(f"⚠️ Paid Ads ROAS {pa_tw['roas']:.2f}x — under break-even")
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
    if kol_actions:
        sections.append(("🎥 KOL — channel signals", "#7c3aed", kol_actions))
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
    tw = pa["this_week"]
    lw = pa["last_week"]

    def _wow(val):
        if val is None:
            return "—"
        color = "#16a34a" if val >= 0 else "#dc2626"
        return f"<span style='color:{color}'>{_signed_pct(val)}</span>"

    if tw["cost"] == 0 and lw["cost"] == 0:
        return f"""
        <div style="margin-top:14px;padding-top:12px;border-top:1px solid #f3f4f6;">
          <p style="margin:0 0 6px;font-size:12px;font-weight:600;color:#374151;">📣 Paid Ads (last {pa['window_days']}d)</p>
          <p style="margin:0;font-size:12px;color:#9ca3af;">No ad spend in the last {pa['window_days']} days.</p>
        </div>"""

    # Channel rows
    ch_rows = "".join(
        f"<tr><td style='{_TABLE_TD}'>{c['channel']}</td>"
        f"<td style='{_TABLE_TD};text-align:right;'>{_fmt(c['cost'], cur)}</td>"
        f"<td style='{_TABLE_TD};text-align:right;'>{c['impressions']:,}</td>"
        f"<td style='{_TABLE_TD};text-align:right;'>{_pct(c['ctr_pct'])}</td>"
        f"<td style='{_TABLE_TD};text-align:right;'>{_pct(c['cvr_pct'])}</td>"
        f"<td style='{_TABLE_TD};text-align:right;'>{c['bookings']}</td>"
        f"<td style='{_TABLE_TD};text-align:right;font-weight:600;'>{c['roas']:.2f}x</td></tr>"
        if c["roas"] is not None else
        f"<tr><td style='{_TABLE_TD}'>{c['channel']}</td>"
        f"<td style='{_TABLE_TD};text-align:right;'>{_fmt(c['cost'], cur)}</td>"
        f"<td style='{_TABLE_TD};text-align:right;'>{c['impressions']:,}</td>"
        f"<td style='{_TABLE_TD};text-align:right;'>{_pct(c['ctr_pct'])}</td>"
        f"<td style='{_TABLE_TD};text-align:right;'>{_pct(c['cvr_pct'])}</td>"
        f"<td style='{_TABLE_TD};text-align:right;'>{c['bookings']}</td>"
        f"<td style='{_TABLE_TD};text-align:right;'>—</td></tr>"
        for c in pa["by_channel"]
    )

    # Funnel rows
    fn_rows = "".join(
        f"<tr><td style='{_TABLE_TD}'>{f['funnel']}</td>"
        f"<td style='{_TABLE_TD};text-align:right;'>{_fmt(f['cost'], cur)}</td>"
        f"<td style='{_TABLE_TD};text-align:right;'>{f['bookings']}</td>"
        f"<td style='{_TABLE_TD};text-align:right;'>{_fmt(f['revenue'], cur)}</td>"
        f"<td style='{_TABLE_TD};text-align:right;font-weight:600;'>{f['roas']:.2f}x</td></tr>"
        if f["roas"] is not None else
        f"<tr><td style='{_TABLE_TD}'>{f['funnel']}</td>"
        f"<td style='{_TABLE_TD};text-align:right;'>{_fmt(f['cost'], cur)}</td>"
        f"<td style='{_TABLE_TD};text-align:right;'>{f['bookings']}</td>"
        f"<td style='{_TABLE_TD};text-align:right;'>{_fmt(f['revenue'], cur)}</td>"
        f"<td style='{_TABLE_TD};text-align:right;'>—</td></tr>"
        for f in pa["by_funnel"]
    )

    def _camp_row(cp, color="#374151"):
        roas = f"{cp['roas']:.2f}x" if cp["roas"] is not None else "—"
        return (
            f"<tr><td style='{_TABLE_TD};color:{color};'>{cp['channel']}</td>"
            f"<td style='{_TABLE_TD};color:{color};'>{(cp['ad_name'] or cp['adset'] or cp['campaign'] or '')[:55]}</td>"
            f"<td style='{_TABLE_TD};text-align:right;'>{_fmt(cp['cost'], cur)}</td>"
            f"<td style='{_TABLE_TD};text-align:right;'>{cp['impressions']:,}</td>"
            f"<td style='{_TABLE_TD};text-align:right;'>{cp['bookings']}</td>"
            f"<td style='{_TABLE_TD};text-align:right;font-weight:600;color:{color};'>{roas}</td></tr>"
        )

    top_html = "".join(_camp_row(c, "#16a34a") for c in pa["top_campaigns"])
    bot_html = "".join(_camp_row(c, "#dc2626") for c in pa["bottom_campaigns"])

    roas_str = f"{tw['roas']:.2f}x" if tw["roas"] is not None else "—"
    return f"""
    <div style="margin-top:14px;padding-top:12px;border-top:1px solid #f3f4f6;">
      <p style="margin:0 0 6px;font-size:12px;font-weight:600;color:#374151;">📣 Paid Ads (last {pa['window_days']}d)</p>
      <p style="margin:0 0 10px;font-size:12px;color:#6b7280;">
        Spend {_fmt(tw['cost'], cur)} · Bookings {tw['bookings']} · Revenue {_fmt(tw['revenue'], cur)} ·
        ROAS {roas_str} ({_wow(pa['wow_roas_pct'])} WoW) · CTR {_pct(tw['ctr_pct'])} · CVR {_pct(tw['cvr_pct'])} · CPA {_fmt(tw['cpa'], cur)}<br/>
        WoW: spend {_wow(pa['wow_cost_pct'])} · revenue {_wow(pa['wow_revenue_pct'])}
      </p>

      <table style="width:48%;display:inline-table;border-collapse:collapse;margin-right:2%;vertical-align:top;">
        <tr><th style="{_TABLE_TH}" colspan="7">By Channel</th></tr>
        <tr><th style="{_TABLE_TH}">Channel</th><th style="{_TABLE_TH};text-align:right;">Cost</th>
            <th style="{_TABLE_TH};text-align:right;">Impr</th><th style="{_TABLE_TH};text-align:right;">CTR</th>
            <th style="{_TABLE_TH};text-align:right;">CVR</th><th style="{_TABLE_TH};text-align:right;">Bk</th>
            <th style="{_TABLE_TH};text-align:right;">ROAS</th></tr>
        {ch_rows or '<tr><td colspan="7" style="'+_TABLE_TD+';color:#9ca3af;">No channel data</td></tr>'}
      </table>

      <table style="width:48%;display:inline-table;border-collapse:collapse;vertical-align:top;">
        <tr><th style="{_TABLE_TH}" colspan="5">By Funnel Stage</th></tr>
        <tr><th style="{_TABLE_TH}">Stage</th><th style="{_TABLE_TH};text-align:right;">Cost</th>
            <th style="{_TABLE_TH};text-align:right;">Bk</th><th style="{_TABLE_TH};text-align:right;">Revenue</th>
            <th style="{_TABLE_TH};text-align:right;">ROAS</th></tr>
        {fn_rows or '<tr><td colspan="5" style="'+_TABLE_TD+';color:#9ca3af;">No funnel data</td></tr>'}
      </table>

      <table style="width:100%;border-collapse:collapse;margin-top:12px;">
        <tr><th style="{_TABLE_TH}" colspan="6">🏆 Top by ROAS (≥1K impressions)</th></tr>
        <tr><th style="{_TABLE_TH}">Ch.</th><th style="{_TABLE_TH}">Name</th>
            <th style="{_TABLE_TH};text-align:right;">Cost</th><th style="{_TABLE_TH};text-align:right;">Impr</th>
            <th style="{_TABLE_TH};text-align:right;">Bk</th><th style="{_TABLE_TH};text-align:right;">ROAS</th></tr>
        {top_html or '<tr><td colspan="6" style="'+_TABLE_TD+';color:#9ca3af;">No qualified campaigns</td></tr>'}
      </table>

      <table style="width:100%;border-collapse:collapse;margin-top:8px;">
        <tr><th style="{_TABLE_TH}" colspan="6">⚠️ Underperformers (ROAS&lt;1, &lt;2 bookings, ≥5K impressions)</th></tr>
        <tr><th style="{_TABLE_TH}">Ch.</th><th style="{_TABLE_TH}">Name</th>
            <th style="{_TABLE_TH};text-align:right;">Cost</th><th style="{_TABLE_TH};text-align:right;">Impr</th>
            <th style="{_TABLE_TH};text-align:right;">Bk</th><th style="{_TABLE_TH};text-align:right;">ROAS</th></tr>
        {bot_html or '<tr><td colspan="6" style="'+_TABLE_TD+';color:#9ca3af;">None — clean week</td></tr>'}
      </table>
    </div>"""


def _render_kol(b: dict) -> str:
    k = b.get("analytics", {}).get("kol")
    if not k:
        return f"""
        <div style="margin-top:14px;padding-top:12px;border-top:1px solid #f3f4f6;">
          <p style="margin:0 0 6px;font-size:12px;font-weight:600;color:#374151;">🎥 KOL</p>
          <p style="margin:0;font-size:12px;color:#9ca3af;">No KOL data available — analytics not computed.</p>
        </div>"""
    if k["total_kols"] == 0:
        return f"""
        <div style="margin-top:14px;padding-top:12px;border-top:1px solid #f3f4f6;">
          <p style="margin:0 0 6px;font-size:12px;font-weight:600;color:#374151;">🎥 KOL (last {k['window_days']}d)</p>
          <p style="margin:0;font-size:12px;color:#9ca3af;">No KOL records for this branch yet — add via /kol page or CSV sync.</p>
        </div>"""
    cur = b["currency"]
    p = k["pipeline"]
    posts = k.get("posts_this_week", 0)

    pipeline_html = (
        f"<span style='color:#6b7280;font-size:12px;'>"
        f"Not Started <strong>{p['Not Started']}</strong> · "
        f"In Progress <strong>{p['In Progress']}</strong> · "
        f"Editing <strong>{p['Editing']}</strong> · "
        f"Done <strong>{p['Done']}</strong>"
        f"</span>"
    )

    stuck_html = (
        "".join(
            f"<li style='margin:2px 0;'>{s['kol_name']} · stuck {s['days_stuck']}d "
            f"<span style='color:#9ca3af;'>(since {s['updated_at']})</span></li>"
            for s in k["stuck"]
        ) or "<li style='color:#9ca3af;'>No stuck deliverables</li>"
    )

    expiring_html = (
        "".join(
            f"<li style='margin:2px 0;'>{e['kol_name']} · {e['expiry_date']} "
            f"<span style='color:{'#dc2626' if e['days_left']<=14 else '#ca8a04'};'>"
            f"({e['days_left']}d left)</span>"
            f"{' · '+e['channel'] if e['channel'] else ''}</li>"
            for e in k["expiring"]
        ) or "<li style='color:#9ca3af;'>No usage rights expiring soon</li>"
    )

    available_html = (
        "".join(
            f"<li style='margin:2px 0;'>{a['kol_name']}"
            f"{' · '+a['nationality'] if a['nationality'] else ''}"
            f"{' · '+a['channel'] if a['channel'] else ''}"
            f"{' · until '+a['expiry'] if a['expiry'] else ''}</li>"
            for a in k["available_for_ads"]
        ) or "<li style='color:#9ca3af;'>None available for ads usage</li>"
    )

    roi_str = f"{k['roi']:.2f}x" if k["roi"] is not None else "—"

    return f"""
    <div style="margin-top:14px;padding-top:12px;border-top:1px solid #f3f4f6;">
      <p style="margin:0 0 6px;font-size:12px;font-weight:600;color:#374151;">
        🎥 KOL (last {k['window_days']}d)
      </p>
      <p style="margin:0 0 10px;font-size:12px;color:#6b7280;">
        Posts published this week: <strong>{posts}</strong> · Open contracts (Draft/Negotiating): <strong>{k['contract_open']}</strong><br/>
        Pipeline (all-time): {pipeline_html}<br/>
        Cost MTD: <strong>{_fmt(k['cost_mtd_native'], cur)}</strong> ·
        Organic bookings: <strong>{k['organic_bookings']}</strong> ({k['organic_nights']} nights) ·
        Revenue: <strong>{_fmt(k['organic_revenue_native'], cur)}</strong> ·
        ROI MTD: <strong>{roi_str}</strong>
      </p>
      <div style="display:flex;gap:14px;flex-wrap:wrap;">
        <div style="flex:1;min-width:240px;">
          <p style="margin:6px 0 4px;font-size:12px;font-weight:600;color:#dc2626;">🚧 Stuck "In Progress" &gt;14d</p>
          <ul style="margin:0;padding-left:18px;font-size:12px;color:#374151;">{stuck_html}</ul>
        </div>
        <div style="flex:1;min-width:240px;">
          <p style="margin:6px 0 4px;font-size:12px;font-weight:600;color:#ca8a04;">⏰ Usage rights expiring (≤60d)</p>
          <ul style="margin:0;padding-left:18px;font-size:12px;color:#374151;">{expiring_html}</ul>
        </div>
        <div style="flex:1;min-width:240px;">
          <p style="margin:6px 0 4px;font-size:12px;font-weight:600;color:#16a34a;">✅ Ads-eligible & Available</p>
          <ul style="margin:0;padding-left:18px;font-size:12px;color:#374151;">{available_html}</ul>
        </div>
      </div>
    </div>"""


def _render_crm(b: dict) -> str:
    """CRM section — revenue only, sourced from CRM-tagged reservations.

    Data source: reservations where room_type or rate_plan_name contains
    'CRM' / "MEANDER'S FRIEND" / 'Travel guide' / 'Grand Open'. Filtered
    on reservation_date (Date Booked) per the team rule. Excludes
    Blogger / House Use / Special Case from revenue.

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
          <p style="margin:0 0 6px;font-size:12px;font-weight:600;color:#374151;">✉️ CRM (last {c['window_days']}d)</p>
          <p style="margin:0;font-size:12px;color:#9ca3af;">
            No CRM-tagged reservations in window. Source: room_type / rate_plan_name
            containing "CRM" / "MEANDER'S FRIEND" / "Travel guide" / "Grand Open".
          </p>
        </div>"""

    return f"""
    <div style="margin-top:14px;padding-top:12px;border-top:1px solid #f3f4f6;">
      <p style="margin:0 0 6px;font-size:12px;font-weight:600;color:#374151;">
        ✉️ CRM (last {c['window_days']}d)
      </p>
      <p style="margin:0;font-size:12px;color:#374151;">
        Bookings: <strong>{rev_t['bookings']}</strong> ({rev_t['nights']} nights) ·
        Revenue: <strong style="color:#111827;">{_fmt(rev_t['revenue'], cur)}</strong> ·
        WoW {_wow(c['wow_revenue_pct'])}
      </p>
      <p style="margin:4px 0 0;font-size:11px;color:#9ca3af;">
        Source: CRM-tagged reservations (room_type/rate_plan contains CRM / MEANDER'S FRIEND / Travel guide / Grand Open). Filtered on Date Booked.
      </p>
    </div>"""


# ── Next-action helpers (Paid Ads / KOL / CRM) ────────────────────────────────


def _ads_next_actions(b: dict) -> list[str]:
    pa = b.get("analytics", {}).get("paid_ads") or {}
    tw = pa.get("this_week") or {}
    cur = b["currency"]
    actions = []
    if tw.get("cost", 0) == 0:
        actions.append("⚠️ No ad spend last 7d — restart campaigns or confirm tracking is wired")
        return actions
    roas = tw.get("roas")
    if roas is not None and roas < 1.0:
        actions.append(f"🔴 Weekly ROAS {roas:.2f}x — pause underperformers + reallocate to top-ROAS ads")
    elif roas is not None and roas >= 3.0:
        actions.append(f"🚀 Weekly ROAS {roas:.2f}x — scale top-channel budget +20%")
    if (tw.get("ctr_pct") or 100) < 1.0 and tw.get("impressions", 0) >= 10_000:
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

        # Country intel next actions — driven by country_scorer tiers
        actions = []
        intel = b.get("country_intel", [])
        hot_countries = [c for c in intel if c["tier"] == "Hot"]
        warm_countries = [c for c in intel if c["tier"] == "Warm"]
        cold_countries = [c for c in intel if c["tier"] == "Cold"]

        for c in hot_countries[:3]:
            wow = f" (WoW {c['wow_growth_pct']:+.0f}%)" if c.get("wow_growth_pct") is not None else ""
            actions.append(
                f"🔥 {c['country']} — Hot (score {c['score']}){wow} — scale ad spend & prioritize OTA rates"
            )
        for c in warm_countries[:2]:
            actions.append(
                f"📈 {c['country']} — Warm (score {c['score']}) — test new ad creatives & increase visibility"
            )
        for c in cold_countries[:1]:
            if c.get("booking_count_this_week", 0) > 0:
                actions.append(
                    f"❄️ {c['country']} — Cold (score {c['score']}) — review content relevance & consider pausing low-ROAS ads"
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

        top_c = " · ".join(f"{c['country']} ({c['bookings']})" for c in b["top_countries"][:3])
        growth_c = " · ".join(
            f"{g['country']} <span style='color:#16a34a'>▲{g['growth_pct']}%</span>"
            for g in b["growth_countries"][:3]
        ) or "—"

        # Country Intel tier summary
        tier_colors = {"Hot": "#dc2626", "Warm": "#f59e0b", "Cold": "#6b7280"}
        intel_c = " · ".join(
            f"<span style='color:{tier_colors.get(c['tier'], '#6b7280')};font-weight:600;'>"
            f"{c['country']} ({c['tier']} {c['score']})</span>"
            for c in intel[:5]
        ) or "—"

        narrative_bullets = _branch_narrative(b)
        narrative_html = (
            "<div style='background:#f9fafb;border-left:3px solid #4f46e5;padding:12px 14px;"
            "margin-bottom:14px;border-radius:6px;'>"
            "<p style='margin:0 0 6px;font-size:11px;font-weight:700;color:#4f46e5;text-transform:uppercase;letter-spacing:0.5px;'>This week at a glance</p>"
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

        sections.append(f"""
        <div style="background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:20px;margin-bottom:16px;">
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
              <td style="padding:8px 12px;text-align:right;font-weight:600;color:#111827;">{_fmt(b['actual_revenue'], cur)}</td>
              <td style="padding:8px 12px;text-align:right;color:#111827;font-weight:600;">
                {_fmt(b.get('next_booked_revenue'), cur)}
                <span style="font-weight:400;color:#6b7280;font-size:11px;display:block;">on-the-books · {_num(b.get('next_booked_nights'))} nights</span>
              </td>
            </tr>
            <tr style="border-top:1px solid #f3f4f6;background:#f9fafb;">
              <td style="padding:8px 12px;color:#374151;">Target</td>
              <td style="padding:8px 12px;text-align:right;color:#6b7280;">{_fmt(b['target_revenue'], cur)}</td>
              <td style="padding:8px 12px;text-align:right;color:#6b7280;">{_fmt(b['next_target'], cur)}</td>
            </tr>
            <tr style="border-top:1px solid #f3f4f6;">
              <td style="padding:8px 12px;color:#374151;">ADR</td>
              <td style="padding:8px 12px;text-align:right;font-weight:600;color:#111827;">{_fmt(b['avg_adr'], cur)}</td>
              <td style="padding:8px 12px;text-align:right;font-weight:600;color:#111827;">{_fmt(b['next_adr'], cur)}</td>
            </tr>
            <tr style="border-top:1px solid #f3f4f6;background:#f9fafb;">
              <td style="padding:8px 12px;color:#374151;">OCC% (actual)</td>
              <td style="padding:8px 12px;text-align:right;font-weight:600;color:#111827;">{_pct(b['avg_occ_pct'])}</td>
              <td style="padding:8px 12px;text-align:right;color:#6b7280;">—</td>
            </tr>
            <tr style="border-top:1px solid #f3f4f6;">
              <td style="padding:8px 12px;color:#374151;">OCC% (forecast)</td>
              <td style="padding:8px 12px;text-align:right;font-weight:600;color:#4f46e5;">{_pct(b['predicted_occ_pct'])}</td>
              <td style="padding:8px 12px;text-align:right;font-weight:600;color:#059669;">{_pct(b['predicted_occ_next'])}</td>
            </tr>
            <tr style="border-top:1px solid #f3f4f6;">
              <td style="padding:8px 12px;color:#374151;font-weight:600;">Forecast</td>
              <td style="padding:8px 12px;text-align:right;font-weight:700;color:#4f46e5;">
                {_fmt(b['occ_forecast'], cur)}
                {f"<span style='font-weight:400;color:#6b7280;'> ({b['occ_forecast_pct']}%)</span>" if b['occ_forecast_pct'] else ""}
              </td>
              <td style="padding:8px 12px;text-align:right;font-weight:700;color:#059669;">
                {_fmt(b['next_forecast'], cur)}
                {f"<span style='font-weight:400;color:#6b7280;'> ({b['next_forecast_pct']}%)</span>" if b['next_forecast_pct'] else ""}
              </td>
            </tr>
          </table>

          <div style="margin-top:14px;padding-top:12px;border-top:1px solid #f3f4f6;font-size:12px;color:#6b7280;">
            <strong style="color:#374151;">Top markets (90d):</strong> {top_c or '—'}<br/>
            <strong style="color:#374151;">Growing markets:</strong> {growth_c}<br/>
            <strong style="color:#374151;">Country Intel:</strong> {intel_c}
          </div>
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


@router.get("/weekly")
def weekly_report(db: Session = Depends(get_db)):
    """Return weekly report data as JSON."""
    today = date.today()
    report = _build_report(db)
    return _envelope({
        "generated_at": datetime.now(timezone.utc).isoformat(),
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

    today = date.today()
    report = _build_report(db)
    html = _build_html(report, today)
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
    """
    recipients_raw = getattr(settings, "EMAIL_RECIPIENTS", "") or ""

    recipients = [r.strip() for r in recipients_raw.split(",") if r.strip()]
    seen = set()
    recipients = [r for r in recipients if not (r in seen or seen.add(r))]
    if not recipients:
        raise HTTPException(400, "EMAIL_RECIPIENTS env var is empty — set it on Zeabur")

    today = date.today()
    report = _build_report(db)
    html = _build_html(report, today)
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


# ── Email preview ─────────────────────────────────────────────────────────────

@router.get("/preview", response_class=HTMLResponse)
def preview_email(db: Session = Depends(get_db)):
    """Return rendered HTML email for iframe preview (no sending)."""
    today = date.today()
    report = _build_report(db)
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
            html = _build_html(report, date.today())
            subject = f"HiD Weekly Report — {date.today().strftime('%d %b %Y')}"
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
