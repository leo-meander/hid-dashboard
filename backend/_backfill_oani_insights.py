"""One-off backfill: refresh Cloudbeds Insights overlay for Oani 2025+2026.

Why
----
Oani's Cloudbeds API key only got the Insights:Create Reports permission on
2026-05-12. Before that, `fetch_cloudbeds_occupancy` failed with 403 on POST
/reports and (per the now-removed fallback) returned data only from stock
report 110 — a fixed ~95-day window around "now". As a result, Oani's
`daily_metrics` rows for any month outside that rolling window are stale,
empty, or missing entirely.

This script triggers the same `/api/sync/insights` endpoint the production
cron uses (with `insights_only=true` so it skips reservation backfill /
proration / revenue refresh — those have been current all along; only the
Insights overlay was broken), but scopes it to Oani via `branch_id` and runs
once per year (2025, 2026) to cover the historical window.

Usage
-----
1. Set env vars: BACKEND_URL (https://meander-hid-dashboard.zeabur.app) and
   SYNC_TRIGGER_TOKEN (same value Zeabur uses).
2. Run: `python backend/_backfill_oani_insights.py`
3. Check Zeabur logs for "Insights sync START branch=MEANDER Oani" lines.
4. Verify in /performance-monthly (Oani filter) — Jan–Apr 2025 rows should
   populate with real revenue/OCC/ADR from Cloudbeds Insights.

Idempotent: re-running just upserts the same daily_metrics rows. Safe to
re-run if Zeabur swaps the container mid-flight (BackgroundTasks die, but
the next invocation picks up where it left off).
"""
import os
import sys
import time

import httpx


OANI_BRANCH_ID = "11111111-1111-1111-1111-111111111104"
YEARS = (2025, 2026)


def main() -> int:
    backend = os.environ.get("BACKEND_URL", "").rstrip("/")
    token = os.environ.get("SYNC_TRIGGER_TOKEN", "")
    if not backend:
        print("ERROR: set BACKEND_URL (e.g. https://meander-hid-dashboard.zeabur.app)")
        return 1
    if not token:
        print("ERROR: set SYNC_TRIGGER_TOKEN (Zeabur production value)")
        return 1

    headers = {"X-Sync-Token": token}
    with httpx.Client(timeout=300) as client:
        for year in YEARS:
            url = (
                f"{backend}/api/sync/insights"
                f"?insights_only=true&year={year}&branch_id={OANI_BRANCH_ID}"
            )
            t = time.time()
            print(f"[Oani {year}] POST {url} ...", flush=True)
            resp = client.post(url, headers=headers)
            elapsed = time.time() - t
            print(
                f"[Oani {year}] HTTP {resp.status_code} in {elapsed:.1f}s — "
                f"{resp.text[:200]}",
                flush=True,
            )
            if resp.status_code != 200:
                return 2
            # Endpoint returns 200 as soon as background task is queued; the
            # actual Insights API roundtrip takes 30–90 s per year. Let it
            # finish before kicking off the next year to avoid hammering
            # Cloudbeds rate limits.
            time.sleep(120 if year < max(YEARS) else 0)

    print("All Oani Insights backfills queued.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
