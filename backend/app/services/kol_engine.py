"""
Fetch KOL data from the KOL Media Engine API and return parsed records
ready for upsert into kol_records / kol_bookings.

API: GET /api/sync/kol-data?organization_id=<org_id>
Auth: X-Sync-API-Key header
"""
from __future__ import annotations

import json
import logging
import urllib.request
from typing import Optional

log = logging.getLogger(__name__)

# KOL Media Engine hotel_id → HiD branch short key
# Mapping derived from case_id prefixes (K-OANI-*, K-SGN-*, etc.)
HOTEL_TO_BRANCH_KEY = {
    "41b5eb59-016d-442f-8c47-455a9bc567a3": "oani",     # Japan (Oani)
    "554923e7-2f80-4b18-8df7-1113277f92f2": "saigon",   # Vietnam
    "4a7976a6-56cb-4a3f-a897-e6ce76c99d31": "1948",     # Taiwan (1948)
    "fad10525-b2db-48ee-b33f-f94958a11d3a": "osaka",    # Japan (Osaka)
    "c07ddc13-524d-4600-b3d8-5cc1871a0286": "taipei",   # Taiwan (Taipei)
}


def fetch_kol_data(base_url: str, org_id: str, api_key: str) -> list[dict]:
    """
    Fetch KOL data from KOL Media Engine and return a flat list of
    collaboration records mapped to HiD branch keys.

    Each record:
        {
            "branch_key":       "saigon" | "taipei" | "1948" | "oani" | "osaka",
            "kol_name":         str,
            "kol_nationality":  str | None,
            "language":         str | None,
            "status":           str,   # KOL-level status
            "collab_status":    str,   # collaboration status
            "collab_type":      str,   # hosted_stay / paid / etc.
            "stay_start_date":  str | None (YYYY-MM-DD),
            "stay_end_date":    str | None (YYYY-MM-DD),
            "promo_code":       str | None,
            "case_id":          str | None,
            "confirmed_room_rate_usd": float | None,
            "booking_fee_usd":  float | None,
            "target_audience":  str | None,
            "deliverables":     str | None,
            "platforms":        list[dict],  # [{handle, platform, profile_url, follower_count}]
            "posts":            list[dict],  # [{platform, post_url, likes, reach, ...}]
        }
    """
    url = f"{base_url}/api/sync/kol-data?organization_id={org_id}"
    req = urllib.request.Request(
        url,
        headers={"X-Sync-API-Key": api_key},
        method="GET",
    )

    with urllib.request.urlopen(req, timeout=60) as resp:
        body = json.loads(resp.read())

    if not body.get("success"):
        raise RuntimeError(f"KOL Engine API error: {body}")

    kols = body["data"]["kols"]
    log.info("KOL Engine: fetched %d KOLs", len(kols))

    results = []
    for kol in kols:
        for collab in kol.get("collaborations") or []:
            hotel_id = collab.get("hotel_id")
            if not hotel_id:
                continue
            branch_key = HOTEL_TO_BRANCH_KEY.get(hotel_id)
            if not branch_key:
                log.debug("Unknown hotel_id %s, skipping", hotel_id)
                continue

            results.append({
                "branch_key":       branch_key,
                "kol_name":         kol["name"],
                "kol_nationality":  kol.get("country"),
                "language":         kol.get("primary_language"),
                "status":           kol.get("status"),
                "collab_status":    collab.get("status"),
                "collab_type":      collab.get("collaboration_type"),
                "stay_start_date":  collab.get("stay_start_date"),
                "stay_end_date":    collab.get("stay_end_date"),
                "promo_code":       collab.get("promo_code"),
                "case_id":          collab.get("case_id"),
                "confirmed_room_rate_usd": collab.get("confirmed_room_rate_usd"),
                "booking_fee_usd":  collab.get("booking_fee_usd"),
                "target_audience":  collab.get("target_audience"),
                "deliverables":     collab.get("deliverables_agreed"),
                "platforms":        kol.get("kol_platform_accounts") or [],
                "posts":            collab.get("posts") or [],
            })

    log.info("KOL Engine: %d collaboration records parsed", len(results))
    return results


# ── Public targets API ─────────────────────────────────────────────────────
# Endpoint: GET {base}/api/public/kol-targets/{slug}?year=YYYY&month=M
# Auth:     Authorization: Bearer <KOL_PUBLIC_API_KEY>
# Response: envelope {success, data, error, timestamp} where data has
#           {organization, period, totals, branches, monthly_targets}.
# Targets and actuals come back per metric: invited_proactive,
# collaborated, posted — each {actual, target, pct}.

import time

_KOL_TARGETS_TTL_SEC = 600  # 10 min — same response shared across the
                            # 5 branch passes inside one report build,
                            # plus survives a manual Re-run of the cron.
_kol_targets_cache: dict[tuple, tuple[float, dict]] = {}


def fetch_kol_targets(
    base_url: str,
    org_slug: str,
    api_key: str,
    year: int,
    month: int,
) -> Optional[dict]:
    """Fetch monthly KOL targets + actuals from the public API.

    Returns the inner `data` payload, or None on any failure (missing
    creds, network error, non-success envelope). Caller should treat
    None as "targets unavailable" and render a fallback in the email.
    """
    if not (base_url and org_slug and api_key):
        log.info("fetch_kol_targets: missing config (base_url/slug/key)")
        return None

    cache_key = (org_slug, int(year), int(month))
    cached = _kol_targets_cache.get(cache_key)
    if cached and (time.time() - cached[0]) < _KOL_TARGETS_TTL_SEC:
        return cached[1]

    url = f"{base_url}/api/public/kol-targets/{org_slug}?year={year}&month={month}"
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {api_key}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read())
    except Exception as e:
        log.warning("fetch_kol_targets: HTTP error %s", e)
        return None

    if not body.get("success"):
        log.warning("fetch_kol_targets: API returned error: %s", body.get("error"))
        return None

    data = body.get("data") or {}
    _kol_targets_cache[cache_key] = (time.time(), data)
    log.info(
        "fetch_kol_targets OK: %s %s/%s — %d branches",
        org_slug, year, month, len(data.get("branches") or []),
    )
    return data


# ── Public revenue API ─────────────────────────────────────────────────────
# Endpoint: GET {base}/api/public/kol-revenue/{slug}?year=YYYY&month=M[&hotel_id=UUID]
# Auth:     Authorization: Bearer <KOL_REVENUE_API_SECRET>
# Response: envelope {success, data, error, timestamp} where data has
#           {organization, period, totals, excluded, branches, months?}.
#
# `excluded` reports rows the KOL Engine pre-filtered as ads-attributed
# (cutoff 2026-05-01); they are NOT in `totals`. Each branches[] row
# carries both native (`revenue`, `cost`) and VND-equivalent
# (`revenue_vnd`, `cost_vnd`), so callers don't need an FX layer for
# cross-branch sums.

_KOL_REVENUE_TTL_SEC = 600  # 10 min — same window as targets cache; the
                            # endpoint is a heavy aggregation and the same
                            # (slug, year, month) is queried repeatedly
                            # by current+prev month MoM views.
_kol_revenue_cache: dict[tuple, tuple[float, dict]] = {}


def fetch_kol_revenue(
    base_url: str,
    org_slug: str,
    api_key: str,
    year: int,
    month: int,
    hotel_id: Optional[str] = None,
) -> Optional[dict]:
    """Fetch KOL bookings/revenue from the public revenue API.

    Returns the inner ``data`` payload (already de-duped against Ads
    Platform attribution from 2026-05-01 onward), or ``None`` on any
    failure (missing creds, network, non-success envelope). Callers
    must treat ``None`` as "API unavailable" and fall back to local
    Cloudbeds aggregation so the card never shows 0.
    """
    if not (base_url and org_slug and api_key):
        log.info("fetch_kol_revenue: missing config (base_url/slug/key)")
        return None

    cache_key = (org_slug, int(year), int(month), hotel_id or "")
    cached = _kol_revenue_cache.get(cache_key)
    if cached and (time.time() - cached[0]) < _KOL_REVENUE_TTL_SEC:
        return cached[1]

    qs = f"year={year}&month={month}"
    if hotel_id:
        qs += f"&hotel_id={hotel_id}"
    url = f"{base_url}/api/public/kol-revenue/{org_slug}?{qs}"
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {api_key}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read())
    except Exception as e:
        log.warning("fetch_kol_revenue: HTTP error %s", e)
        return None

    if not body.get("success"):
        log.warning("fetch_kol_revenue: API returned error: %s", body.get("error"))
        return None

    data = body.get("data") or {}
    _kol_revenue_cache[cache_key] = (time.time(), data)
    log.info(
        "fetch_kol_revenue OK: %s %s/%s — %d branches",
        org_slug, year, month, len(data.get("branches") or []),
    )
    return data


# Inverse of HOTEL_TO_BRANCH_KEY — short branch-key → KOL Engine hotel UUID.
# Used by weekly_report_builder.kol_section() to look up the right branch
# row in the targets API response.
BRANCH_KEY_TO_HOTEL: dict[str, str] = {
    v: k for k, v in HOTEL_TO_BRANCH_KEY.items()
}


def resolve_hotel_id_from_branch_name(branch_name: str) -> Optional[str]:
    """Map HiD branch.name (e.g. 'MEANDER Saigon') → KOL Engine hotel UUID.

    Substring match against the lowercase branch keys ('saigon', 'taipei',
    '1948', 'oani', 'osaka'). Returns None if no key is found in the name.
    """
    if not branch_name:
        return None
    bn = branch_name.lower().strip()
    for key, hotel_id in BRANCH_KEY_TO_HOTEL.items():
        if key in bn:
            return hotel_id
    return None
