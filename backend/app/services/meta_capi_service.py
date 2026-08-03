"""
Meta Conversions API (CAPI) service — offline Purchase event.

Replaces the Make.com "Cloudbeds to Meta Offline Conversion API" flow.
Sends a Purchase event when a new non-website reservation is created.
PII fields are SHA256-hashed as required by Meta.
"""
import hashlib
import logging
from typing import Optional

import httpx

from app.services.email_utils import usable_email
from app.services.phone_utils import normalize_e164_digits

logger = logging.getLogger(__name__)

META_GRAPH_BASE = "https://graph.facebook.com/v19.0"


def _sha256(value: Optional[str]) -> Optional[str]:
    """Lowercase + strip + SHA256 hash a PII string. Returns None if empty."""
    if not value or not str(value).strip():
        return None
    return hashlib.sha256(value.strip().lower().encode()).hexdigest()


# Meta hashes its own copy of a user's details *after* normalizing them, so any
# field we hash in a different shape misses by definition — a wrong separator is
# as fatal as a wrong value. Lowercasing alone (what _sha256 does) is enough for
# em and ph but not for the fields below.


def _alnum(value: Optional[str]) -> Optional[str]:
    """
    Lowercase and drop everything that is not a letter or digit.

    Covers ct, st and zp: Meta normalizes those to bare lowercase strings, so
    "Ho Chi Minh" has to be hashed as "hochiminh" and "700 000" as "700000".

    Accented letters are kept as-is rather than folded to ASCII. Cloudbeds gets
    city and state from the OTA channels, which send plain English in practice,
    so folding would only change the rare case — and would change it on a guess
    about how Meta stores non-Latin place names.
    """
    if not value:
        return None
    cleaned = "".join(ch for ch in str(value).lower() if ch.isalnum())
    return cleaned or None


def _birthdate(raw: Optional[str]) -> Optional[str]:
    """
    Cloudbeds ISO birthdate ("1990-01-15") to Meta's YYYYMMDD ("19900115").

    Returns None for anything that does not yield at least 8 digits, which also
    covers the empty string Cloudbeds sends when a channel supplies no birthday.
    """
    if not raw:
        return None
    digits = "".join(ch for ch in str(raw) if ch.isdigit())
    return digits[:8] if len(digits) >= 8 else None


# Cloudbeds sends 'M', 'F' or the literal 'N/A' (see services/cloudbeds.py).
# Anything outside this map — 'N/A' included — means no usable gender, and must
# be left out rather than hashed into a junk identifier.
_GENDERS = {"m": "m", "male": "m", "f": "f", "female": "f"}


def _gender(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    return _GENDERS.get(str(raw).strip().lower())


def _parse_event_time(
    date_created: Optional[str],
    tz_offset_hours: int = 8,
    extra_offset_hours: int = 1,
) -> Optional[int]:
    """
    Convert Cloudbeds dateCreated to Unix timestamp.

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
        return int(utc_dt.replace(tzinfo=timezone.utc).timestamp())
    except (ValueError, TypeError):
        return None


def _first_guest(guest_list: dict) -> dict:
    if not guest_list or not isinstance(guest_list, dict):
        return {}
    first_key = next(iter(guest_list), None)
    return guest_list.get(first_key, {}) if first_key else {}


def send_purchase_event(
    reservation: dict,
    pixel_id: str,
    access_token: str,
    currency: str = "TWD",
    tz_offset_hours: int = 8,
    event_time_extra_offset: int = 1,
    phone_country_code: str = "886",
) -> dict:
    """
    Send a Purchase event to Meta CAPI for the given reservation.
    Returns {"success": bool, "status_code": int, "response": dict}.
    """
    # None for OTA alias addresses and Cloudbeds "N/A" placeholders — hashing
    # those only drags the reported match quality down. The phone carries the
    # match for OTA bookings.
    email = usable_email(reservation.get("guestEmail"))
    guest_list = reservation.get("guestList") or {}
    guest = _first_guest(guest_list)

    event_time = _parse_event_time(
        reservation.get("dateCreated"),
        tz_offset_hours=tz_offset_hours,
        extra_offset_hours=event_time_extra_offset,
    )
    if not event_time:
        logger.warning("Meta CAPI: could not parse dateCreated=%s", reservation.get("dateCreated"))
        return {"success": False, "error": "invalid_event_time"}

    phone = normalize_e164_digits(guest.get("guestPhone", ""), phone_country_code)

    dob = _birthdate(guest.get("guestBirthDate") or guest.get("guestBirthdate"))
    gender = _gender(guest.get("guestGender"))
    city = _alnum(guest.get("guestCity"))
    state = _alnum(guest.get("guestState"))
    zip_code = _alnum(guest.get("guestZip"))

    user_data: dict = {}
    if email:
        user_data["em"] = _sha256(email)
    if phone:
        user_data["ph"] = _sha256(phone)
    # fn and ln are left to _sha256's lowercase-and-trim. Meta's own SDK strips
    # names down to a-z, which would mangle the Vietnamese and Chinese names
    # that make up most of this data, so that is not applied here.
    if guest.get("guestFirstName"):
        user_data["fn"] = _sha256(guest["guestFirstName"])
    if guest.get("guestLastName"):
        user_data["ln"] = _sha256(guest["guestLastName"])
    if gender:
        user_data["ge"] = _sha256(gender)
    if dob:
        user_data["db"] = _sha256(dob)
    if city:
        user_data["ct"] = _sha256(city)
    if state:
        user_data["st"] = _sha256(state)
    if zip_code:
        user_data["zp"] = _sha256(zip_code)
    if guest.get("guestCountry"):
        country = guest["guestCountry"].strip()
        if len(country) == 2:
            user_data["country"] = _sha256(country.lower())

    if not user_data:
        logger.warning("Meta CAPI: no user data to send for reservation %s", reservation.get("reservationID"))
        return {"success": False, "error": "no_user_data"}

    try:
        total = float(reservation.get("total") or 0)
    except (TypeError, ValueError):
        total = 0.0

    event = {
        "event_name": "Purchase",
        "event_time": event_time,
        "action_source": "physical_store",
        "event_id": str(reservation.get("reservationID", "")),
        "user_data": user_data,
        "custom_data": {
            "value": total,
            "currency": currency,
            "order_id": str(reservation.get("reservationID", "")),
            "Source": reservation.get("source", ""),
        },
    }

    payload = {"data": [event]}

    try:
        with httpx.Client(timeout=20) as client:
            resp = client.post(
                f"{META_GRAPH_BASE}/{pixel_id}/events",
                params={"access_token": access_token},
                json=payload,
            )
            result = resp.json() if resp.text else {}
            if resp.status_code == 200:
                logger.info(
                    "Meta CAPI Purchase sent reservation=%s events_received=%s",
                    reservation.get("reservationID"),
                    result.get("events_received"),
                )
                return {"success": True, "status_code": resp.status_code, "response": result}
            else:
                logger.warning(
                    "Meta CAPI failed reservation=%s status=%d: %s",
                    reservation.get("reservationID"), resp.status_code, resp.text[:300],
                )
                return {"success": False, "status_code": resp.status_code, "response": result}
    except Exception as e:
        logger.error("Meta CAPI error reservation=%s: %s", reservation.get("reservationID"), e)
        return {"success": False, "error": str(e)}
