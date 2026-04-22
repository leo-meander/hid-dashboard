"""Ads Platform client — unified source for Meta / Google / TikTok ad data.

Replaces the legacy Meta Graph + Google Sheets integrations. All reads go
through the Meander-internal aggregator at ``settings.ADS_PLATFORM_BASE_URL``
using the ``X-API-Key`` header. Rate limit: 1000 req/day/key, so callers
should batch.

All methods are synchronous (they run on APScheduler's threadpool executor
alongside the other sync services). Paginated endpoints expose generators
so callers don't have to think about offset math.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Iterator, Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 60  # seconds
MAX_RETRIES = 3
RETRY_STATUSES = {429, 500, 502, 503, 504}


# ── Platform / channel mapping ──────────────────────────────────────────────

_PLATFORM_TO_CHANNEL = {"meta": "Meta", "google": "Google", "tiktok": "TikTok"}
_CHANNEL_TO_PLATFORM = {v: k for k, v in _PLATFORM_TO_CHANNEL.items()}


def platform_to_channel(platform: Optional[str]) -> Optional[str]:
    if not platform:
        return None
    return _PLATFORM_TO_CHANNEL.get(platform.lower(), platform.title())


def channel_to_platform(channel: Optional[str]) -> Optional[str]:
    if not channel:
        return None
    return _CHANNEL_TO_PLATFORM.get(channel, channel.lower())


def branch_slug_for(branch) -> str:
    """Return the Ads Platform slug for a local ``Branch`` row.

    Prefers the explicit ``branches.ads_platform_slug`` (backfilled by
    migration 028). Falls back to a normalised branch name so newly created
    branches still work until someone sets the column.
    """
    slug = getattr(branch, "ads_platform_slug", None)
    if slug:
        return slug
    name = (branch.name or "").lower().replace("meander ", "").strip()
    return name.replace(" ", "")


# ── Client ──────────────────────────────────────────────────────────────────

class AdsPlatformError(RuntimeError):
    """Raised when the Ads Platform returns a non-retriable error."""


class AdsPlatformClient:
    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout: int = DEFAULT_TIMEOUT,
    ):
        self.base_url = (base_url or settings.ADS_PLATFORM_BASE_URL).rstrip("/")
        self.api_key = api_key or settings.ADS_PLATFORM_API_KEY
        self.timeout = timeout
        if not self.api_key:
            logger.warning("ADS_PLATFORM_API_KEY is empty — every request will 401")

    # -- Internal HTTP --------------------------------------------------------

    def _headers(self) -> dict:
        return {"X-API-Key": self.api_key, "Accept": "application/json"}

    def _get(self, path: str, params: Optional[dict] = None) -> Any:
        """GET ``path`` with retry on 429/5xx and exponential backoff.

        Returns the parsed JSON ``data`` field if the response uses the
        ``{success, data, error, timestamp}`` envelope; otherwise the raw
        parsed body.
        """
        url = f"{self.base_url}{path}"
        clean_params = {k: v for k, v in (params or {}).items() if v is not None}

        delay = 1.0
        last_exc: Optional[Exception] = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                with httpx.Client(timeout=self.timeout) as client:
                    resp = client.get(url, params=clean_params, headers=self._headers())
                if resp.status_code in RETRY_STATUSES:
                    retry_after = resp.headers.get("Retry-After")
                    wait = float(retry_after) if retry_after else delay
                    logger.warning(
                        "Ads Platform %s returned %s; retrying in %.1fs (attempt %s/%s)",
                        path, resp.status_code, wait, attempt, MAX_RETRIES,
                    )
                    time.sleep(wait)
                    delay *= 2
                    continue
                resp.raise_for_status()
                body = resp.json()
                if isinstance(body, dict) and "data" in body and "success" in body:
                    if not body.get("success", False):
                        raise AdsPlatformError(
                            f"{path} returned success=false: {body.get('error')}"
                        )
                    return body["data"]
                return body
            except httpx.HTTPError as exc:
                last_exc = exc
                logger.warning(
                    "Ads Platform %s failed (attempt %s/%s): %s",
                    path, attempt, MAX_RETRIES, exc,
                )
                time.sleep(delay)
                delay *= 2
        raise AdsPlatformError(
            f"Ads Platform GET {path} failed after {MAX_RETRIES} attempts: {last_exc}"
        )

    def _paginate(
        self,
        path: str,
        params: Optional[dict] = None,
        page_size: int = 200,
    ) -> Iterator[dict]:
        """Yield each item from a paginated ``/api/export/*`` endpoint.

        The platform uses the envelope ``{data: {items, total, limit, offset}}``.
        Stops when ``len(items) < page_size`` (covers both end-of-list and
        total-reached cases).
        """
        offset = 0
        params = dict(params or {})
        while True:
            params.update({"limit": page_size, "offset": offset})
            data = self._get(path, params=params)
            if isinstance(data, dict) and "items" in data:
                items = data["items"] or []
            elif isinstance(data, list):
                items = data
            else:
                items = []
            for item in items:
                yield item
            if len(items) < page_size:
                break
            offset += page_size

    # -- Metrics --------------------------------------------------------------

    def get_spend_daily(
        self,
        date_from: str,
        date_to: str,
        *,
        platform: Optional[str] = None,
        branch: Optional[str] = None,
    ) -> list[dict]:
        """Daily aggregate: ``{date, platform, spend, impressions, clicks,
        conversions, revenue}`` (one row per date × platform × branch)."""
        return self._get(
            "/api/export/spend/daily",
            params={
                "date_from": date_from, "date_to": date_to,
                "platform": platform, "branch": branch,
            },
        ) or []

    def get_booking_matches(
        self,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        *,
        branch: Optional[str] = None,
        channel: Optional[str] = None,
        match_result: Optional[str] = None,
        purchase_kind: Optional[str] = None,
    ) -> Iterator[dict]:
        return self._paginate(
            "/api/export/booking-matches",
            params={
                "date_from": date_from, "date_to": date_to,
                "branch": branch, "channel": channel,
                "match_result": match_result, "purchase_kind": purchase_kind,
            },
        )

    def get_booking_matches_summary(
        self,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        *,
        branch: Optional[str] = None,
    ) -> dict:
        return self._get(
            "/api/export/booking-matches/summary",
            params={"date_from": date_from, "date_to": date_to, "branch": branch},
        ) or {}

    # -- Planning -------------------------------------------------------------

    def get_budget_monthly(self, month: Optional[str] = None) -> dict:
        """Returns ``{month, plans: [...]}`` for the given YYYY-MM."""
        return self._get(
            "/api/export/budget/monthly",
            params={"month": month},
        ) or {}

    # -- Reference data (commit 342bb52) --------------------------------------

    def get_accounts(self) -> list[dict]:
        data = self._get("/api/export/accounts")
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and "items" in data:
            return data["items"] or []
        return []

    def get_countries(self) -> list[dict]:
        data = self._get("/api/export/countries")
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and "items" in data:
            return data["items"] or []
        return []

    # -- Campaign tree (commit 342bb52) ---------------------------------------

    def get_campaigns(
        self,
        *,
        platform: Optional[str] = None,
        status: Optional[str] = None,
        account_id: Optional[str] = None,
        search: Optional[str] = None,
    ) -> Iterator[dict]:
        return self._paginate(
            "/api/export/campaigns",
            params={"platform": platform, "status": status,
                    "account_id": account_id, "search": search},
        )

    def get_ads(self, *, campaign_id: Optional[str] = None) -> Iterator[dict]:
        return self._paginate(
            "/api/export/ads",
            params={"campaign_id": campaign_id},
        )

    # -- Creative library (commit 342bb52) ------------------------------------

    def get_angles(
        self,
        *,
        branch_id: Optional[str] = None,
        status: Optional[str] = None,
    ) -> list[dict]:
        data = self._get(
            "/api/export/angles",
            params={"branch_id": branch_id, "status": status},
        )
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and "items" in data:
            return data["items"] or []
        return []

    def get_keypoints(
        self,
        *,
        branch_id: Optional[str] = None,
        category: Optional[str] = None,
    ) -> list[dict]:
        data = self._get(
            "/api/export/keypoints",
            params={"branch_id": branch_id, "category": category},
        )
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and "items" in data:
            return data["items"] or []
        return []

    def get_copies(
        self,
        *,
        branch_id: Optional[str] = None,
        target_audience: Optional[str] = None,
    ) -> Iterator[dict]:
        return self._paginate(
            "/api/export/copies",
            params={"branch_id": branch_id, "target_audience": target_audience},
        )

    def get_materials(
        self,
        *,
        branch_id: Optional[str] = None,
        material_type: Optional[str] = None,
    ) -> Iterator[dict]:
        return self._paginate(
            "/api/export/materials",
            params={"branch_id": branch_id, "material_type": material_type},
        )

    def get_combos(
        self,
        *,
        branch_id: Optional[str] = None,
        verdict: Optional[str] = None,
        target_audience: Optional[str] = None,
        country: Optional[str] = None,
        sort_by: Optional[str] = None,
        sort_dir: Optional[str] = None,
    ) -> Iterator[dict]:
        return self._paginate(
            "/api/export/combos",
            params={"branch_id": branch_id, "verdict": verdict,
                    "target_audience": target_audience, "country": country,
                    "sort_by": sort_by, "sort_dir": sort_dir},
        )

    def get_spy_ads(
        self,
        *,
        collection: Optional[str] = None,
        tags: Optional[str] = None,
        page_id: Optional[str] = None,
        country: Optional[str] = None,
        sort_by: Optional[str] = None,
        sort_dir: Optional[str] = None,
    ) -> Iterator[dict]:
        return self._paginate(
            "/api/export/spy-ads",
            params={"collection": collection, "tags": tags,
                    "page_id": page_id, "country": country,
                    "sort_by": sort_by, "sort_dir": sort_dir},
        )


def get_client() -> AdsPlatformClient:
    return AdsPlatformClient()
