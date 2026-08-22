"""Google PageSpeed Insights API — Avg Website Load Speed KPI (Paid Ads).

PSI is a live synthetic (Lighthouse) test, not a historical data source —
there is no way to ask it "what was March's number". So unlike GA4
purchase_cvr (re-queried live for any past month), this module's job is to
run the test once a month and persist the reading into ``page_speed_cache``;
``get_page_speed_actuals_yearly`` then just reads that table back, same
shape as every other actuals-yearly function team_kpi_service combines.

Metric: Speed Index (``audits.speed-index.numericValue``, ms → s), mobile
strategy — matches what Mason was reading off the PSI report UI by hand
before this was automated. Not a Core Web Vital in the strict sense (that
would be LCP), but it's the number this KPI's history was already built on.
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from typing import Optional

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.models.page_speed_cache import PageSpeedCache

log = logging.getLogger(__name__)

PSI_URL = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
DEFAULT_TIMEOUT = 60  # seconds — Lighthouse runs are slow

# Branch UUID ↔ short key (stable seed data, hardcoded — same convention as
# app.services.upstream_actuals.BRANCH_TO_KOL_HOTEL_ID).
BRANCH_KEY_TO_UUID: dict[str, str] = {
    "taipei": "11111111-1111-1111-1111-111111111101",
    "saigon": "11111111-1111-1111-1111-111111111102",
    "1948":   "11111111-1111-1111-1111-111111111103",
    "oani":   "11111111-1111-1111-1111-111111111104",
    "osaka":  "11111111-1111-1111-1111-111111111105",
}


def _upstream_reason(exc: Exception, resp: Optional[httpx.Response]) -> str:
    """Google's own words for why the call failed, not a generic message.

    PSI reports quota and key problems in a JSON ``error.message`` body. A
    caller staring at "Failed" cannot act; "Quota exceeded … limit 'Queries
    per day'" tells them exactly what to fix, so the upstream text is what
    gets carried back to the KPI grid.
    """
    detail = ""
    if resp is not None:
        try:
            detail = str(resp.json().get("error", {}).get("message") or "").strip()
        except Exception:
            detail = (resp.text or "").strip()[:200]
        detail = f"HTTP {resp.status_code}: {detail}" if detail else f"HTTP {resp.status_code}"
    else:
        detail = f"{type(exc).__name__}: {exc}"
    if resp is not None and resp.status_code == 429 and not settings.PAGESPEED_API_KEY:
        # Keyless PSI is quota 0 as of 2026 — the anonymous project's
        # "Queries per day" limit is literally zero, so every keyless call
        # 429s instantly. Nothing in the app can fix that; only a key can.
        detail += " — PAGESPEED_API_KEY is not set, and keyless PageSpeed Insights is rate-limited to zero queries per day"
    return detail


def fetch_speed_index(url: str) -> tuple[Optional[float], Optional[str]]:
    """Run a PageSpeed Insights (mobile) test against ``url``.

    Returns ``(speed_index_seconds, None)``, or ``(None, reason)`` on any
    failure — never raises, same convention as every other upstream API call
    in this codebase. The reason travels back to the caller because a failed
    PSI run is nearly always a config problem (missing key, exhausted quota)
    that only a human can clear, and they need to be told which one.
    """
    params = {"url": url, "strategy": "mobile", "category": "performance"}
    if settings.PAGESPEED_API_KEY:
        params["key"] = settings.PAGESPEED_API_KEY
    resp = None
    try:
        resp = httpx.get(PSI_URL, params=params, timeout=DEFAULT_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        ms = data["lighthouseResult"]["audits"]["speed-index"]["numericValue"]
        return round(float(ms) / 1000.0, 2), None
    except Exception as exc:
        reason = _upstream_reason(exc, resp if resp is not None and resp.is_error else None)
        log.error("PageSpeed Insights fetch failed for %s: %s", url, reason)
        return None, reason


def sync_page_speed(db: Session, year: Optional[int] = None, month: Optional[int] = None) -> dict:
    """Fetch Speed Index for every configured branch and upsert into page_speed_cache.

    Defaults to the current year/month — called monthly by GitHub Actions
    (POST /api/sync/page-speed), same trigger pattern as run-migrations and
    the marketing-activity cache refresh.
    """
    today = date.today()
    year = year or today.year
    month = month or today.month

    url_map = settings.pagespeed_url_map
    synced, errors = [], []

    # Lighthouse takes tens of seconds per URL, so the five branches run
    # together rather than end to end — sequential runs could exceed the
    # gateway timeout on the on-demand refresh path (POST /api/team-kpi/refresh).
    jobs = [(bk, url, BRANCH_KEY_TO_UUID[bk])
            for bk, url in url_map.items() if bk in BRANCH_KEY_TO_UUID]
    with ThreadPoolExecutor(max_workers=max(1, len(jobs))) as pool:
        results = list(pool.map(lambda job: fetch_speed_index(job[1]), jobs))

    for (branch_key, url, branch_uuid), (seconds, reason) in zip(jobs, results):
        if seconds is None:
            errors.append({"branch": branch_key, "url": url, "error": reason})
            continue

        row = (
            db.query(PageSpeedCache)
            .filter(
                PageSpeedCache.branch_id == branch_uuid,
                PageSpeedCache.year == year,
                PageSpeedCache.month == month,
            )
            .first()
        )
        if row is None:
            row = PageSpeedCache(branch_id=branch_uuid, year=year, month=month)
            db.add(row)
        row.speed_index_seconds = seconds
        row.strategy = "mobile"
        row.synced_at = datetime.now(timezone.utc)
        synced.append({"branch": branch_key, "speed_index_seconds": seconds})

    db.commit()
    # A sync that does not clear the read cache is invisible for up to 10 more
    # minutes — which is exactly the wait the on-demand refresh button exists
    # to remove.
    invalidate_page_speed_cache(year)
    return {"synced": synced, "errors": errors, "year": year, "month": month}


def page_speed_failure_detail(result: dict) -> str:
    """One line explaining why a sync recorded nothing, for an HTTP 502 detail.

    Every branch fails for the same reason (missing key, exhausted quota), so
    the distinct reasons are shown once each rather than repeated per branch.
    """
    errors = result.get("errors") or []
    if not errors:
        return "PageSpeed Insights recorded nothing — no branch URLs are configured"
    branches = ", ".join(e["branch"] for e in errors)
    reasons = list(dict.fromkeys(e.get("error") for e in errors if e.get("error")))
    why = f" — {'; '.join(reasons)}" if reasons else ""
    return f"PageSpeed Insights returned nothing for any branch ({branches}){why}"


_page_speed_cache: dict[int, tuple[float, dict]] = {}
_PAGE_SPEED_TTL = 600  # 10 min — mirrors _KOL_ACTUALS_TTL


def invalidate_page_speed_cache(year: Optional[int] = None) -> None:
    """Drop the read cache so the next read hits the table."""
    if year is None:
        _page_speed_cache.clear()
    else:
        _page_speed_cache.pop(year, None)


def get_page_speed_actuals_yearly(db: Session, year: int) -> dict[int, dict[str, dict]]:
    """Return {month: {branch_key: {page_load_speed: seconds}}} from the persisted cache.

    Months never synced (e.g. before this KPI was automated) are simply
    absent — team_kpi_service falls back to the manual actuals entry for
    those, same as every other auto KPI.
    """
    cached = _page_speed_cache.get(year)
    if cached and (time.time() - cached[0]) < _PAGE_SPEED_TTL:
        return cached[1]

    uuid_to_key = {v: k for k, v in BRANCH_KEY_TO_UUID.items()}
    out: dict[int, dict[str, dict]] = {}
    rows = db.query(PageSpeedCache).filter(PageSpeedCache.year == year).all()
    for row in rows:
        branch_key = uuid_to_key.get(str(row.branch_id))
        if not branch_key or row.speed_index_seconds is None:
            continue
        out.setdefault(row.month, {})[branch_key] = {
            "page_load_speed": float(row.speed_index_seconds),
        }

    _page_speed_cache[year] = (time.time(), out)
    return out
