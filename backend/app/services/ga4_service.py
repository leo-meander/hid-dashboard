"""GA4 Data API v1beta — property-level readings for the Team KPI grid.

One KPI reads this today: Purchase Conversion Rate (Paid Ads), which is GA4's
own ``userKeyEventRate:purchase`` — the column shown at Reports → Acquisition →
User acquisition → "User key event rate (purchase)".

Two properties of that metric shape everything in this module:

  * It is **user-scoped**. Unique users de-duplicate across time and across
    rows, so a month is NOT the average of its days and a total is NOT the sum
    of its rows. Every figure is therefore its own request with its own date
    range: months are never assembled from daily data, and a year-to-date
    number is a separate Jan-1 query rather than a roll-up of monthly cells.
  * Purchases fire on the Cloudbeds booking-engine domain, not on the branch's
    own site. ``hostName`` is event-scoped, so filtering on it would exclude
    the purchase events themselves and drive the rate toward zero. No
    ``dimensionFilter`` is ever sent — this metric only exists at whole-property
    scope, and one property cannot be split by host for it.

Auth is a Google service account (JSON key in ``GA4_SERVICE_ACCOUNT_JSON``)
granted the Viewer role on each property. No end-user OAuth flow: we sign a
JWT with the key and exchange it for an access token, same shape as the token
exchange in ``google_ads_service`` but with the ``jwt-bearer`` grant.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Optional

import httpx
import jwt

from app.config import settings

log = logging.getLogger(__name__)

BASE_URL = "https://analyticsdata.googleapis.com/v1beta"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/analytics.readonly"
JWT_BEARER_GRANT = "urn:ietf:params:oauth:grant-type:jwt-bearer"

DEFAULT_TIMEOUT = 30  # seconds

# Order matters — the response returns metricValues in the order requested.
# Only the first is displayed; the rest exist so the pipeline can self-verify
# (see _implied_denominator) and so a suspicious number can be debugged without
# re-querying.
PURCHASE_METRICS = (
    "userKeyEventRate:purchase",
    "totalUsers",
    "activeUsers",
    "keyEvents:purchase",
)


@dataclass
class Ga4PurchaseReading:
    """One property × one date range.

    ``rate_pct`` is the API's decimal rate scaled to HiD's 0–100 convention:
    ``"0.0163"`` → ``1.63``.
    """

    rate_pct: Optional[float]
    total_users: float
    active_users: float
    purchasing_users: float
    thresholded: bool


# ── Auth ─────────────────────────────────────────────────────────────────────

_token_cache: Optional[tuple[float, str]] = None  # (expires_at_epoch, token)


def _service_account_info() -> Optional[dict]:
    """Parse GA4_SERVICE_ACCOUNT_JSON. None (with a log line) when unusable."""
    raw = (settings.GA4_SERVICE_ACCOUNT_JSON or "").strip()
    if not raw:
        log.warning("GA4_SERVICE_ACCOUNT_JSON not configured; GA4 metrics blank")
        return None
    try:
        info = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.error("GA4_SERVICE_ACCOUNT_JSON is not valid JSON: %s", exc)
        return None
    if not info.get("client_email") or not info.get("private_key"):
        log.error("GA4_SERVICE_ACCOUNT_JSON is missing client_email/private_key")
        return None
    return info


def _access_token() -> Optional[str]:
    """Signed-JWT → access token, cached until a minute before it expires."""
    global _token_cache
    if _token_cache and _token_cache[0] > time.time():
        return _token_cache[1]

    info = _service_account_info()
    if not info:
        return None

    now = int(time.time())
    try:
        headers = {"kid": info["private_key_id"]} if info.get("private_key_id") else None
        assertion = jwt.encode(
            {
                "iss": info["client_email"],
                "scope": SCOPE,
                "aud": TOKEN_URL,
                "iat": now,
                "exp": now + 3600,
            },
            info["private_key"],
            algorithm="RS256",
            headers=headers,
        )
        with httpx.Client(timeout=DEFAULT_TIMEOUT) as client:
            resp = client.post(
                TOKEN_URL,
                data={"grant_type": JWT_BEARER_GRANT, "assertion": assertion},
            )
    except Exception as exc:
        log.error("GA4 token exchange error: %s", exc)
        return None

    if resp.status_code != 200:
        # A 400 "invalid_grant" here almost always means the service account
        # key was rotated or the clock is skewed, not a property permission
        # problem — those surface as 403 on runReport instead.
        log.error("GA4 token exchange failed status=%s: %s",
                  resp.status_code, resp.text[:300])
        return None

    body = resp.json()
    token = body.get("access_token")
    if not token:
        log.error("GA4 token exchange returned no access_token")
        return None
    _token_cache = (now + int(body.get("expires_in") or 3600) - 60, token)
    return token


def reset_token_cache() -> None:
    """Drop the cached access token (tests, and after a key rotation)."""
    global _token_cache
    _token_cache = None


# ── runReport ────────────────────────────────────────────────────────────────

def _as_float(raw) -> float:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def _implied_denominator(reading: Ga4PurchaseReading) -> Optional[str]:
    """Which user count actually reproduces the rate.

    GA4's docs do not spell out whether ``userKeyEventRate`` divides by
    totalUsers or activeUsers, and the two are usually close but not identical.
    Rather than guess, every reading checks both and reports what the response
    itself shows: ``"totalUsers"``, ``"activeUsers"``, ``"both"`` (the counts
    were too close to tell apart), or None when neither reproduces it — which
    normally means the response was thresholded.
    """
    if not reading.rate_pct or not reading.purchasing_users:
        return None
    fraction = reading.rate_pct / 100
    tolerance = max(1.0, reading.purchasing_users * 0.02)

    def reproduces(denominator: float) -> bool:
        return (
            denominator > 0
            and abs(fraction * denominator - reading.purchasing_users) <= tolerance
        )

    by_total = reproduces(reading.total_users)
    by_active = reproduces(reading.active_users)
    if by_total and by_active:
        return "both"
    if by_total:
        return "totalUsers"
    if by_active:
        return "activeUsers"
    return None


def run_purchase_report(
    property_id: str,
    start_date: str,
    end_date: str,
) -> Optional[Ga4PurchaseReading]:
    """One ``runReport`` for one property over one date range.

    Returns None on any failure or when the property reported no rows for the
    window — the caller renders a blank cell rather than a wrong 0%.

    The request carries no ``dimensions`` (the KPI is a single property-level
    number) and no ``dimensionFilter`` (see the module docstring).
    """
    token = _access_token()
    if not token:
        return None

    body = {
        "dateRanges": [{"startDate": start_date, "endDate": end_date}],
        "metrics": [{"name": name} for name in PURCHASE_METRICS],
    }
    url = f"{BASE_URL}/properties/{property_id}:runReport"
    try:
        with httpx.Client(timeout=DEFAULT_TIMEOUT) as client:
            resp = client.post(
                url,
                json=body,
                headers={"Authorization": f"Bearer {token}",
                         "Content-Type": "application/json"},
            )
    except Exception as exc:
        log.warning("GA4 runReport %s %s→%s failed: %s",
                    property_id, start_date, end_date, exc)
        return None

    if resp.status_code != 200:
        # 403 here = the service account is not a Viewer on this property.
        log.warning("GA4 runReport %s %s→%s HTTP %s: %s",
                    property_id, start_date, end_date, resp.status_code,
                    resp.text[:300])
        return None

    payload = resp.json()
    rows = payload.get("rows") or []
    if not rows:
        # No traffic in the window, or GA4 withheld everything. metadata
        # .emptyReason names which when it is set.
        empty = (payload.get("metadata") or {}).get("emptyReason")
        log.info("GA4 %s %s→%s returned no rows%s",
                 property_id, start_date, end_date,
                 f" ({empty})" if empty else "")
        return None

    values = [v.get("value") for v in (rows[0].get("metricValues") or [])]
    if len(values) < len(PURCHASE_METRICS):
        log.warning("GA4 %s %s→%s returned %d metrics, expected %d",
                    property_id, start_date, end_date,
                    len(values), len(PURCHASE_METRICS))
        return None

    raw_rate = values[0]
    reading = Ga4PurchaseReading(
        rate_pct=(round(_as_float(raw_rate) * 100, 2) if raw_rate is not None else None),
        total_users=_as_float(values[1]),
        active_users=_as_float(values[2]),
        purchasing_users=_as_float(values[3]),
        thresholded=bool((payload.get("metadata") or {}).get("subjectToThresholding")),
    )

    denominator = _implied_denominator(reading)
    if denominator is None and reading.rate_pct:
        log.warning(
            "GA4 %s %s→%s: rate %.2f%% reproduces neither totalUsers (%.0f) nor "
            "activeUsers (%.0f) against %.0f purchasing users — most likely "
            "thresholding (subjectToThresholding=%s)",
            property_id, start_date, end_date, reading.rate_pct,
            reading.total_users, reading.active_users,
            reading.purchasing_users, reading.thresholded,
        )
    else:
        log.info("GA4 %s %s→%s: %.2f%% (denominator=%s, thresholded=%s)",
                 property_id, start_date, end_date, reading.rate_pct or 0.0,
                 denominator, reading.thresholded)

    return reading


def describe_purchase_report(
    property_id: str,
    start_date: str,
    end_date: str,
) -> dict:
    """The same call, returned as a plain dict for the debug endpoint.

    Exists so the totalUsers-vs-activeUsers question in the feature request can
    be settled against the live property once, rather than guessed from docs.
    """
    reading = run_purchase_report(property_id, start_date, end_date)
    if reading is None:
        return {
            "property_id": property_id,
            "start_date": start_date,
            "end_date": end_date,
            "reading": None,
        }
    return {
        "property_id": property_id,
        "start_date": start_date,
        "end_date": end_date,
        "reading": {
            "rate_pct": reading.rate_pct,
            "total_users": reading.total_users,
            "active_users": reading.active_users,
            "purchasing_users": reading.purchasing_users,
            "thresholded": reading.thresholded,
        },
        # round(rate × denominator) vs the reported purchasing-user count —
        # whichever lands on it is the real denominator.
        "implied_purchasers_from_total_users": round(
            (reading.rate_pct or 0) / 100 * reading.total_users, 1
        ),
        "implied_purchasers_from_active_users": round(
            (reading.rate_pct or 0) / 100 * reading.active_users, 1
        ),
        "denominator": _implied_denominator(reading),
    }
