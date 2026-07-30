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

# Maps GHL custom field keys → Cloudbeds reservation data keys.
# Field IDs are fetched dynamically per location (see _get_location_field_map).
FIELD_KEY_MAP: dict[str, str] = {
    "contact.reservation_number": "reservationID",
    "contact.reservation_date":   "dateCreated",
    "contact.checkin_date":       "startDate",
    "contact.checkout_date":      "endDate",
    "contact.checkin_status":     "status",
    "contact.roomtypename":       "roomTypeShort",
    "contact.booking_source":     "source",
    "contact.booking_channel":    "source",
    "contact.gender":             "gender",
}

# Default country dialing code per branch for E.164 phone normalization.
BRANCH_COUNTRY_CODE: dict[str, str] = {
    "saigon": "+84",
    "taipei": "+886",
    "1948":   "+886",
    "oani":   "+886",
    "osaka":  "+81",
}

# In-memory cache: location_id → {fieldKey: fieldId}
_field_cache: dict[str, dict[str, str]] = {}


def _headers(api_key: str) -> dict:
    return {
        "Authorization": f"Bearer {api_key}",
        "Version": "2021-07-28",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _normalize_phone(raw: Optional[str], branch: str) -> Optional[str]:
    """
    Normalize phone to E.164.
    - If raw starts with '+': strip non-digits after +, return +{digits}
    - Otherwise: prepend branch default country code, strip leading zero
    Returns None if result is too short to be valid.
    """
    if not raw:
        return None
    stripped = raw.strip()
    if stripped.startswith("+"):
        digits = re.sub(r"\D", "", stripped[1:])
        return f"+{digits}" if len(digits) >= 7 else None
    digits = re.sub(r"\D", "", stripped).lstrip("0")
    code = BRANCH_COUNTRY_CODE.get(branch, "")
    if not code or len(digits) < 7:
        return None
    return f"{code}{digits}"


def _get_location_field_map(
    client: httpx.Client, location_id: str, api_key: str
) -> dict[str, str]:
    """Return {fieldKey: fieldId} for a GHL location. Cached in-process."""
    if location_id in _field_cache:
        return _field_cache[location_id]
    try:
        resp = client.get(
            f"{GHL_BASE}/locations/{location_id}/customFields",
            params={"model": "contact"},
            headers=_headers(api_key),
            timeout=15,
        )
        if resp.status_code == 200:
            fields = resp.json().get("customFields") or []
            field_map = {f["fieldKey"]: f["id"] for f in fields if "fieldKey" in f and "id" in f}
            _field_cache[location_id] = field_map
            return field_map
        logger.warning("GHL get custom fields failed status=%d location=%s", resp.status_code, location_id)
    except Exception as e:
        logger.error("GHL get custom fields error location=%s: %s", location_id, e)
    return {}


# ISO 3166-1 alpha-2. A two-letter shape is not enough: GHL validates the value
# against the real list and rejects the whole create with "country must be
# valid", so codes like UK (should be GB) or Cloudbeds' own placeholders have to
# be filtered out here rather than sent and hoped for.
_ISO_3166_ALPHA2 = frozenset("""
AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE BF BG BH BI BJ BL
BM BN BO BQ BR BS BT BV BW BY BZ CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV
CW CX CY CZ DE DJ DK DM DO DZ EC EE EG EH ER ES ET FI FJ FK FM FO FR GA GB GD
GE GF GG GH GI GL GM GN GP GQ GR GS GT GU GW GY HK HM HN HR HT HU ID IE IL IM
IN IO IQ IR IS IT JE JM JO JP KE KG KH KI KM KN KP KR KW KY KZ LA LB LC LI LK
LR LS LT LU LV LY MA MC MD ME MF MG MH MK ML MM MN MO MP MQ MR MS MT MU MV MW
MX MY MZ NA NC NE NF NG NI NL NO NP NR NU NZ OM PA PE PF PG PH PK PL PM PN PR
PS PT PW PY QA RE RO RS RU RW SA SB SC SD SE SG SH SI SJ SK SL SM SN SO SR SS
ST SV SX SY SZ TC TD TF TG TH TJ TK TL TM TN TO TR TT TV TW TZ UA UG UM US UY
UZ VA VC VE VG VI VN VU WF WS YE YT ZA ZM ZW
""".split())

# Non-ISO codes seen in the wild that have an unambiguous ISO equivalent.
_COUNTRY_ALIASES = {"UK": "GB", "EL": "GR"}


def _clean_country(raw: Optional[str]) -> Optional[str]:
    """Return a valid ISO 3166-1 alpha-2 code, or None if it isn't one."""
    if not raw:
        return None
    s = raw.strip().upper()
    s = _COUNTRY_ALIASES.get(s, s)
    if s in _ISO_3166_ALPHA2:
        return s
    logger.debug("GHL: dropping unrecognised country code %r", raw)
    return None


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


def _build_custom_fields(
    reservation: dict,
    guest: dict,
    location_id: str,
    api_key: str,
    client: httpx.Client,
) -> list:
    """Build the GHL v2 customFields array using dynamic field ID lookup."""
    field_map = _get_location_field_map(client, location_id, api_key)
    if not field_map:
        return []

    room_type_short = _get_room_type_short(reservation)
    data = {
        "reservationID": str(reservation.get("reservationID") or ""),
        "dateCreated":   reservation.get("dateCreated") or "",
        "startDate":     reservation.get("startDate") or "",
        "endDate":       reservation.get("endDate") or "",
        "status":        reservation.get("status") or "",
        "source":        reservation.get("source") or "",
        "roomTypeShort": room_type_short or "",
        "gender":        _normalize_gender(guest.get("guestGender")),
    }

    custom_fields = []
    for field_key, data_key in FIELD_KEY_MAP.items():
        field_id = field_map.get(field_key)
        if not field_id:
            continue
        value = data.get(data_key, "")
        if value:
            custom_fields.append({"id": field_id, "field_value": str(value)})

    return custom_fields


def _build_contact_payload(
    reservation: dict,
    branch: str,
    location_id: str,
    api_key: str,
    client: httpx.Client,
    is_update: bool = False,
) -> dict:
    """Build the GHL contact payload for a specific branch."""
    guest_list = reservation.get("guestList") or {}
    guest = _first_guest(guest_list)

    email = (reservation.get("guestEmail") or "").strip().lower()
    country = _clean_country(guest.get("guestCountry"))

    # Phone — Osaka uses guestCellPhone; others use guestPhone. Normalize to E.164.
    raw_phone = guest.get("guestPhone", "")
    if branch == "osaka":
        raw_phone = guest.get("guestCellPhone") or raw_phone
    phone = _normalize_phone(raw_phone, branch)

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

    # Address — GHL v2 takes these as flat top-level fields. A nested "address"
    # object is rejected outright ("property address should not exist"), which
    # was failing every create. Sent on create only: PUT /contacts/:id rejects
    # them.
    if not is_update:
        if guest.get("guestCity"):
            payload["city"] = guest["guestCity"]
        if guest.get("guestState"):
            payload["state"] = guest["guestState"]
        if country:
            payload["country"] = country
        if guest.get("guestZip"):
            payload["postalCode"] = guest["guestZip"]

    custom_fields = _build_custom_fields(reservation, guest, location_id, api_key, client)
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


def create_contact(
    client: httpx.Client, location_id: str, api_key: str, payload: dict
) -> tuple[str | None, str | None]:
    """Create a new GHL contact. Returns (contact_id, error_message)."""
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
            return contact.get("id"), None

        # GHL rejects the create when the phone already belongs to another
        # contact (location dedup setting). OTAs hand out proxy phone numbers
        # that repeat across guests, so the match is often a different person —
        # updating that contact would overwrite someone else's record. Create
        # without the phone instead, same as update_contact does.
        if (
            resp.status_code == 400
            and "duplicated contacts" in resp.text
            and "phone" in resp.text
            and "phone" in body
        ):
            logger.warning("GHL create: phone duplicate detected — retrying without phone")
            resp2 = client.post(
                f"{GHL_BASE}/contacts/",
                json={k: v for k, v in body.items() if k != "phone"},
                headers=_headers(api_key),
                timeout=15,
            )
            if resp2.status_code in (200, 201):
                contact = resp2.json().get("contact") or {}
                logger.info("GHL contact created (no phone) id=%s", contact.get("id"))
                return contact.get("id"), None
            err = f"HTTP {resp2.status_code}: {resp2.text[:200]}"
            logger.warning("GHL create (no phone) failed status=%d: %s", resp2.status_code, resp2.text[:300])
            return None, err

        err = f"HTTP {resp.status_code}: {resp.text[:200]}"
        logger.warning("GHL create failed status=%d: %s", resp.status_code, resp.text[:300])
        return None, err
    except Exception as e:
        logger.error("GHL create error: %s", e)
        return None, str(e)


def update_contact(client: httpx.Client, contact_id: str, api_key: str, location_id: str, payload: dict) -> tuple[bool, str | None]:
    """Update an existing GHL contact. Returns (success, error_message)."""
    try:
        resp = client.put(
            f"{GHL_BASE}/contacts/{contact_id}",
            json=payload,
            headers=_headers(api_key),
            timeout=15,
        )
        if resp.status_code in (200, 201):
            logger.info("GHL contact updated id=%s", contact_id)
            return True, None
        # GHL rejects if phone already belongs to another contact (location dedup setting).
        # Retry without phone field so the rest of the data still gets updated.
        if resp.status_code == 400 and "duplicated contacts" in resp.text and "phone" in resp.text and "phone" in payload:
            logger.warning("GHL update: phone duplicate detected for id=%s — retrying without phone", contact_id)
            payload_no_phone = {k: v for k, v in payload.items() if k != "phone"}
            resp2 = client.put(
                f"{GHL_BASE}/contacts/{contact_id}",
                json=payload_no_phone,
                headers=_headers(api_key),
                timeout=15,
            )
            if resp2.status_code in (200, 201):
                logger.info("GHL contact updated (no phone) id=%s", contact_id)
                return True, None
            err = f"HTTP {resp2.status_code}: {resp2.text[:200]}"
            logger.warning("GHL update (no phone) failed status=%d: %s", resp2.status_code, resp2.text[:300])
            return False, err
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
    Returns {"action": "created"|"updated"|"create_failed"|"update_failed"|"skipped",
             "contact_id": str|None, "error": str|None}.

    A failed create reports "create_failed", not "created" with a null id — the
    webhook monitor treats anything outside created/updated as a failure, and a
    green "created" row for a contact that was never created is worse than no
    row at all.
    """
    email = (reservation.get("guestEmail") or "").strip()
    if not email or email.upper() in ("N/A", "NA"):
        logger.info("GHL upsert skipped — no guest email (branch=%s)", branch)
        return {"action": "skipped", "contact_id": None}

    b = branch.lower()

    with httpx.Client(timeout=20) as client:
        create_payload = _build_contact_payload(reservation, b, location_id, api_key, client, is_update=False)
        update_payload = _build_contact_payload(reservation, b, location_id, api_key, client, is_update=True)

        existing = search_contact(client, location_id, api_key, email)

        if existing is None:
            contact_id, err = create_contact(client, location_id, api_key, create_payload)
            if not contact_id:
                return {"action": "create_failed", "contact_id": None, "error": err}
            return {"action": "created", "contact_id": contact_id}
        else:
            contact_id = existing.get("id")
            success, err = update_contact(client, contact_id, api_key, location_id, update_payload)
            return {"action": "updated" if success else "update_failed", "contact_id": contact_id, "error": err}
