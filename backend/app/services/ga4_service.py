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

import base64
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
    # The same rate unrounded. Kept because rounding to 2dp destroys the only
    # signal that distinguishes the two candidate denominators (see
    # _implied_denominator) — they differ by well under a percentage point.
    rate_raw: Optional[float]
    total_users: float
    active_users: float
    # keyEvents:purchase counts EVENTS, not people. One guest booking twice is
    # two events and one purchasing user, so this is never the numerator of a
    # user-scoped rate.
    purchase_events: float
    thresholded: bool


# ── Auth ─────────────────────────────────────────────────────────────────────

_token_cache: Optional[tuple[float, str]] = None  # (expires_at_epoch, token)


def _configured_key() -> tuple[str, str]:
    """The service-account key as stored, plus the env var it came from.

    ``GA4_SERVICE_ACCOUNT_JSON_B64`` is the one to set on Zeabur: the key
    file's private_key carries literal newlines that the env-var text box does
    not preserve, so the raw JSON arrives corrupted. The raw variant remains a
    fallback for environments that handle newlines, such as a local .env.
    """
    b64 = (settings.GA4_SERVICE_ACCOUNT_JSON_B64 or "").strip()
    if b64:
        return b64, "GA4_SERVICE_ACCOUNT_JSON_B64"
    return (settings.GA4_SERVICE_ACCOUNT_JSON or "").strip(), "GA4_SERVICE_ACCOUNT_JSON"


def credentials_configured() -> bool:
    """Whether a service-account key is present at all.

    Callers fanning out over many (property × month) windows check this once
    up front: without a key every one of those requests would fail identically,
    and each would log its own line.
    """
    return bool(_configured_key()[0])


def _service_account_info() -> tuple[Optional[dict], Optional[str]]:
    """Parse GA4_SERVICE_ACCOUNT_JSON into ``(info, why_not)``.

    Failure reasons are returned rather than only logged: they are what a
    blank KPI cell actually means, and reading them off a debug response beats
    going to hunt for the log line.
    """
    raw, var = _configured_key()
    raw = raw.lstrip("﻿").strip()
    if not raw:
        return None, ("neither GA4_SERVICE_ACCOUNT_JSON_B64 nor "
                      "GA4_SERVICE_ACCOUNT_JSON is set")

    # Either variable accepts either format, so setting the wrong one degrades
    # to working rather than to a blank grid. Two more manglings are absorbed
    # here as well: a UTF-8 BOM survives .strip(), and some dashboards store
    # the value wrapped in quotes.
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("'", '"'):
        raw = raw[1:-1].strip()
    if not raw.startswith("{"):
        try:
            raw = base64.b64decode(raw, validate=True).decode("utf-8").strip()
        except Exception:
            pass  # not base64 either; the JSON error below says what it is

    try:
        info = json.loads(raw)
    except json.JSONDecodeError as exc:
        # Deliberately describes the value without quoting it — this travels to
        # an unauthenticated debug endpoint.
        return None, (
            f"{var} is not valid JSON ({exc}). The value is {len(raw)} "
            f"characters and begins with {raw[:1]!r}; a key file begins with "
            "'{'. Set GA4_SERVICE_ACCOUNT_JSON_B64 to the output of "
            "`base64 -w 0 your-service-account.json` — the raw JSON's "
            "private_key contains newlines the env-var box does not preserve."
        )
    if not isinstance(info, dict):
        return None, f"{var} is not a JSON object"
    missing = [f for f in ("client_email", "private_key") if not info.get(f)]
    if missing:
        return None, f"{var} is missing {', '.join(missing)}"
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
    """Which user count GA4 divided by: ``totalUsers`` or ``activeUsers``.

    Google's docs do not say, and the two counts sit within a percent or two of
    each other, so the answer has to be read off the numbers themselves.

    It cannot be read off ``keyEvents:purchase``: that counts events, and one
    guest booking twice is two events but one purchasing user. Comparing a rate
    numerator against it is a category error — an earlier version of this
    function made exactly that mistake and reported every healthy reading as
    unverifiable.

    What does work is integrality. The numerator is a whole number of people,
    so ``rate × real_denominator`` lands on an integer while the same rate
    against the wrong denominator generally does not. Returns None when both
    land equally well (the counts were too close to separate) or neither does
    (usually thresholding).
    """
    if not reading.rate_raw or reading.rate_raw <= 0:
        return None

    def distance_from_whole(denominator: float) -> Optional[float]:
        if denominator <= 0:
            return None
        implied = reading.rate_raw * denominator
        return abs(implied - round(implied))

    by_total = distance_from_whole(reading.total_users)
    by_active = distance_from_whole(reading.active_users)
    if by_total is None or by_active is None:
        return None
    # GA4 returns the rate to ~9 significant figures, so the true denominator
    # lands far closer to whole than chance would allow.
    tolerance = 0.01
    total_fits, active_fits = by_total <= tolerance, by_active <= tolerance
    if total_fits and active_fits:
        return "both"
    if total_fits:
        return "totalUsers"
    if active_fits:
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
        rate_raw=(_as_float(raw_rate) if raw_rate is not None else None),
        total_users=_as_float(values[1]),
        active_users=_as_float(values[2]),
        purchase_events=_as_float(values[3]),
        thresholded=bool((payload.get("metadata") or {}).get("subjectToThresholding")),
    )

    log.info(
        "GA4 %s %s→%s: %.2f%% of %.0f users (denominator=%s), %.0f purchase "
        "events, thresholded=%s",
        property_id, start_date, end_date, reading.rate_pct or 0.0,
        reading.total_users, _implied_denominator(reading) or "undetermined",
        reading.purchase_events, reading.thresholded,
    )

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
            "rate_raw": reading.rate_raw,
            "total_users": reading.total_users,
            "active_users": reading.active_users,
            "purchase_events": reading.purchase_events,
            "thresholded": reading.thresholded,
        },
        # rate × denominator is a whole number of people for the real
        # denominator. Not comparable to purchase_events — that counts events,
        # and one guest can book twice.
        "implied_purchasing_users_from_total_users": round(
            (reading.rate_raw or 0) * reading.total_users, 4
        ),
        "implied_purchasing_users_from_active_users": round(
            (reading.rate_raw or 0) * reading.active_users, 4
        ),
        "denominator": _implied_denominator(reading),
        "purchase_events_per_purchasing_user": (
            round(reading.purchase_events / ((reading.rate_raw or 0) * reading.total_users), 2)
            if reading.rate_raw and reading.total_users else None
        ),
    }
