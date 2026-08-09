"""
Fetch KOL data from the KOL Media Engine API and return parsed records
ready for upsert into kol_records / kol_bookings.

API: GET /api/sync/kol-data?organization_id=<org_id>
Auth: X-Sync-API-Key header
"""
from __future__ import annotations

import json
import logging
import time
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
                # Post-performance rollup, used by the Bi-Weekly report. Kept
                # optional: these are populated by the KOL Engine's own
                # insights job, so an un-scored collaboration returns None
                # rather than a misleading zero.
                "published_at":     collab.get("published_at"),
                "total_reach":      collab.get("total_reach"),
                "total_engagements": collab.get("total_engagements"),
            })

    log.info("KOL Engine: %d collaboration records parsed", len(results))
    return results


# ── Post-performance insights (reach / engagement) ──────────────────────────
#
# HiD's own `kol_records` table stores no reach or engagement columns — that
# data only exists in the KOL Engine. The Engine has no public per-period
# insights endpoint (only /api/public/kol-{targets,revenue,reservations}),
# so this aggregates the sync payload client-side instead.
#
# One HTTP call serves all five branches of a report build, hence the TTL
# cache — the same pattern `fetch_kol_targets` uses above.

_KOL_INSIGHTS_TTL_SEC = 600
_kol_insights_cache: dict[tuple, tuple[float, list]] = {}


def _as_date(value) -> Optional[str]:
    """Normalise an ISO timestamp or date to a plain YYYY-MM-DD string."""
    if not value:
        return None
    return str(value)[:10]


def fetch_kol_insights(
    base_url: str,
    org_id: str,
    api_key: str,
    branch_key: str,
    date_from,
    date_to,
) -> dict:
    """Reach / engagement totals for one branch over [date_from, date_to].

    Returns `{"available": False, ...}` whenever the Engine cannot answer —
    unreachable, unauthenticated, or simply carrying no scored posts in the
    window. The caller renders that as "not tracked" rather than as zero,
    because a zero here would read as "the KOLs got no views", which is a
    very different claim from "we have no numbers".
    """
    empty = {
        "available": False, "posts": 0, "reach": 0,
        "engagements": 0, "engagement_rate_pct": None,
    }
    if not api_key:
        return empty

    key = (base_url, org_id)
    now = time.time()
    hit = _kol_insights_cache.get(key)
    if hit and now - hit[0] < _KOL_INSIGHTS_TTL_SEC:
        records = hit[1]
    else:
        try:
            records = fetch_kol_data(base_url, org_id, api_key)
        except Exception as e:
            log.warning("KOL Engine insights unavailable: %s: %s", type(e).__name__, e)
            return empty
        _kol_insights_cache[key] = (now, records)

    d_from, d_to = date_from.isoformat(), date_to.isoformat()
    posts = reach = engagements = 0
    scored = False
    for r in records:
        if r.get("branch_key") != branch_key:
            continue
        published = _as_date(r.get("published_at"))
        if not published or not (d_from <= published <= d_to):
            continue
        posts += 1
        if r.get("total_reach") is not None or r.get("total_engagements") is not None:
            scored = True
        reach += int(r.get("total_reach") or 0)
        engagements += int(r.get("total_engagements") or 0)

    if not scored:
        # Collaborations published in the window exist, but none carry
        # performance numbers yet — still "no data", not "zero views".
        return {**empty, "posts": posts}

    return {
        "available": True,
        "posts": posts,
        "reach": reach,
        "engagements": engagements,
        "engagement_rate_pct": (
            round(engagements / reach * 100, 2) if reach > 0 else None
        ),
    }


# ── Public targets API ─────────────────────────────────────────────────────
# Endpoint: GET {base}/api/public/kol-targets/{slug}?year=YYYY&month=M
# Auth:     Authorization: Bearer <KOL_PUBLIC_API_KEY>
# Response: envelope {success, data, error, timestamp} where data has
#           {organization, period, totals, branches, monthly_targets}.
# Targets and actuals come back per metric: invited_proactive,
# collaborated, posted — each {actual, target, pct}.

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


_KOL_RES_IDS_TTL_SEC = 600
_kol_res_ids_cache: dict[tuple, tuple[float, list]] = {}


def fetch_kol_reservation_ids(
    base_url: str,
    org_slug: str,
    api_key: str,
    year: int,
    month: int,
    hotel_id: Optional[str] = None,
) -> Optional[list[str]]:
    """Fetch the list of Cloudbeds reservation IDs attributed to KOL for a month.

    Calls the KOL Engine /api/public/kol-reservations/:slug endpoint which applies
    the same filters as the Insights page (not cancelled, not ads-attributed).
    Returns a list of cloudbeds_reservation_id strings, or None on failure.
    """
    if not (base_url and org_slug and api_key):
        return None

    cache_key = (org_slug, int(year), int(month), hotel_id or "")
    cached = _kol_res_ids_cache.get(cache_key)
    if cached and (time.time() - cached[0]) < _KOL_RES_IDS_TTL_SEC:
        return cached[1]

    qs = f"year={year}&month={month}"
    if hotel_id:
        qs += f"&hotel_id={hotel_id}"
    url = f"{base_url}/api/public/kol-reservations/{org_slug}?{qs}"
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {api_key}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read())
    except Exception as e:
        log.warning("fetch_kol_reservation_ids: HTTP error %s", e)
        return None

    if not body.get("success"):
        log.warning("fetch_kol_reservation_ids: API error: %s", body.get("error"))
        return None

    ids = body.get("data", {}).get("reservation_ids") or []
    _kol_res_ids_cache[cache_key] = (time.time(), ids)
    log.info("fetch_kol_reservation_ids OK: %s %s/%s — %d IDs", org_slug, year, month, len(ids))
    return ids


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
