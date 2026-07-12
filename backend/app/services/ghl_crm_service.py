"""
GHL CRM service — upsert contacts from Cloudbeds reservation data.

Replaces the Make.com "CB New Customer -> GHL" flow.
Flow:
  1. Search GHL for existing contact by email
  2. If not found → create new contact
  3. If found → update existing contact
"""
import hashlib
import logging
import re
from typing import Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

GHL_BASE = "https://services.leadconnectorhq.com"


def _headers(api_key: str) -> dict:
    return {
        "Authorization": f"Bearer {api_key}",
        "Version": "2021-07-28",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _clean_phone(raw: Optional[str]) -> Optional[str]:
    """Strip +, -, spaces, ( from phone. Return None if too short."""
    if not raw:
        return None
    cleaned = re.sub(r"[+\-\s()]", "", raw.strip())
    return cleaned if len(cleaned) > 5 else None


def _clean_country(raw: Optional[str]) -> Optional[str]:
    """Only pass 2-letter ISO codes; anything else is discarded."""
    if not raw:
        return None
    s = raw.strip().upper()
    return s if len(s) == 2 else None


def _first_guest(guest_list: dict) -> dict:
    """Return the first guest from the guestList dict."""
    if not guest_list or not isinstance(guest_list, dict):
        return {}
    first_key = next(iter(guest_list), None)
    return guest_list.get(first_key, {}) if first_key else {}


def _parse_dob(raw: Optional[str]) -> Optional[str]:
    """Parse guestBirthDate → YYYY-MM-DD string or None."""
    if not raw or not raw.strip():
        return None
    s = raw.strip()
    if len(s) >= 10 and s[4] == "-":
        return s[:10]
    return None


def _build_contact_payload(reservation: dict) -> dict:
    """Build the GHL contact payload from a Cloudbeds /getReservation response."""
    guest_list = reservation.get("guestList") or {}
    guest = _first_guest(guest_list)

    phone = _clean_phone(guest.get("guestPhone"))
    country = _clean_country(guest.get("guestCountry"))

    # Check-in/checkout dates — prefer top-level fields (from /getReservation response)
    # assigned[*] may have per-room dates but top-level is more reliable for CRM
    start_date = reservation.get("startDate") or ""
    end_date = reservation.get("endDate") or ""

    # Try to get room type from assigned rooms
    room_type_short = None
    assigned = reservation.get("assigned") or {}
    if isinstance(assigned, dict):
        for room in assigned.values():
            if isinstance(room, dict):
                room_type_short = room.get("roomTypeNameShort") or room.get("roomTypeName")
                if room_type_short:
                    break
    elif isinstance(assigned, list):
        for room in assigned:
            if isinstance(room, dict):
                room_type_short = room.get("roomTypeNameShort") or room.get("roomTypeName")
                if room_type_short:
                    break

    dob = _parse_dob(guest.get("guestBirthDate") or guest.get("guestBirthdate"))

    payload: dict = {
        "email": reservation.get("guestEmail", ""),
        "firstName": guest.get("guestFirstName", ""),
        "lastName": guest.get("guestLastName", ""),
        "source": reservation.get("source", ""),
        "dnd": False,
    }

    if phone:
        payload["phone"] = phone
    if dob:
        payload["dateOfBirth"] = dob

    gender = guest.get("guestGender", "")
    address_payload = {}
    if guest.get("guestCity"):
        address_payload["city"] = guest["guestCity"]
    if guest.get("guestState"):
        address_payload["state"] = guest["guestState"]
    if country:
        address_payload["country"] = country
    if guest.get("guestZip"):
        address_payload["postalCode"] = guest["guestZip"]
    if address_payload:
        payload["address"] = address_payload

    # Custom fields — GHL v2 format
    custom_fields = []

    def _cf(field_id: str, value):
        if value is not None and value != "":
            custom_fields.append({"id": field_id, "field_value": str(value)})

    _cf("Ci8yVMKmQd5sRjCoToib", start_date)          # Check-in Date
    _cf("0AVJ2U2l2QSSlk4bIhWa", end_date)             # Check-out Date
    _cf("EW5RlkEiPxgYp5d3oT1C", reservation.get("reservationID"))  # Reservation Number
    _cf("10vLyNuVAWOiX6mLJqi0", reservation.get("source"))          # Booking Channel
    _cf("UEWJCyNCHON1gEZ97tiB", reservation.get("status"))          # Check-in Status
    _cf("ilkDHYVKJtF3C2cykg0T", reservation.get("dateCreated"))     # Reservation Date
    _cf("IygIv3bld8BlsjRvHIIm", gender)                             # Gender
    if room_type_short:
        _cf("JoPkKzArZMGnjVkRpjBY", room_type_short)               # roomTypeName

    if custom_fields:
        payload["customFields"] = custom_fields

    return payload


def search_contact(client: httpx.Client, location_id: str, api_key: str, email: str) -> Optional[dict]:
    """Search GHL for a contact by email. Returns the first match or None."""
    try:
        resp = client.get(
            f"{GHL_BASE}/contacts/",
            params={"locationId": location_id, "query": email, "limit": 1},
            headers=_headers(api_key),
            timeout=15,
        )
        if resp.status_code == 200:
            contacts = resp.json().get("contacts") or []
            return contacts[0] if contacts else None
        logger.warning("GHL search failed status=%d: %s", resp.status_code, resp.text[:200])
        return None
    except Exception as e:
        logger.error("GHL search error: %s", e)
        return None


def create_contact(client: httpx.Client, location_id: str, api_key: str, payload: dict) -> Optional[str]:
    """Create a new GHL contact. Returns the new contact ID or None."""
    body = {**payload, "locationId": location_id}
    try:
        resp = client.post(
            f"{GHL_BASE}/contacts/",
            json=body,
            headers=_headers(api_key),
            timeout=15,
        )
        if resp.status_code in (200, 201):
            contact = resp.json().get("contact") or {}
            logger.info("GHL contact created id=%s", contact.get("id"))
            return contact.get("id")
        logger.warning("GHL create failed status=%d: %s", resp.status_code, resp.text[:300])
        return None
    except Exception as e:
        logger.error("GHL create error: %s", e)
        return None


def update_contact(client: httpx.Client, contact_id: str, api_key: str, payload: dict) -> bool:
    """Update an existing GHL contact. Returns True on success."""
    try:
        resp = client.put(
            f"{GHL_BASE}/contacts/{contact_id}",
            json=payload,
            headers=_headers(api_key),
            timeout=15,
        )
        if resp.status_code in (200, 201):
            logger.info("GHL contact updated id=%s", contact_id)
            return True
        logger.warning("GHL update failed status=%d: %s", resp.status_code, resp.text[:300])
        return False
    except Exception as e:
        logger.error("GHL update error: %s", e)
        return False


def upsert_contact_from_reservation(
    reservation: dict,
    location_id: str,
    api_key: str,
) -> dict:
    """
    Main entry point: upsert a GHL contact from Cloudbeds reservation data.
    Returns {"action": "created"|"updated"|"skipped", "contact_id": str|None}.
    """
    email = reservation.get("guestEmail", "").strip()
    if not email or email.upper() in ("N/A", "NA", ""):
        logger.info("GHL upsert skipped — no guest email")
        return {"action": "skipped", "contact_id": None}

    payload = _build_contact_payload(reservation)

    with httpx.Client(timeout=20) as client:
        existing = search_contact(client, location_id, api_key, email)

        if existing is None:
            contact_id = create_contact(client, location_id, api_key, payload)
            return {"action": "created", "contact_id": contact_id}
        else:
            contact_id = existing.get("id")
            # Remove dnd from update payload (we never change DND on existing contacts)
            update_payload = {k: v for k, v in payload.items() if k != "dnd"}
            success = update_contact(client, contact_id, api_key, update_payload)
            return {"action": "updated" if success else "update_failed", "contact_id": contact_id}
