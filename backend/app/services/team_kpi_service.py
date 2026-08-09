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
from app.services.kol_engine import HOTEL_TO_BRANCH_KEY, fetch_kol_revenue
from app.services.upstream_actuals import BRANCH_TO_KOL_HOTEL_ID

log = logging.getLogger(__name__)

# ── KPI metadata ────────────────────────────────────────────────────────────

KPI_DEFS: dict[str, list[dict]] = {
    "kol": [
        {"key": "kol_invited",     "label": "KOLs Invited",          "unit": "KOLs",   "org_wide": True,  "higher_is_better": True},
        {"key": "kol_revenue",     "label": "Revenue via KOL",        "unit": "mil VND","org_wide": False, "higher_is_better": True,  "is_revenue": True},
        {"key": "kol_collaborated","label": "KOLs Collaborated",      "unit": "KOLs",   "org_wide": False, "higher_is_better": True},
        {"key": "kol_posted",      "label": "KOLs Posted",            "unit": "posts",  "org_wide": False, "higher_is_better": True,
         "computed_target": "kol_upstream", "computed_note": "= % of prev-month collab (KOL Engine)",
         "computed_note_pct": "= {pct}% of prev-month collab (KOL Engine)"},
        {"key": "kol_ads_collab",  "label": "KOL Ads Collab",         "unit": "KOLs",   "org_wide": False, "higher_is_better": True,
         "computed_target": "kol_upstream", "computed_note": "= % of Posted target (KOL Engine)",
         "computed_note_pct": "= {pct}% of Posted target (KOL Engine)"},
    ],
    "paid_ads": [
        {"key": "roas",            "label": "ROAS",                   "unit": "×",      "org_wide": False, "higher_is_better": True,  "decimals": 2},
        {"key": "ads_revenue",     "label": "Revenue via Paid Ads",   "unit": "mil VND","org_wide": False, "higher_is_better": True,  "is_revenue": True, "computed_target": "spend_x_roas", "computed_note": "= spend × ROAS target"},
        # GA4 userKeyEventRate:purchase, whole-property. See ga4_service for why
        # it can only be read at property scope and why every figure is its own
        # query. Manual target, auto actual — same shape as the ROAS row.
        {"key": "purchase_cvr",    "label": "Purchase Conversion Rate","unit": "%",     "org_wide": False, "higher_is_better": True,  "decimals": 2, "is_pct": True,
         # Targets here are literal small percentages (1.8 means 1.8%), so they
         # must skip the ≤2.0 → ×100 fraction normalisation the other is_pct
         # KPIs rely on.
         "normalize_fraction_targets": False,
         "fixed_decimals": True, "value_suffix": "%",
         # A user-scoped rate cannot be summed across months, so YTD is its own
         # Jan-1 → today GA4 query and is blank when that query has no answer.
         "ytd_mode": "query",
         "provisional_note": "GA4 withheld part of this window (Google Signals data thresholding), so the rate is approximate rather than a confident number.",
         # Oani's tag was removed from the 1948 and Osaka sites on 2026-08-09
         # (verified at the GTM container source, not just on the page). GA4
         # never cleans history, so every month through August still measures
         # three branches — September is the first month that is Oani alone.
         "starts_by_branch": {
             "oani": {
                 "from": "2026-09",
                 "why": "Oani's analytics tag was also live on the 1948 and Osaka websites until 9 Aug 2026, so earlier months measure three branches rather than Oani alone. GA4 cannot clean historical data, which makes September the first month this rate is really Oani's.",
             },
         },
         # Reader-facing text: branch names only. A GA4 property ID must never
         # surface in the KPI grid — the grid speaks in branches, and the
         # branch → property mapping stays in config.
         "blocked": {
             "all": "Each branch has its own separate GA4 analytics — a visitor cannot be de-duplicated across them, so there is no correct group-wide rate. Read this KPI per branch.",
         }},
    ],
    "designer": [
        # Temporarily hidden (2026-08-06) — remove "hidden" to bring them back
        # with their stored targets/actuals intact.
        {"key": "design_assets",   "label": "Design Assets Completed","unit": "designs","org_wide": False, "higher_is_better": True, "hidden": True},
        {"key": "videos_delivered","label": "Videos Delivered",       "unit": "videos", "org_wide": False, "higher_is_better": True, "hidden": True},
        {"key": "design_ideas",    "label": "Design Ideas",           "unit": "ideas",  "org_wide": False, "higher_is_better": True,  "auto": False, "starts": "2026-08"},
        {"key": "ads_win_rate",    "label": "% Ads Win",              "unit": "%",      "org_wide": False, "higher_is_better": True,  "decimals": 1, "is_pct": True, "starts": "2026-08"},
        # Same number as Nora's Task Overview scorecard. Org-wide: Lark tasks
        # carry no branch split. Starts with the Lark data (_LARK_START_MONTH).
        {"key": "delivery_rate",   "label": "On-Time Delivery Rate",  "unit": "%",      "org_wide": True,  "higher_is_better": True,  "decimals": 1, "is_pct": True, "starts": "2026-07"},
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

def visible_kpi_defs(role_key: str) -> list[dict]:
    """KPI defs for a role, minus any marked hidden (temporarily retired KPIs).

    Targets/actuals for hidden KPIs stay in the DB untouched — dropping the
    "hidden" flag in KPI_DEFS brings the row back with its history intact.
    """
    return [d for d in KPI_DEFS.get(role_key, []) if not d.get("hidden")]


def kpi_branch_start(defn: dict, branch_key: Optional[str] = None) -> tuple[Optional[str], Optional[str]]:
    """The ("YYYY-MM", why) in force for one branch view.

    A single branch can start later than the KPI itself — a branch whose
    measurement was only corrected part-way through the year has no honest
    history before the fix, even though its siblings do. `starts_by_branch`
    carries that per branch, either as a bare month or as
    ``{"from": ..., "why": ...}`` so the grid can say why the earlier months
    are empty. Falls back to the KPI-wide "starts".
    """
    entry = (defn.get("starts_by_branch") or {}).get(branch_key or "")
    if isinstance(entry, dict):
        return entry.get("from"), entry.get("why")
    if entry:
        return str(entry), None
    return defn.get("starts"), None


def kpi_start_month(defn: dict, year: int, branch_key: Optional[str] = None) -> Optional[int]:
    """First month of `year` this KPI is live for this branch view.

    None when the KPI has always been live (or the year is past its start);
    13 when the whole year predates it. Months before the returned value are
    rendered blank and locked instead of showing a misleading 0.
    """
    starts, _why = kpi_branch_start(defn, branch_key)
    if not starts:
        return None
    s_year, _, s_month = str(starts).partition("-")
    try:
        s_year, s_month = int(s_year), int(s_month)
    except ValueError:
        log.warning("bad 'starts' on KPI %s: %r", defn.get("key"), starts)
        return None
    if year < s_year:
        return 13
    return s_month if year == s_year else None


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

BRANCH_KEYS: tuple[str, ...] = ("saigon", "taipei", "1948", "oani", "osaka")

# Branches that use a non-VND currency; defaults to VND for all others
BRANCH_CURRENCY: dict[str, str] = {
    "osaka":  "JPY",
    "taipei": "TWD",
    "1948":   "TWD",
    "oani":   "JPY",
}

# How to display revenue in each currency
_CURRENCY_DISPLAY: dict[str, dict] = {
    "VND": {"unit": "mil VND", "scale": 1_000_000, "decimals": 1},
    "JPY": {"unit": "JPY",     "scale": 1,          "decimals": 0},
    "TWD": {"unit": "TWD",     "scale": 1,          "decimals": 0},
}

def _fmt_pct(v: float) -> str:
    """Percentage for display: 90.0 → '90', 92.5 → '92.5'."""
    return str(int(v)) if float(v).is_integer() else f"{v:g}"


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

    # Fetch org total per month (one call instead of 5 per-hotel calls):
    #  - store totals.revenue in 'all' bucket for the All-branches view
    #    (branches[] sums miss kol_reservations rows with null hotel_id)
    #  - also fill per-branch revenue for months not in cache (branches[] rows)
    today = date.today()
    cur_month = today.month if today.year == year else (12 if today.year > year else 0)
    try:
        for m in range(1, cur_month + 1):
            org_data = fetch_kol_revenue(
                base_url=settings.KOL_ENGINE_URL,
                org_slug=settings.KOL_TARGETS_ORG_SLUG,
                api_key=settings.KOL_REVENUE_API_SECRET,
                year=year,
                month=m,
                hotel_id=None,
            )
            if org_data is None:
                continue
            org_totals = org_data.get("totals") or {}
            out.setdefault(m, {}).setdefault("all", {})["kol_revenue"] = float(org_totals.get("revenue") or 0)
            if m not in cached_months:
                for b in org_data.get("branches") or []:
                    hotel_id = b.get("hotel_id")
                    bk = HOTEL_TO_BRANCH_KEY.get(hotel_id)
                    if bk:
                        out.setdefault(m, {})[bk] = {
                            "kol_revenue":      float(b.get("revenue_vnd") or 0),
                            "kol_collaborated": 0.0,
                            "kol_posted":       0.0,
                            "kol_ads_collab":   0.0,
                        }
    except Exception as exc:
        log.warning("KOL Engine revenue fallback failed: %s", exc)

    # Ensure every month up to today has an 'all' key (for kol_invited)
    today = date.today()
    cur_month = today.month if today.year == year else (12 if today.year > year else 0)
    for m in range(1, min(cur_month + 1, 13)):
        out.setdefault(m, {}).setdefault("all", {"kol_invited": 0.0, "kol_revenue": 0.0})

    # Step 2: merge counts + kol_invited from targets API — never fatal
    #
    # The same response also carries the KOL Engine's own posted / ads_allowed
    # TARGETS (percentage-driven: posted = prev-month collab × posted_pct,
    # ads_allowed = posted target × ads_pct). Those are stored under
    # "<kpi_key>__target" so the Team KPI target row mirrors KOL Engine
    # instead of a hand-entered HiD number.
    #
    # One month past today is fetched too: next month's posted target only
    # depends on THIS month's collaborations, so it is already computable.
    try:
        from app.services.kol_engine import fetch_kol_targets
        for m in range(1, min(cur_month + 2, 13)):
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
            out.setdefault(m, {}).setdefault("all", {})["kol_invited"] = inv_val

            for br in tgt_data.get("branches") or []:
                hotel_id = br.get("hotel_id") or br.get("id") or ""
                branch_key = HOTEL_TO_BRANCH_KEY.get(hotel_id)
                if not branch_key:
                    name = (br.get("hotel_name") or "").lower()
                    for k in ("saigon", "taipei", "1948", "oani", "osaka"):
                        if k in name:
                            branch_key = k
                            break
                if not branch_key:
                    continue

                def _v(field, sub="actual", _br=br):
                    """Read branches[].<field>.<sub>; None when absent."""
                    v = _br.get(field)
                    if isinstance(v, dict):
                        raw = v.get(sub)
                        return float(raw) if raw is not None else None
                    # legacy scalar payload carries the actual only
                    return float(v or 0) if sub == "actual" else None

                # Counts only land on months that already have a branch bucket —
                # keeps months with no revenue row blank instead of showing 0.
                existing = out.get(m, {}).get(branch_key)
                if existing is not None:
                    existing["kol_collaborated"] = _v("collaborated") or 0.0
                    existing["kol_posted"]       = _v("posted") or 0.0
                    existing["kol_ads_collab"]   = _v("ads_allowed") or 0.0

                # Targets always land, including on the month ahead.
                bucket = out.setdefault(m, {}).setdefault(branch_key, {})
                bucket["kol_posted__target"]     = _v("posted", "target")
                bucket["kol_ads_collab__target"] = _v("ads_allowed", "target")
                # The % rules behind those targets — only for labelling the
                # target row. Absent on older KOL Engine deploys; the label
                # then falls back to its generic wording.
                def _pct(field, _br=br):
                    v = _br.get(field)
                    return float(v) if v is not None else None
                bucket["kol_posted__pct"]     = _pct("posted_pct")
                bucket["kol_ads_collab__pct"] = _pct("ads_allowed_pct")
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
        }

    _ads_actuals_cache[cache_key] = (time.time(), out)
    return out


# ── % Ads Win actuals (Designer) ─────────────────────────────────────────────

_ads_win_cache: dict[tuple, tuple[float, dict]] = {}
_ADS_WIN_TTL = 600


def get_ads_win_actuals_yearly(year: int) -> dict[int, dict[str, dict]]:
    """Return ``{month: {branch_key|'all': {ads_win_rate, ...__provisional}}}``.

    Source: Ads Platform ``/api/export/winning-ads-monthly``. An ad wins a
    month when its ROAS that month beats the branch's blended CRTV ROAS for
    the same month, with enough data behind it (> 4,500 clicks or ≥ 5
    bookings); anything below that stays TEST and counts on neither side.
    win_rate = wins / (wins + losses) among the ads decided that month.

    Notes that shape the mapping below:
      * upstream ``win_rate`` is a 0–1 fraction; HiD stores is_pct KPIs 0–100.
      * ``null`` means nothing was decided that month (tested == 0) → blank,
        not 0. A month where every tested ad lost really does return 0.0.
      * the "All branches" figure is ``by_month[].win_rate``, never an average
        of the branch percentages — the denominators differ.
      * branch labels come back capitalised (``Saigon``, ``1948``, …);
        ``Bread`` has no HiD branch and is dropped.
      * ``in_progress`` marks the newest synced month: LOSE verdicts only
        freeze once a month closes, so its rate is provisional and usually
        inflated. Flagged for the UI, not suppressed.
    """
    cache_key = ("ads_win", year)
    cached = _ads_win_cache.get(cache_key)
    if cached and (time.time() - cached[0]) < _ADS_WIN_TTL:
        return cached[1]

    from app.services.upstream_actuals import fetch_winning_ads_monthly

    out: dict[int, dict[str, dict]] = {}
    try:
        data = fetch_winning_ads_monthly(year)
        for entry in data.get("by_month") or []:
            month = _parse_ym(entry.get("month"), year)
            if month is None:
                continue
            provisional = bool(entry.get("in_progress"))

            def _cell(src: dict) -> Optional[dict]:
                # Absent (upstream predates the win/loss fields) or null
                # (nothing decided) both mean "no ratio" → blank cell.
                wr = src.get("win_rate")
                if wr is None:
                    return None
                return {
                    "ads_win_rate": round(float(wr) * 100, 2),
                    "ads_win_rate__provisional": provisional,
                }

            org = _cell(entry)
            if org is not None:
                out.setdefault(month, {})["all"] = org
            for br in entry.get("by_branch") or []:
                branch_key = str(br.get("branch") or "").strip().lower()
                if branch_key not in BRANCH_KEYS:
                    continue  # "Bread" has no HiD branch
                cell = _cell(br)
                if cell is not None:
                    out.setdefault(month, {})[branch_key] = cell
    except Exception as exc:
        log.warning("ads win-rate actuals unavailable: %s", exc)
        return {}

    _ads_win_cache[cache_key] = (time.time(), out)
    return out


# ── Purchase Conversion Rate actuals (Paid Ads, GA4) ─────────────────────────

_ga4_cvr_cache: dict[tuple, tuple[float, dict]] = {}
# GA4 is 24–48h from final on the current day and only moves as it processes,
# so an hour on the read path is plenty. Closed months get re-queried rather
# than frozen: runReport is cheap and a re-query can never go stale.
_GA4_CVR_TTL = 3600

# Synthetic bucket for the year-to-date reading. build_monthly_summary iterates
# months 1–12, so month 0 can never leak into a monthly cell — it exists purely
# so the Jan-1 → today query travels in the same cached payload as the months.
GA4_YTD_MONTH = 0


def get_purchase_cvr_actuals_yearly(year: int) -> dict[int, dict[str, dict]]:
    """Return ``{month: {branch_key: {purchase_cvr, purchase_cvr__provisional}}}``
    plus a ``GA4_YTD_MONTH`` bucket holding the year-to-date reading.

    One GA4 request per (branch × month), each with that month's own date range.
    That is not an optimisation choice — ``userKeyEventRate:purchase`` counts
    unique users, which de-duplicate across time, so a month assembled from
    daily rows double-counts anyone who visited twice and the error grows with
    the window. For the same reason the YTD figure is its own Jan-1 → today
    request rather than anything derived from the monthly values.

    Branches with no property configured (Oani) are simply absent. A failed or
    empty response leaves that month blank rather than recording a 0%.
    """
    cache_key = ("ga4_cvr", year)
    cached = _ga4_cvr_cache.get(cache_key)
    if cached and (time.time() - cached[0]) < _GA4_CVR_TTL:
        return cached[1]

    from concurrent.futures import ThreadPoolExecutor

    from app.services.ga4_service import run_purchase_report

    property_map = settings.ga4_property_map
    if not property_map:
        log.warning("no GA4 properties configured; purchase_cvr blank")
        return {}

    today = date.today()
    if today.year > year:
        cur_month, ytd_end = 12, f"{year}-12-31"
    elif today.year < year:
        return {}
    else:
        cur_month, ytd_end = today.month, today.isoformat()

    jobs: list[tuple[int, str, str, str, str]] = []
    for branch_key, property_id in property_map.items():
        for m in range(1, cur_month + 1):
            last_day = calendar.monthrange(year, m)[1]
            jobs.append((m, branch_key, property_id,
                         f"{year}-{m:02d}-01", f"{year}-{m:02d}-{last_day:02d}"))
        jobs.append((GA4_YTD_MONTH, branch_key, property_id,
                     f"{year}-01-01", ytd_end))

    out: dict[int, dict[str, dict]] = {}
    try:
        with ThreadPoolExecutor(max_workers=6) as pool:
            readings = list(pool.map(
                lambda job: run_purchase_report(job[2], job[3], job[4]), jobs
            ))
    except Exception as exc:
        log.warning("GA4 purchase-rate actuals unavailable: %s", exc)
        return {}

    for (month, branch_key, _pid, _start, _end), reading in zip(jobs, readings):
        if reading is None or reading.rate_pct is None:
            continue
        out.setdefault(month, {})[branch_key] = {
            "purchase_cvr": reading.rate_pct,
            "purchase_cvr__provisional": reading.thresholded,
        }

    _ga4_cvr_cache[cache_key] = (time.time(), out)
    return out


def _parse_ym(value, year: int) -> Optional[int]:
    """"2026-08" → 8, but only for the year asked for; None otherwise."""
    y, _, m = str(value or "").partition("-")
    try:
        if int(y) != year:
            return None
        month = int(m)
    except ValueError:
        return None
    return month if 1 <= month <= 12 else None


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
        db.query(KPITarget, Branch)
        .join(Branch, KPITarget.branch_id == Branch.id)
        .filter(KPITarget.year == year)
        .all()
    )
    target_meta: dict[tuple, dict] = {}
    for kpi, branch in kpi_rows:
        name_lower = (branch.name or "").lower()
        branch_key = None
        for k in ("saigon", "taipei", "1948", "oani", "osaka"):
            if k in name_lower:
                branch_key = k
                break
        if not branch_key:
            continue
        # deduct% / other revenue live on Branch — that's where the Summary page
        # saves them and what the Revenue KPI grid reads. The same columns on
        # KPITarget are legacy and never written; reading those made this metric
        # disagree with Revenue KPI on months with no accounting override.
        deduct = float(branch.deduction_pct or 0) / 100
        target_meta[(branch_key, kpi.month)] = {
            "target":      float(kpi.target_revenue_native or 0),
            "override":    float(kpi.actual_revenue_override) if kpi.actual_revenue_override is not None else None,
            "deduct_mult": 1.0 - deduct,
            "other_rev":   float(branch.other_revenue_native or 0),
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
        # Manual override = final number as typed. No deduct%/other_rev on top.
        if meta["override"] is not None:
            actual = raw
        else:
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

def _combined_actuals(*sources: dict[int, dict[str, dict]]) -> dict[int, dict[str, dict]]:
    """Fold several actuals sources into one FRESH dict, per month × bucket.

    Every fetcher hands back the object it holds in its own TTL cache. Merging
    into one of them in place would write the other sources' values into that
    cache — where they would then outlive their own TTL and be served as
    current long after they went stale.
    """
    out: dict[int, dict[str, dict]] = {}
    for source in sources:
        for month, buckets in source.items():
            for bucket, values in buckets.items():
                out.setdefault(month, {}).setdefault(bucket, {}).update(values)
    return out

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

    defs = visible_kpi_defs(role_key)
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

    # is_pct KPIs are never summed across branches for the All view — the
    # denominators differ, so a sum of percentages means nothing.
    _pct_keys = {d["key"] for d in defs if d.get("is_pct")}
    # Most is_pct targets may have been entered as fractions (e.g. 0.9 = 90%);
    # normalize those on load. KPIs whose real targets are small percentages
    # opt out via normalize_fraction_targets — for them 1.8 means 1.8%.
    _fraction_pct_keys = {
        d["key"] for d in defs
        if d.get("is_pct") and d.get("normalize_fraction_targets", True)
    }
    # CRM revenue targets entered in bil VND (e.g. 0.056) instead of mil VND (56). Normalize on load.
    _bil_vnd_keys = {"crm_revenue"} if role_key == "crm" else set()
    # Revenue KPI keys — targets stored in native currency units, need FX conversion for All view
    _revenue_keys = {d["key"] for d in defs if d.get("is_revenue")}

    targets_map: dict[tuple, float] = {}        # (kpi_key, month) → target value (summed for All)
    manual_actuals_map: dict[tuple, float] = {} # (kpi_key, month) → manual actual value
    per_branch_targets_map: dict[tuple, dict[str, float]] = {}  # (kpi_key, month) → {branch_key: value}
    # Separate accumulator for branch revenue sums (merged after loop to avoid double-counting org-wide)
    _branch_rev_sums: dict[tuple, float] = {}
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
            if row.kpi_key in _fraction_pct_keys and raw_val <= 2.0:
                raw_val = round(raw_val * 100, 1)
            # Normalize CRM revenue targets entered in bil VND (< 1) → mil VND (× 1000)
            if row.kpi_key in _bil_vnd_keys and raw_val < 1.0:
                raw_val = round(raw_val * 1000, 3)
            if all_branches_view and row.branch_id is not None:
                if row.kpi_key in _revenue_keys:
                    bk_curr = BRANCH_CURRENCY.get(BRANCH_UUID_TO_KEY.get(str(row.branch_id), ""), "VND")
                    if bk_curr == "VND":
                        vnd_val = raw_val * 1_000_000
                    else:
                        rate = get_cached_rate(bk_curr, "VND") or 1.0
                        vnd_val = raw_val * rate
                    sum_val = round(vnd_val / 1_000_000, 3)
                    _branch_rev_sums[key] = round(_branch_rev_sums.get(key, 0.0) + sum_val, 3)
                elif row.kpi_key in _pct_keys:
                    targets_map.setdefault(key, raw_val)
                else:
                    # Count KPIs: sum across branches
                    targets_map[key] = targets_map.get(key, 0.0) + raw_val
                bk = BRANCH_UUID_TO_KEY.get(str(row.branch_id))
                if bk:
                    per_branch_targets_map.setdefault(key, {})[bk] = raw_val
            elif all_branches_view and row.branch_id is None:
                targets_map.setdefault(key, raw_val)
            else:
                # Single-branch view: branch-specific row wins over org-wide fallback
                if row.branch_id is not None:
                    targets_map[key] = raw_val
                else:
                    targets_map.setdefault(key, raw_val)

    # Merge branch revenue sums — overrides any org-wide setdefault value so branch targets always win
    for rev_key, rev_sum in _branch_rev_sums.items():
        targets_map[rev_key] = rev_sum

    # Merge branch revenue sums — overrides any org-wide setdefault value so branch targets always win
    for rev_key, rev_sum in _branch_rev_sums.items():
        targets_map[rev_key] = rev_sum

    # Fetch actuals
    actuals_yearly: dict[int, dict[str, dict]] = {}
    if auto and role_key == "kol":
        actuals_yearly = get_kol_actuals_yearly_db(db, year)
    elif auto and role_key == "paid_ads":
        sources = [get_paid_ads_actuals_yearly(db, year)]
        if any(d["key"] == "purchase_cvr" for d in defs):
            sources.append(get_purchase_cvr_actuals_yearly(year))
        actuals_yearly = _combined_actuals(*sources)
    elif auto and role_key == "designer":
        # Each source is fetched only when a visible KPI still needs it.
        sources = []
        if any(d["key"] in ("design_assets", "videos_delivered") for d in defs):
            from app.services.lark_service import get_designer_actuals_yearly
            sources.append(get_designer_actuals_yearly(year))
        if any(d["key"] == "ads_win_rate" for d in defs):
            sources.append(get_ads_win_actuals_yearly(year))
        if any(d["key"] == "delivery_rate" for d in defs):
            try:
                from app.services.lark_service import get_delivery_rate_yearly
                sources.append(get_delivery_rate_yearly(year))
            except Exception as exc:
                log.warning("designer delivery_rate from Lark unavailable: %s", exc)
        actuals_yearly = _combined_actuals(*sources)
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

    _ALL_BRANCH_KEYS = BRANCH_KEYS

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
        # Months before this are blank + locked. Resolved per branch: one
        # branch can have no honest history where its siblings do.
        starts_view       = None if all_branches_view else branch_key
        start_month       = kpi_start_month(defn, year, starts_view)
        starts_str, starts_note = kpi_branch_start(defn, starts_view)

        # Views where this KPI cannot be measured honestly (e.g. a branch whose
        # GA4 tag is polluted). The actual row goes blank with the reason
        # attached; stored targets are left alone so the row comes straight back
        # when the blocker is lifted.
        blocked_note = (defn.get("blocked") or {}).get(
            "all" if all_branches_view else (branch_key or "")
        )

        # Percentage rules behind an upstream-owned target, collected while
        # resolving the months so the target row can be labelled with the
        # actual number (e.g. "= 90% of prev-month collab").
        upstream_pcts: set[float] = set()

        monthly = []
        for m in range(1, 13):
            is_future = (m > cur_month)

            # KPI didn't exist yet: blank + locked, never a 0.
            if start_month is not None and m < start_month:
                monthly.append({
                    "month": m,
                    "target": None,
                    "actual": None,
                    "pct": None,
                    "is_future": is_future,
                    "has_target": False,
                    "not_started": True,
                    "provisional": False,
                })
                continue

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
            elif computed_target_t == "kol_upstream":
                # Target owned by KOL Engine (percentage-driven, recomputed as
                # collaborations land) — HiD mirrors it, never stores it.
                m_act = actuals_yearly.get(m, {})
                tgt_field = f"{kpi_key}__target"
                src_keys = _ALL_BRANCH_KEYS if all_branches_view else (branch_key or "",)
                if all_branches_view:
                    vals = [m_act.get(bk, {}).get(tgt_field) for bk in _ALL_BRANCH_KEYS]
                    total = sum(float(v) for v in vals if v is not None)
                    target = round(total) if total > 0 else None
                else:
                    v = m_act.get(branch_key or "", {}).get(tgt_field)
                    target = round(float(v)) if v is not None and float(v) > 0 else None
                for bk in src_keys:
                    p = m_act.get(bk, {}).get(f"{kpi_key}__pct")
                    if p is not None:
                        upstream_pcts.add(float(p))
            elif computed_target_t:
                target = None  # future month or unknown computed type
            else:
                target = targets_map.get((kpi_key, m))
                # ROAS all-branches: weighted average = Σ(spend_bk × roas_target_bk) / Σ(spend_bk)
                if all_branches_view and kpi_key == "roas" and role_key == "paid_ads" and not is_future:
                    per_branch_roas = per_branch_targets_map.get(("roas", m), {})
                    m_actuals = actuals_yearly.get(m, {})
                    tot_weighted = 0.0
                    tot_spend = 0.0
                    for bk, roas_tgt in per_branch_roas.items():
                        spend = float(m_actuals.get(bk, {}).get("ads_spend") or 0)
                        if spend > 0:
                            tot_weighted += spend * float(roas_tgt)
                            tot_spend += spend
                    target = round(tot_weighted / tot_spend, 2) if tot_spend > 0 else None
                elif all_branches_view and kpi_key == "roas":
                    target = None

            # Actual: from upstream API (auto roles) or manual DB entry (non-auto)
            actual = None
            if blocked_note:
                pass  # no honest number exists for this view
            elif kpi_auto and not is_future:
                month_actuals = actuals_yearly.get(m, {})
                if org_wide:
                    raw = month_actuals.get("all", {}).get(kpi_key)
                elif all_branches_view and kpi_key == "roas" and role_key == "paid_ads":
                    # Weighted ROAS = total_revenue_vnd / total_spend_vnd across branches
                    tot_rev   = sum(float(month_actuals.get(bk, {}).get("ads_revenue") or 0) for bk in _ALL_BRANCH_KEYS)
                    tot_spend = sum(float(month_actuals.get(bk, {}).get("ads_spend")   or 0) for bk in _ALL_BRANCH_KEYS)
                    raw = round(tot_rev / tot_spend, 2) if tot_spend > 0 else None
                elif all_branches_view:
                    # kol_revenue: use org total from 'all' bucket (includes null-hotel_id rows
                    # that are missed when summing per-branch values from branches[]).
                    if kpi_key == "kol_revenue" and role_key == "kol":
                        raw = month_actuals.get("all", {}).get("kol_revenue") or None
                    elif kpi_key == "ads_win_rate":
                        # Org-wide ratio from upstream. Averaging the branch
                        # percentages would be wrong — different denominators.
                        raw = month_actuals.get("all", {}).get("ads_win_rate")
                    else:
                        # is_pct KPIs (e.g. data_fill_rate): average across branches
                        # all other KPIs: sum across branches
                        total = 0.0
                        count = 0
                        for bk in _ALL_BRANCH_KEYS:
                            v = month_actuals.get(bk, {}).get(kpi_key)
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
                if raw is not None:
                    actual = float(raw)
                    if is_revenue and native_currency != "VND" and vnd_to_native_rate:
                        actual = round(actual / vnd_to_native_rate, decimals)
                    elif scale:
                        actual = round(actual / scale, decimals or 1)
            if actual is None and not is_future and not blocked_note:
                actual = manual_actuals_map.get((kpi_key, m))

            # Upstream flags a value it may still revise (see the KPI's own
            # actuals fetcher for what "provisional" means for that source).
            src_bucket = "all" if (org_wide or all_branches_view) else (branch_key or "")
            provisional = bool(
                actual is not None
                and actuals_yearly.get(m, {}).get(src_bucket, {}).get(f"{kpi_key}__provisional")
            )

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
                "not_started": False,
                "provisional": provisional,
            })

        # Label the target row with the real percentage when upstream reports
        # one; a single shared % reads "= 90% …", mixed per-branch settings in
        # the All view read as a range.
        note = defn.get("computed_note")
        pct_tmpl = defn.get("computed_note_pct")
        if pct_tmpl and upstream_pcts:
            ordered = sorted(upstream_pcts)
            shown = (
                _fmt_pct(ordered[0]) if len(ordered) == 1
                else f"{_fmt_pct(ordered[0])}–{_fmt_pct(ordered[-1])}"
            )
            note = pct_tmpl.format(pct=shown)

        # YTD: "sum" (the default — add up the monthly actuals) or "query",
        # for metrics whose months cannot legitimately be added together. A
        # "query" KPI carries its own year-to-date reading and shows nothing
        # when that reading is missing, rather than falling back to a sum.
        ytd_mode = defn.get("ytd_mode", "sum")
        ytd_actual = None
        if ytd_mode == "query" and not blocked_note:
            raw_ytd = (
                actuals_yearly.get(GA4_YTD_MONTH, {})
                .get("all" if org_wide else (branch_key or ""), {})
                .get(kpi_key)
            )
            if raw_ytd is not None:
                ytd_actual = round(float(raw_ytd), decimals)

        kpis_out.append({
            "key": kpi_key,
            "label": defn["label"],
            "unit": kpi_unit,
            "is_pct": is_pct,
            "decimals": decimals,
            "fixed_decimals": defn.get("fixed_decimals", False),
            "value_suffix": defn.get("value_suffix"),
            "higher_is_better": higher,
            "org_wide": org_wide,
            "auto_actuals": kpi_auto,
            "no_target": no_target,
            "computed_target": computed_target_t is not None,
            "computed_target_note": note,
            "starts": starts_str,
            "starts_note": starts_note,
            "ytd_mode": ytd_mode,
            "ytd_actual": ytd_actual,
            "unavailable_note": blocked_note,
            "provisional_note": defn.get("provisional_note"),
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
