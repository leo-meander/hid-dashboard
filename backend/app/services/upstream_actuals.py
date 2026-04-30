"""Pull actual-spend numbers for Paid Ads and KOL channels from their
respective upstream platforms.

Both APIs expose per-month actuals already aggregated against the same
budget plans the platform UIs show, so the values match what marketing
sees when they open Ads Platform / KOL Engine directly.

Called from ``app.routers.marketing_budget`` to replace the prior
local-DB SUM(cost_vnd) which over-counted (it included spend not tagged
to any budget plan).
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any, Optional
from uuid import UUID

from app.config import settings

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30  # seconds

# Branch UUID → KOL Engine hotel_id (reverse of kol_engine.HOTEL_TO_BRANCH_KEY).
# Hardcoded because both ID sets are stable seed data.
BRANCH_TO_KOL_HOTEL_ID: dict[str, str] = {
    "11111111-1111-1111-1111-111111111101": "c07ddc13-524d-4600-b3d8-5cc1871a0286",  # Taipei
    "11111111-1111-1111-1111-111111111102": "554923e7-2f80-4b18-8df7-1113277f92f2",  # Saigon
    "11111111-1111-1111-1111-111111111103": "4a7976a6-56cb-4a3f-a897-e6ce76c99d31",  # 1948
    "11111111-1111-1111-1111-111111111104": "41b5eb59-016d-442f-8c47-455a9bc567a3",  # Oani
    "11111111-1111-1111-1111-111111111105": "fad10525-b2db-48ee-b33f-f94958a11d3a",  # Osaka
}


def _fetch_json(url: str, headers: dict) -> Optional[dict]:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as exc:
        log.warning("upstream %s -> HTTP %s: %s", url, exc.code,
                    exc.read()[:200].decode("utf-8", "replace"))
    except Exception as exc:
        log.warning("upstream %s failed: %s", url, exc)
    return None


# ── Paid Ads (Ads Platform) ──────────────────────────────────────────────────

def fetch_paid_ads_yearly(branch_slug: str, year: int) -> dict[int, dict]:
    """Return ``{1..12: {spent_vnd, spent_native, budget_vnd, budget_native}}``
    for a single branch & year by calling the Ads Platform's
    ``/api/export/budget/yearly-plan`` mirror (X-API-Key auth).

    Returns an empty dict if the upstream call fails — caller should treat
    each missing month as 0 actual rather than crashing.
    """
    if not settings.ADS_PLATFORM_API_KEY:
        log.warning("ADS_PLATFORM_API_KEY not configured; paid-ads actuals=0")
        return {}
    base = settings.ADS_PLATFORM_BASE_URL.rstrip("/")
    url = f"{base}/api/export/budget/yearly-plan?branch={branch_slug}&year={year}"
    body = _fetch_json(url, {
        "X-API-Key": settings.ADS_PLATFORM_API_KEY,
        "Accept": "application/json",
    })
    if not body:
        return {}
    data = body.get("data", body) if isinstance(body, dict) else {}
    months = data.get("months") or []
    out: dict[int, dict] = {}
    for m in months:
        idx = m.get("month")
        if idx is None:
            continue
        out[int(idx)] = {
            "spent_vnd":     float(m.get("spent_vnd")     or 0),
            "spent_native":  float(m.get("spent_native")  or 0),
            "budget_vnd":    float(m.get("budget_vnd")    or 0),
            "budget_native": float(m.get("budget_native") or 0),
        }
    return out


# ── KOL (KOL Media Engine) ───────────────────────────────────────────────────

def fetch_kol_yearly(hotel_id: str, year: int, currency: str = "VND") -> dict[int, float]:
    """Return ``{1..12: actual}`` for a hotel & year. ``currency`` controls
    the unit upstream converts everything to (we ask for VND so the value
    drops straight into our cost_vnd column)."""
    if not settings.KOL_SYNC_API_KEY:
        log.warning("KOL_SYNC_API_KEY not configured; kol actuals=0")
        return {}
    base = settings.KOL_ENGINE_URL.rstrip("/")
    org_id = settings.KOL_ENGINE_ORG_ID
    url = (
        f"{base}/api/sync/budgets"
        f"?organization_id={org_id}&year={year}&hotel_id={hotel_id}&currency={currency}"
    )
    body = _fetch_json(url, {
        "X-Sync-API-Key": settings.KOL_SYNC_API_KEY,
        "Accept": "application/json",
    })
    if not body:
        return {}
    data = body.get("data", body) if isinstance(body, dict) else {}
    months = data.get("monthly_breakdown") or []
    out: dict[int, float] = {}
    for m in months:
        idx = m.get("month")
        if idx is None:
            continue
        out[int(idx)] = float(m.get("actual") or 0)
    return out


def kol_hotel_id_for(branch_id: UUID | str) -> Optional[str]:
    return BRANCH_TO_KOL_HOTEL_ID.get(str(branch_id))
