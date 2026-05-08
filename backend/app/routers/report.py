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

        # Forecast: ADR × predicted-room-nights (set in KPI Dashboard).
        # Forecast % drives the color — pacing alone (actual MTD / target)
        # is misleading early in the month.
        fcst = b.get("occ_forecast")
        fcst_pct = b.get("occ_forecast_pct")
        if fcst_pct is not None:
            fcst_color = (
                "#16a34a" if fcst_pct >= 100 else
                "#ca8a04" if fcst_pct >= 90 else
                "#ea580c" if fcst_pct >= 75 else
                "#dc2626"
            )
            fcst_html = (
                f"<div style='font-weight:700;color:{fcst_color};'>{_fmt(fcst, cur)}</div>"
                f"<div style='font-size:10px;color:{fcst_color};'>{_pct(fcst_pct)} of target</div>"
            )
        else:
            fcst_html = "<span style='color:#9ca3af;'>not set</span>"

        # Pacing color now follows forecast % (true risk signal), not actual MTD %
        if fcst_pct is not None:
            ach_color = (
                "#16a34a" if fcst_pct >= 100 else
                "#ca8a04" if fcst_pct >= 90 else
                "#ea580c" if fcst_pct >= 75 else
                "#dc2626"
            )
        else:
            ach_color = "#6b7280"

        rows_html.append(f"""
          <tr>
            <td style="{_TABLE_TD}"><strong>{b['branch_name']}</strong></td>
            <td style="{_TABLE_TD};text-align:right;">{_fmt(b.get('actual_revenue'), cur)}</td>
            <td style="{_TABLE_TD};text-align:right;">{_fmt(b.get('target_revenue'), cur)}</td>
            <td style="{_TABLE_TD};text-align:right;color:{ach_color};font-weight:700;">{_pct(ach)}</td>
            <td style="{_TABLE_TD};text-align:right;vertical-align:top;">{fcst_html}</td>
            <td style="{_TABLE_TD};text-align:right;">{_pct(b.get('avg_occ_pct'))}</td>
            <td style="{_TABLE_TD};text-align:right;">{_fmt(b.get('avg_adr'), cur)}</td>
            <td style="{_TABLE_TD};text-align:right;">{_fmt(revpar, cur)}</td>
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
        Forecast = ADR × predicted-nights (set predicted OCC% in KPI Dashboard). Pacing color follows forecast vs target:
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
        cxl_rows.append(
            f"<tr><td style='{_TABLE_TD}'>{c['source_category']}</td>"
            f"<td style='{_TABLE_TD};text-align:right;'>{c['total']:,}</td>"
            f"<td style='{_TABLE_TD};text-align:right;'>{c['cancelled']:,}</td>"
            f"<td style='{_TABLE_TD};text-align:right;font-weight:600;'>{_pct(c['pct'])}</td>"
            f"<td style='{_TABLE_TD};text-align:right;color:#9ca3af;font-size:10px;'>"
            f"{_pct(c.get('prev_pct'))}</td>"
            f"<td style='{_TABLE_TD};text-align:right;'>{_pp_delta_html(c.get('pp_delta'))}</td></tr>"
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

    def _wow(val):
        if val is None:
            return "<span style='color:#9ca3af;'>n/a</span>"
        color = "#16a34a" if val >= 0 else "#dc2626"
        return f"<span style='color:{color}'>{_signed_pct(val)}</span>"

    cat_rows = []
    for c in mix["categories"]:
        cat_rows.append(
            f"<tr>"
            f"<td style='{_TABLE_TD}'>{c['source_category']}</td>"
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
        src_rows.append(
            f"<tr>"
            f"<td style='{_TABLE_TD}'>{s['source']}</td>"
            f"<td style='{_TABLE_TD};text-align:right;'>{s['room_nights']:,}</td>"
            f"<td style='{_TABLE_TD};text-align:right;color:#9ca3af;font-size:10px;'>{s.get('prev_room_nights', 0):,}</td>"
            f"<td style='{_TABLE_TD};text-align:right;'>{_wow(s.get('wow_nights_pct'))}</td>"
            f"<td style='{_TABLE_TD};text-align:right;'>{_pct(s['nights_share_pct'])}</td>"
            f"<td style='{_TABLE_TD};text-align:right;'>{_fmt(s['revenue_native'], cur)}</td>"
            f"</tr>"
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
      <p style="margin:0 0 4px;font-size:12px;font-weight:600;color:#374151;">
        📡 Channel Mix (last week · {mix['window_start']} → {mix['window_end']})
      </p>
      <p style="margin:0 0 6px;font-size:11px;color:#9ca3af;">
        Source: reservations by check-in date · {mix['total_nights']:,} nights ({_wow(mix.get('wow_total_nights_pct'))} WoW) ·
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
    windows = ci.get("windows") or {}

    def _delta(val, neutral_color="#9ca3af"):
        if val is None:
            return f"<span style='color:{neutral_color};'>n/a</span>"
        color = "#16a34a" if val >= 0 else "#dc2626"
        return f"<span style='color:{color};font-weight:600;'>{_signed_pct(val)}</span>"

    def _build_rows(rows: list) -> str:
        return "".join(
            f"<tr>"
            f"<td style='{_TABLE_TD}'>{c['country']}</td>"
            f"<td style='{_TABLE_TD};text-align:right;'>{c['bookings']:,}</td>"
            f"<td style='{_TABLE_TD};text-align:right;'>{c['nights']:,}</td>"
            f"<td style='{_TABLE_TD};text-align:right;'>{_fmt(c['revenue_native'], cur)}</td>"
            f"<td style='{_TABLE_TD};text-align:right;'>{_fmt(c['adr_native'], cur)}</td>"
            f"<td style='{_TABLE_TD};text-align:right;'>{_delta(c.get('wow_pct'))}</td>"
            f"<td style='{_TABLE_TD};text-align:right;'>{_delta(c.get('d30_pct'))}</td>"
            f"<td style='{_TABLE_TD};text-align:right;'>{_delta(c.get('yoy_pct'))}</td>"
            f"</tr>"
            for c in rows
        )

    book_rows = _build_rows(by_book) or (
        f"<tr><td colspan='8' style='{_TABLE_TD};color:#9ca3af;'>No bookings in last 30d</td></tr>"
    )
    stay_rows = _build_rows(by_stay) or (
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
        YoY = last 30d vs same window {date.today().year - 1}
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

    # 1. Pacing — judged by FORECAST vs target (will the month hit KPI?),
    #    not by actual MTD vs target. Early-month MTD is naturally low —
    #    flagging "🔴 At risk" when MTD < 80% would fire on day 5 of every
    #    month even if forecast is 110%. Forecast = ADR × (predicted OCC ×
    #    rooms × days), set per branch in the KPI Dashboard.
    fcst_pct = b.get("occ_forecast_pct")
    ach = b.get("achievement_pct")  # current actual/target — shown as context
    month_name = MONTHS_EN[date.today().month]
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

    # Channel rows — each metric paired with its WoW delta. Cost / Bookings /
    # ROAS get explicit WoW columns; CTR / CVR / impressions stay as point-
    # in-time signals to keep the table from getting unreadable.
    def _ch_cell(value_html: str, wow_pct):
        if wow_pct is None:
            return f"<td style='{_TABLE_TD};text-align:right;vertical-align:top;'>{value_html}</td>"
        color = "#16a34a" if wow_pct >= 0 else "#dc2626"
        return (
            f"<td style='{_TABLE_TD};text-align:right;vertical-align:top;'>"
            f"<div>{value_html}</div>"
            f"<div style='font-size:10px;color:{color};'>{_signed_pct(wow_pct)}</div>"
            f"</td>"
        )

    ch_rows_parts = []
    for c in pa["by_channel"]:
        roas_html = f"{c['roas']:.2f}x" if c["roas"] is not None else "—"
        cost_html = _fmt(c["cost"], cur)
        bk_html = f"{c['bookings']}"
        ch_rows_parts.append(
            "<tr>"
            f"<td style='{_TABLE_TD};vertical-align:top;'>{c['channel']}</td>"
            + _ch_cell(cost_html, c.get("wow_cost_pct"))
            + f"<td style='{_TABLE_TD};text-align:right;vertical-align:top;'>{c['impressions']:,}</td>"
            + f"<td style='{_TABLE_TD};text-align:right;vertical-align:top;'>{_pct(c['ctr_pct'])}</td>"
            + f"<td style='{_TABLE_TD};text-align:right;vertical-align:top;'>{_pct(c['cvr_pct'])}</td>"
            + _ch_cell(bk_html, c.get("wow_bookings_pct"))
            + _ch_cell(f"<strong>{roas_html}</strong>", c.get("wow_roas_pct"))
            + "</tr>"
        )
    ch_rows = "".join(ch_rows_parts)

    # Activity log — what changed vs prev week (NEW / ENDED / SCALED / CUT)
    act = pa.get("activity_log") or {}

    def _act_row(item, change_label=None):
        ad_name = (item.get("name") or item.get("ad_name") or
                   item.get("adset") or item.get("campaign") or "")[:60]
        change_cell = (
            f"<td style='{_TABLE_TD};text-align:right;'>{change_label}</td>"
            if change_label else ""
        )
        return (
            f"<tr><td style='{_TABLE_TD}'>{item.get('channel') or '—'}</td>"
            f"<td style='{_TABLE_TD}'>{ad_name}</td>"
            f"<td style='{_TABLE_TD};text-align:right;'>{_fmt(item.get('cost'), cur)}</td>"
            f"<td style='{_TABLE_TD};text-align:right;'>{item.get('bookings', 0)}</td>"
            + change_cell
            + "</tr>"
        )

    new_rows = "".join(_act_row(x) for x in act.get("new", []))
    ended_rows = "".join(_act_row(x) for x in act.get("ended", []))
    scaled_rows = "".join(
        _act_row(x, f"<span style='color:#16a34a;'>{_signed_pct(x['change_pct'])}</span> "
                    f"<span style='color:#9ca3af;font-size:10px;'>(was {_fmt(x['prev_cost'], cur)})</span>")
        for x in act.get("scaled", [])
    )
    cut_rows = "".join(
        _act_row(x, f"<span style='color:#dc2626;'>{_signed_pct(x['change_pct'])}</span> "
                    f"<span style='color:#9ca3af;font-size:10px;'>(was {_fmt(x['prev_cost'], cur)})</span>")
        for x in act.get("cut", [])
    )

    has_activity = bool(new_rows or ended_rows or scaled_rows or cut_rows)
    if has_activity:
        def _act_table(title: str, rows_html: str, ncols: int, color: str):
            if not rows_html:
                return ""
            hdr_change = "<th style='" + _TABLE_TH + ";text-align:right;'>WoW</th>" if ncols == 5 else ""
            return (
                f"<table style='width:100%;border-collapse:collapse;margin-top:8px;'>"
                f"<tr><th style='{_TABLE_TH};color:{color};' colspan='{ncols}'>{title}</th></tr>"
                f"<tr><th style='{_TABLE_TH}'>Ch.</th><th style='{_TABLE_TH}'>Ad / Adset</th>"
                f"<th style='{_TABLE_TH};text-align:right;'>Cost</th>"
                f"<th style='{_TABLE_TH};text-align:right;'>Bk</th>"
                f"{hdr_change}</tr>"
                f"{rows_html}</table>"
            )

        activity_html = (
            "<div style='margin-top:12px;'>"
            "<p style='margin:0 0 4px;font-size:12px;font-weight:600;color:#374151;'>📋 Activity Log (vs prev week)</p>"
            + _act_table("🆕 New ads (started this week)", new_rows, 4, "#16a34a")
            + _act_table("⏸ Ended ads (stopped this week)", ended_rows, 4, "#6b7280")
            + _act_table("📈 Scaled (cost up ≥25%)", scaled_rows, 5, "#16a34a")
            + _act_table("📉 Cut (cost down ≥25%)", cut_rows, 5, "#dc2626")
            + "</div>"
        )
    else:
        activity_html = (
            "<p style='margin-top:10px;font-size:12px;color:#9ca3af;'>"
            "📋 Activity Log: no significant ad-lineup changes vs prev week."
            "</p>"
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

    roas_str = f"{lw['roas']:.2f}x" if lw["roas"] is not None else "—"
    return f"""
    <div style="margin-top:14px;padding-top:12px;border-top:1px solid #f3f4f6;">
      <p style="margin:0 0 4px;font-size:12px;font-weight:600;color:#374151;">📣 Paid Ads (last week · {pa['window_start']} → {pa['window_end']})</p>
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

      {activity_html}

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
    """Render the KOL section as a monthly progress table (Invited /
    Collaborated / Posted vs target).

    Source = KOL Engine public API GET /api/public/kol-targets/{slug}.
    All other KOL signals (pipeline, stuck, expiring, ads-available,
    posts, cost MTD, organic ROI) were removed per feedback (2026-05-04)
    to keep the section focused on monthly target progress.
    """
    k = b.get("analytics", {}).get("kol") or {}
    targets = k.get("targets")

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
        f"{MONTHS_EN[targets.get('period_month') or date.today().month]} "
        f"{targets.get('period_year') or date.today().year}"
    )

    def _progress_row(label: str, metric: dict) -> str:
        actual = int(metric.get("actual") or 0)
        target = int(metric.get("target") or 0)
        pct = metric.get("pct")  # may be None when target == 0
        if pct is None:
            pct_str = "<span style='color:#9ca3af;'>n/a</span>"
            color = "#9ca3af"
            bar_pct = 0.0
        else:
            pct_val = float(pct)
            bar_pct = max(0.0, min(100.0, pct_val))
            if pct_val >= 100:
                color = "#16a34a"
            elif pct_val >= 75:
                color = "#ca8a04"
            elif pct_val >= 50:
                color = "#ea580c"
            else:
                color = "#dc2626"
            pct_str = f"<span style='color:{color};font-weight:700;'>{pct_val:.1f}%</span>"
        bar_html = (
            f"<div style='background:#e5e7eb;border-radius:6px;height:6px;width:120px;display:inline-block;vertical-align:middle;'>"
            f"<div style='background:{color};height:6px;border-radius:6px;width:{bar_pct:.1f}%;'></div>"
            f"</div>"
        )
        return (
            f"<tr>"
            f"<td style='{_TABLE_TD};'><strong>{label}</strong></td>"
            f"<td style='{_TABLE_TD};text-align:right;font-weight:600;'>{actual:,}</td>"
            f"<td style='{_TABLE_TD};text-align:right;color:#6b7280;'>/ {target:,}</td>"
            f"<td style='{_TABLE_TD};text-align:right;'>{pct_str}</td>"
            f"<td style='{_TABLE_TD};text-align:right;'>{bar_html}</td>"
            f"</tr>"
        )

    rows = (
        _progress_row("📨 Invited (Proactive)", targets.get("invited_proactive") or {})
        + _progress_row("🤝 Collaborated", targets.get("collaborated") or {})
        + _progress_row("🎬 Posted", targets.get("posted") or {})
    )

    return f"""
    <div style="margin-top:14px;padding-top:12px;border-top:1px solid #f3f4f6;">
      <p style="margin:0 0 6px;font-size:12px;font-weight:600;color:#374151;">
        🎥 KOL — Monthly Progress · {period_label}
      </p>
      <table style="width:100%;border-collapse:collapse;">
        <tr>
          <th style="{_TABLE_TH}">Metric</th>
          <th style="{_TABLE_TH};text-align:right;">Actual</th>
          <th style="{_TABLE_TH};text-align:right;">Target</th>
          <th style="{_TABLE_TH};text-align:right;">Pacing</th>
          <th style="{_TABLE_TH};text-align:right;">Progress</th>
        </tr>
        {rows}
      </table>
      <p style="margin:6px 0 0;font-size:11px;color:#9ca3af;">
        Source: KOL Engine targets API. Pacing color: ≥100% green · 75-99% yellow · 50-74% orange · &lt;50% red.
      </p>
    </div>"""


def _render_crm(b: dict) -> str:
    """CRM section — revenue only, sourced from CRM-tagged reservations.

    Data source: reservations where room_type or rate_plan_name contains
    'CRM' / "MEANDER'S FRIEND" / 'Travel guide' / 'Grand Open'. Filtered
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
            containing "CRM" / "MEANDER'S FRIEND" / "Travel guide" / "Grand Open".
          </p>
        </div>"""

    # Per-rate-plan breakdown — operators want to know WHICH plan drove
    # the CRM bookings, not just the aggregate. Sorted by revenue desc.
    by_rate_plan = c.get("by_rate_plan") or []
    if by_rate_plan:
        rp_rows = "".join(
            f"<tr>"
            f"<td style='{_TABLE_TD}'>{rp['label']}</td>"
            f"<td style='{_TABLE_TD};text-align:right;'>{rp['bookings']}</td>"
            f"<td style='{_TABLE_TD};text-align:right;color:#6b7280;'>{rp['nights']}</td>"
            f"<td style='{_TABLE_TD};text-align:right;font-weight:600;'>{_fmt(rp['revenue'], cur)}</td>"
            f"</tr>"
            for rp in by_rate_plan
        )
        rp_table = f"""
      <table style="width:100%;border-collapse:collapse;margin-top:8px;">
        <tr>
          <th style="{_TABLE_TH}">Rate Plan / Room Type</th>
          <th style="{_TABLE_TH};text-align:right;">Bookings</th>
          <th style="{_TABLE_TH};text-align:right;">Nights</th>
          <th style="{_TABLE_TH};text-align:right;">Revenue</th>
        </tr>
        {rp_rows}
      </table>"""
    else:
        rp_table = ""

    return f"""
    <div style="margin-top:14px;padding-top:12px;border-top:1px solid #f3f4f6;">
      <p style="margin:0 0 4px;font-size:12px;font-weight:600;color:#374151;">
        ✉️ CRM (last week · {c['window_start']} → {c['window_end']})
      </p>
      <p style="margin:0 0 6px;font-size:11px;color:#9ca3af;">
        Source: CRM-tagged reservations (room_type/rate_plan contains CRM / MEANDER'S FRIEND / Travel guide / Grand Open) by reservation_date (Date Booked).
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

        # Country intel next actions — driven by country_scorer tiers
        actions = []
        intel = b.get("country_intel", [])
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
