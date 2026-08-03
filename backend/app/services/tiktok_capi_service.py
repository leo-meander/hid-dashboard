"""
TikTok Events API (offline) service — Saigon branch only.

Sends a CompletePayment event when a new Saigon reservation is created.
Mirrors the Make.com TikTok branch in the SGN blueprint.
"""
import hashlib
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx

from app.services.email_utils import usable_email
from app.services.phone_utils import normalize_e164_digits

logger = logging.getLogger(__name__)

TIKTOK_EVENTS_URL = "https://business-api.tiktok.com/open_api/v1.3/event/track/"

# Saigon is the only branch on TikTok — guest phones without a country code are
# assumed Vietnamese.
SAIGON_PHONE_COUNTRY_CODE = "84"


def _sha256(value: Optional[str]) -> Optional[str]:
    if not value or not str(value).strip():
        return None
    return hashlib.sha256(value.strip().lower().encode()).hexdigest()


def _parse_event_time(date_created: Optional[str]) -> Optional[int]:
    """
    Convert Cloudbeds dateCreated (Vietnam local time, UTC+7) to Unix timestamp.
    Saigon Make flow passes raw dateCreated without any timezone conversion.
    """
    if not date_created:
        return None
    try:
        local_dt = datetime.strptime(date_created[:16], "%Y-%m-%d %H:%M")
        utc_dt = local_dt - timedelta(hours=7)
        return int(utc_dt.replace(tzinfo=timezone.utc).timestamp())
    except (ValueError, TypeError):
        return None


def _first_guest(guest_list: dict) -> dict:
    if not guest_list or not isinstance(guest_list, dict):
        return {}
    first_key = next(iter(guest_list), None)
    return guest_list.get(first_key, {}) if first_key else {}


def send_complete_payment_event(
    reservation: dict,
    access_token: str,
    event_source_id: str,
) -> dict:
    """
    Send a CompletePayment offline event to TikTok Events API for the given reservation.
    Returns {"success": bool, "status_code": int, "response": dict}.
    """
    # None for OTA alias addresses and Cloudbeds "N/A" placeholders — Saigon
    # sells heavily through Ctrip, so this is most of the branch's volume.
    email = usable_email(reservation.get("guestEmail"))
    guest_list = reservation.get("guestList") or {}
    guest = _first_guest(guest_list)
    phone = normalize_e164_digits(guest.get("guestPhone", ""), SAIGON_PHONE_COUNTRY_CODE)

    event_time = _parse_event_time(reservation.get("dateCreated"))
    if not event_time:
        logger.warning(
            "TikTok CAPI: could not parse dateCreated=%s", reservation.get("dateCreated")
        )
        return {"success": False, "error": "invalid_event_time"}

    user: dict = {}
    if email:
        user["email"] = [_sha256(email)]
    if phone:
        user["phone_number"] = [_sha256(phone)]

    if not user:
        logger.warning(
            "TikTok CAPI: no user data for reservation=%s", reservation.get("reservationID")
        )
        return {"success": False, "error": "no_user_data"}

    try:
        value = float(reservation.get("total") or 0)
    except (TypeError, ValueError):
        value = 0.0

    order_id = str(reservation.get("reservationID", ""))

    payload = {
        "event_source": "offline",
        "event_source_id": event_source_id,
        "data": [
            {
                "event": "CompletePayment",
                "event_time": event_time,
                "event_id": order_id,
                "user": user,
                "properties": {
                    "value": value,
                    "currency": "VND",
                    "order_id": order_id,
                },
            }
        ],
    }

    headers = {
        "Access-Token": access_token,
        "Content-Type": "application/json",
    }

    try:
        with httpx.Client(timeout=20) as client:
            resp = client.post(TIKTOK_EVENTS_URL, json=payload, headers=headers)
            result = resp.json() if resp.text else {}
            if resp.status_code == 200 and result.get("code") == 0:
                logger.info(
                    "TikTok CAPI CompletePayment sent reservation=%s", order_id
                )
                return {"success": True, "status_code": resp.status_code, "response": result}
            else:
                logger.warning(
                    "TikTok CAPI failed reservation=%s status=%d code=%s: %s",
                    order_id, resp.status_code, result.get("code"), resp.text[:300],
                )
                return {"success": False, "status_code": resp.status_code, "response": result}
    except Exception as e:
        logger.error("TikTok CAPI error reservation=%s: %s", order_id, e)
        return {"success": False, "error": str(e)}
