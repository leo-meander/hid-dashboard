"""
TikTok Events API (offline) service.

Sends a CompletePayment event when a new reservation is created on a branch
that advertises on TikTok (see config.TIKTOK_BRANCHES). Mirrors the Make.com
TikTok branch in the SGN blueprint; the per-branch currency, timezone and phone
country code come from get_webhook_config_for_branch, exactly as they do for
Meta CAPI and Google Ads.
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


def _sha256(value: Optional[str]) -> Optional[str]:
    if not value or not str(value).strip():
        return None
    return hashlib.sha256(value.strip().lower().encode()).hexdigest()


def _parse_event_time(
    date_created: Optional[str],
    tz_offset_hours: int = 7,
    extra_offset_hours: int = 0,
) -> Optional[int]:
    """
    Convert Cloudbeds dateCreated (branch local time) to a Unix timestamp.

    Same two-part offset Meta CAPI and Google Ads use, so a reservation carries
    one timestamp across all three channels:
      - tz_offset_hours: UTC offset of branch local time (7=Saigon, 9=Tokyo)
      - extra_offset_hours: additional hours subtracted (Make's addHours value;
        0 for Saigon, which is why the defaults reproduce the old behaviour)
    """
    if not date_created:
        return None
    try:
        local_dt = datetime.strptime(date_created[:16], "%Y-%m-%d %H:%M")
        utc_dt = local_dt - timedelta(hours=tz_offset_hours) - timedelta(hours=extra_offset_hours)
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
    currency: str = "VND",
    tz_offset_hours: int = 7,
    event_time_extra_offset: int = 0,
    phone_country_code: str = "84",
) -> dict:
    """
    Send a CompletePayment offline event to TikTok Events API for the given reservation.

    `event_source_id` is the branch's Offline Event Set ID, not its web pixel.
    Defaults describe Saigon, the first branch on this path.

    Returns {"success": bool, "status_code": int, "response": dict}.
    """
    # None for OTA alias addresses and Cloudbeds "N/A" placeholders — both
    # branches sell heavily through Ctrip, so this is most of their volume.
    email = usable_email(reservation.get("guestEmail"))
    guest_list = reservation.get("guestList") or {}
    guest = _first_guest(guest_list)
    phone = normalize_e164_digits(guest.get("guestPhone", ""), phone_country_code)

    event_time = _parse_event_time(
        reservation.get("dateCreated"),
        tz_offset_hours=tz_offset_hours,
        extra_offset_hours=event_time_extra_offset,
    )
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
                    "currency": currency,
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
