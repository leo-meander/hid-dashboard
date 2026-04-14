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
