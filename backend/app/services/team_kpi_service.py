"""Team KPI Service — aggregate actuals from upstream APIs for the Team KPI page.

Phase 1:
  KOL (Mel)        — marketing_activity_cache (channel=kol) for revenue;
                     KOL Engine targets API for collaborated/posted counts
  Paid Ads (Mason) — marketing_activity_cache (channel=paid_ads) for revenue;
                     AdsPerformance table for ROAS
  Designer (Nora)  — Lark Base API
  CRM (Kin)        — Cloudbeds Reservation table (reservation_date, CRM filter)

Phase 2:
  PM (Nuha) — branch_kpi_rate from KPITarget+DailyMetrics (Revenue KPI hit%);
              budget_utilisation from MarketingBudget actual/allocated
"""
from __future__ import annotations

import calendar
import logging
import time
from datetime import date
from typing import Optional
from uuid import UUID

from sqlalchemy.orm import Session

from app.config import settings
from app.models.team_kpi import TeamKPITarget
from app.services.currency import get_cached_rate
from app.services.kol_engine import HOTEL_TO_BRANCH_KEY, fetch_kol_reservation_ids
from app.services.upstream_actuals import BRANCH_TO_KOL_HOTEL_ID

log = logging.getLogger(__name__)

# ── KPI metadata ────────────────────────────────────────────────────────────

KPI_DEFS: dict[str, list[dict]] = {
    "kol": [
        {"key": "kol_invited",     "label": "KOLs Invited",          "unit": "KOLs",   "org_wide": True,  "higher_is_better": True},
        {"key": "kol_revenue",     "label": "Revenue via KOL",        "unit": "mil VND","org_wide": False, "higher_is_better": True,  "is_revenue": True},
        {"key": "kol_collaborated","label": "KOLs Collaborated",      "unit": "KOLs",   "org_wide": False, "higher_is_better": True},
        {"key": "kol_posted",      "label": "KOLs Posted",            "unit": "posts",  "org_wide": False, "higher_is_better": True},
        {"key": "kol_ads_collab",  "label": "KOL Ads Collab",         "unit": "KOLs",   "org_wide": False, "higher_is_better": True},
    ],
    "paid_ads": [
        {"key": "ads_material",    "label": "Variation Ads Material", "unit": "count",  "org_wide": False, "higher_is_better": True},
        {"key": "roas",            "label": "ROAS",                   "unit": "×",      "org_wide": False, "higher_is_better": True,  "decimals": 2},
        {"key": "ads_revenue",     "label": "Revenue via Paid Ads",   "unit": "mil VND","org_wide": False, "higher_is_better": True,  "is_revenue": True, "computed_target": "spend_x_roas"},
    ],
    "designer": [
        {"key": "design_assets",   "label": "Design Assets Completed","unit": "designs","org_wide": False, "higher_is_better": True},
        {"key": "videos_delivered","label": "Videos Delivered",       "unit": "videos", "org_wide": False, "higher_is_better": True},
        {"key": "design_ideas",    "label": "Design Ideas",           "unit": "ideas",  "org_wide": False, "higher_is_better": True,  "auto": False},
    ],
    "crm": [
        {"key": "data_fill_rate",  "label": "Data Fill-Rate",         "unit": "%",      "org_wide": False, "higher_is_better": True,  "decimals": 1, "is_pct": True, "auto": False},
        {"key": "crm_campaigns",   "label": "CRM Campaigns Sent",     "unit": "campaigns","org_wide": False,"higher_is_better": True,  "auto": False},
        {"key": "crm_revenue",     "label": "Revenue from CRM",       "unit": "mil VND","org_wide": False, "higher_is_better": True,  "is_revenue": True},
    ],
    "pm": [
        {"key": "team_activities",      "label": "Team Activities",         "unit": "activities","org_wide": True, "higher_is_better": True, "auto": False},
        {"key": "task_completion_rate", "label": "Task Completion Rate",    "unit": "%",   "org_wide": True, "higher_is_better": True, "decimals": 1, "is_pct": True},
        {"key": "branch_kpi_rate",      "label": "Branch KPI Achievement",  "unit": "%",   "org_wide": False,"higher_is_better": True, "decimals": 1, "is_pct": True},
        {"key": "budget_utilisation",   "label": "Budget Utilisation",      "unit": "%",   "org_wide": False,"higher_is_better": False, "decimals": 1, "is_pct": True},
    ],
}

ROLE_META = {
    "kol":       {"label": "KOL",       "person": "Mel",   "emoji": "🤝", "auto_actuals": True},
    "paid_ads":  {"label": "Paid Ads",  "person": "Mason", "emoji": "📢", "auto_actuals": True},
    "designer":  {"label": "Designer",  "person": "Nora",  "emoji": "🎨", "auto_actuals": True},
    "crm":       {"label": "CRM",       "person": "Kin",   "emoji": "📊", "auto_actuals": True},
    "pm":        {"label": "PM",        "person": "Nuha",  "emoji": "🗂️", "auto_actuals": True},
}

# Branch short-key → branch UUID (stable seed data)
BRANCH_KEY_TO_UUID: dict[str, str] = {v: k for k, v in {
    "11111111-1111-1111-1111-111111111101": "taipei",
    "11111111-1111-1111-1111-111111111102": "saigon",
    "11111111-1111-1111-1111-111111111103": "1948",
    "11111111-1111-1111-1111-111111111104": "oani",
    "11111111-1111-1111-1111-111111111105": "osaka",
}.items()}

BRANCH_UUID_TO_KEY: dict[str, str] = {v: k for k, v in BRANCH_KEY_TO_UUID.items()}

# Branches that use a non-VND currency; defaults to VND for all others
BRANCH_CURRENCY: dict[str, str] = {
    "osaka":  "JPY",
    "taipei": "TWD",
    "1948":   "TWD",
    "oani":   "TWD",
}

# How to display revenue in each currency
_CURRENCY_DISPLAY: dict[str, dict] = {
    "VND": {"unit": "mil VND", "scale": 1_000_000, "decimals": 1},
    "JPY": {"unit": "JPY",     "scale": 1,          "decimals": 0},
    "TWD": {"unit": "TWD",     "scale": 1,          "decimals": 0},
}

# ── KOL actuals ─────────────────────────────────────────────────────────────

_kol_actuals_cache: dict[tuple, tuple[float, dict]] = {}
_KOL_ACTUALS_TTL = 600  # 10 min


def get_kol_actuals_yearly_db(
    db: Session, year: int
) -> dict[int, dict[str, dict]]:
    """Return {month: {branch_key|'all': {kpi_key: value}}} for all 12 months.

    Revenue (kol_revenue) comes from marketing_activity_cache (channel='kol'),
    same source as the Marketing Activity page — avoids the live KOL Engine API.
    Counts (collaborated, posted, ads_collab) come from the KOL Engine targets API;
    failure is non-fatal (values default to 0).
    kol_invited comes from the targets API totals; defaults to 0 on failure.
    """
    cache_key = ("kol_db", year)
    cached = _kol_actuals_cache.get(cache_key)
    if cached and (time.time() - cached[0]) < _KOL_ACTUALS_TTL:
        return cached[1]

    from app.models.marketing_activity_cache import MarketingActivityCache
    from app.models.branch import Branch

    # Step 1: revenue from marketing_activity_cache per branch/month
    rows = (
        db.query(MarketingActivityCache, Branch.name)
        .join(Branch, MarketingActivityCache.branch_id == Branch.id)
        .filter(
            MarketingActivityCache.year == year,
            MarketingActivityCache.channel == "kol",
        )
        .all()
    )

    out: dict[int, dict[str, dict]] = {}
    cached_months: set[int] = set()
    for mac, branch_name in rows:
        name_lower = (branch_name or "").lower()
        branch_key = None
        for k in ("saigon", "taipei", "1948", "oani", "osaka"):
            if k in name_lower:
                branch_key = k
                break
        if not branch_key:
            continue
        month = mac.month
        cached_months.add(month)
        out.setdefault(month, {})[branch_key] = {
            "kol_revenue":      float(mac.revenue_vnd or 0),
            "kol_collaborated": 0.0,
            "kol_posted":       0.0,
            "kol_ads_collab":   0.0,
        }

    # Fallback: for months not in cache, query via KOL Engine reservation IDs
    today = date.today()
    cur_month = today.month if today.year == year else (12 if today.year > year else 0)
    try:
        from sqlalchemy import func as _func
        from app.models.reservation import Reservation
        from app.models.branch import Branch as _Branch
        BRANCH_KEYS_ALL = ("saigon", "taipei", "1948", "oani", "osaka")
        for m in range(1, cur_month + 1):
            if m in cached_months:
                continue
            res_ids = fetch_kol_reservation_ids(
                base_url=settings.KOL_ENGINE_URL,
                org_slug=settings.KOL_TARGETS_ORG_SLUG,
                api_key=settings.KOL_REVENUE_API_SECRET,
                year=year,
                month=m,
            )
            if res_ids is None:
                # New endpoint unavailable — fall back to room_type filter
                d_from = date(year, m, 1)
                d_to = date(year, m, calendar.monthrange(year, m)[1])
                cb_rows = (
                    db.query(_Branch.name, _func.coalesce(_func.sum(Reservation.grand_total_vnd), 0))
                    .join(Reservation, Reservation.branch_id == _Branch.id)
                    .filter(
                        Reservation.room_type.ilike("%KOL_%"),
                        Reservation.reservation_date >= d_from,
                        Reservation.reservation_date <= d_to,
                        Reservation.status.notin_(["cancelled", "no_show"]),
                    )
                    .group_by(_Branch.name)
                    .all()
                )
            elif not res_ids:
                continue
            else:
                cb_rows = (
                    db.query(_Branch.name, _func.coalesce(_func.sum(Reservation.grand_total_vnd), 0))
                    .join(Reservation, Reservation.branch_id == _Branch.id)
                    .filter(
                        Reservation.cloudbeds_reservation_id.in_(res_ids),
                        Reservation.status.notin_(["cancelled", "no_show"]),
                    )
                    .group_by(_Branch.name)
                    .all()
                )
            if not cb_rows:
                continue
            for bname, rev in cb_rows:
                bk = None
                for k in BRANCH_KEYS_ALL:
                    if k in (bname or "").lower():
                        bk = k
                        break
                if not bk:
                    continue
                out.setdefault(m, {})[bk] = {
                    "kol_revenue":      float(rev),
                    "kol_collaborated": 0.0,
                    "kol_posted":       0.0,
                    "kol_ads_collab":   0.0,
                }
    except Exception as exc:
        log.warning("KOL Cloudbeds fallback failed: %s", exc)

    # Ensure every month up to today has an 'all' key (for kol_invited)
    today = date.today()
    cur_month = today.month if today.year == year else (12 if today.year > year else 0)
    for m in range(1, min(cur_month + 1, 13)):
        out.setdefault(m, {}).setdefault("all", {"kol_invited": 0.0, "kol_revenue": 0.0})

    # Step 2: merge counts + kol_invited from targets API — never fatal
    try:
        from app.services.kol_engine import fetch_kol_targets
        for m in range(1, min(cur_month + 1, 13)):
            tgt_data = fetch_kol_targets(
                base_url=settings.KOL_ENGINE_URL,
                org_slug=settings.KOL_TARGETS_ORG_SLUG,
                api_key=settings.KOL_PUBLIC_API_KEY,
                year=year,
                month=m,
            )
            if not tgt_data:
                continue
            # org-wide invited count
            inv = (tgt_data.get("totals") or {}).get("invited_proactive")
            inv_val = float(inv.get("actual") if isinstance(inv, dict) else (inv or 0))
            out[m].setdefault("all", {})["kol_invited"] = inv_val

            for br in tgt_data.get("branches") or []:
                hotel_id = br.get("hotel_id") or br.get("id") or ""
                branch_key = HOTEL_TO_BRANCH_KEY.get(hotel_id)
                if not branch_key:
                    name = (br.get("hotel_name") or "").lower()
                    for k in ("saigon", "taipei", "1948", "oani", "osaka"):
                        if k in name:
                            branch_key = k
                            break
                if not branch_key or branch_key not in out.get(m, {}):
                    continue
                def _v(field, _br=br):
                    v = _br.get(field)
                    return float(v.get("actual") if isinstance(v, dict) else (v or 0))
                out[m][branch_key]["kol_collaborated"] = _v("collaborated")
                out[m][branch_key]["kol_posted"]       = _v("posted")
                out[m][branch_key]["kol_ads_collab"]   = _v("ads_allowed")
    except Exception as exc:
        log.warning("kol targets API merge failed (counts will be 0): %s", exc)

    _kol_actuals_cache[cache_key] = (time.time(), out)
    return out


# ── Paid Ads actuals ─────────────────────────────────────────────────────────

_ads_actuals_cache: dict[tuple, tuple[float, dict]] = {}
_ADS_ACTUALS_TTL = 600


def get_paid_ads_actuals_yearly(
    db: Session, year: int
) -> dict[int, dict[str, dict]]:
    """Return {month: {branch_key: {ads_revenue, roas}}} for the year.

    Reads from marketing_activity_cache (same source as Marketing Activity page).
    ads_revenue is raw VND — build_monthly_summary converts to native currency.
    roas is sourced from AdsPerformance aggregation via the cache's companion
    query; falls back to 0 when unavailable.
    """
    cache_key = ("paid_ads", year)
    cached = _ads_actuals_cache.get(cache_key)
    if cached and (time.time() - cached[0]) < _ADS_ACTUALS_TTL:
        return cached[1]

    from app.models.marketing_activity_cache import MarketingActivityCache
    from app.models.branch import Branch

    rows = (
        db.query(MarketingActivityCache, Branch.name)
        .join(Branch, MarketingActivityCache.branch_id == Branch.id)
        .filter(
            MarketingActivityCache.year == year,
            MarketingActivityCache.channel == "paid_ads",
        )
        .all()
    )

    # Also fetch ROAS from AdsPerformance for each branch/month
    try:
        from app.models.ads import AdsPerformance
        from sqlalchemy import func as sqlfunc
        roas_rows = (
            db.query(
                AdsPerformance.branch_id,
                sqlfunc.extract("month", AdsPerformance.date_from).label("month"),
                sqlfunc.sum(AdsPerformance.cost_vnd).label("spend"),
                sqlfunc.sum(AdsPerformance.revenue_vnd).label("revenue"),
            )
            .filter(sqlfunc.extract("year", AdsPerformance.date_from) == year)
            .group_by(AdsPerformance.branch_id, sqlfunc.extract("month", AdsPerformance.date_from))
            .all()
        )
        roas_map:  dict[tuple, float] = {}
        spend_map: dict[tuple, float] = {}
        for r in roas_rows:
            bid = str(r.branch_id)
            m = int(r.month)
            spend = float(r.spend or 0)
            rev = float(r.revenue or 0)
            roas_map[(bid, m)]  = round(rev / spend, 2) if spend > 0 else 0.0
            spend_map[(bid, m)] = spend
    except Exception as exc:
        log.warning("paid_ads roas lookup failed: %s", exc)
        roas_map = {}
        spend_map = {}

    out: dict[int, dict[str, dict]] = {}
    for mac, branch_name in rows:
        # Map branch name → branch_key
        name_lower = (branch_name or "").lower()
        branch_key = None
        for k in ("saigon", "taipei", "1948", "oani", "osaka"):
            if k in name_lower:
                branch_key = k
                break
        if not branch_key:
            continue
        month = mac.month
        bid = str(mac.branch_id)
        revenue_vnd = float(mac.revenue_vnd or 0)
        roas = roas_map.get((bid, month), 0.0)
        spend = spend_map.get((bid, month), 0.0)
        out.setdefault(month, {})[branch_key] = {
            "ads_revenue": revenue_vnd,
            "ads_spend":   spend,
            "roas":        roas,
            "ads_material": None,  # filled from Lark in build_monthly_summary
        }

    _ads_actuals_cache[cache_key] = (time.time(), out)
    return out


# ── CRM actuals ───────────────────────────────────────────────────────────────

_crm_actuals_cache: dict[tuple, tuple[float, dict]] = {}
_CRM_ACTUALS_TTL = 600  # 10 min


def get_crm_actuals_yearly(
    db: Session, year: int
) -> dict[int, dict[str, dict]]:
    """Return {month: {branch_key: {crm_revenue}}} for the year.

    Reads from Cloudbeds Reservation table filtered by reservation_date (booking date)
    and CRM rate-plan filter — same logic as Marketing Activity CRM section.
    Revenue is raw grand_total_vnd; build_monthly_summary converts to native currency.
    """
    cache_key = ("crm", year)
    cached = _crm_actuals_cache.get(cache_key)
    if cached and (time.time() - cached[0]) < _CRM_ACTUALS_TTL:
        return cached[1]

    from datetime import date as _date
    from sqlalchemy import func as sqlfunc, extract
    from app.models.reservation import Reservation
    from app.models.branch import Branch
    from app.services.crm_filters import crm_reservation_filter

    _EXCLUDED_STATUSES = {"cancelled", "canceled", "no_show", "noshow", "no show", "no-show", "cancelled_by_guest"}
    _EXCLUDED_SOURCES  = {"blogger", "house use", "houseuse", "special case", "work exchange"}

    rows = (
        db.query(
            Branch.name,
            extract("month", Reservation.reservation_date).label("month"),
            sqlfunc.coalesce(sqlfunc.sum(Reservation.grand_total_vnd), 0).label("revenue"),
        )
        .join(Branch, Reservation.branch_id == Branch.id)
        .filter(
            crm_reservation_filter(),
            extract("year", Reservation.reservation_date) == year,
            ~sqlfunc.lower(sqlfunc.coalesce(Reservation.status, "")).in_(list(_EXCLUDED_STATUSES)),
            ~sqlfunc.lower(sqlfunc.coalesce(Reservation.source, "")).in_(list(_EXCLUDED_SOURCES)),
        )
        .group_by(Branch.name, extract("month", Reservation.reservation_date))
        .all()
    )

    out: dict[int, dict[str, dict]] = {}
    for branch_name, month, revenue in rows:
        name_lower = (branch_name or "").lower()
        branch_key = None
        for k in ("saigon", "taipei", "1948", "oani", "osaka"):
            if k in name_lower:
                branch_key = k
                break
        if not branch_key:
            continue
        out.setdefault(int(month), {})[branch_key] = {
            "crm_revenue": float(revenue),
        }

    _crm_actuals_cache[cache_key] = (time.time(), out)
    return out


# ── PM actuals ───────────────────────────────────────────────────────────────

_pm_actuals_cache: dict[tuple, tuple[float, dict]] = {}
_PM_ACTUALS_TTL = 600


def get_pm_actuals_yearly(db: Session, year: int) -> dict[int, dict[str, dict]]:
    """Return {month: {branch_key: {branch_kpi_rate, budget_utilisation}}}

    branch_kpi_rate:   Revenue KPI hit% — same formula as Revenue KPI page:
                       actual = (override or DailyMetrics.revenue_native)
                                * (1 - deduction_pct) + other_revenue_native
                       hit% = actual / target_revenue_native * 100
    budget_utilisation: sum(actual_spend) / sum(allocated) * 100 across all
                        budget channels (paid_ads + kol + crm) per branch/month.
    """
    cache_key = ("pm", year)
    cached = _pm_actuals_cache.get(cache_key)
    if cached and (time.time() - cached[0]) < _PM_ACTUALS_TTL:
        return cached[1]

    from app.models.kpi import KPITarget
    from app.models.branch import Branch
    from app.models.daily_metrics import DailyMetrics
    from app.models.marketing_budget import MarketingBudget
    from sqlalchemy import func as sqlfunc, extract

    # 1. Revenue KPI targets + overrides per branch/month
    kpi_rows = (
        db.query(KPITarget, Branch.name)
        .join(Branch, KPITarget.branch_id == Branch.id)
        .filter(KPITarget.year == year)
        .all()
    )
    target_meta: dict[tuple, dict] = {}
    for kpi, branch_name in kpi_rows:
        name_lower = (branch_name or "").lower()
        branch_key = None
        for k in ("saigon", "taipei", "1948", "oani", "osaka"):
            if k in name_lower:
                branch_key = k
                break
        if not branch_key:
            continue
        deduct = float(kpi.deduction_pct or 0) / 100
        target_meta[(branch_key, kpi.month)] = {
            "target":      float(kpi.target_revenue_native or 0),
            "override":    float(kpi.actual_revenue_override) if kpi.actual_revenue_override is not None else None,
            "deduct_mult": 1.0 - deduct,
            "other_rev":   float(kpi.other_revenue_native or 0),
            "branch_id":   str(kpi.branch_id),
        }

    # 2. DailyMetrics actual revenue per branch/month (Cloudbeds source)
    daily_rows = (
        db.query(
            DailyMetrics.branch_id,
            extract("month", DailyMetrics.date).label("month"),
            sqlfunc.coalesce(sqlfunc.sum(DailyMetrics.revenue_native), 0).label("revenue"),
        )
        .filter(extract("year", DailyMetrics.date) == year)
        .group_by(DailyMetrics.branch_id, extract("month", DailyMetrics.date))
        .all()
    )
    daily_map: dict[tuple, float] = {
        (str(r.branch_id), int(r.month)): float(r.revenue) for r in daily_rows
    }

    # 3. MarketingBudget actual spend per branch/month (all channels summed)
    budget_rows = (
        db.query(
            MarketingBudget.branch_id,
            MarketingBudget.month,
            sqlfunc.coalesce(sqlfunc.sum(MarketingBudget.allocated_vnd), 0).label("allocated"),
            sqlfunc.coalesce(
                sqlfunc.sum(
                    sqlfunc.coalesce(MarketingBudget.manual_actual_vnd, MarketingBudget.cached_actual_vnd, 0)
                ), 0
            ).label("actual_spend"),
        )
        .filter(MarketingBudget.year == year)
        .group_by(MarketingBudget.branch_id, MarketingBudget.month)
        .all()
    )
    budget_map: dict[tuple, tuple] = {
        (str(r.branch_id), r.month): (float(r.allocated), float(r.actual_spend))
        for r in budget_rows
    }

    out: dict[int, dict[str, dict]] = {}
    for (branch_key, month), meta in target_meta.items():
        bid = meta["branch_id"]
        target = meta["target"]
        cloudbeds = daily_map.get((bid, month), 0.0)
        raw = meta["override"] if meta["override"] is not None else cloudbeds
        actual = raw * meta["deduct_mult"] + meta["other_rev"]
        hit_pct = round(actual / target * 100, 1) if target > 0 else None

        alloc, spent = budget_map.get((bid, month), (0.0, 0.0))
        budget_pct = round(spent / alloc * 100, 1) if alloc > 0 else None

        row: dict = {}
        if hit_pct is not None:
            row["branch_kpi_rate"] = hit_pct
        if budget_pct is not None:
            row["budget_utilisation"] = budget_pct
        if row:
            out.setdefault(month, {})[branch_key] = row

    # Merge task_completion_rate from Lark (org-wide → stored under 'all' key)
    try:
        from app.services.lark_service import get_task_completion_rate_yearly
        tcr = get_task_completion_rate_yearly(year)
        for month, data in tcr.items():
            for bucket, vals in data.items():
                out.setdefault(month, {}).setdefault(bucket, {}).update(vals)
    except Exception as exc:
        log.warning("task_completion_rate from Lark unavailable: %s", exc)

    _pm_actuals_cache[cache_key] = (time.time(), out)
    return out


# ── Core summary builder ──────────────────────────────────────────────────────

def build_monthly_summary(
    db: Session,
    role_key: str,
    year: int,
    branch_id: Optional[str],
) -> dict:
    """Build the full monthly KPI summary for one role × branch × year.

    Returns:
    {
      role, year, branch_id, overall_avg_pct, current_month_pct,
      kpis: [
        {key, label, unit, unit_display, is_pct, decimals, higher_is_better,
         org_wide, auto_actuals,
         monthly: [{month, target, actual, pct, is_future, has_target}]
        }
      ]
    }
    """
    today = date.today()
    cur_month = today.month if today.year == year else (12 if today.year > year else 0)

    defs = KPI_DEFS.get(role_key, [])
    role_m = ROLE_META.get(role_key, {})
    auto = role_m.get("auto_actuals", False)

    # Load targets from DB — flat list for this role+branch+year
    q = db.query(TeamKPITarget).filter(
        TeamKPITarget.role_key == role_key,
        TeamKPITarget.year == year,
    )
    all_branches_view = not branch_id  # "All" tab — aggregate across branches

    if branch_id:
        # per-branch: match exact branch OR org-wide (branch_id IS NULL)
        q = q.filter(
            (TeamKPITarget.branch_id == branch_id) | (TeamKPITarget.branch_id.is_(None))
        )
    # All view: load every branch + org-wide so we can sum per-branch targets

    # For is_pct KPIs, targets may have been entered as fractions (e.g. 0.9 = 90%). Normalize on load.
    _pct_keys = {d["key"] for d in defs if d.get("is_pct")}
    # CRM revenue targets entered in bil VND (e.g. 0.056) instead of mil VND (56). Normalize on load.
    _bil_vnd_keys = {"crm_revenue"} if role_key == "crm" else set()
    # Revenue KPI keys — targets stored in native currency units, need FX conversion for All view
    _revenue_keys = {d["key"] for d in defs if d.get("is_revenue")}

    targets_map: dict[tuple, float] = {}        # (kpi_key, month) → target value (summed for All)
    manual_actuals_map: dict[tuple, float] = {} # (kpi_key, month) → manual actual value
    # Per-branch targets for computed-target KPIs that need branch-level data in All view
    per_branch_targets_map: dict[tuple, dict[str, float]] = {}  # (kpi_key, month) → {branch_key: value}
    for row in q.all():
        if row.target_value is None:
            continue
        if row.kpi_key.endswith("__actual"):
            base_key = row.kpi_key[:-len("__actual")]
            manual_actuals_map[(base_key, row.month)] = float(row.target_value)
        else:
            key = (row.kpi_key, row.month)
            raw_val = float(row.target_value)
            # Normalize PM is_pct targets entered as fractions (≤ 2.0 → × 100)
            if row.kpi_key in _pct_keys and raw_val <= 2.0:
                raw_val = round(raw_val * 100, 1)
            # Normalize CRM revenue targets entered in bil VND (< 1) → mil VND (× 1000)
            if row.kpi_key in _bil_vnd_keys and raw_val < 1.0:
                raw_val = round(raw_val * 1000, 3)
            if all_branches_view and row.branch_id is not None:
                # For revenue KPIs: convert branch target to mil VND before summing
                # (branches store targets in native currency: mil VND, TWD, or JPY)
                if row.kpi_key in _revenue_keys:
                    bk_curr = BRANCH_CURRENCY.get(BRANCH_UUID_TO_KEY.get(str(row.branch_id), ""), "VND")
                    if bk_curr == "VND":
                        vnd_val = raw_val * 1_000_000  # mil VND → VND
                    else:
                        rate = get_cached_rate(bk_curr, "VND") or 1.0
                        vnd_val = raw_val * rate  # native × VND/native → VND
                    sum_val = vnd_val / 1_000_000  # back to mil VND for display
                    targets_map[key] = targets_map.get(key, 0.0) + sum_val
                elif row.kpi_key in _pct_keys:
                    # is_pct targets: take the first branch value (all branches share same target %)
                    targets_map.setdefault(key, raw_val)
                else:
                    targets_map[key] = targets_map.get(key, 0.0) + raw_val
                # also keep per-branch copy for computed-target lookups
                bk = BRANCH_UUID_TO_KEY.get(str(row.branch_id))
                if bk:
                    per_branch_targets_map.setdefault(key, {})[bk] = raw_val
            elif all_branches_view and row.branch_id is None:
                # org-wide target (e.g. kol_invited) — use as-is, don't double-add
                targets_map.setdefault(key, raw_val)
            else:
                targets_map[key] = raw_val

    # Fetch actuals
    actuals_yearly: dict[int, dict[str, dict]] = {}
    lark_ads_material: dict[int, dict[str, dict]] = {}
    if auto and role_key == "kol":
        actuals_yearly = get_kol_actuals_yearly_db(db, year)
    elif auto and role_key == "paid_ads":
        actuals_yearly = get_paid_ads_actuals_yearly(db, year)
        try:
            from app.services.lark_service import get_ads_material_yearly
            lark_ads_material = get_ads_material_yearly(year)
        except Exception as exc:
            log.warning("lark ads_material unavailable: %s", exc)
    elif auto and role_key == "designer":
        from app.services.lark_service import get_designer_actuals_yearly
        actuals_yearly = get_designer_actuals_yearly(year)
    elif auto and role_key == "crm":
        actuals_yearly = get_crm_actuals_yearly(db, year)
    elif auto and role_key == "pm":
        actuals_yearly = get_pm_actuals_yearly(db, year)

    # Determine branch key for actuals lookup
    branch_key = None
    if branch_id:
        branch_key = BRANCH_UUID_TO_KEY.get(str(branch_id))

    # All view: always VND (mil VND) since mixing JPY/TWD/VND is meaningless
    # Per-branch: use native currency for that branch
    native_currency = "VND" if all_branches_view else BRANCH_CURRENCY.get(branch_key or "", "VND")
    currency_display = _CURRENCY_DISPLAY[native_currency]
    vnd_to_native_rate = (
        get_cached_rate(native_currency, "VND") if native_currency != "VND" else None
    )

    _ALL_BRANCH_KEYS = ("saigon", "taipei", "1948", "oani", "osaka")

    kpis_out = []
    all_pcts: list[float] = []
    cur_pcts: list[float] = []

    for defn in defs:
        kpi_key = defn["key"]
        org_wide = defn.get("org_wide", False)
        is_pct = defn.get("is_pct", False)
        is_revenue = defn.get("is_revenue", False)
        higher = defn.get("higher_is_better", True)

        # Revenue KPIs use dynamic scale/decimals based on branch currency
        if is_revenue:
            decimals = currency_display["decimals"]
            scale = currency_display["scale"]
        else:
            decimals = defn.get("decimals", 0)
            scale = defn.get("scale")

        # Unit: revenue KPIs show branch-native currency; others use static unit
        kpi_unit = currency_display["unit"] if is_revenue else (defn.get("unit_display") or defn["unit"])

        kpi_auto          = auto and defn.get("auto", True)   # per-KPI override via auto: False
        no_target         = defn.get("no_target", False)       # display-only: suppress target editing
        computed_target_t = defn.get("computed_target")        # computed target type (e.g. "spend_x_roas")

        monthly = []
        for m in range(1, 13):
            is_future = (m > cur_month)

            if no_target:
                target = None
            elif computed_target_t == "spend_x_roas" and not is_future:
                # Revenue target = actual ads spend × ROAS target for that month
                target = None
                if all_branches_view:
                    per_branch_roas = per_branch_targets_map.get(("roas", m), {})
                    total_rev_vnd = 0.0
                    found = False
                    for bk in _ALL_BRANCH_KEYS:
                        spend_raw = actuals_yearly.get(m, {}).get(bk, {}).get("ads_spend")
                        roas_tgt_bk = per_branch_roas.get(bk)
                        if spend_raw is not None and roas_tgt_bk is not None:
                            total_rev_vnd += float(spend_raw) * float(roas_tgt_bk)
                            found = True
                    if found and scale:
                        target = round(total_rev_vnd / scale, decimals or 1)
                else:
                    spend_raw = actuals_yearly.get(m, {}).get(branch_key or "", {}).get("ads_spend")
                    roas_tgt = targets_map.get(("roas", m))
                    if spend_raw is not None and roas_tgt is not None:
                        rev_target_vnd = float(spend_raw) * float(roas_tgt)
                        if is_revenue and native_currency != "VND" and vnd_to_native_rate:
                            target = round(rev_target_vnd / vnd_to_native_rate, decimals)
                        elif scale:
                            target = round(rev_target_vnd / scale, decimals or 1)
            elif computed_target_t:
                target = None  # future month or unknown computed type
            else:
                target = targets_map.get((kpi_key, m))
                # ROAS can't be summed across branches — suppress target in All view
                if all_branches_view and kpi_key == "roas":
                    target = None

            # Actual: from upstream API (auto roles) or manual DB entry (non-auto)
            actual = None
            if kpi_auto and not is_future:
                month_actuals = actuals_yearly.get(m, {})
                if org_wide:
                    raw = month_actuals.get("all", {}).get(kpi_key)
                elif all_branches_view and kpi_key == "roas" and role_key == "paid_ads":
                    # Weighted ROAS = total_revenue_vnd / total_spend_vnd across branches
                    tot_rev   = sum(float(month_actuals.get(bk, {}).get("ads_revenue") or 0) for bk in _ALL_BRANCH_KEYS)
                    tot_spend = sum(float(month_actuals.get(bk, {}).get("ads_spend")   or 0) for bk in _ALL_BRANCH_KEYS)
                    raw = round(tot_rev / tot_spend, 2) if tot_spend > 0 else None
                elif all_branches_view:
                    # is_pct KPIs (e.g. data_fill_rate): average across branches
                    # all other KPIs: sum across branches
                    total = 0.0
                    count = 0
                    for bk in _ALL_BRANCH_KEYS:
                        v = month_actuals.get(bk, {}).get(kpi_key)
                        if v is None and kpi_key == "ads_material" and lark_ads_material:
                            v = lark_ads_material.get(m, {}).get(bk, {}).get("ads_material")
                        if v is not None:
                            total += float(v)
                            count += 1
                    if count == 0:
                        raw = None
                    elif is_pct:
                        raw = round(total / count, 2)
                    else:
                        raw = total
                else:
                    raw = month_actuals.get(branch_key or "", {}).get(kpi_key)
                    # Merge Lark ads_material on top of paid_ads actuals
                    if raw is None and kpi_key == "ads_material" and lark_ads_material:
                        raw = lark_ads_material.get(m, {}).get(branch_key or "", {}).get("ads_material")
                if raw is not None:
                    actual = float(raw)
                    if is_revenue and native_currency != "VND" and vnd_to_native_rate:
                        actual = round(actual / vnd_to_native_rate, decimals)
                    elif scale:
                        actual = round(actual / scale, decimals or 1)
            if actual is None and not is_future:
                actual = manual_actuals_map.get((kpi_key, m))

            pct = None
            if target and target != 0 and actual is not None:
                pct = round(actual / target * 100, 1)
                if not is_future:
                    all_pcts.append(pct)
                    if m == cur_month:
                        cur_pcts.append(pct)

            monthly.append({
                "month": m,
                "target": target,
                "actual": actual,
                "pct": pct,
                "is_future": is_future,
                "has_target": target is not None,
            })

        kpis_out.append({
            "key": kpi_key,
            "label": defn["label"],
            "unit": kpi_unit,
            "is_pct": is_pct,
            "decimals": decimals,
            "higher_is_better": higher,
            "org_wide": org_wide,
            "auto_actuals": kpi_auto,
            "no_target": no_target,
            "computed_target": computed_target_t is not None,
            "monthly": monthly,
        })

    overall_avg_pct = round(sum(all_pcts) / len(all_pcts), 1) if all_pcts else None
    current_month_pct = round(sum(cur_pcts) / len(cur_pcts), 1) if cur_pcts else None

    return {
        "role": role_key,
        "role_label": role_m.get("label"),
        "person": role_m.get("person"),
        "year": year,
        "branch_id": str(branch_id) if branch_id else None,
        "overall_avg_pct": overall_avg_pct,
        "current_month_pct": current_month_pct,
        "current_month": cur_month,
        "auto_actuals": auto,
        "kpis": kpis_out,
    }
