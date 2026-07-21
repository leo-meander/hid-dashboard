"""
Google Ads offline conversion upload service.

Replaces the Make.com Google Ads conversion modules in "Cloudbeds to Meta Offline Conversion API".
Uses Google Ads API v17 REST (ClickConversions upload) via OAuth2.

Three routing cases (mirrors the Make router exactly):
  A. Email only (phone absent OR email is N/A)  → conversion_action_single
  B. Phone only (email absent OR email is N/A)  → conversion_action_single
  C. Both email + phone                          → conversion_action_both
"""
import hashlib
import logging
import re
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

GOOGLE_ADS_API_VERSION = "v17"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"


def _sha256(value: Optional[str]) -> Optional[str]:
    if not value or not str(value).strip():
        return None
    return hashlib.sha256(value.strip().lower().encode()).hexdigest()


def _clean_phone(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    cleaned = re.sub(r"[+\-\s()]", "", raw.strip())
    return cleaned if len(cleaned) > 5 else None


def _get_access_token(
    client_id: str,
    client_secret: str,
    refresh_token: str,
) -> Optional[str]:
    """Exchange refresh token for an access token."""
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
    Convert Cloudbeds dateCreated to Google Ads conversionDateTime format.

    Mirrors Make formula: addHours(parseDate(date, branch_timezone), -extra_offset)
      - tz_offset_hours: UTC offset of branch local time (8=Taipei, 9=Tokyo, 7=Saigon)
      - extra_offset_hours: additional hours subtracted (Make's addHours value)
    """
    if not date_created:
        return None
    from datetime import datetime, timezone, timedelta
    try:
        local_dt = datetime.strptime(date_created[:16], "%Y-%m-%d %H:%M")
        utc_dt = local_dt - timedelta(hours=tz_offset_hours) - timedelta(hours=extra_offset_hours)
        utc_dt = utc_dt.replace(tzinfo=timezone.utc)
        return utc_dt.strftime("%Y-%m-%d %H:%M:%S+00:00")
    except (ValueError, TypeError):
        return None


def _first_guest(guest_list: dict) -> dict:
    if not guest_list or not isinstance(guest_list, dict):
        return {}
    first_key = next(iter(guest_list), None)
    return guest_list.get(first_key, {}) if first_key else {}


def upload_offline_conversion(
    reservation: dict,
    customer_id: str,
    developer_token: str,
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
) -> dict:
    """
    Upload offline click conversion to Google Ads for the given reservation.

    Routing (mirrors Make.com flow):
      - email only → conversion_action_single
      - phone only → conversion_action_phone (falls back to conversion_action_single)
      - both       → conversion_action_both

    Returns {"success": bool, "case": str, "response": dict}.
    """
    email = (reservation.get("guestEmail") or "").strip()
    guest_list = reservation.get("guestList") or {}
    guest = _first_guest(guest_list)
    raw_phone = guest.get("guestPhone", "")
    phone = _clean_phone(raw_phone)

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
            {"hashedEmail": _sha256(email)},
            {"hashedPhoneNumber": _sha256(phone)},
        ]
    elif phone_present and email_invalid:
        # Case B: phone only
        case = "phone_only"
        action_id = phone_action
        user_identifiers = [{"hashedPhoneNumber": _sha256(phone)}]
    else:
        # Case A: email only
        case = "email_only"
        action_id = conversion_action_single
        user_identifiers = [{"hashedEmail": _sha256(email)}]

    access_token = _get_access_token(client_id, client_secret, refresh_token)
    if not access_token:
        return {"success": False, "error": "token_refresh_failed"}

    conversion_action_resource = (
        f"customers/{customer_id_clean}/conversionActions/{action_id}"
    )

    payload = {
        "conversions": [
            {
                "conversionAction": conversion_action_resource,
                "conversionDateTime": conversion_time,
                "conversionValue": value,
                "currencyCode": currency,
                "orderId": order_id,
                "userIdentifiers": user_identifiers,
            }
        ],
        "partialFailure": True,
    }

    url = (
        f"https://googleads.googleapis.com/{GOOGLE_ADS_API_VERSION}"
        f"/customers/{customer_id_clean}:uploadClickConversions"
    )
    headers = {
        "Authorization": f"Bearer {access_token}",
        "developer-token": developer_token,
        "Content-Type": "application/json",
    }
    # MCC manager account — required when OAuth is authenticated as the MCC
    # and the target customer_id is a sub-account underneath it.
    if login_customer_id:
        headers["login-customer-id"] = login_customer_id.replace("-", "")

    try:
        with httpx.Client(timeout=20) as client:
            resp = client.post(url, json=payload, headers=headers)
            result = resp.json() if resp.text else {}
            if resp.status_code != 200:
                logger.warning(
                    "Google Ads upload HTTP error reservation=%s case=%s status=%d: %s",
                    order_id, case, resp.status_code, resp.text[:400],
                )
                return {"success": False, "case": case, "status_code": resp.status_code, "response": result}

            # partialFailure=True → HTTP 200 even on conversion-level errors;
            # real errors land in partialFailureError or results[*].status
            partial_err = result.get("partialFailureError")
            if partial_err:
                logger.warning(
                    "Google Ads partial failure reservation=%s case=%s: %s",
                    order_id, case, partial_err,
                )
                return {"success": False, "case": case, "partial_failure_error": partial_err, "response": result}

            logger.info(
                "Google Ads conversion uploaded reservation=%s case=%s results=%s",
                order_id, case, result.get("results"),
            )
            return {"success": True, "case": case, "response": result}
    except Exception as e:
        logger.error("Google Ads upload error reservation=%s: %s", order_id, e)
        return {"success": False, "case": case, "error": str(e)}
