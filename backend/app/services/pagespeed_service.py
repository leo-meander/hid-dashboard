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


def fetch_speed_index(url: str) -> Optional[float]:
    """Run a PageSpeed Insights (mobile) test against ``url``.

    Returns Speed Index in seconds, or None on any failure — never raises,
    same convention as every other upstream API call in this codebase.
    """
    params = {"url": url, "strategy": "mobile", "category": "performance"}
    if settings.PAGESPEED_API_KEY:
        params["key"] = settings.PAGESPEED_API_KEY
    try:
        resp = httpx.get(PSI_URL, params=params, timeout=DEFAULT_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        ms = data["lighthouseResult"]["audits"]["speed-index"]["numericValue"]
        return round(float(ms) / 1000.0, 2)
    except Exception as exc:
        log.error("PageSpeed Insights fetch failed for %s: %s", url, exc)
        return None


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

    for branch_key, url in url_map.items():
        branch_uuid = BRANCH_KEY_TO_UUID.get(branch_key)
        if not branch_uuid:
            continue
        seconds = fetch_speed_index(url)
        if seconds is None:
            errors.append({"branch": branch_key, "url": url})
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
    return {"synced": synced, "errors": errors}


_page_speed_cache: dict[int, tuple[float, dict]] = {}
_PAGE_SPEED_TTL = 600  # 10 min — mirrors _KOL_ACTUALS_TTL


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
