"""
GHL CRM service — upsert contacts from Cloudbeds reservation data.

Replaces the Make.com "CB New Customer -> GHL" flow for all 5 branches.
Flow:
  1. Search GHL for existing contact by email
  2. If not found → create new contact
  3. If found → update existing contact
"""
import logging
import re
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

GHL_BASE = "https://services.leadconnectorhq.com"

# Per-branch GHL custom field IDs.
# Each entry: { field_id: reservation_field_name_or_None }
# reservation_field_name conventions:
#   "startDate", "endDate", "source", "reservationID", "status", "dateCreated"
#   "roomTypeShort"  → extracted from assigned[]
#   "gender"         → from guestList[0].guestGender
BRANCH_CUSTOM_FIELDS: dict[str, list[tuple[str, str]]] = {
    "taipei": [
        ("9ynXRQM5jnmTsH0vPMkN", "roomTypeShort"),
        ("Ku9p0QhdXSCSVzYfCl33", "startDate"),
        ("isORAKLwe8h4Humcuixp", "endDate"),
        ("v8Nr2YnLXwVlTlL1hNpb", "source"),
        ("yjlbiIqb4VUBNiG02Twt", "reservationID"),
    ],
    "saigon": [
        ("E14Quzy2vEoNgTvQB0P9", "source"),
        ("Egn8vjjNc6nb9zc4l6vB", "dateCreated"),
        ("Nd4TAjq2ymqnxOmCvAen", "reservationID"),
        ("PlwsbIxlsEDjLK5sheSm", "endDate"),
        ("Z5UbwQLkvqiZuSPbuo3g", "startDate"),
        ("cyb2RaJmRbIBRRE4jZaB", "status"),
        ("v0WUcQZmhhx66G65AL79", "roomTypeShort"),
    ],
    "oani": [
        ("QQ32TpgtZM4JjZ8jndVU", "reservationID"),
        ("gm2J6IoFhEAMWiRhY43g", "source"),
        ("gw90Ed7o4NhEUW8NgJuF", "roomTypeShort"),
    ],
    "osaka": [
        ("2U1N1UE2Co7ejCsmg1sp", "roomTypeShort"),
        ("6gXSzTrk0WYKRHbpCLiK", "reservationID"),
        ("Fa0IMxR8L4ng2RkBvjIl", "source"),
        ("bdbI4avHMebzvRcoTJUf", "endDate"),
        ("bg3ZbiKJ7njqCv1Be5rd", "startDate"),
        ("qQ6H4j0LKsDsIZFRMRdV", "dateCreated"),
    ],
    "1948": [
        ("0AVJ2U2l2QSSlk4bIhWa", "endDate"),
        ("10vLyNuVAWOiX6mLJqi0", "source"),
        ("Ci8yVMKmQd5sRjCoToib", "startDate"),
        ("EW5RlkEiPxgYp5d3oT1C", "reservationID"),
        ("IygIv3bld8BlsjRvHIIm", "gender"),
        ("JoPkKzArZMGnjVkRpjBY", "roomTypeShort"),
        ("UEWJCyNCHON1gEZ97tiB", "status"),
        ("ilkDHYVKJtF3C2cykg0T", "dateCreated"),
    ],
}


def _headers(api_key: str) -> dict:
    return {
        "Authorization": f"Bearer {api_key}",
        "Version": "2021-07-28",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _clean_phone(raw: Optional[str]) -> Optional[str]:
    """Strip +, -, spaces, ( ) from phone. Return None if too short."""
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


def _first_guest(guest_list) -> dict:
    """Return the first guest from the guestList dict."""
    if not guest_list or not isinstance(guest_list, dict):
        return {}
    first_key = next(iter(guest_list), None)
    return guest_list.get(first_key, {}) if first_key else {}


def _parse_dob(raw: Optional[str]) -> Optional[str]:
    """Parse guestBirthDate / guestBirthdate → YYYY-MM-DD or None. Rejects 0000-00-00."""
    if not raw or not raw.strip():
        return None
    s = raw.strip()
    if len(s) >= 10 and s[4] == "-":
        date = s[:10]
        return None if date.startswith("0000") else date
    return None


def _normalize_name(raw: Optional[str]) -> str:
    """Strip extra whitespace from names."""
    if not raw:
        return ""
    return " ".join(raw.split())


def _normalize_gender(raw: Optional[str]) -> str:
    """Lowercase gender value for GHL v2 API."""
    if not raw:
        return ""
    return raw.strip().lower()


def _get_room_type_short(reservation: dict) -> Optional[str]:
    """Extract roomTypeNameShort (or roomTypeName) from assigned rooms."""
    assigned = reservation.get("assigned") or {}
    rooms = assigned.values() if isinstance(assigned, dict) else (assigned if isinstance(assigned, list) else [])
    for room in rooms:
        if isinstance(room, dict):
            val = room.get("roomTypeNameShort") or room.get("roomTypeName")
            if val:
                return val
    return None


def _build_custom_fields(reservation: dict, guest: dict, branch: str) -> list:
    """Build the GHL v2 customFields array for the given branch."""
    room_type_short = _get_room_type_short(reservation)

    field_values = {
        "startDate":    reservation.get("startDate", ""),
        "endDate":      reservation.get("endDate", ""),
        "source":       reservation.get("source", ""),
        "reservationID": reservation.get("reservationID", ""),
        "status":       reservation.get("status", ""),
        "dateCreated":  reservation.get("dateCreated", ""),
        "roomTypeShort": room_type_short or "",
        "gender":       _normalize_gender(guest.get("guestGender")),
    }

    custom_fields = []
    for field_id, key in BRANCH_CUSTOM_FIELDS.get(branch, []):
        value = field_values.get(key, "")
        if value:
            custom_fields.append({"id": field_id, "field_value": str(value)})

    return custom_fields


def _build_contact_payload(reservation: dict, branch: str, is_update: bool = False) -> dict:
    """Build the GHL contact payload for a specific branch."""
    guest_list = reservation.get("guestList") or {}
    guest = _first_guest(guest_list)

    email = (reservation.get("guestEmail") or "").strip().lower()
    country = _clean_country(guest.get("guestCountry"))

    # Phone — Osaka uses guestCellPhone when raw guestPhone exists; others use cleaned guestPhone
    raw_phone = guest.get("guestPhone", "")
    if branch == "osaka":
        phone = guest.get("guestCellPhone") if raw_phone and len(raw_phone.strip()) > 5 else None
    else:
        phone = _clean_phone(raw_phone)

    # Name — differs per branch and create vs update
    guest_name = _normalize_name(guest.get("guestName"))
    first_name_raw = _normalize_name(guest.get("guestFirstName"))
    last_name_raw = _normalize_name(guest.get("guestLastName"))

    if branch in ("taipei",):
        first_name = guest_name
        last_name = guest_name
    elif branch in ("saigon", "osaka"):
        first_name = guest_name
        last_name = ""
    elif branch == "oani":
        if is_update:
            first_name = first_name_raw
            last_name = last_name_raw
        else:
            first_name = guest_name
            last_name = ""
    else:  # 1948 — always use guestFirstName / guestLastName
        first_name = first_name_raw
        last_name = last_name_raw

    # Date of birth — not used for saigon
    dob = None
    if branch != "saigon":
        raw_dob = guest.get("guestBirthDate") or guest.get("guestBirthdate")
        dob = _parse_dob(raw_dob)

    payload: dict = {
        "email": email,
        "firstName": first_name,
    }
    if last_name:
        payload["lastName"] = last_name
    if phone:
        payload["phone"] = phone
    if dob:
        payload["dateOfBirth"] = dob
    if not is_update:
        payload["dnd"] = False

    # Address — Osaka update only sets country; all other cases set full address
    if is_update and branch == "osaka":
        if country:
            payload["address"] = {"country": country}
    else:
        address_payload: dict = {}
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

    custom_fields = _build_custom_fields(reservation, guest, branch)
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


def update_contact(client: httpx.Client, contact_id: str, api_key: str, location_id: str, payload: dict) -> tuple[bool, str | None]:
    """Update an existing GHL contact. Returns (success, error_message)."""
    try:
        resp = client.put(
            f"{GHL_BASE}/contacts/{contact_id}",
            json={**payload, "locationId": location_id},
            headers=_headers(api_key),
            timeout=15,
        )
        if resp.status_code in (200, 201):
            logger.info("GHL contact updated id=%s", contact_id)
            return True, None
        err = f"HTTP {resp.status_code}: {resp.text[:200]}"
        logger.warning("GHL update failed status=%d: %s", resp.status_code, resp.text[:300])
        return False, err
    except Exception as e:
        logger.error("GHL update error: %s", e)
        return False, str(e)


def upsert_contact_from_reservation(
    reservation: dict,
    location_id: str,
    api_key: str,
    branch: str = "1948",
) -> dict:
    """
    Main entry point: upsert a GHL contact from Cloudbeds reservation data.
    Returns {"action": "created"|"updated"|"update_failed"|"skipped", "contact_id": str|None}.
    """
    email = (reservation.get("guestEmail") or "").strip()
    if not email or email.upper() in ("N/A", "NA"):
        logger.info("GHL upsert skipped — no guest email (branch=%s)", branch)
        return {"action": "skipped", "contact_id": None}

    b = branch.lower()
    create_payload = _build_contact_payload(reservation, b, is_update=False)
    update_payload = _build_contact_payload(reservation, b, is_update=True)

    with httpx.Client(timeout=20) as client:
        existing = search_contact(client, location_id, api_key, email)

        if existing is None:
            contact_id = create_contact(client, location_id, api_key, create_payload)
            return {"action": "created", "contact_id": contact_id}
        else:
            contact_id = existing.get("id")
            success, err = update_contact(client, contact_id, api_key, location_id, update_payload)
            return {"action": "updated" if success else "update_failed", "contact_id": contact_id, "error": err}
