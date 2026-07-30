"""
Google Ads offline conversion upload service — Data Manager API.

Replaces the Google Ads API `uploadClickConversions` path, which Google blocked
for offline conversion imports on 2026-06-15. Uploads now go to the Data Manager
API (`datamanager.googleapis.com/v1/events:ingest`).

Three routing cases (unchanged — mirrors the original Make.com router):
  A. Email only (phone absent OR email is N/A)  → conversion_action_single
  B. Phone only (email absent OR email is N/A)  → conversion_action_phone
  C. Both email + phone                          → conversion_action_both

The per-branch conversion action IDs are reused verbatim: in Data Manager terms
the conversion action ID is the destination's `productDestinationId`, and it must
be a Google Ads conversion action of type UPLOAD_CLICKS (same actions as before).

Two things differ from the old service and are easy to miss:
  - OAuth scope is `https://www.googleapis.com/auth/datamanager`, NOT `adwords`.
    A refresh token minted for the Ads API will not work here.
  - Phone numbers must be normalized to E.164 *before* hashing. The old service
    stripped the leading "+", which is not valid E.164.
"""
import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from app.services.phone_utils import normalize_e164

logger = logging.getLogger(__name__)

DATA_MANAGER_INGEST_URL = "https://datamanager.googleapis.com/v1/events:ingest"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
DATA_MANAGER_SCOPE = "https://www.googleapis.com/auth/datamanager"


def _sha256(value: Optional[str]) -> Optional[str]:
    """SHA-256 of a normalized value, hex-encoded (request sets encoding=HEX)."""
    if not value or not str(value).strip():
        return None
    return hashlib.sha256(value.strip().lower().encode()).hexdigest()


def _get_access_token(
    client_id: str,
    client_secret: str,
    refresh_token: str,
) -> Optional[str]:
    """
    Exchange refresh token for an access token.

    The refresh token must have been granted the Data Manager scope; a token
    minted for the Ads API returns 200 here but 403 on ingest.
    """
    try:
        with httpx.Client(timeout=15) as client:
            resp = client.post(
                GOOGLE_TOKEN_URL,
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                },
            )
            if resp.status_code == 200:
                return resp.json().get("access_token")
            logger.error("Google token refresh failed status=%d: %s", resp.status_code, resp.text[:200])
            return None
    except Exception as e:
        logger.error("Google token refresh error: %s", e)
        return None


def _parse_conversion_time(
    date_created: Optional[str],
    tz_offset_hours: int = 8,
    extra_offset_hours: int = 1,
) -> Optional[str]:
    """
    Convert Cloudbeds dateCreated to a Data Manager RFC 3339 UTC timestamp.

    Mirrors Make formula: addHours(parseDate(date, branch_timezone), -extra_offset)
      - tz_offset_hours: UTC offset of branch local time (8=Taipei, 9=Tokyo, 7=Saigon)
      - extra_offset_hours: additional hours subtracted (Make's addHours value)

    Only the output format changed from the Ads API version: "2026-07-30T04:21:00Z"
    instead of "2026-07-30 04:21:00+00:00".
    """
    if not date_created:
        return None
    try:
        local_dt = datetime.strptime(date_created[:16], "%Y-%m-%d %H:%M")
        utc_dt = local_dt - timedelta(hours=tz_offset_hours) - timedelta(hours=extra_offset_hours)
        utc_dt = utc_dt.replace(tzinfo=timezone.utc)
        return utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, TypeError):
        return None


def _describe_error(result: dict) -> Optional[str]:
    """
    Flatten a Google API error envelope into one readable line.

    The top-level `message` for a rejected ingest is the generic "There was a
    problem with the request." — everything actionable (which field, which
    account, which permission) lives in `error.details[]`, so surface that too
    or the webhook log says nothing useful.
    """
    err = (result or {}).get("error") or {}
    parts = [err.get("message")] if err.get("message") else []

    for detail in err.get("details") or []:
        for violation in detail.get("fieldViolations") or []:
            field = violation.get("field") or "?"
            parts.append(f"{field}: {violation.get('description')}")
        if detail.get("reason"):
            metadata = detail.get("metadata") or {}
            meta_str = " ".join(f"{k}={v}" for k, v in metadata.items())
            parts.append(f"{detail['reason']} {meta_str}".strip())
        if detail.get("errors"):
            for sub in detail["errors"]:
                parts.append(str(sub.get("message") or sub))

    return " | ".join(p for p in parts if p) or None


def _first_guest(guest_list: dict) -> dict:
    if not guest_list or not isinstance(guest_list, dict):
        return {}
    first_key = next(iter(guest_list), None)
    return guest_list.get(first_key, {}) if first_key else {}


def upload_offline_conversion(
    reservation: dict,
    customer_id: str,
    client_id: str,
    client_secret: str,
    refresh_token: str,
    conversion_action_single: str,
    conversion_action_both: str,
    login_customer_id: str = "",
    currency: str = "TWD",
    tz_offset_hours: int = 8,
    event_time_extra_offset: int = 1,
    conversion_action_phone: str = "",
    phone_country_code: str = "886",
    validate_only: bool = False,
) -> dict:
    """
    Send one reservation to Google Ads as an offline conversion via Data Manager.

    Routing (unchanged from the Make.com flow):
      - email only → conversion_action_single
      - phone only → conversion_action_phone (falls back to conversion_action_single)
      - both       → conversion_action_both

    Set validate_only=True to have Google check the payload without recording a
    conversion — useful for verifying credentials after a token rotation.

    Returns {"success": bool, "case": str, "response": dict}.
    """
    email = (reservation.get("guestEmail") or "").strip()
    guest_list = reservation.get("guestList") or {}
    guest = _first_guest(guest_list)
    phone = normalize_e164(guest.get("guestPhone", ""), phone_country_code)

    email_invalid = not email or "N/A" in email.upper()
    phone_present = bool(phone)

    if email_invalid and not phone_present:
        logger.info(
            "Google Ads: skipping reservation=%s — no email or phone",
            reservation.get("reservationID"),
        )
        return {"success": False, "case": "skipped_no_identifiers"}

    conversion_time = _parse_conversion_time(
        reservation.get("dateCreated"),
        tz_offset_hours=tz_offset_hours,
        extra_offset_hours=event_time_extra_offset,
    )
    if not conversion_time:
        return {"success": False, "error": "invalid_conversion_time"}

    try:
        value = float(reservation.get("total") or 0)
    except (TypeError, ValueError):
        value = 0.0

    order_id = str(reservation.get("reservationID", ""))
    customer_id_clean = customer_id.replace("-", "")

    # conversion_action_phone falls back to conversion_action_single when not set
    phone_action = conversion_action_phone or conversion_action_single

    # Determine which conversion action and which user identifiers to use
    if phone_present and not email_invalid:
        # Case C: both
        case = "both"
        action_id = conversion_action_both
        user_identifiers = [
            {"emailAddress": _sha256(email)},
            {"phoneNumber": _sha256(phone)},
        ]
    elif phone_present and email_invalid:
        # Case B: phone only
        case = "phone_only"
        action_id = phone_action
        user_identifiers = [{"phoneNumber": _sha256(phone)}]
    else:
        # Case A: email only
        case = "email_only"
        action_id = conversion_action_single
        user_identifiers = [{"emailAddress": _sha256(email)}]

    if not action_id:
        logger.warning(
            "Google Ads: no conversion action configured reservation=%s case=%s",
            order_id, case,
        )
        return {"success": False, "case": case, "error": "no_conversion_action"}

    access_token = _get_access_token(client_id, client_secret, refresh_token)
    if not access_token:
        return {"success": False, "case": case, "error": "token_refresh_failed"}

    destination = {
        "operatingAccount": {
            "accountType": "GOOGLE_ADS",
            "accountId": customer_id_clean,
        },
        "productDestinationId": str(action_id),
    }
    # MCC manager account — required when OAuth is authenticated as the MCC and
    # the operating account is a sub-account underneath it.
    if login_customer_id:
        destination["loginAccount"] = {
            "accountType": "GOOGLE_ADS",
            "accountId": login_customer_id.replace("-", ""),
        }

    payload = {
        "destinations": [destination],
        "events": [
            {
                "transactionId": order_id,
                "eventTimestamp": conversion_time,
                "conversionValue": value,
                "currency": currency,
                "userData": {"userIdentifiers": user_identifiers},
            }
        ],
        "encoding": "HEX",
        "validateOnly": validate_only,
    }

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    try:
        with httpx.Client(timeout=20) as client:
            resp = client.post(DATA_MANAGER_INGEST_URL, json=payload, headers=headers)
            result = resp.json() if resp.text else {}
            if resp.status_code != 200:
                logger.warning(
                    "Google Ads ingest HTTP error reservation=%s case=%s status=%d: %s",
                    order_id, case, resp.status_code, resp.text[:400],
                )
                return {
                    "success": False,
                    "case": case,
                    "status_code": resp.status_code,
                    "error": _describe_error(result) or f"HTTP {resp.status_code}",
                    "response": result,
                }

            logger.info(
                "Google Ads conversion ingested reservation=%s case=%s request_id=%s",
                order_id, case, result.get("requestId"),
            )
            return {"success": True, "case": case, "request_id": result.get("requestId"), "response": result}
    except Exception as e:
        logger.error("Google Ads ingest error reservation=%s: %s", order_id, e)
        return {"success": False, "case": case, "error": str(e)}
