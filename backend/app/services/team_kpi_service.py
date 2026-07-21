"""Team KPI Service — aggregate actuals from upstream APIs for the Team KPI page.

Phase 1:
  KOL (Mel)       — KOL Engine public revenue API (fetch_kol_revenue)
  Paid Ads (Mason) — Ads Platform get_spend_daily + full-year aggregation

Phase 2 (future):
  Designer (Nora)  — Lark Base API
  CRM (Kin)        — Email marketing + CRM fill rate
  PM (Nuha)        — Derived from branch KPI rates
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
from app.services.kol_engine import HOTEL_TO_BRANCH_KEY, fetch_kol_revenue
from app.services.upstream_actuals import BRANCH_TO_KOL_HOTEL_ID

log = logging.getLogger(__name__)

# ── KPI metadata ────────────────────────────────────────────────────────────

KPI_DEFS: dict[str, list[dict]] = {
    "kol": [
        {"key": "kol_invited",     "label": "KOLs Invited",          "unit": "KOLs",   "org_wide": True,  "higher_is_better": True},
        {"key": "kol_revenue",     "label": "Revenue via KOL",        "unit": "VND",    "org_wide": False, "higher_is_better": True,  "scale": 1e6, "unit_display": "mil VND"},
        {"key": "kol_collaborated","label": "KOLs Collaborated",      "unit": "KOLs",   "org_wide": False, "higher_is_better": True},
        {"key": "kol_posted",      "label": "KOLs Posted",            "unit": "posts",  "org_wide": False, "higher_is_better": True},
        {"key": "kol_ads_collab",  "label": "KOL Ads Collab",         "unit": "videos", "org_wide": False, "higher_is_better": True},
    ],
    "paid_ads": [
        {"key": "ads_material",    "label": "Variation Ads Material", "unit": "count",  "org_wide": False, "higher_is_better": True},
        {"key": "roas",            "label": "ROAS",                   "unit": "×",      "org_wide": False, "higher_is_better": True,  "decimals": 2},
        {"key": "ads_revenue",     "label": "Revenue via Paid Ads",   "unit": "mil VND","org_wide": False, "higher_is_better": True,  "decimals": 1},
    ],
    "designer": [
        {"key": "design_assets",   "label": "Design Assets Completed","unit": "designs","org_wide": False, "higher_is_better": True},
        {"key": "videos_delivered","label": "Videos Delivered",       "unit": "videos", "org_wide": False, "higher_is_better": True},
        {"key": "delivery_rate",   "label": "On-Time Delivery Rate",  "unit": "%",      "org_wide": False, "higher_is_better": True,  "decimals": 1, "is_pct": True},
        {"key": "design_ideas",    "label": "Design Ideas",           "unit": "ideas",  "org_wide": False, "higher_is_better": True},
    ],
    "crm": [
        {"key": "data_fill_rate",  "label": "Data Fill-Rate",         "unit": "%",      "org_wide": False, "higher_is_better": True,  "decimals": 1, "is_pct": True},
        {"key": "crm_campaigns",   "label": "CRM Campaigns Sent",     "unit": "campaigns","org_wide": False,"higher_is_better": True},
        {"key": "crm_revenue",     "label": "Revenue from CRM",       "unit": "VND",    "org_wide": False, "higher_is_better": True},
    ],
    "pm": [
        {"key": "team_activities",      "label": "Team Activities",         "unit": "activities","org_wide": True, "higher_is_better": True},
        {"key": "task_completion_rate", "label": "Task Completion Rate",    "unit": "%",   "org_wide": True, "higher_is_better": True, "decimals": 1, "is_pct": True},
        {"key": "branch_kpi_rate",      "label": "Branch KPI Achievement",  "unit": "%",   "org_wide": False,"higher_is_better": True, "decimals": 1, "is_pct": True},
        {"key": "budget_utilisation",   "label": "Budget Utilisation",      "unit": "%",   "org_wide": False,"higher_is_better": False, "decimals": 1, "is_pct": True},
    ],
}

ROLE_META = {
    "kol":       {"label": "KOL",       "person": "Mel",   "emoji": "🤝", "auto_actuals": True},
    "paid_ads":  {"label": "Paid Ads",  "person": "Mason", "emoji": "📢", "auto_actuals": True},
    "designer":  {"label": "Designer",  "person": "Nora",  "emoji": "🎨", "auto_actuals": True},
    "crm":       {"label": "CRM",       "person": "Kin",   "emoji": "📊", "auto_actuals": False},
    "pm":        {"label": "PM",        "person": "Nuha",  "emoji": "🗂️", "auto_actuals": False},
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

# ── KOL actuals ─────────────────────────────────────────────────────────────

_kol_actuals_cache: dict[tuple, tuple[float, dict]] = {}
_KOL_ACTUALS_TTL = 600  # 10 min


def _get_kol_actuals_for_month(year: int, month: int) -> dict:
    """Fetch KOL actuals for a single month from KOL Engine.

    Returns dict keyed by branch_key (+ 'all' for org-wide):
    {
      'all':    {kol_invited, kol_revenue},
      'saigon': {kol_revenue, kol_collaborated, kol_posted, kol_ads_collab},
      ...
    }
    All monetary values in VND.
    """
    cache_key = (year, month)
    cached = _kol_actuals_cache.get(cache_key)
    if cached and (time.time() - cached[0]) < _KOL_ACTUALS_TTL:
        return cached[1]

    data = fetch_kol_revenue(
        base_url=settings.KOL_ENGINE_URL,
        org_slug=settings.KOL_TARGETS_ORG_SLUG,
        api_key=settings.KOL_REVENUE_API_SECRET,
        year=year,
        month=month,
    )
    result: dict[str, dict] = {}
    if not data:
        return result

    totals = data.get("totals") or {}
    result["all"] = {
        "kol_invited": float(totals.get("invited_proactive") or 0),
        "kol_revenue":  float(totals.get("revenue_vnd") or totals.get("revenue") or 0),
    }

    for br in data.get("branches") or []:
        hotel_id = br.get("hotel_id") or br.get("id") or ""
        branch_key = HOTEL_TO_BRANCH_KEY.get(hotel_id)
        if not branch_key:
            # fallback: match by hotel_name substring
            name = (br.get("hotel_name") or "").lower()
            for k in ("saigon", "taipei", "1948", "oani", "osaka"):
                if k in name:
                    branch_key = k
                    break
        if not branch_key:
            continue
        result[branch_key] = {
            "kol_revenue":      float(br.get("revenue_vnd") or 0),
            "kol_collaborated": float(br.get("collaborated") or 0),
            "kol_posted":       float(br.get("posted") or 0),
            "kol_ads_collab":   float(br.get("ads_allowed") or 0),
        }

    _kol_actuals_cache[cache_key] = (time.time(), result)
    return result


def get_kol_actuals_yearly(year: int) -> dict[int, dict[str, dict]]:
    """Return {month: {branch_key|'all': {kpi_key: value}}} for all 12 months."""
    today = date.today()
    cur_month = today.month if today.year == year else (12 if today.year > year else 0)
    out: dict[int, dict] = {}
    for m in range(1, min(cur_month + 1, 13)):
        out[m] = _get_kol_actuals_for_month(year, m)
    return out


# ── Paid Ads actuals ─────────────────────────────────────────────────────────

_ads_actuals_cache: dict[tuple, tuple[float, dict]] = {}
_ADS_ACTUALS_TTL = 600


def get_paid_ads_actuals_yearly(year: int) -> dict[int, dict[str, dict]]:
    """Return {month: {branch_key: {ads_revenue, roas, ads_material}}} for the year.

    Uses Ads Platform get_spend_daily (valid_country_only=True) for a full-year
    date range, then aggregates by month × branch_slug.
    ads_revenue in mil VND. roas = revenue / spend (0 if no spend).
    ads_material = 0 (placeholder; counted separately from AdCombos DB).
    """
    cache_key = ("paid_ads", year)
    cached = _ads_actuals_cache.get(cache_key)
    if cached and (time.time() - cached[0]) < _ADS_ACTUALS_TTL:
        return cached[1]

    if not settings.ADS_PLATFORM_API_KEY:
        log.warning("ADS_PLATFORM_API_KEY not set; paid_ads actuals unavailable")
        return {}

    try:
        from app.services.ads_platform import AdsPlatformClient
        client = AdsPlatformClient()
        rows = client.get_spend_daily(
            date_from=f"{year}-01-01",
            date_to=f"{year}-12-31",
            valid_country_only=True,
        )
    except Exception as exc:
        log.warning("get_paid_ads_actuals_yearly: ads platform error: %s", exc)
        return {}

    # Aggregate spend+revenue per (month, branch_slug)
    agg: dict[tuple, dict] = {}  # (month, branch_slug) → {spend, revenue}
    for row in rows:
        d = row.get("date") or ""
        month = int(d[5:7]) if len(d) >= 7 else None
        if not month:
            continue
        branch_slug = (row.get("branch") or "").lower().strip()
        if not branch_slug:
            continue
        k = (month, branch_slug)
        if k not in agg:
            agg[k] = {"spend": 0.0, "revenue": 0.0}
        agg[k]["spend"]   += float(row.get("spend")   or 0)
        agg[k]["revenue"] += float(row.get("revenue") or 0)

    # Normalise branch slug → branch_key
    _slug_to_key = {"saigon": "saigon", "sai gon": "saigon",
                    "taipei": "taipei", "tpe": "taipei",
                    "1948": "1948", "oani": "oani", "osaka": "osaka"}

    out: dict[int, dict[str, dict]] = {}
    for (month, slug), vals in agg.items():
        branch_key = _slug_to_key.get(slug) or slug
        spend = vals["spend"]
        revenue_vnd = vals["revenue"]  # platform returns VND
        roas = round(revenue_vnd / spend, 2) if spend > 0 else 0.0
        ads_rev_mil = round(revenue_vnd / 1e6, 1)
        out.setdefault(month, {})[branch_key] = {
            "ads_revenue": ads_rev_mil,
            "roas":        roas,
            "ads_material": 0,  # filled by caller from DB if needed
        }

    _ads_actuals_cache[cache_key] = (time.time(), out)
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
    if branch_id:
        # per-branch KPIs: match exact branch OR org-wide (branch_id IS NULL)
        q = q.filter(
            (TeamKPITarget.branch_id == branch_id) | (TeamKPITarget.branch_id.is_(None))
        )
    else:
        # "All" view: only org-wide targets (branch_id IS NULL) for aggregated display
        q = q.filter(TeamKPITarget.branch_id.is_(None))

    targets_map: dict[tuple, float] = {}        # (kpi_key, month) → target value
    manual_actuals_map: dict[tuple, float] = {} # (kpi_key, month) → manual actual value
    for row in q.all():
        if row.target_value is None:
            continue
        if row.kpi_key.endswith("__actual"):
            base_key = row.kpi_key[:-len("__actual")]
            manual_actuals_map[(base_key, row.month)] = float(row.target_value)
        else:
            targets_map[(row.kpi_key, row.month)] = float(row.target_value)

    # Fetch actuals
    actuals_yearly: dict[int, dict[str, dict]] = {}
    if auto and role_key == "kol":
        actuals_yearly = get_kol_actuals_yearly(year)
    elif auto and role_key == "paid_ads":
        actuals_yearly = get_paid_ads_actuals_yearly(year)
    elif auto and role_key == "designer":
        from app.services.lark_service import get_designer_actuals_yearly
        actuals_yearly = get_designer_actuals_yearly(year)

    # Determine branch key for actuals lookup
    branch_key = None
    if branch_id:
        branch_key = BRANCH_UUID_TO_KEY.get(str(branch_id))

    kpis_out = []
    all_pcts: list[float] = []
    cur_pcts: list[float] = []

    for defn in defs:
        kpi_key = defn["key"]
        org_wide = defn.get("org_wide", False)
        is_pct = defn.get("is_pct", False)
        decimals = defn.get("decimals", 0)
        scale = defn.get("scale")
        higher = defn.get("higher_is_better", True)

        monthly = []
        for m in range(1, 13):
            is_future = (m > cur_month)
            target = targets_map.get((kpi_key, m))

            # Actual: from upstream API (auto roles) or manual DB entry (non-auto)
            actual = None
            if auto and not is_future:
                month_actuals = actuals_yearly.get(m, {})
                lookup_key = "all" if org_wide else (branch_key or "")
                branch_data = month_actuals.get(lookup_key, {})
                raw = branch_data.get(kpi_key)
                if raw is not None:
                    actual = float(raw)
                    if scale:
                        actual = round(actual / scale, decimals or 1)
            elif not auto and not is_future:
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
            "unit": defn.get("unit_display") or defn["unit"],
            "is_pct": is_pct,
            "decimals": decimals,
            "higher_is_better": higher,
            "org_wide": org_wide,
            "auto_actuals": auto,
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
