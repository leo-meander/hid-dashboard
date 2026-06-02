"""
Chat tools — tool definitions exposed to Claude for the HiD assistant.

Each tool wraps existing business logic (services + raw SQL) and returns a
slim JSON payload Claude can reason over. Claude decides which tools to call
based on the user's question.

Phase 1: read-only. No mutation tools. Phase 2 will add execute-action tools
gated behind an explicit permission model.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.branch import Branch
from app.services.metrics_engine import (
    get_daily_metrics,
    get_ota_mix,
)

logger = logging.getLogger(__name__)


# ── Tool schemas (Anthropic tool-use format) ────────────────────────────────

TOOL_DEFS: list[dict] = [
    {
        "name": "get_branches",
        "description": (
            "List all active hotel branches with id, name, currency, capacity. "
            "Use this when the user asks 'which branches', or when you need to "
            "resolve a branch name to an id."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_performance",
        "description": (
            "Performance metrics (OCC, ADR, RevPAR, Revenue, bookings, cancellations) "
            "aggregated daily, weekly, or monthly. Defaults: branch_id = current "
            "selected branch (or all if 'all'); period = 'monthly'; last 6 months. "
            "ADR and RevPAR come blended AND split by segment, so you CAN break "
            "both out by dorm vs room: avg_room_adr_native / avg_dorm_adr_native, "
            "and avg_room_revpar_native / avg_dorm_revpar_native (true per-available-"
            "unit RevPAR = segment revenue ÷ available units × days). The available-"
            "inventory denominators are returned too — total_room_count (private room "
            "units), total_dorm_count (dorm beds), plus avg_room_occ_pct / "
            "avg_dorm_occ_pct and days. Dorm-heavy branches (Taipei, 1948, Oani) have "
            "much lower dorm ADR/RevPAR than the blended figure."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "branch_id": {"type": "string", "description": "UUID of branch, or 'all' for all branches. Defaults to current."},
                "period": {"type": "string", "enum": ["daily", "weekly", "monthly"], "description": "Aggregation level"},
                "date_from": {"type": "string", "description": "ISO date YYYY-MM-DD"},
                "date_to": {"type": "string", "description": "ISO date YYYY-MM-DD"},
            },
        },
    },
    {
        "name": "get_kpi_status",
        "description": (
            "Revenue KPI achievement vs target for a given month. Returns target, "
            "actual revenue, achievement %, projected end-of-month, and gap. Use "
            "when user asks about KPI, target, achievement, or 'are we on track'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "branch_id": {"type": "string"},
                "year": {"type": "integer"},
                "month": {"type": "integer", "description": "1-12"},
            },
        },
    },
    {
        "name": "get_ota_mix",
        "description": (
            "Channel mix breakdown — bookings + revenue per channel "
            "(Booking.com, Agoda, Direct, etc.) over a period. Use for 'channel mix', "
            "'OTA share', 'Direct vs OTA' questions."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "branch_id": {"type": "string"},
                "date_from": {"type": "string"},
                "date_to": {"type": "string"},
            },
        },
    },
    {
        "name": "get_country_breakdown",
        "description": (
            "Top guest source countries by booking volume + revenue over the last N "
            "days, with growth comparison vs prior period. Use for 'top markets', "
            "'where are guests from', 'growing markets'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "branch_id": {"type": "string"},
                "days": {"type": "integer", "description": "Window size, default 30"},
                "limit": {"type": "integer", "description": "Top N countries, default 10"},
            },
        },
    },
    {
        "name": "get_source_by_country",
        "description": (
            "Bookings + revenue broken down by source AND country together — the "
            "source × country cross-tab. Each row is one (source, country) pair with "
            "current-period bookings, revenue, prior-period bookings, and growth "
            "(delta + %). Use when the user wants both dimensions at once: 'which "
            "country grew Website/Booking Engine bookings last week', 'which markets "
            "drove Agoda', 'Direct bookings by country', 'where did OTA growth come "
            "from'. Filter with `source` (substring match on raw source name, e.g. "
            "'website', 'booking engine', 'agoda', 'booking.com') and/or "
            "`source_category` ('OTA' | 'Direct' | 'Local travel agency'); pass "
            "`country` to pin one market. Defaults to date_basis='reservation' "
            "(when booked) over the last 7 days, with growth vs the prior 7 days. "
            "For top markets WITHOUT a source split use get_country_breakdown; for "
            "channel mix WITHOUT a country split use get_ota_mix."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "branch_id": {"type": "string", "description": "UUID of branch, or 'all'. Defaults to current."},
                "source": {"type": "string", "description": "Case-insensitive substring match on raw source name (e.g. 'website', 'agoda'). Omit for all sources."},
                "source_category": {"type": "string", "enum": ["OTA", "Direct", "Local travel agency"], "description": "Exact source_category filter. Omit for all."},
                "country": {"type": "string", "description": "Pin to one guest country (case-insensitive). Omit for all."},
                "date_from": {"type": "string", "description": "ISO date YYYY-MM-DD. If set, overrides `days`."},
                "date_to": {"type": "string", "description": "ISO date YYYY-MM-DD."},
                "days": {"type": "integer", "description": "Window size in days when date_from/date_to omitted, default 7."},
                "date_basis": {"type": "string", "enum": ["reservation", "checkin"], "description": "Filter by reservation_date (when booked, default) or check_in_date (when staying)."},
                "limit": {"type": "integer", "description": "Max (source, country) rows, default 15."},
            },
        },
    },
    {
        "name": "get_alerts",
        "description": (
            "Active alerts — anomalies/issues the system flagged today (drops in "
            "OCC, spike in cancellations, ad ROAS dropping, etc.). Use when the "
            "user asks 'what's wrong', 'any alerts', or wants to triage issues."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "branch_id": {"type": "string"},
                "severity": {"type": "string", "enum": ["all", "critical", "warning", "info"]},
            },
        },
    },
    {
        "name": "get_upcoming_holidays",
        "description": (
            "Upcoming holiday windows across source markets in the next N days, "
            "with travel propensity and recommended action notes. Use for "
            "'upcoming holidays', 'what to plan for', seasonal pushes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "Window in days, default 60"},
            },
        },
    },
    {
        "name": "get_ads_performance",
        "description": (
            "Paid ads aggregates: spend, revenue, ROAS, impressions, clicks, "
            "bookings — grouped by channel and target country. Includes top "
            "performers and worst performers. Use for ad performance questions."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "branch_id": {"type": "string"},
                "date_from": {"type": "string"},
                "date_to": {"type": "string"},
            },
        },
    },
    {
        "name": "get_kol_performance",
        "description": (
            "KOL summary: invited, collaborated, posted, organic bookings, "
            "and rights expiring soon. Use for KOL/influencer questions."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "branch_id": {"type": "string"},
            },
        },
    },
    {
        "name": "get_country_profile",
        "description": (
            "Detailed booking profile for one or many source countries: lead time "
            "(avg + 0-7/8-30/31-60/60+ buckets), length of stay, pax distribution "
            "(solo=1 adult, couple=2, friends=3-4, family=5+), room type split "
            "(Dorm vs Room), and revenue. Use when the user asks about lead time, "
            "pax/segment composition, room type by country, 'who books from X', "
            "'what target should we run for X', or any booking-behavior question. "
            "Pass `country` to drill into one country (also returns its top 5 "
            "room_type names); omit to get top N countries. Excludes cancellations "
            "and non-paying sources (KOL, Blogger, House Use, Special Case, Work "
            "Exchange, Maintenance) so figures reflect real paying guests."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "branch_id": {"type": "string"},
                "country": {"type": "string", "description": "Country name e.g. 'Canada' (case-insensitive). Omit to get top N."},
                "days": {"type": "integer", "description": "Window size in days, default 90"},
                "limit": {"type": "integer", "description": "Top N countries when no country filter, default 10"},
            },
        },
    },
    {
        "name": "get_marketing_activity",
        "description": (
            "Consolidated marketing activity for a date range: CRM bookings, "
            "KOL bookings, paid ads bookings + revenue. Filtered by reservation_date "
            "(when booked), not check_in_date. Use for 'how's marketing performing'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "branch_id": {"type": "string"},
                "date_from": {"type": "string"},
                "date_to": {"type": "string"},
            },
        },
    },
    {
        "name": "get_cancellation_leadtime",
        "description": (
            "How long before check-in the CANCELLED / no-show cohort cancelled: "
            "days between cancel date and check_in_date, bucketed (after/same-day, "
            "1-7, 8-30, 31-60, 60+ days) with avg + median. Use for 'how far in "
            "advance were the cancellations', cancellation timing/behaviour. NOTE: "
            "the cancel date is APPROXIMATE — derived from the reservation's "
            "last-modified timestamp (Cloudbeds exposes no exact cancellationDate "
            "in HiD's data; for a cancelled booking the final modification is "
            "effectively the cancellation). Filtered by check_in_date; defaults to "
            "the last 90 days."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "branch_id": {"type": "string", "description": "UUID of branch, or 'all'. Defaults to current."},
                "date_from": {"type": "string", "description": "ISO date YYYY-MM-DD (by check-in date)"},
                "date_to": {"type": "string", "description": "ISO date YYYY-MM-DD (by check-in date)"},
            },
        },
    },
]


# ── Helpers ──────────────────────────────────────────────────────────────────

def _resolve_branch_id(input_branch_id: Any, default_branch_id: Optional[str]) -> Optional[str]:
    """Resolve branch_id from tool input, falling back to caller default.
    Returns None when 'all' (means no branch filter)."""
    val = input_branch_id if input_branch_id else default_branch_id
    if not val or str(val).lower() == "all":
        return None
    return str(val)


def _parse_date(s: Optional[str], default: date) -> date:
    if not s:
        return default
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return default


def _b_filter_clause(branch_id: Optional[str], col_alias: str = "r") -> tuple[str, dict]:
    if branch_id:
        return f"AND {col_alias}.branch_id = :bid", {"bid": branch_id}
    return "", {}


def _resolve_compare_windows(
    inp: dict, today: date, default_days: int = 7
) -> tuple[date, date, date, date]:
    """Resolve a current window and the equal-length window immediately before it.

    If date_from/date_to are given they define the current window (inclusive);
    otherwise the current window is the last `days` ending today. The previous
    window is the same number of days ending the day before the current window
    starts. Returns (d_from, d_to, prev_from, prev_to)."""
    if inp.get("date_from") or inp.get("date_to"):
        d_to = _parse_date(inp.get("date_to"), today)
        d_from = _parse_date(inp.get("date_from"), d_to - timedelta(days=default_days - 1))
    else:
        days = int(inp.get("days") or default_days)
        days = max(days, 1)
        d_to = today
        d_from = today - timedelta(days=days - 1)
    if d_from > d_to:
        d_from, d_to = d_to, d_from
    window_len = (d_to - d_from).days + 1
    prev_to = d_from - timedelta(days=1)
    prev_from = prev_to - timedelta(days=window_len - 1)
    return d_from, d_to, prev_from, prev_to


# ── Tool implementations ─────────────────────────────────────────────────────

def tool_get_branches(db: Session, _input: dict, _default_branch: Optional[str]) -> dict:
    rows = db.query(Branch).filter_by(is_active=True).order_by(Branch.name).all()
    return {
        "branches": [
            {
                "id": str(b.id),
                "name": b.name,
                "city": b.city,
                "country": b.country,
                "currency": b.currency or "VND",
                "total_rooms": b.total_rooms,
            }
            for b in rows
        ]
    }


def tool_get_performance(db: Session, inp: dict, default_branch: Optional[str]) -> dict:
    branch_id = _resolve_branch_id(inp.get("branch_id"), default_branch)
    period = (inp.get("period") or "monthly").lower()
    today = date.today()

    if period == "daily":
        d_to = _parse_date(inp.get("date_to"), today)
        d_from = _parse_date(inp.get("date_from"), d_to - timedelta(days=29))
    elif period == "weekly":
        d_to = _parse_date(inp.get("date_to"), today)
        d_from = _parse_date(inp.get("date_from"), d_to - timedelta(weeks=12))
    else:  # monthly
        d_to = _parse_date(inp.get("date_to"), today)
        d_from = _parse_date(
            inp.get("date_from"),
            date(d_to.year - (1 if d_to.month <= 6 else 0), ((d_to.month - 6 - 1) % 12) + 1, 1),
        )

    bid_uuid = UUID(branch_id) if branch_id else None
    rows = get_daily_metrics(db, bid_uuid, d_from, d_to)

    # Branch lookup: name + per-segment available inventory. total_room_count
    # (private room units) and total_dorm_count (dorm beds) are the available-
    # inventory denominators that let RevPAR be split dorm vs room — they exist
    # on the branch but were never exposed, so segment RevPAR looked impossible.
    branch_info = {
        str(b.id): {
            "name": b.name,
            "total_rooms": b.total_rooms or 0,
            "total_room_count": b.total_room_count or 0,
            "total_dorm_count": b.total_dorm_count or 0,
        }
        for b in db.query(Branch).filter_by(is_active=True).all()
    }
    name_map = {bid: info["name"] for bid, info in branch_info.items()}

    if period == "daily":
        out = [
            {
                "branch_id": str(dm.branch_id),
                "branch_name": name_map.get(str(dm.branch_id), "Unknown"),
                "date": dm.date.isoformat(),
                "occ_pct": float(dm.occ_pct or 0),
                "adr_native": float(dm.adr_native or 0),
                "room_adr_native": float(dm.room_adr_native) if dm.room_adr_native is not None else None,
                "dorm_adr_native": float(dm.dorm_adr_native) if dm.dorm_adr_native is not None else None,
                "revpar_native": float(dm.revpar_native or 0),
                "revenue_native": float(dm.revenue_native or 0),
                "revenue_vnd": float(dm.revenue_vnd or 0),
                "room_revenue_native": float(dm.room_revenue_native) if dm.room_revenue_native is not None else None,
                "dorm_revenue_native": float(dm.dorm_revenue_native) if dm.dorm_revenue_native is not None else None,
                "rooms_sold": dm.rooms_sold,
                "dorms_sold": dm.dorms_sold,
                "total_sold": dm.total_sold,
                "total_room_count": branch_info.get(str(dm.branch_id), {}).get("total_room_count"),
                "total_dorm_count": branch_info.get(str(dm.branch_id), {}).get("total_dorm_count"),
                "new_bookings": dm.new_bookings,
                "cancellations": dm.cancellations,
                "cancellation_pct": float(dm.cancellation_pct or 0),
            }
            for dm in rows
        ]
        return {"period": "daily", "date_from": d_from.isoformat(), "date_to": d_to.isoformat(), "rows": out[-90:]}

    # Aggregate
    agg: dict = {}
    if period == "weekly":
        from datetime import date as _date

        def _key(d: _date) -> tuple:
            iso = d.isocalendar()
            return (str(dm.branch_id), iso.year, iso.week)
    else:  # monthly
        def _key(d):
            return (str(dm.branch_id), d.year, d.month)

    for dm in rows:
        k = _key(dm.date)
        a = agg.setdefault(k, {
            "branch_id": str(dm.branch_id),
            "total_sold": 0, "rooms_sold": 0, "dorms_sold": 0,
            "revenue_native": 0.0, "revenue_vnd": 0.0,
            "room_revenue_native": 0.0, "dorm_revenue_native": 0.0,
            "new_bookings": 0, "cancellations": 0, "occ_sum": 0.0, "n": 0,
        })
        if period == "weekly":
            a["year"] = k[1]; a["week"] = k[2]
        else:
            a["year"] = k[1]; a["month"] = k[2]
        # ADR denominator must include dorm beds, not just private rooms.
        # dm.rooms_sold is Room-category only; dm.total_sold = rooms + dorms,
        # which matches the revenue numerator (rooms + dorm revenue) and the
        # dashboard's SOLD column. Using rooms_sold here over-stated ADR/RevPAR
        # several-fold for dorm-heavy branches (Taipei, 1948, Oani).
        a["total_sold"] += dm.total_sold or 0
        a["rooms_sold"] += dm.rooms_sold or 0
        a["dorms_sold"] += dm.dorms_sold or 0
        a["room_revenue_native"] += float(dm.room_revenue_native or 0)
        a["dorm_revenue_native"] += float(dm.dorm_revenue_native or 0)
        a["revenue_native"] += float(dm.revenue_native or 0)
        a["revenue_vnd"] += float(dm.revenue_vnd or 0)
        a["new_bookings"] += dm.new_bookings or 0
        a["cancellations"] += dm.cancellations or 0
        a["occ_sum"] += float(dm.occ_pct or 0)
        a["n"] += 1

    out = []
    for v in agg.values():
        n = v["n"] or 1
        adr = v["revenue_native"] / v["total_sold"] if v["total_sold"] > 0 else 0
        # Per-segment ADR: Room revenue ÷ private rooms sold, Dorm revenue ÷
        # dorm beds sold. Lets the assistant answer "ADR by dorm vs room" —
        # the split lives on daily_metrics but was never exposed by any tool.
        room_adr = v["room_revenue_native"] / v["rooms_sold"] if v["rooms_sold"] > 0 else None
        dorm_adr = v["dorm_revenue_native"] / v["dorms_sold"] if v["dorms_sold"] > 0 else None
        occ = v["occ_sum"] / n
        # True per-segment RevPAR = segment revenue ÷ (segment available units ×
        # days). total_room_count = private room units, total_dorm_count = dorm
        # beds — the per-segment available inventory the dashboard already holds.
        info = branch_info.get(v["branch_id"], {})
        trc = info.get("total_room_count", 0) or 0
        tdc = info.get("total_dorm_count", 0) or 0
        room_revpar = v["room_revenue_native"] / (trc * n) if trc > 0 else None
        dorm_revpar = v["dorm_revenue_native"] / (tdc * n) if tdc > 0 else None
        # Segment OCC on the sold-units basis (reservation_daily) — consistent
        # with segment ADR/RevPAR so RevPAR = ADR × OCC holds. NOT daily_metrics
        # room_occ_pct/dorm_occ_pct, which are spanning-based and badly
        # under-count dorm beds (dorm reservations lack per-bed room numbers).
        room_occ = v["rooms_sold"] / (trc * n) if trc > 0 else None
        dorm_occ = v["dorms_sold"] / (tdc * n) if tdc > 0 else None
        v["branch_name"] = name_map.get(v["branch_id"], "Unknown")
        v["days"] = n
        v["total_rooms"] = info.get("total_rooms")
        v["total_room_count"] = trc
        v["total_dorm_count"] = tdc
        v["avg_occ_pct"] = round(occ, 4)
        v["avg_room_occ_pct"] = round(room_occ, 4) if room_occ is not None else None
        v["avg_dorm_occ_pct"] = round(dorm_occ, 4) if dorm_occ is not None else None
        v["avg_adr_native"] = round(adr, 2)
        v["avg_room_adr_native"] = round(room_adr, 2) if room_adr is not None else None
        v["avg_dorm_adr_native"] = round(dorm_adr, 2) if dorm_adr is not None else None
        v["avg_revpar_native"] = round(occ * adr, 2)
        v["avg_room_revpar_native"] = round(room_revpar, 2) if room_revpar is not None else None
        v["avg_dorm_revpar_native"] = round(dorm_revpar, 2) if dorm_revpar is not None else None
        v["revenue_native"] = round(v["revenue_native"], 2)
        v["revenue_vnd"] = round(v["revenue_vnd"], 2)
        v["room_revenue_native"] = round(v["room_revenue_native"], 2)
        v["dorm_revenue_native"] = round(v["dorm_revenue_native"], 2)
        v.pop("occ_sum", None); v.pop("n", None)
        out.append(v)

    out.sort(key=lambda x: (x.get("branch_id"), x.get("year", 0), x.get("month", x.get("week", 0))))
    return {"period": period, "date_from": d_from.isoformat(), "date_to": d_to.isoformat(), "rows": out}


def tool_get_kpi_status(db: Session, inp: dict, default_branch: Optional[str]) -> dict:
    branch_id = _resolve_branch_id(inp.get("branch_id"), default_branch)
    today = date.today()
    year = int(inp.get("year") or today.year)
    month = int(inp.get("month") or today.month)

    bf, params = _b_filter_clause(branch_id, "kt")
    params.update({"y": year, "m": month})
    rows = db.execute(text(f"""
        SELECT b.id, b.name, b.currency,
               kt.target_revenue_native, kt.actual_revenue_override
        FROM branches b
        LEFT JOIN kpi_targets kt
               ON kt.branch_id = b.id AND kt.year = :y AND kt.month = :m
        WHERE b.is_active = true {bf.replace('AND kt.branch_id', 'AND b.id') if bf else ''}
        ORDER BY b.name
    """), params).fetchall()

    # Actual revenue from daily_metrics for the month
    bf2, params2 = _b_filter_clause(branch_id, "dm")
    params2.update({"y": year, "m": month})
    actual_rows = db.execute(text(f"""
        SELECT dm.branch_id, COALESCE(SUM(dm.revenue_native), 0) AS rev
        FROM daily_metrics dm
        WHERE EXTRACT(YEAR FROM dm.date) = :y
          AND EXTRACT(MONTH FROM dm.date) = :m
          {bf2}
        GROUP BY dm.branch_id
    """), params2).fetchall()
    actual_map = {str(r[0]): float(r[1]) for r in actual_rows}

    import calendar
    days_in_month = calendar.monthrange(year, month)[1]
    is_current = (year == today.year and month == today.month)
    days_elapsed = today.day if is_current else days_in_month
    progress = days_elapsed / days_in_month if days_in_month else 1

    out = []
    for r in rows:
        bid = str(r[0])
        target = float(r[3] or 0)
        override = float(r[4]) if r[4] is not None else None
        actual = override if override is not None else actual_map.get(bid, 0.0)
        achievement = (actual / target * 100) if target > 0 else None
        projected_eom = actual / progress if progress > 0 and is_current else actual
        gap = target - projected_eom
        out.append({
            "branch_id": bid,
            "branch_name": r[1],
            "currency": r[2],
            "year": year, "month": month,
            "target_revenue_native": target,
            "actual_revenue_native": round(actual, 2),
            "achievement_pct": round(achievement, 2) if achievement is not None else None,
            "days_elapsed": days_elapsed,
            "days_in_month": days_in_month,
            "projected_eom_native": round(projected_eom, 2),
            "gap_to_target_native": round(gap, 2),
            "on_track": (projected_eom >= target * 0.98) if target > 0 else None,
        })
    return {"year": year, "month": month, "branches": out}


def tool_get_ota_mix(db: Session, inp: dict, default_branch: Optional[str]) -> dict:
    branch_id = _resolve_branch_id(inp.get("branch_id"), default_branch)
    today = date.today()
    d_to = _parse_date(inp.get("date_to"), today)
    d_from = _parse_date(inp.get("date_from"), d_to - timedelta(days=29))

    bid_uuid = UUID(branch_id) if branch_id else None
    mix = get_ota_mix(db, bid_uuid, d_from, d_to)
    total_count = sum(v["count"] for v in mix.values()) or 1
    total_rev = sum(v["revenue_native"] for v in mix.values()) or 1
    rows = []
    for ch, v in sorted(mix.items(), key=lambda x: -x[1]["count"]):
        rows.append({
            "channel": ch,
            "category": v["category"],
            "count": v["count"],
            "share_pct": round(v["count"] / total_count * 100, 2),
            "revenue_native": round(v["revenue_native"], 2),
            "revenue_share_pct": round(v["revenue_native"] / total_rev * 100, 2),
        })
    return {"date_from": d_from.isoformat(), "date_to": d_to.isoformat(), "total_bookings": total_count, "channels": rows}


def tool_get_country_breakdown(db: Session, inp: dict, default_branch: Optional[str]) -> dict:
    branch_id = _resolve_branch_id(inp.get("branch_id"), default_branch)
    days = int(inp.get("days") or 30)
    limit = int(inp.get("limit") or 10)
    bf, params = _b_filter_clause(branch_id, "r")
    params.update({"d": days, "limit": limit})

    rows = db.execute(text(f"""
        WITH recent AS (
            SELECT r.guest_country, r.guest_country_code, COUNT(*) AS cnt,
                   COALESCE(SUM(r.grand_total_vnd), 0) AS rev_vnd
            FROM reservations r
            WHERE r.guest_country IS NOT NULL AND r.guest_country != '' AND r.guest_country != '0'
              AND length(r.guest_country) > 1
              AND r.status NOT IN ('canceled','cancelled','no_show','no-show','cancelled_by_guest')
              AND r.check_in_date >= CURRENT_DATE - (:d || ' days')::interval
              {bf}
            GROUP BY r.guest_country, r.guest_country_code
        ),
        prev AS (
            SELECT r.guest_country, COUNT(*) AS cnt
            FROM reservations r
            WHERE r.guest_country IS NOT NULL AND r.guest_country != '' AND r.guest_country != '0'
              AND length(r.guest_country) > 1
              AND r.status NOT IN ('canceled','cancelled','no_show','no-show','cancelled_by_guest')
              AND r.check_in_date >= CURRENT_DATE - (2 * :d || ' days')::interval
              AND r.check_in_date <  CURRENT_DATE - (:d || ' days')::interval
              {bf}
            GROUP BY r.guest_country
        )
        SELECT recent.guest_country, recent.guest_country_code, recent.cnt, recent.rev_vnd,
               COALESCE(prev.cnt, 0) AS prev_cnt
        FROM recent
        LEFT JOIN prev ON prev.guest_country = recent.guest_country
        ORDER BY recent.cnt DESC
        LIMIT :limit
    """), params).fetchall()

    out = []
    for r in rows:
        cur, prv = int(r[2]), int(r[4] or 0)
        growth = None if prv == 0 else round((cur - prv) / prv * 100, 2)
        out.append({
            "country": r[0], "country_code": r[1],
            "bookings": cur, "revenue_vnd": float(r[3] or 0),
            "prev_period_bookings": prv,
            "growth_pct": growth,
        })
    return {"window_days": days, "countries": out}


def tool_get_source_by_country(db: Session, inp: dict, default_branch: Optional[str]) -> dict:
    """Source × country cross-tab with prior-period growth. Each row is one
    (source, country) pair. Defaults to reservation_date (when booked) over the
    last 7 days vs the prior 7 days. Excludes cancellations/no-shows only — the
    point is to see every booking source, so no source is dropped."""
    branch_id = _resolve_branch_id(inp.get("branch_id"), default_branch)
    limit = int(inp.get("limit") or 15)
    date_basis = (inp.get("date_basis") or "reservation").lower()
    date_col = "r.check_in_date" if date_basis == "checkin" else "r.reservation_date"

    d_from, d_to, prev_from, prev_to = _resolve_compare_windows(inp, date.today(), default_days=7)

    bf, params = _b_filter_clause(branch_id, "r")
    params.update({"df": d_from, "dt": d_to, "pf": prev_from, "pt": prev_to, "limit": limit})

    # Optional filters — built once and reused in both window CTEs.
    filters = ""
    if inp.get("source"):
        filters += " AND lower(r.source) LIKE :src"
        params["src"] = f"%{str(inp['source']).lower().strip()}%"
    if inp.get("source_category"):
        filters += " AND lower(r.source_category) = lower(:cat)"
        params["cat"] = str(inp["source_category"]).strip()
    if inp.get("country"):
        filters += " AND lower(r.guest_country) = lower(:country)"
        params["country"] = str(inp["country"]).strip()

    country_valid = (
        "r.guest_country IS NOT NULL AND r.guest_country != '' "
        "AND r.guest_country != '0' AND length(r.guest_country) > 1"
    )
    status_ok = "r.status NOT IN ('canceled','cancelled','no_show','no-show','cancelled_by_guest')"

    rows = db.execute(text(f"""
        WITH recent AS (
            SELECT COALESCE(r.source, 'Unknown') AS source,
                   COALESCE(r.source_category, 'OTA') AS source_category,
                   r.guest_country, r.guest_country_code,
                   COUNT(*) AS cnt,
                   COALESCE(SUM(r.grand_total_vnd), 0) AS rev_vnd
            FROM reservations r
            WHERE {country_valid} AND {status_ok}
              AND {date_col} BETWEEN :df AND :dt
              {bf}{filters}
            GROUP BY r.source, r.source_category, r.guest_country, r.guest_country_code
        ),
        prev AS (
            SELECT COALESCE(r.source, 'Unknown') AS source,
                   r.guest_country, COUNT(*) AS cnt
            FROM reservations r
            WHERE {country_valid} AND {status_ok}
              AND {date_col} BETWEEN :pf AND :pt
              {bf}{filters}
            GROUP BY r.source, r.guest_country
        )
        SELECT recent.source, recent.source_category, recent.guest_country,
               recent.guest_country_code, recent.cnt, recent.rev_vnd,
               COALESCE(prev.cnt, 0) AS prev_cnt
        FROM recent
        LEFT JOIN prev
               ON prev.source = recent.source
              AND prev.guest_country = recent.guest_country
        ORDER BY recent.cnt DESC
        LIMIT :limit
    """), params).fetchall()

    out = []
    for r in rows:
        cur, prv = int(r[4]), int(r[6] or 0)
        growth = None if prv == 0 else round((cur - prv) / prv * 100, 2)
        out.append({
            "source": r[0],
            "source_category": r[1],
            "country": r[2],
            "country_code": r[3],
            "bookings": cur,
            "revenue_vnd": float(r[5] or 0),
            "prev_period_bookings": prv,
            "delta_bookings": cur - prv,
            "growth_pct": growth,
        })

    return {
        "date_basis": "check_in_date" if date_basis == "checkin" else "reservation_date",
        "current_period": {"date_from": d_from.isoformat(), "date_to": d_to.isoformat()},
        "prior_period": {"date_from": prev_from.isoformat(), "date_to": prev_to.isoformat()},
        "filters": {
            "source": inp.get("source"),
            "source_category": inp.get("source_category"),
            "country": inp.get("country"),
        },
        "exclusions": "cancelled/no-show only",
        "note": "growth_pct is null when the country had no bookings for this source in the prior period (new market).",
        "rows": out,
    }


def tool_get_alerts(db: Session, inp: dict, default_branch: Optional[str]) -> dict:
    branch_id = _resolve_branch_id(inp.get("branch_id"), default_branch)
    severity = (inp.get("severity") or "all").lower()
    bf, params = _b_filter_clause(branch_id, "a")
    sev_clause = "" if severity == "all" else "AND a.severity = :sev"
    if severity != "all":
        params["sev"] = severity

    try:
        rows = db.execute(text(f"""
            SELECT a.id, a.branch_id, b.name, a.alert_type, a.severity,
                   a.title, a.message, a.metric_value, a.threshold_value,
                   a.status, a.triggered_at
            FROM alerts a
            LEFT JOIN branches b ON a.branch_id = b.id
            WHERE a.status IN ('active','acknowledged')
              {bf} {sev_clause}
            ORDER BY a.triggered_at DESC
            LIMIT 30
        """), params).fetchall()
    except Exception as e:
        logger.warning("alerts table query failed: %s", e)
        return {"alerts": [], "note": "Alerts table not available"}

    return {
        "alerts": [
            {
                "id": str(r[0]),
                "branch_id": str(r[1]) if r[1] else None,
                "branch_name": r[2],
                "alert_type": r[3],
                "severity": r[4],
                "title": r[5],
                "message": r[6],
                "metric_value": float(r[7]) if r[7] is not None else None,
                "threshold_value": float(r[8]) if r[8] is not None else None,
                "status": r[9],
                "triggered_at": r[10].isoformat() if r[10] else None,
            }
            for r in rows
        ]
    }


def tool_get_upcoming_holidays(db: Session, inp: dict, _default: Optional[str]) -> dict:
    days = int(inp.get("days") or 60)
    try:
        from app.services.holiday_intel import get_upcoming_windows
        data = get_upcoming_windows(db, days)
        return {"days": days, "windows": data}
    except Exception as e:
        logger.warning("holiday intel query failed: %s", e)
        return {"windows": [], "note": "Holiday intel not available"}


def tool_get_ads_performance(db: Session, inp: dict, default_branch: Optional[str]) -> dict:
    branch_id = _resolve_branch_id(inp.get("branch_id"), default_branch)
    today = date.today()
    d_to = _parse_date(inp.get("date_to"), today)
    d_from = _parse_date(inp.get("date_from"), d_to - timedelta(days=29))
    bf, params = _b_filter_clause(branch_id, "a")
    params.update({"df": d_from, "dt": d_to})

    summary_rows = db.execute(text(f"""
        SELECT a.channel,
               COALESCE(SUM(a.cost_native), 0) AS spend,
               COALESCE(SUM(a.revenue_native), 0) AS revenue,
               COALESCE(SUM(a.impressions), 0) AS impressions,
               COALESCE(SUM(a.clicks), 0) AS clicks,
               COALESCE(SUM(a.bookings), 0) AS bookings
        FROM ads_performance a
        WHERE a.date_from >= :df AND a.date_to <= :dt
          {bf}
        GROUP BY a.channel
        ORDER BY spend DESC
    """), params).fetchall()

    by_country_rows = db.execute(text(f"""
        SELECT a.target_country,
               COALESCE(SUM(a.cost_native), 0) AS spend,
               COALESCE(SUM(a.revenue_native), 0) AS revenue,
               COALESCE(SUM(a.bookings), 0) AS bookings
        FROM ads_performance a
        WHERE a.date_from >= :df AND a.date_to <= :dt
          AND a.target_country IS NOT NULL AND a.target_country != ''
          {bf}
        GROUP BY a.target_country
        ORDER BY spend DESC
        LIMIT 10
    """), params).fetchall()

    by_channel = []
    for r in summary_rows:
        spend = float(r[1])
        rev = float(r[2])
        by_channel.append({
            "channel": r[0],
            "spend_native": round(spend, 2),
            "revenue_native": round(rev, 2),
            "roas": round(rev / spend, 2) if spend > 0 else None,
            "impressions": int(r[3]),
            "clicks": int(r[4]),
            "bookings": int(r[5]),
            "ctr_pct": round(int(r[4]) / int(r[3]) * 100, 2) if r[3] else None,
        })

    by_country = []
    for r in by_country_rows:
        spend = float(r[1]); rev = float(r[2])
        by_country.append({
            "target_country": r[0],
            "spend_native": round(spend, 2),
            "revenue_native": round(rev, 2),
            "roas": round(rev / spend, 2) if spend > 0 else None,
            "bookings": int(r[3]),
        })
    return {"date_from": d_from.isoformat(), "date_to": d_to.isoformat(),
            "by_channel": by_channel, "top_countries": by_country}


def tool_get_kol_performance(db: Session, inp: dict, default_branch: Optional[str]) -> dict:
    branch_id = _resolve_branch_id(inp.get("branch_id"), default_branch)
    bf, params = _b_filter_clause(branch_id, "k")

    summary_rows = db.execute(text(f"""
        SELECT k.deliverable_status, k.contract_status, COUNT(*)
        FROM kol_records k
        WHERE 1=1 {bf}
        GROUP BY k.deliverable_status, k.contract_status
    """), params).fetchall()

    counts = {"invited": 0, "collaborated": 0, "posted": 0, "total": 0}
    for r in summary_rows:
        ds = (r[0] or "").lower(); cs = (r[1] or "").lower(); n = int(r[2])
        counts["total"] += n
        if "post" in ds: counts["posted"] += n
        if "collab" in cs or "signed" in cs: counts["collaborated"] += n
        if "invit" in cs or cs in ("contacted", "outreach"): counts["invited"] += n

    expiring_rows = db.execute(text(f"""
        SELECT k.kol_name, k.usage_rights_expiry_date, k.paid_ads_channel,
               k.kol_nationality, k.branch_id, b.name AS branch_name
        FROM kol_records k
        LEFT JOIN branches b ON k.branch_id = b.id
        WHERE k.usage_rights_expiry_date IS NOT NULL
          AND k.usage_rights_expiry_date >= CURRENT_DATE
          AND k.usage_rights_expiry_date <= CURRENT_DATE + INTERVAL '30 days'
          {bf}
        ORDER BY k.usage_rights_expiry_date ASC
        LIMIT 20
    """), params).fetchall()

    expiring = [
        {
            "kol_name": r[0],
            "expiry_date": r[1].isoformat() if r[1] else None,
            "days_left": (r[1] - date.today()).days if r[1] else None,
            "paid_ads_channel": r[2],
            "nationality": r[3],
            "branch_name": r[5],
        }
        for r in expiring_rows
    ]
    return {"counts": counts, "rights_expiring_soon": expiring}


def tool_get_country_profile(db: Session, inp: dict, default_branch: Optional[str]) -> dict:
    """Lead time, LOS, pax distribution, room type split per source country.
    Used by chat to answer 'who books from X / what target / what room' questions.
    Excludes cancellations and non-paying sources (KOL, Blogger, House Use,
    Special Case, Work Exchange, Maintenance) — matches metrics_engine
    EXCLUDED_SOURCES_REVENUE so figures reflect real paying guests."""
    branch_id = _resolve_branch_id(inp.get("branch_id"), default_branch)
    days = int(inp.get("days") or 90)
    limit = int(inp.get("limit") or 10)
    country_name = inp.get("country") or None

    bf, params = _b_filter_clause(branch_id, "r")
    params.update({"d": days, "limit": limit})

    country_clause = ""
    if country_name:
        country_clause = "AND lower(r.guest_country) = lower(:country)"
        params["country"] = country_name

    excluded_sources = "('blogger','kol','house use','houseuse','special case','work exchange','maintain','maintenance')"

    rows = db.execute(text(f"""
        WITH base AS (
            SELECT r.guest_country, r.guest_country_code, r.adults, r.nights,
                   r.room_type_category, r.grand_total_vnd,
                   CASE WHEN r.reservation_date IS NOT NULL AND r.check_in_date IS NOT NULL
                        THEN (r.check_in_date - r.reservation_date) END AS lead_days
            FROM reservations r
            WHERE r.guest_country IS NOT NULL AND r.guest_country != '' AND r.guest_country != '0'
              AND length(r.guest_country) > 1
              AND r.status NOT IN ('canceled','cancelled','no_show','no-show','cancelled_by_guest')
              AND lower(COALESCE(r.source, '')) NOT IN {excluded_sources}
              AND r.check_in_date >= CURRENT_DATE - (:d || ' days')::interval
              {bf}
              {country_clause}
        )
        SELECT guest_country, guest_country_code,
               COUNT(*) AS bookings,
               COALESCE(SUM(grand_total_vnd), 0) AS revenue_vnd,
               AVG(lead_days) FILTER (WHERE lead_days IS NOT NULL AND lead_days >= 0) AS lead_avg,
               AVG(nights) AS los_avg,
               COUNT(*) FILTER (WHERE adults = 1) AS p_solo,
               COUNT(*) FILTER (WHERE adults = 2) AS p_couple,
               COUNT(*) FILTER (WHERE adults BETWEEN 3 AND 4) AS p_group,
               COUNT(*) FILTER (WHERE adults >= 5) AS p_family,
               COUNT(*) FILTER (WHERE adults IS NULL OR adults = 0) AS p_unknown,
               COUNT(*) FILTER (WHERE room_type_category = 'Dorm') AS rt_dorm,
               COUNT(*) FILTER (WHERE room_type_category = 'Room') AS rt_room,
               COUNT(*) FILTER (WHERE room_type_category IS NULL OR room_type_category = '') AS rt_unknown,
               COUNT(*) FILTER (WHERE lead_days BETWEEN 0 AND 7) AS lt_0_7,
               COUNT(*) FILTER (WHERE lead_days BETWEEN 8 AND 30) AS lt_8_30,
               COUNT(*) FILTER (WHERE lead_days BETWEEN 31 AND 60) AS lt_31_60,
               COUNT(*) FILTER (WHERE lead_days > 60) AS lt_60_plus,
               COUNT(*) FILTER (WHERE lead_days IS NULL OR lead_days < 0) AS lt_unknown
        FROM base
        GROUP BY guest_country, guest_country_code
        ORDER BY bookings DESC
        LIMIT :limit
    """), params).fetchall()

    def pct(num: int, den: int) -> float:
        return round(num / den * 100, 2) if den else 0.0

    out: list[dict] = []
    for r in rows:
        total = int(r[2]) or 1
        out.append({
            "country": r[0],
            "country_code": r[1],
            "bookings": int(r[2]),
            "revenue_vnd": float(r[3] or 0),
            "lead_time_avg_days": round(float(r[4]), 1) if r[4] is not None else None,
            "los_avg_nights": round(float(r[5]), 2) if r[5] is not None else None,
            "pax_distribution_pct": {
                "solo_1": pct(int(r[6]), total),
                "couple_2": pct(int(r[7]), total),
                "friends_3_4": pct(int(r[8]), total),
                "family_5_plus": pct(int(r[9]), total),
                "unknown": pct(int(r[10]), total),
            },
            "room_type_split_pct": {
                "Dorm": pct(int(r[11]), total),
                "Room": pct(int(r[12]), total),
                "unknown": pct(int(r[13]), total),
            },
            "lead_time_distribution_pct": {
                "0_7_days": pct(int(r[14]), total),
                "8_30_days": pct(int(r[15]), total),
                "31_60_days": pct(int(r[16]), total),
                "60_plus_days": pct(int(r[17]), total),
                "unknown": pct(int(r[18]), total),
            },
        })

    if country_name and len(out) == 1:
        rt_rows = db.execute(text(f"""
            SELECT r.room_type, COUNT(*) AS cnt
            FROM reservations r
            WHERE r.guest_country IS NOT NULL
              AND lower(r.guest_country) = lower(:country)
              AND r.room_type IS NOT NULL AND r.room_type != ''
              AND r.status NOT IN ('canceled','cancelled','no_show','no-show','cancelled_by_guest')
              AND lower(COALESCE(r.source, '')) NOT IN {excluded_sources}
              AND r.check_in_date >= CURRENT_DATE - (:d || ' days')::interval
              {bf}
            GROUP BY r.room_type
            ORDER BY cnt DESC
            LIMIT 5
        """), params).fetchall()
        out[0]["top_room_types"] = [
            {"room_type": rr[0], "bookings": int(rr[1])} for rr in rt_rows
        ]

    return {
        "window_days": days,
        "country_filter": country_name,
        "exclusions": "cancelled/no-show + KOL/Blogger/House Use/Special Case/Work Exchange/Maintenance",
        "countries": out,
    }


def tool_get_marketing_activity(db: Session, inp: dict, default_branch: Optional[str]) -> dict:
    """Bookings + revenue grouped by source category (CRM, KOL, OTA, Direct)
    using reservation_date (when booked), per feedback memory."""
    branch_id = _resolve_branch_id(inp.get("branch_id"), default_branch)
    today = date.today()
    d_to = _parse_date(inp.get("date_to"), today)
    d_from = _parse_date(inp.get("date_from"), d_to - timedelta(days=29))
    bf, params = _b_filter_clause(branch_id, "r")
    params.update({"df": d_from, "dt": d_to})

    rows = db.execute(text(f"""
        SELECT
            COALESCE(r.source_category, 'Unknown') AS cat,
            COUNT(*) AS bookings,
            COALESCE(SUM(r.grand_total_vnd), 0) AS revenue_vnd,
            COALESCE(SUM(r.grand_total_native), 0) AS revenue_native
        FROM reservations r
        WHERE r.reservation_date >= :df AND r.reservation_date <= :dt
          AND r.status NOT IN ('canceled','cancelled','no_show','no-show','cancelled_by_guest')
          {bf}
        GROUP BY r.source_category
        ORDER BY bookings DESC
    """), params).fetchall()

    kol_rows = db.execute(text(f"""
        SELECT COUNT(*) AS bookings,
               COALESCE(SUM(r.grand_total_vnd), 0) AS revenue_vnd
        FROM reservations r
        WHERE r.reservation_date >= :df AND r.reservation_date <= :dt
          AND r.room_type ILIKE '%KOL_%'
          AND r.status NOT IN ('canceled','cancelled','no_show','no-show','cancelled_by_guest')
          {bf}
    """), params).fetchall()

    return {
        "date_from": d_from.isoformat(),
        "date_to": d_to.isoformat(),
        "filter_basis": "reservation_date (when booked)",
        "by_source_category": [
            {"category": r[0], "bookings": int(r[1]),
             "revenue_vnd": float(r[2]), "revenue_native": float(r[3])}
            for r in rows
        ],
        "kol_organic": {
            "bookings": int(kol_rows[0][0]) if kol_rows else 0,
            "revenue_vnd": float(kol_rows[0][1]) if kol_rows else 0.0,
        } if kol_rows else None,
    }


def tool_get_cancellation_leadtime(db: Session, inp: dict, default_branch: Optional[str]) -> dict:
    """How many days before check-in the CANCELLED / no-show cohort cancelled.

    Uses cancellation_date when populated; otherwise falls back to raw_data's
    'dateModified' (the last-modified timestamp — for a cancelled booking the
    final modification is effectively the cancellation). This is an APPROXIMATE
    cancel date: Cloudbeds does not expose an exact cancellationDate in the data
    HiD ingests. Positive lead_days = cancelled in advance; <= 0 = cancelled on
    or after the check-in date (includes no-shows). Filtered by check_in_date
    window; defaults to the last 90 days."""
    branch_id = _resolve_branch_id(inp.get("branch_id"), default_branch)
    today = date.today()
    d_to = _parse_date(inp.get("date_to"), today)
    d_from = _parse_date(inp.get("date_from"), d_to - timedelta(days=90))
    bf, params = _b_filter_clause(branch_id, "r")
    params.update({"df": d_from, "dt": d_to})

    row = db.execute(text(f"""
        WITH base AS (
            SELECT lower(r.status) AS status,
                   CASE
                       WHEN r.cancellation_date IS NOT NULL AND r.check_in_date IS NOT NULL
                            THEN (r.check_in_date - r.cancellation_date)
                       WHEN r.raw_data->>'dateModified' ~ '^\\d{{4}}-\\d{{2}}-\\d{{2}}'
                            AND left(r.raw_data->>'dateModified', 10) <> '0000-00-00'
                            AND r.check_in_date IS NOT NULL
                            THEN (r.check_in_date - left(r.raw_data->>'dateModified', 10)::date)
                   END AS lead_days
            FROM reservations r
            WHERE lower(r.status) IN
                  ('cancelled','canceled','no_show','noshow','no show','no-show','cancelled_by_guest')
              AND r.check_in_date >= :df AND r.check_in_date <= :dt
              {bf}
        )
        SELECT COUNT(*) AS total,
               COUNT(*) FILTER (WHERE status IN ('no_show','noshow','no show','no-show')) AS no_show,
               AVG(lead_days) AS lead_avg,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY lead_days) AS lead_median,
               COUNT(*) FILTER (WHERE lead_days <= 0) AS lt_after_or_same,
               COUNT(*) FILTER (WHERE lead_days BETWEEN 1 AND 7) AS lt_1_7,
               COUNT(*) FILTER (WHERE lead_days BETWEEN 8 AND 30) AS lt_8_30,
               COUNT(*) FILTER (WHERE lead_days BETWEEN 31 AND 60) AS lt_31_60,
               COUNT(*) FILTER (WHERE lead_days > 60) AS lt_60_plus,
               COUNT(*) FILTER (WHERE lead_days IS NULL) AS lt_unknown
        FROM base
    """), params).fetchone()

    total = int(row[0] or 0)

    def bucket(cnt) -> dict:
        c = int(cnt or 0)
        return {"count": c, "pct": round(c / total * 100, 2) if total else 0.0}

    return {
        "basis": "cancellation_lead_time_approx",
        "note": (
            "Days between cancellation and check-in (check_in_date − cancel date). "
            "Cancel date is approximated from the reservation's last-modified "
            "timestamp — Cloudbeds exposes no exact cancellationDate in HiD's data, "
            "and for a cancelled booking the last modification is effectively the "
            "cancellation. Positive = cancelled in advance; 'after_or_same_day' = "
            "cancelled on/after check-in (includes no-shows)."
        ),
        "branch_id": branch_id or "all",
        "date_from": d_from.isoformat(),
        "date_to": d_to.isoformat(),
        "total_cancellations": total,
        "no_shows": int(row[1] or 0),
        "lead_time_avg_days": round(float(row[2]), 1) if row[2] is not None else None,
        "lead_time_median_days": round(float(row[3]), 1) if row[3] is not None else None,
        "lead_time_distribution": {
            "after_or_same_day": bucket(row[4]),
            "1_7_days": bucket(row[5]),
            "8_30_days": bucket(row[6]),
            "31_60_days": bucket(row[7]),
            "60_plus_days": bucket(row[8]),
            "unknown": bucket(row[9]),
        },
    }


# ── Dispatch ────────────────────────────────────────────────────────────────

TOOL_HANDLERS = {
    "get_branches": tool_get_branches,
    "get_performance": tool_get_performance,
    "get_kpi_status": tool_get_kpi_status,
    "get_ota_mix": tool_get_ota_mix,
    "get_country_breakdown": tool_get_country_breakdown,
    "get_source_by_country": tool_get_source_by_country,
    "get_alerts": tool_get_alerts,
    "get_upcoming_holidays": tool_get_upcoming_holidays,
    "get_ads_performance": tool_get_ads_performance,
    "get_kol_performance": tool_get_kol_performance,
    "get_country_profile": tool_get_country_profile,
    "get_marketing_activity": tool_get_marketing_activity,
    "get_cancellation_leadtime": tool_get_cancellation_leadtime,
}


def execute_tool(name: str, tool_input: dict, db: Session, default_branch_id: Optional[str]) -> dict:
    handler = TOOL_HANDLERS.get(name)
    if not handler:
        return {"error": f"Unknown tool: {name}"}
    try:
        return handler(db, tool_input or {}, default_branch_id)
    except Exception as e:
        logger.exception("Tool %s failed: %s", name, e)
        return {"error": f"Tool {name} failed: {str(e)[:200]}"}
