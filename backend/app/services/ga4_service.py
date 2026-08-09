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


def credentials_configured() -> bool:
    """Whether a service-account key is present at all.

    Callers fanning out over many (property × month) windows check this once
    up front: without a key every one of those requests would fail identically,
    and each would log its own line.
    """
    return bool((settings.GA4_SERVICE_ACCOUNT_JSON or "").strip())


def _service_account_info() -> tuple[Optional[dict], Optional[str]]:
    """Parse GA4_SERVICE_ACCOUNT_JSON into ``(info, why_not)``.

    Failure reasons are returned rather than only logged: they are what a
    blank KPI cell actually means, and reading them off a debug response beats
    going to hunt for the log line.
    """
    raw = (settings.GA4_SERVICE_ACCOUNT_JSON or "").strip()
    if not raw:
        return None, "GA4_SERVICE_ACCOUNT_JSON is not set"
    try:
        info = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, f"GA4_SERVICE_ACCOUNT_JSON is not valid JSON: {exc}"
    if not isinstance(info, dict):
        return None, "GA4_SERVICE_ACCOUNT_JSON is not a JSON object"
    missing = [f for f in ("client_email", "private_key") if not info.get(f)]
    if missing:
        return None, f"GA4_SERVICE_ACCOUNT_JSON is missing {', '.join(missing)}"
    return info, None


def _access_token() -> tuple[Optional[str], Optional[str]]:
    """Signed-JWT → access token as ``(token, why_not)``, cached to expiry."""
    global _token_cache
    if _token_cache and _token_cache[0] > time.time():
        return _token_cache[1], None

    info, why = _service_account_info()
    if not info:
        return None, why

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
    except Exception as exc:
        # A NotImplementedError about RS256 here means `cryptography` did not
        # install — PyJWT alone cannot sign the assertion.
        return None, f"could not sign the JWT assertion ({type(exc).__name__}: {exc})"

    try:
        with httpx.Client(timeout=DEFAULT_TIMEOUT) as client:
            resp = client.post(
                TOKEN_URL,
                data={"grant_type": JWT_BEARER_GRANT, "assertion": assertion},
            )
    except Exception as exc:
        return None, f"token endpoint unreachable ({exc})"

    if resp.status_code != 200:
        # "invalid_grant" here almost always means the key was rotated or the
        # clock is skewed — a property permission problem surfaces as a 403 on
        # runReport instead.
        return None, f"token exchange HTTP {resp.status_code}: {resp.text[:300]}"

    body = resp.json()
    token = body.get("access_token")
    if not token:
        return None, "token exchange returned no access_token"
    _token_cache = (now + int(body.get("expires_in") or 3600) - 60, token)
    return token, None


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


def _run_purchase_report(
    property_id: str,
    start_date: str,
    end_date: str,
) -> tuple[Optional[Ga4PurchaseReading], Optional[str]]:
    """One ``runReport``, returned as ``(reading, why_not)``.

    Every path that yields a blank cell names itself, so a caller can tell
    "no Viewer on this property" apart from "no key configured" and from "the
    property genuinely had no traffic in this window".

    The request carries no ``dimensions`` (the KPI is a single property-level
    number) and no ``dimensionFilter`` (see the module docstring).
    """
    token, why = _access_token()
    if not token:
        return None, why

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
        return None, f"runReport unreachable ({exc})"

    if resp.status_code == 403:
        return None, (
            f"HTTP 403 — the service account is probably not a Viewer on "
            f"property {property_id}: {resp.text[:250]}"
        )
    if resp.status_code != 200:
        return None, f"runReport HTTP {resp.status_code}: {resp.text[:250]}"

    payload = resp.json()
    rows = payload.get("rows") or []
    if not rows:
        # No traffic in the window, or GA4 withheld everything.
        empty = (payload.get("metadata") or {}).get("emptyReason")
        return None, f"no rows returned{f' ({empty})' if empty else ''}"

    values = [v.get("value") for v in (rows[0].get("metricValues") or [])]
    if len(values) < len(PURCHASE_METRICS):
        return None, (f"returned {len(values)} metrics, expected "
                      f"{len(PURCHASE_METRICS)}")

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

    return reading, None


def run_purchase_report(
    property_id: str,
    start_date: str,
    end_date: str,
) -> Optional[Ga4PurchaseReading]:
    """The reading for one property over one window, or None on any failure.

    The grid renders a blank cell rather than a wrong 0%; the reason goes to
    the log, and to :func:`describe_purchase_report` for the debug endpoint.
    """
    reading, why = _run_purchase_report(property_id, start_date, end_date)
    if why:
        log.warning("GA4 %s %s→%s: %s", property_id, start_date, end_date, why)
    return reading


def describe_purchase_report(
    property_id: str,
    start_date: str,
    end_date: str,
) -> dict:
    """The same call, returned as a plain dict for the debug endpoint.

    Carries the failure reason so a blank cell can be diagnosed from the
    response, and exists so the totalUsers-vs-activeUsers question in the
    feature request can be settled against the live property once rather than
    guessed from docs.
    """
    reading, why = _run_purchase_report(property_id, start_date, end_date)
    if reading is None:
        return {
            "property_id": property_id,
            "start_date": start_date,
            "end_date": end_date,
            "reading": None,
            "error": why,
        }
    return {
        "property_id": property_id,
        "start_date": start_date,
        "end_date": end_date,
        "error": None,
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
