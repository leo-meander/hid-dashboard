import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import httpx
from sqlalchemy import text as _text
from sqlalchemy.orm import Session

from app.config import settings
from app.database import SessionLocal
from app.models.reservation import Reservation
from app.services.currency import convert_to_vnd, get_cached_rate

logger = logging.getLogger(__name__)

CLOUDBEDS_BASE_URL = "https://hotels.cloudbeds.com/api/v1.2"
PAGE_SIZE = 100
SYNC_LOOKBACK_DAYS = 90
# For nightly full sync: checked-out guests rarely change after 14 days
CHECKIN_LOOKBACK_DAYS = 14    # past: 14 days (catch late adjustments/refunds)
CHECKIN_FUTURE_DAYS = 180     # future: 6 months of upcoming check-ins

# ── Mapping helpers ────────────────────────────────────────────────────────────

# ISO 2-letter code → full name
_ISO_TO_NAME: dict[str, str] = {
    "US": "United States", "GB": "United Kingdom",
    "AU": "Australia", "CA": "Canada", "NZ": "New Zealand",
    "DE": "Germany", "FR": "France", "IT": "Italy", "ES": "Spain",
    "NL": "Netherlands", "BE": "Belgium", "AT": "Austria", "CH": "Switzerland",
    "SE": "Sweden", "NO": "Norway", "DK": "Denmark", "FI": "Finland",
    "PT": "Portugal", "IE": "Ireland", "PL": "Poland", "CZ": "Czech Republic",
    "RO": "Romania", "JP": "Japan", "KR": "South Korea", "CN": "China",
    "TW": "Taiwan", "HK": "Hong Kong", "MO": "Macau",
    "SG": "Singapore", "MY": "Malaysia", "TH": "Thailand", "VN": "Vietnam",
    "PH": "Philippines", "ID": "Indonesia", "IN": "India",
    "RU": "Russia", "BR": "Brazil", "MX": "Mexico",
    "ZA": "South Africa", "SA": "Saudi Arabia", "AE": "United Arab Emirates",
    "TR": "Turkey", "KH": "Cambodia", "MM": "Myanmar", "LK": "Sri Lanka",
    "CY": "Cyprus", "UY": "Uruguay", "PE": "Peru", "AR": "Argentina",
    "CO": "Colombia", "CL": "Chile", "EG": "Egypt", "IL": "Israel",
    "QA": "Qatar", "LA": "Laos", "NP": "Nepal", "BD": "Bangladesh",
    "GR": "Greece", "HR": "Croatia", "HU": "Hungary", "BG": "Bulgaria",
    "RS": "Serbia", "SK": "Slovakia", "SI": "Slovenia", "LT": "Lithuania",
    "LV": "Latvia", "EE": "Estonia", "IS": "Iceland",
    "NG": "Nigeria", "KE": "Kenya", "GH": "Ghana",
    "PK": "Pakistan", "LB": "Lebanon", "JO": "Jordan", "KW": "Kuwait",
    "BH": "Bahrain", "OM": "Oman",
}

# Full name aliases → canonical name
_NAME_ALIASES: dict[str, str] = {
    "United States of America": "United States",
    "Republic of Korea": "South Korea",
    "Korea, Republic of": "South Korea",
    "Korea": "South Korea",
    "Viet Nam": "Vietnam",
    "Czechia": "Czech Republic",
    "Russian Federation": "Russia",
    "Türkiye": "Turkey",
    "Turkiye": "Turkey",
    "Hong Kong SAR China": "Hong Kong",
    "Macao": "Macau",
    "Macau SAR China": "Macau",
    "Taiwan, Province of China": "Taiwan",
}


def map_country_code(raw: Optional[str]) -> str:
    """Normalize country to a canonical display name.

    Semantics:
    - NULL / empty / whitespace input → "Unknown" (data missing)
    - Known ISO code or alias → canonical name
    - Recognized but un-mapped string → returned as-is (preserve granularity)
    """
    if not raw or not str(raw).strip():
        return "Unknown"
    stripped = raw.strip()
    # 1. If it's a 2-3 letter ISO code, map to full name
    if len(stripped) <= 3:
        upper = stripped.upper()
        if upper in _ISO_TO_NAME:
            return _ISO_TO_NAME[upper]
    # 2. Check aliases
    if stripped in _NAME_ALIASES:
        return _NAME_ALIASES[stripped]
    # 3. Fallback: return as-is (preserve unrecognized country names)
    return stripped


def map_room_type_category(room_type: Optional[str]) -> str:
    if room_type and "dorm" in room_type.lower():
        return "Dorm"
    return "Room"


# Cloudbeds frequently returns NULL for `ratePlanNamePublic` / `ratePlanNamePrivate`
# even in /getReservation detail responses (e.g. CRM event rate plans). However
# the rate plan label is also embedded as the trailing parenthesised segment of
# `roomTypeName`, e.g. "8 Beds Mixed Dormitory Shared Bathroom (CRM_June 2026 Events)".
# We treat that as the canonical fallback so the quota engine's ILIKE match still works.
_RATE_PLAN_PARENS_RE = re.compile(r"\(([^()]+)\)\s*$")


def extract_rate_plan_from_room_type(room_type: Optional[str]) -> Optional[str]:
    """Return the trailing parenthesised segment of room_type, or None."""
    if not room_type:
        return None
    m = _RATE_PLAN_PARENS_RE.search(room_type)
    return m.group(1).strip() if m else None


def _extract_guest_country_from_detail(data: dict) -> Optional[str]:
    """Extract primary guest country from a /getReservation detail response.

    The bulk /getReservations endpoint returns a lite payload without country
    info. The detail endpoint returns a `guestList` dict keyed by guest ID,
    where each entry has `guestCountry` as an ISO-2 code (e.g. 'IT', 'AU').

    Cloudbeds uses '00' as a placeholder when the guest didn't select a
    country — treat that the same as missing. Returns the first non-empty
    ISO code found, or None if no guest in the list has a country set.
    """
    gl = data.get("guestList") or {}
    if not isinstance(gl, dict):
        return None
    for ginfo in gl.values():
        if not isinstance(ginfo, dict):
            continue
        gc = (ginfo.get("guestCountry") or "").strip()
        if gc and gc != "00":
            return gc
    return None


DIRECT_KEYWORDS = [
    "website", "booking engine", "direct", "blogger",
    "walk-in", "walk in", "walkin",
    "extension", "phone", "email",
    "facebook", "public relations",
]

# "Local travel agency" covers Cloudbeds source groups "Corporate Client" + "Travel Agent".
# Cloudbeds API doesn't return the source group, only the raw source name — so we match by
# common company / agency name tokens across Vietnamese, English, Chinese, and Japanese.
LOCAL_TA_KEYWORDS = [
    # Vietnamese corporate
    "công ty", "cong ty", "tnhh",
    # English corporate
    "co., ltd", "co.,ltd", "co ltd", "co.ltd", "co.,ltd.",
    "company", "corp", "corporate",
    # Travel agent / agency
    "travel agent", "travel agency", "agency",
    # Wholesale / tour operator
    "wholesaler", "tour operator",
    # CJK corporate suffixes
    "株式会社", "有限会社",           # Japanese: kabushiki-gaisha, yugen-gaisha
    "有限公司", "股份有限公司",         # Chinese: Co. Ltd
]


def map_source_category(source: Optional[str]) -> str:
    if not source:
        return "OTA"
    s = source.lower()
    if any(kw in s for kw in DIRECT_KEYWORDS):
        return "Direct"
    if any(kw in s for kw in LOCAL_TA_KEYWORDS):
        return "Local travel agency"
    return "OTA"


OTA_CANONICAL: dict[str, str] = {
    "booking.com": "Booking.com",
    "hostelworld": "Hostelworld",
    "agoda": "Agoda",
    "ctrip": "Ctrip",
    "trip.com": "Ctrip",
    "expedia": "Expedia",
}


def normalize_source(source: Optional[str]) -> Optional[str]:
    if not source:
        return source
    lower = source.lower()
    for key, canonical in OTA_CANONICAL.items():
        if key in lower:
            return canonical
    return source


# ── API client ─────────────────────────────────────────────────────────────────

def _headers(api_key: Optional[str] = None) -> dict:
    key = api_key or settings.CLOUDBEDS_API_KEY
    return {"Authorization": f"Bearer {key}"}


def _fetch_transactions_page(
    property_id: str,
    page: int,
    api_key: Optional[str] = None,
    checkin_from: Optional[date] = None,
    checkin_to: Optional[date] = None,
) -> dict:
    """Fetch transactions filtered by guest check-in date (for reservation revenue sync)."""
    params: dict = {
        "propertyID": property_id,
        "pageNumber": page,
        "pageSize": PAGE_SIZE,
    }
    if checkin_from:
        params["guestCheckIn[gte]"] = checkin_from.isoformat()
    if checkin_to:
        params["guestCheckIn[lte]"] = checkin_to.isoformat()
    with httpx.Client(timeout=60) as client:
        response = client.get(
            f"{CLOUDBEDS_BASE_URL}/getTransactions",
            headers=_headers(api_key),
            params=params,
        )
        response.raise_for_status()
        return response.json()


def _fetch_transactions_by_date_page(
    property_id: str,
    page: int,
    api_key: Optional[str] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
) -> dict:
    """Fetch transactions filtered by TRANSACTION DATE (the night the charge was posted).
    Each night of a multi-night stay has its own Room Revenue transaction on that date.
    This naturally produces per-night revenue without any proration math.
    Matches the Cloudbeds OCC report methodology exactly.
    """
    params: dict = {
        "propertyID": property_id,
        "pageNumber": page,
        "pageSize": PAGE_SIZE,
    }
    if date_from:
        params["date[gte]"] = date_from.isoformat()
    if date_to:
        params["date[lte]"] = date_to.isoformat()
    with httpx.Client(timeout=60) as client:
        response = client.get(
            f"{CLOUDBEDS_BASE_URL}/getTransactions",
            headers=_headers(api_key),
            params=params,
        )
        response.raise_for_status()
        return response.json()


def sync_daily_revenue(
    branch_id: str,
    property_id: str,
    currency: str,
    api_key: Optional[str] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
) -> dict:
    """
    Sync daily revenue directly from Cloudbeds transaction dates into daily_metrics.

    Fetches all Room Revenue debit transactions filtered by TRANSACTION DATE
    (= the actual night the room charge was posted). Groups by date and writes
    revenue_native / revenue_vnd into daily_metrics rows.

    This matches the Cloudbeds OCC report exactly — no per-night proration needed
    since each stay-night already has its own transaction. Also handles stayovers
    automatically (Feb check-in → March transactions appear in March naturally).
    No per-reservation API calls, no fallback logic needed.
    """
    today = date.today()
    if date_from is None:
        date_from = today.replace(day=1)  # start of current month
    if date_to is None:
        date_to = today

    rate = get_cached_rate(currency, "VND")
    daily_rev: dict[date, float] = {}  # date → sum of Room Revenue debits

    logger.info(
        "Daily revenue sync branch %s property %s [%s → %s]",
        branch_id, property_id, date_from, date_to,
    )

    page = 1
    while True:
        data = _fetch_transactions_by_date_page(
            property_id, page, api_key,
            date_from=date_from,
            date_to=date_to,
        )
        records = data.get("data", [])
        total_count = data.get("total", 0)

        for txn in records:
            is_room_revenue = (
                txn.get("category") == "Room Revenue"
                and txn.get("transactionType") == "debit"
                and not txn.get("isDeleted", False)
            )
            if not is_room_revenue:
                continue
            txn_date_str = txn.get("serviceDate") or txn.get("transactionDateTime") or ""
            try:
                txn_date = date.fromisoformat(txn_date_str[:10])
            except (ValueError, TypeError):
                continue
            amount = float(_safe_decimal(txn.get("amount")) or 0)
            daily_rev[txn_date] = daily_rev.get(txn_date, 0.0) + amount

        logger.info("Daily rev page %d/%d — %d txns, %d dates so far",
                    page, (total_count // PAGE_SIZE) + 1, len(records), len(daily_rev))
        if (page - 1) * PAGE_SIZE + len(records) >= total_count or not records:
            break
        page += 1

    # Write to daily_metrics — only fill gaps (revenue_native IS NULL or 0).
    # Insights data written by sync_cloudbeds_filtered takes priority and must
    # not be overwritten by partial transaction coverage.
    updated = 0
    for d, rev in daily_rev.items():
        rev_vnd = round(rev * rate, 2) if rate is not None else None
        _s = SessionLocal()
        try:
            _s.execute(_text(
                "UPDATE daily_metrics "
                "SET revenue_native=:n, revenue_vnd=:v, computed_at=NOW() "
                "WHERE branch_id=CAST(:bid AS UUID) AND date=:d "
                "AND (revenue_native IS NULL OR revenue_native = 0)"
            ), {"n": round(rev, 2), "v": rev_vnd, "bid": branch_id, "d": d})
            _s.commit()
            updated += 1
        except Exception as e:
            _s.rollback()
            logger.warning("Daily rev write failed date %s: %s", d, e)
        finally:
            _s.close()

    logger.info("Daily revenue sync complete branch %s: %d dates updated", branch_id, updated)
    return {"branch_id": branch_id, "dates_updated": updated, "date_from": str(date_from), "date_to": str(date_to)}


def sync_branch_revenue(
    branch_id: str,
    property_id: str,
    currency: str,
    api_key: Optional[str] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
) -> dict:
    """
    Pull Accommodation transactions from Cloudbeds and update grand_total_native
    for matching reservations. Uses getTransactions?category=Accommodation which
    returns bulk revenue data without needing per-reservation API calls.

    Groups transaction amounts by reservationID, sums them, and writes to DB.
    """
    if not api_key:
        raise ValueError(f"No API key for property {property_id}")

    today = date.today()
    if date_from is None:
        # Default: first day of current month — past months already settled, skip them
        date_from = today.replace(day=1)
    if date_to is None:
        date_to = today + timedelta(days=CHECKIN_FUTURE_DAYS)

    db = SessionLocal()
    page = 1
    revenue_map: dict[str, float] = {}   # cloudbeds_reservation_id → total accommodation revenue
    room_type_map: dict[str, str] = {}    # cloudbeds_reservation_id → roomTypeName

    logger.info(
        "Revenue sync branch %s property %s [checkIn %s → %s]",
        branch_id, property_id, date_from, date_to,
    )

    try:
        rate = get_cached_rate(currency, "VND")

        # Fetch all transaction pages
        while True:
            data = _fetch_transactions_page(
                property_id, page, api_key,
                checkin_from=date_from,
                checkin_to=date_to,
            )
            records = data.get("data", [])
            total_count = data.get("total", 0)

            for txn in records:
                res_id = str(txn.get("reservationID", ""))
                # USALI Room Revenue: only "Room Revenue" debit transactions.
                # Excludes OTA credits (Booking.com, Expedia, etc.), cash/CC payments,
                # Items & Services, cancellation fees, and taxes — per USALI standards.
                is_room_revenue = (
                    txn.get("category") == "Room Revenue"
                    and txn.get("transactionType") == "debit"
                    and not txn.get("isDeleted", False)
                )
                if is_room_revenue:
                    amount = _safe_decimal(txn.get("amount")) or 0.0
                    revenue_map[res_id] = revenue_map.get(res_id, 0.0) + amount
                rt = txn.get("roomTypeName")
                if rt and res_id not in room_type_map:
                    room_type_map[res_id] = rt

            logger.info(
                "Revenue page %d/%d — %d transactions for %d reservations",
                page, (total_count // PAGE_SIZE) + 1, len(records), len(revenue_map),
            )

            fetched_so_far = (page - 1) * PAGE_SIZE + len(records)
            if fetched_so_far >= total_count or not records:
                break
            page += 1

        # Update DB — raw SQL per row, commit every BATCH_SIZE rows to avoid
        # pgBouncer executemany failures while keeping round-trips reasonable.
        BATCH_SIZE = 20
        updated = 0
        now = datetime.now(timezone.utc)
        items = list(revenue_map.items())
        for batch_start in range(0, len(items), BATCH_SIZE):
            batch = items[batch_start: batch_start + BATCH_SIZE]
            _s = SessionLocal()
            try:
                for cloudbeds_id, total_rev in batch:
                    native = round(total_rev, 2)
                    vnd = round(total_rev * rate, 2) if rate is not None else None
                    _s.execute(_text(
                        "UPDATE reservations SET grand_total_native=:n, grand_total_vnd=:v, updated_at=:t "
                        "WHERE cloudbeds_reservation_id=:cid"
                    ), {"n": native, "v": vnd, "t": now, "cid": str(cloudbeds_id)})
                    if cloudbeds_id in room_type_map:
                        rt = room_type_map[cloudbeds_id]
                        _s.execute(_text(
                            "UPDATE reservations SET room_type=:rt, room_type_category=:rc "
                            "WHERE cloudbeds_reservation_id=:cid AND room_type IS NULL"
                        ), {"rt": rt, "rc": map_room_type_category(rt), "cid": str(cloudbeds_id)})
                    updated += 1
                _s.commit()
            except Exception as e:
                _s.rollback()
                logger.warning("Revenue batch write failed at row %d: %s", batch_start, e)
            finally:
                _s.close()

        fallback_updated = 0
        # Cloudbeds bulk getReservations returns lite payload (no accommodation total).
        # For NULL reservations, use backfill_accommodation_total() as a one-time job.

        logger.info(
            "Revenue sync complete branch %s: %d from transactions + %d from fallback",
            branch_id, updated, fallback_updated,
        )
        return {
            "branch_id": branch_id,
            "revenue_reservations_updated": updated,
            "revenue_fallback_updated": fallback_updated,
        }

    except Exception as exc:
        logger.error("Revenue sync failed for branch %s: %s", branch_id, exc)
        raise
    finally:
        db.close()


def backfill_accommodation_total(
    branch_id: str,
    property_id: str,
    currency: str,
    api_key: str,
    checkin_from: Optional[date] = None,
    checkin_to: Optional[date] = None,
    limit: Optional[int] = None,
) -> dict:
    """
    One-time backfill: for reservations with NULL grand_total_native, call
    getReservation individually and store balanceDetailed.subTotal (accommodation only).
    Slow (~1s/call) — run once, not in the daily sync loop.
    """
    today = date.today()
    df = checkin_from or today.replace(day=1)
    dt = checkin_to or (today + timedelta(days=CHECKIN_FUTURE_DAYS))

    db = SessionLocal()
    from sqlalchemy import or_
    query = db.query(Reservation).filter(
        Reservation.branch_id == branch_id,
        Reservation.check_in_date >= df,
        Reservation.check_in_date <= dt,
        or_(
            Reservation.grand_total_native == None,  # noqa: E711
            Reservation.grand_total_native == 0,
        ),
        Reservation.status.notin_(["cancelled", "canceled", "no_show", "noshow"]),
    )
    if limit:
        query = query.limit(limit)
    null_res = query.all()
    db.close()

    rate = get_cached_rate(currency, "VND")
    total_fetched = filled = 0
    now = datetime.now(timezone.utc)
    BATCH_SIZE = 20

    logger.info("Backfill: %d NULL reservations for branch %s", len(null_res), branch_id)

    with httpx.Client(timeout=30) as client:
        # (cb_id, accom, rate_plan, room_type, guest_country_iso)
        batch_buf: list[tuple[str, float, Optional[str], Optional[str], Optional[str]]] = []
        for i, r in enumerate(null_res):
            try:
                resp = client.get(
                    f"{CLOUDBEDS_BASE_URL}/getReservation",
                    headers=_headers(api_key),
                    params={"propertyID": property_id, "reservationID": r.cloudbeds_reservation_id},
                )
                resp.raise_for_status()
                res_data = resp.json().get("data") or {}
                bd = res_data.get("balanceDetailed") or {}
                sub = float(_safe_decimal(bd.get("subTotal")) or 0)
                extra = float(_safe_decimal(bd.get("additionalItems")) or 0)
                accom = sub - extra

                # Also extract rate plan and room type while we have the full response
                rate_plan = None
                room_type_name = None
                for room_list_key in ("assigned", "unassigned"):
                    rooms = res_data.get(room_list_key) or {}
                    if isinstance(rooms, dict):
                        rooms = list(rooms.values())
                    for room in rooms:
                        if not rate_plan:
                            rate_plan = room.get("ratePlanNamePublic") or room.get("ratePlanNamePrivate")
                        if not room_type_name:
                            room_type_name = room.get("roomTypeName")

                # Bulk /getReservations doesn't include guestList — only the detail
                # endpoint exposes country. Capture it here while we already have
                # the full response, but only when current value is missing.
                gc_iso = _extract_guest_country_from_detail(res_data) if r.guest_country in (None, "Unknown") else None

                if accom > 0:
                    batch_buf.append((r.cloudbeds_reservation_id, accom, rate_plan, room_type_name if not r.room_type else None, gc_iso))
                total_fetched += 1
            except Exception as e:
                logger.warning("Backfill fetch failed res %s: %s", r.cloudbeds_reservation_id, e)

            # Flush batch every BATCH_SIZE rows
            if len(batch_buf) >= BATCH_SIZE or (i == len(null_res) - 1 and batch_buf):
                _s = SessionLocal()
                try:
                    for cb_id, accom, rp, rt, gc in batch_buf:
                        native = round(accom, 2)
                        vnd = round(accom * rate, 2) if rate else None
                        set_parts = ["grand_total_native=:n", "grand_total_vnd=:v", "updated_at=:t"]
                        params = {"n": native, "v": vnd, "t": now, "cid": cb_id}
                        if rp:
                            set_parts.append("rate_plan_name=:rp")
                            params["rp"] = rp
                        if rt:
                            set_parts.append("room_type=:rt")
                            set_parts.append("room_type_category=:rc")
                            params["rt"] = rt
                            params["rc"] = map_room_type_category(rt)
                        if gc:
                            mapped = map_country_code(gc)
                            set_parts.append("guest_country=:gc")
                            set_parts.append("guest_country_code=:gcc")
                            params["gc"] = mapped
                            params["gcc"] = mapped
                        result = _s.execute(_text(
                            f"UPDATE reservations SET {', '.join(set_parts)} "
                            "WHERE cloudbeds_reservation_id=:cid"
                        ), params)
                        if result.rowcount:
                            filled += 1
                    _s.commit()
                except Exception as e:
                    _s.rollback()
                    logger.warning("Backfill batch write failed: %s", e)
                finally:
                    _s.close()
                batch_buf.clear()

            if (i + 1) % 50 == 0:
                logger.info("Backfill progress: %d/%d fetched, %d filled", i + 1, len(null_res), filled)

    logger.info("Backfill complete branch %s: %d fetched, %d filled", branch_id, total_fetched, filled)
    return {"branch_id": branch_id, "fetched": total_fetched, "filled": filled}


def backfill_room_type_and_rate_plan(
    branch_id: str,
    property_id: str,
    api_key: str,
    checkin_from: Optional[date] = None,
    checkin_to: Optional[date] = None,
    limit: Optional[int] = None,
) -> dict:
    """
    Backfill room_type (and rate_plan_name if available) by calling
    getReservation individually for each reservation with NULL room_type.
    The Cloudbeds roomTypeName includes the rate plan name in parentheses,
    e.g. 'Female Dorm* (CRM_April 2026)'. This is the primary field for CRM filtering.

    Two-pass strategy: rows updated within the last 2 hours run uncapped
    (so newly-synced bookings always get filled in the same cron tick),
    older backlog rows are capped by `limit` and processed newest-first.
    Without this, backlog of old NULL rows starved new bookings out of the
    150-slot budget and quota counts undercounted for hours.
    """
    import time

    today = date.today()
    df = checkin_from or (today - timedelta(days=30))
    dt = checkin_to or today

    # Two-pass query: row vừa được bulk-synced trong 2h qua được fill TRƯỚC và
    # không bị `limit` cắt, đảm bảo booking mới (rate_plan_name/room_type NULL
    # do bulk /getReservations không trả về) luôn lọt trong cùng cron tick.
    # Pass 2 mới xử lý backlog cũ và áp dụng `limit` để bảo vệ timeout cron.
    cutoff = datetime.now(timezone.utc) - timedelta(hours=2)
    db = SessionLocal()
    base = db.query(Reservation).filter(
        Reservation.branch_id == branch_id,
        Reservation.check_in_date >= df,
        Reservation.check_in_date <= dt,
        Reservation.room_type == None,  # noqa: E711  — target NULL room_type
        Reservation.cloudbeds_reservation_id != None,  # noqa: E711
    )
    recent = base.filter(Reservation.updated_at >= cutoff).all()
    backlog_q = (
        base.filter(Reservation.updated_at < cutoff)
            .order_by(Reservation.updated_at.desc())
    )
    if limit:
        backlog_q = backlog_q.limit(limit)
    backlog = backlog_q.all()
    null_res = recent + backlog
    db.close()

    total_fetched = room_type_filled = rate_plan_filled = 0
    now = datetime.now(timezone.utc)
    BATCH_SIZE = 20

    logger.info("Room type backfill: %d reservations for branch %s", len(null_res), branch_id)

    with httpx.Client(timeout=30) as client:
        # (cb_id, room_type, rate_plan, guest_country_iso)
        batch_buf: list[tuple[str, Optional[str], Optional[str], Optional[str]]] = []
        for i, r in enumerate(null_res):
            try:
                resp = client.get(
                    f"{CLOUDBEDS_BASE_URL}/getReservation",
                    headers=_headers(api_key),
                    params={"propertyID": property_id, "reservationID": r.cloudbeds_reservation_id},
                )
                resp.raise_for_status()
                data = resp.json().get("data") or {}

                # Extract room type and rate plan from assigned/unassigned rooms
                rate_plan = None
                room_type_name = None
                for room_list_key in ("assigned", "unassigned"):
                    rooms = data.get(room_list_key) or {}
                    if isinstance(rooms, dict):
                        rooms = list(rooms.values())
                    for room in rooms:
                        if not rate_plan:
                            rate_plan = room.get("ratePlanNamePublic") or room.get("ratePlanNamePrivate")
                        if not room_type_name:
                            room_type_name = room.get("roomTypeName")
                # Cloudbeds returns NULL on both ratePlanName fields for many
                # bookings (esp. CRM event rates) — fall back to the trailing
                # parens segment of roomTypeName, which Cloudbeds always embeds.
                if not rate_plan:
                    rate_plan = extract_rate_plan_from_room_type(room_type_name)

                # Country lives in guestList, only in detail responses — capture
                # opportunistically when current row is missing it.
                gc_iso = _extract_guest_country_from_detail(data) if r.guest_country in (None, "Unknown") else None

                batch_buf.append((r.cloudbeds_reservation_id, room_type_name, rate_plan, gc_iso))
                total_fetched += 1
            except Exception as e:
                logger.warning("Room type backfill fetch failed res %s: %s", r.cloudbeds_reservation_id, e)

            # Flush batch
            if len(batch_buf) >= BATCH_SIZE or (i == len(null_res) - 1 and batch_buf):
                _s = SessionLocal()
                try:
                    for cb_id, rt, rp, gc in batch_buf:
                        updates = ["updated_at=:t"]
                        params = {"t": now, "cid": cb_id}
                        if rt:
                            updates.append("room_type=:rt")
                            updates.append("room_type_category=:rc")
                            params["rt"] = rt
                            params["rc"] = map_room_type_category(rt)
                        if rp:
                            updates.append("rate_plan_name=:rp")
                            params["rp"] = rp
                        if gc:
                            mapped = map_country_code(gc)
                            updates.append("guest_country=:gc")
                            updates.append("guest_country_code=:gcc")
                            params["gc"] = mapped
                            params["gcc"] = mapped
                        if len(updates) > 1:  # more than just updated_at
                            result = _s.execute(_text(
                                f"UPDATE reservations SET {', '.join(updates)} "
                                "WHERE cloudbeds_reservation_id=:cid AND room_type IS NULL"
                            ), params)
                            if result.rowcount:
                                room_type_filled += 1
                                if rp:
                                    rate_plan_filled += 1
                    _s.commit()
                except Exception as e:
                    _s.rollback()
                    logger.warning("Room type backfill batch write failed: %s", e)
                finally:
                    _s.close()
                batch_buf.clear()

            if (i + 1) % 50 == 0:
                logger.info("Room type backfill progress: %d/%d fetched, %d room types filled",
                            i + 1, len(null_res), room_type_filled)

            # Rate limit: 0.2s between API calls
            time.sleep(0.2)

    logger.info("Room type backfill complete branch %s: %d fetched, %d room types, %d rate plans",
                branch_id, total_fetched, room_type_filled, rate_plan_filled)
    return {
        "branch_id": branch_id,
        "fetched": total_fetched,
        "room_types_filled": room_type_filled,
        "rate_plans_filled": rate_plan_filled,
    }


def backfill_guest_country(
    branch_id: str,
    property_id: str,
    api_key: str,
    checkin_from: Optional[date] = None,
    checkin_to: Optional[date] = None,
    limit: Optional[int] = None,
) -> dict:
    """Dedicated backfill of guest_country for rows currently 'Unknown'.

    Calls /getReservation per row and reads ISO-2 country from
    guestList[*].guestCountry. Skips rows where Cloudbeds also returns no
    country (rows stay 'Unknown'). Mutates only guest_country and
    guest_country_code; raw_data is not touched.

    Default window: 2025-01-01 to today+365d. Pre-2025 reservations have
    no guestCountry in either bulk OR detail responses (verified empirically),
    so backfilling them is wasted API calls. The forward year covers future
    check-ins (booked-now-stay-later) which dominate "By Date Booked" views;
    without it, recently-booked rows for far-future stays stay Unknown forever.
    """
    import time

    today = date.today()
    df = checkin_from or date(2025, 1, 1)
    dt = checkin_to or (today + timedelta(days=365))

    db = SessionLocal()
    query = db.query(Reservation).filter(
        Reservation.branch_id == branch_id,
        Reservation.check_in_date >= df,
        Reservation.check_in_date <= dt,
        Reservation.guest_country == "Unknown",
        Reservation.cloudbeds_reservation_id != None,  # noqa: E711
    )
    if limit:
        query = query.limit(limit)
    targets = query.all()
    db.close()

    total_fetched = filled = empty = 0
    now = datetime.now(timezone.utc)
    BATCH_SIZE = 20

    logger.info("Guest country backfill: %d Unknown rows for branch %s (%s..%s)",
                len(targets), branch_id, df, dt)

    # Periodic progress flush: without this, if many consecutive rows return
    # empty guestCountry from Cloudbeds (common for old check-outs), batch_buf
    # never reaches BATCH_SIZE and the run looks stalled for tens of minutes
    # until either the next country-bearing row hits or the loop ends.
    PROGRESS_FLUSH_EVERY = 100

    with httpx.Client(timeout=30) as client:
        # (cb_id, guest_country_iso)
        batch_buf: list[tuple[str, str]] = []
        for i, r in enumerate(targets):
            try:
                resp = client.get(
                    f"{CLOUDBEDS_BASE_URL}/getReservation",
                    headers=_headers(api_key),
                    params={"propertyID": property_id, "reservationID": r.cloudbeds_reservation_id},
                )
                resp.raise_for_status()
                data = resp.json().get("data") or {}
                gc_iso = _extract_guest_country_from_detail(data)
                total_fetched += 1
                if gc_iso:
                    batch_buf.append((r.cloudbeds_reservation_id, gc_iso))
                else:
                    empty += 1
            except Exception as e:
                logger.warning("Guest country backfill failed res %s: %s", r.cloudbeds_reservation_id, e)

            # Flush batch when full, every PROGRESS_FLUSH_EVERY iterations, or at end
            should_flush = (
                len(batch_buf) >= BATCH_SIZE
                or (i == len(targets) - 1 and batch_buf)
                or ((i + 1) % PROGRESS_FLUSH_EVERY == 0 and batch_buf)
            )
            if should_flush:
                _s = SessionLocal()
                try:
                    for cb_id, gc in batch_buf:
                        mapped = map_country_code(gc)
                        result = _s.execute(_text(
                            "UPDATE reservations "
                            "SET guest_country=:gc, guest_country_code=:gcc, updated_at=:t "
                            "WHERE cloudbeds_reservation_id=:cid AND guest_country = 'Unknown'"
                        ), {"gc": mapped, "gcc": mapped, "t": now, "cid": cb_id})
                        if result.rowcount:
                            filled += 1
                    _s.commit()
                except Exception as e:
                    _s.rollback()
                    logger.warning("Guest country backfill batch write failed: %s", e)
                finally:
                    _s.close()
                batch_buf.clear()

            if (i + 1) % 50 == 0:
                logger.info("Guest country progress: %d/%d fetched, %d filled, %d still empty",
                            i + 1, len(targets), filled, empty)

            # Rate limit: 0.2s between API calls (matches existing backfills)
            time.sleep(0.2)

    logger.info("Guest country backfill complete branch %s: %d fetched, %d filled, %d still empty",
                branch_id, total_fetched, filled, empty)
    return {
        "branch_id": branch_id,
        "fetched": total_fetched,
        "filled": filled,
        "still_unknown": empty,
    }


def _fetch_reservations_page(
    property_id: str,
    page: int,
    api_key: Optional[str] = None,
    modified_since: Optional[date] = None,
    checkin_from: Optional[date] = None,
    checkin_to: Optional[date] = None,
) -> dict:
    params: dict = {
        "propertyID": property_id,
        "pageNumber": page,
        "pageSize": PAGE_SIZE,
    }
    if modified_since:
        params["modifiedAt[gte]"] = modified_since.isoformat()
    if checkin_from:
        params["checkIn[gte]"] = checkin_from.isoformat()
    if checkin_to:
        params["checkIn[lte]"] = checkin_to.isoformat()
    with httpx.Client(timeout=60) as client:
        response = client.get(
            f"{CLOUDBEDS_BASE_URL}/getReservations",
            headers=_headers(api_key),
            params=params,
        )
        response.raise_for_status()
        return response.json()


def pull_reservations(
    property_id: str, modified_since: Optional[date] = None, api_key: Optional[str] = None
) -> list[dict]:
    """Fetch all reservation pages for a property from Cloudbeds."""
    if modified_since is None:
        modified_since = date.today() - timedelta(days=SYNC_LOOKBACK_DAYS)

    all_reservations: list[dict] = []
    page = 1

    try:
        while True:
            data = _fetch_reservations_page(property_id, page, api_key, modified_since)
            records = data.get("data", [])
            all_reservations.extend(records)

            total = data.get("total", 0)
            if len(all_reservations) >= total or not records:
                break
            page += 1

        logger.info("Pulled %d reservations for property %s", len(all_reservations), property_id)
    except Exception as exc:
        logger.error("Failed to pull reservations for property %s: %s", property_id, exc)
        raise

    return all_reservations


# ── Ingestion ──────────────────────────────────────────────────────────────────

def _parse_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def ingest_reservations(
    db: Session,
    branch_id: str,
    currency: str,
    raw_records: list[dict],
) -> tuple[int, int]:
    """
    Upsert reservation rows. Returns (created_count, updated_count).
    Converts grand_total to VND using cached rate (sync context).
    """
    created = updated = 0
    rate = get_cached_rate(currency, "VND")

    for raw in raw_records:
        cloudbeds_id = str(raw.get("reservationID", ""))
        if not cloudbeds_id:
            continue

        check_in = _parse_date(raw.get("startDate"))
        check_out = _parse_date(raw.get("endDate"))
        if not check_in or not check_out:
            continue

        nights = (check_out - check_in).days
        # Revenue = accommodation only (subTotal - additionalItems), NOT Cloudbeds "total"
        # (which includes tax, fees, extras). balanceDetailed is only present in full
        # payloads (modifiedAt filter); lite payloads skip revenue fields entirely
        # and let backfill_accommodation_total fetch getReservation later.
        bd = raw.get("balanceDetailed") or {}
        bd_sub = _safe_decimal(bd.get("subTotal"))
        bd_extra = _safe_decimal(bd.get("additionalItems")) or 0
        accom_native = (
            round(float(bd_sub) - float(bd_extra), 2)
            if bd_sub is not None else None
        )
        has_revenue = accom_native is not None and accom_native > 0
        grand_total_native = accom_native if has_revenue else None
        grand_total_vnd = (
            round(grand_total_native * rate, 2)
            if grand_total_native is not None and rate is not None
            else None
        )

        room_type = raw.get("roomTypeName")
        source = raw.get("sourceName") or raw.get("sourceID")
        guest_country = raw.get("guestCountry")

        # Base payload — fields always present in the API response
        payload = dict(
            branch_id=branch_id,
            check_in_date=check_in,
            check_out_date=check_out,
            nights=nights,
            status=raw.get("status"),
            adults=raw.get("adults"),
            cancellation_date=_parse_date(raw.get("cancellationDate")),
            reservation_date=_parse_date(raw.get("dateCreated")),
        )

        # Rate plan name — Cloudbeds returns NULL for both ratePlanNamePublic
        # and ratePlanNamePrivate on many bookings (esp. CRM/event rates), so we
        # fall back to the trailing parens of room_type, which Cloudbeds always
        # embeds. See extract_rate_plan_from_room_type().
        rate_plan = (
            raw.get("ratePlanNamePublic")
            or raw.get("ratePlanNamePrivate")
            or extract_rate_plan_from_room_type(room_type)
        )
        if rate_plan is not None:
            payload["rate_plan_name"] = rate_plan

        # Only update enriched fields when the API returned them
        if room_type is not None:
            payload["room_type"] = room_type
            payload["room_type_category"] = map_room_type_category(room_type)
        if source is not None:
            payload["source"] = normalize_source(source)
            payload["source_category"] = map_source_category(source)
        # guest_country: only set if the API actually returned a value. The bulk
        # /getReservations endpoint does NOT return guestCountry — it lives in
        # /getReservation > guestList[*].guestCountry and is filled in later by
        # backfill_guest_country. Setting it unconditionally here would clobber
        # previously backfilled country back to "Unknown" on every modified-window
        # sync, since the bulk payload always returns NULL for this field.
        if guest_country is not None and str(guest_country).strip():
            payload["guest_country"] = map_country_code(guest_country)
            payload["guest_country_code"] = map_country_code(guest_country)
        if has_revenue:
            payload["grand_total_native"] = grand_total_native
            payload["grand_total_vnd"] = grand_total_vnd
        if raw:
            payload["raw_data"] = raw

        existing = db.query(Reservation).filter_by(cloudbeds_reservation_id=cloudbeds_id).first()
        if existing:
            for k, v in payload.items():
                setattr(existing, k, v)
            updated += 1
        else:
            # New reservation — set branch_id and all available fields
            payload["branch_id"] = branch_id
            if "room_type" not in payload:
                payload["room_type"] = room_type
                payload["room_type_category"] = map_room_type_category(room_type)
            if "source" not in payload:
                payload["source"] = normalize_source(source)
                payload["source_category"] = map_source_category(source)
            if "guest_country" not in payload:
                payload["guest_country"] = map_country_code(guest_country)
                payload["guest_country_code"] = map_country_code(guest_country)
            db.add(Reservation(cloudbeds_reservation_id=cloudbeds_id, **payload))
            created += 1

    db.commit()
    logger.info("Ingested %d created, %d updated for branch %s", created, updated, branch_id)
    return created, updated


def _safe_decimal(value) -> Optional[float]:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


# ── Room count ─────────────────────────────────────────────────────────────────

def fetch_total_rooms(property_id: str, api_key: Optional[str] = None) -> int:
    """
    Call Cloudbeds getRooms (paginated) and return the total physical room/bed count.
    API returns count=20 per page with a `total` field for the full count.
    """
    url = f"{CLOUDBEDS_BASE_URL}/getRooms"
    with httpx.Client(timeout=30) as client:
        resp = client.get(url, headers=_headers(api_key), params={"propertyID": property_id, "pageSize": 1})
        resp.raise_for_status()
        payload = resp.json()

    if not payload.get("success"):
        raise RuntimeError(f"getRooms error for property {property_id}: {payload.get('message')}")

    # `total` is the true count of all physical units across all pages
    total = payload.get("total")
    if total is not None:
        return int(total)

    # Fallback: sum rooms from data if total not present
    data = payload.get("data") or []
    return sum(len(item.get("rooms", [])) for item in data)


# ── Orchestration ──────────────────────────────────────────────────────────────

def sync_branch(
    branch_id: str,
    property_id: str,
    currency: str,
    api_key: Optional[str] = None,
    incremental: bool = False,
    lookback_days: int = 2,
    checkin_from: Optional[date] = None,
    checkin_to: Optional[date] = None,
) -> dict:
    """
    Sync a single branch — ingest page-by-page for progressive DB writes.

    incremental=True:  use modifiedAt (last `lookback_days` days, default 2) —
                       catches cancellations/modifications.
    incremental=False: use checkIn window (past CHECKIN_LOOKBACK_DAYS → future CHECKIN_FUTURE_DAYS)
                       to populate ALL reservations in the date range regardless of when modified.
    checkin_from/to:   explicit override — used by daily sync to limit to current+next month only.
    Revenue (grand_total_native) is populated separately via sync_branch_revenue.
    """
    if not api_key:
        raise ValueError(f"No API key for property {property_id}")

    today = date.today()
    db = SessionLocal()
    total_created = total_updated = 0
    page = 1

    if checkin_from is not None or checkin_to is not None:
        # Explicit date range — used for focused/daily sync
        fetch_kwargs = {"checkin_from": checkin_from, "checkin_to": checkin_to}
        window_desc  = f"checkIn {checkin_from} → {checkin_to}"
    elif incremental:
        fetch_kwargs = {"modified_since": today - timedelta(days=lookback_days)}
        window_desc = f"modifiedAt {lookback_days}d"
    else:
        _from = today - timedelta(days=CHECKIN_LOOKBACK_DAYS)
        _to   = today + timedelta(days=CHECKIN_FUTURE_DAYS)
        fetch_kwargs = {"checkin_from": _from, "checkin_to": _to}
        window_desc  = f"checkIn {_from} → {_to}"

    logger.info("Syncing branch %s property %s [%s]", branch_id, property_id, window_desc)

    try:
        while True:
            data = _fetch_reservations_page(property_id, page, api_key, **fetch_kwargs)
            records = data.get("data", [])
            total_count = data.get("total", 0)
            total_pages = (total_count // PAGE_SIZE) + 1

            if records:
                created, updated = ingest_reservations(db, branch_id, currency, records)
                total_created += created
                total_updated += updated
                logger.info(
                    "Branch %s page %d/%d — +%d created, ~%d updated",
                    branch_id, page, total_pages, created, updated,
                )

            fetched_so_far = (page - 1) * PAGE_SIZE + len(records)
            if fetched_so_far >= total_count or not records:
                break
            page += 1

        logger.info(
            "Sync complete for branch %s: %d created, %d updated",
            branch_id, total_created, total_updated,
        )
        return {"branch_id": branch_id, "created": total_created, "updated": total_updated}
    except Exception as exc:
        logger.error("Sync failed for branch %s: %s", branch_id, exc)
        raise
    finally:
        db.close()


# ── Reservation Daily population (v2.0) ───────────────────────────────────────

def _fetch_nightly_rates_from_transactions(
    property_id: str,
    currency: str,
    api_key: Optional[str],
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
) -> dict[str, dict[date, float]]:
    """
    Fetch Room Revenue transactions from Cloudbeds grouped by (reservationID, serviceDate).
    Returns: { cloudbeds_reservation_id: { date: nightly_rate } }

    Cloudbeds posts one Room Revenue debit transaction per night of stay on the
    serviceDate = the actual night. This gives us TRUE per-night rates without
    any proration math needed.
    """
    rev_map: dict[str, dict[date, float]] = {}  # res_id → { date → amount }

    page = 1
    while True:
        data = _fetch_transactions_by_date_page(
            property_id, page, api_key,
            date_from=date_from, date_to=date_to,
        )
        records = data.get("data", [])
        total_count = data.get("total", 0)

        for txn in records:
            is_room_revenue = (
                txn.get("category") == "Room Revenue"
                and txn.get("transactionType") == "debit"
                and not txn.get("isDeleted", False)
            )
            if not is_room_revenue:
                continue

            res_id = str(txn.get("reservationID", ""))
            if not res_id:
                continue

            txn_date_str = txn.get("serviceDate") or txn.get("transactionDateTime") or ""
            try:
                txn_date = date.fromisoformat(txn_date_str[:10])
            except (ValueError, TypeError):
                continue

            amount = float(_safe_decimal(txn.get("amount")) or 0)
            if res_id not in rev_map:
                rev_map[res_id] = {}
            rev_map[res_id][txn_date] = rev_map[res_id].get(txn_date, 0.0) + amount

        fetched_so_far = (page - 1) * PAGE_SIZE + len(records)
        if fetched_so_far >= total_count or not records:
            break
        page += 1

    logger.info(
        "Fetched nightly rates for %d reservations from Cloudbeds transactions [%s → %s]",
        len(rev_map), date_from, date_to,
    )
    return rev_map


def populate_reservation_daily(
    db: Session,
    branch_id: str,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    property_id: Optional[str] = None,
    currency: Optional[str] = None,
    api_key: Optional[str] = None,
) -> int:
    """
    v2.0: Expand reservations into per-night rows in reservation_daily.

    Strategy:
    1. PRIMARY — If Cloudbeds API credentials are provided, fetch actual per-night
       Room Revenue transactions (serviceDate-based). Each night gets its true rate.
    2. FALLBACK — If no API access or no transaction found for a night, prorate:
       nightly_rate = grand_total_native / nights.

    Upserts on (reservation_id, date). Returns count of rows written.
    """
    from app.models.reservation_daily import ReservationDaily

    # ── Fetch actual nightly rates from Cloudbeds if API available ────────
    txn_rates: dict[str, dict[date, float]] = {}
    if property_id and api_key:
        try:
            txn_rates = _fetch_nightly_rates_from_transactions(
                property_id, currency or "USD", api_key,
                date_from=date_from, date_to=date_to,
            )
        except Exception as e:
            logger.warning(
                "Failed to fetch nightly rates from Cloudbeds for branch %s: %s. "
                "Falling back to proration.", branch_id, e,
            )

    # ── Build cloudbeds_id → txn_rates lookup ────────────────────────────
    # txn_rates keys are cloudbeds_reservation_id, need to map to DB reservation.id

    q = db.query(Reservation).filter(Reservation.branch_id == branch_id)
    if date_from:
        q = q.filter(Reservation.check_out_date > date_from)
    if date_to:
        q = q.filter(Reservation.check_in_date <= date_to)

    reservations = q.all()

    # Map cloudbeds_id → per-night rates
    cb_id_to_rates: dict[str, dict[date, float]] = {}
    for cb_id, rates in txn_rates.items():
        cb_id_to_rates[cb_id] = rates

    vnd_rate = get_cached_rate(currency or "USD", "VND") if currency else None
    count = 0

    for res in reservations:
        if not res.check_in_date or not res.check_out_date:
            continue
        nights = res.nights or (res.check_out_date - res.check_in_date).days
        if nights <= 0:
            continue

        # Try to get actual per-night rates from Cloudbeds transactions
        actual_rates = cb_id_to_rates.get(res.cloudbeds_reservation_id, {})

        # Fallback proration
        grand_total = float(res.grand_total_native or 0)
        fallback_rate = round(grand_total / nights, 2) if nights > 0 else 0.0

        # Determine room_id (first room from comma-separated list)
        room_id = None
        if res.room_number:
            for rm in str(res.room_number).split(","):
                rm = rm.strip()
                if rm:
                    room_id = rm
                    break

        current = res.check_in_date
        end = res.check_out_date
        while current < end:
            # Use actual Cloudbeds nightly rate if available, otherwise prorate
            if current in actual_rates:
                night_rate = round(actual_rates[current], 2)
            else:
                night_rate = fallback_rate

            night_rate_vnd = round(night_rate * vnd_rate, 2) if vnd_rate else None

            existing = db.query(ReservationDaily).filter_by(
                reservation_id=res.id, date=current,
            ).first()

            if existing:
                existing.nightly_rate = night_rate
                existing.nightly_rate_vnd = night_rate_vnd
                existing.status = res.status
                existing.source = res.source
                existing.source_category = res.source_category
                existing.room_type_category = res.room_type_category
                existing.room_id = room_id
            else:
                db.add(ReservationDaily(
                    reservation_id=res.id,
                    branch_id=branch_id,
                    date=current,
                    room_id=room_id,
                    nightly_rate=night_rate,
                    nightly_rate_vnd=night_rate_vnd,
                    status=res.status,
                    source=res.source,
                    source_category=res.source_category,
                    room_type_category=res.room_type_category,
                ))

            count += 1
            current += timedelta(days=1)

    db.commit()
    logger.info("Populated %d reservation_daily rows for branch %s", count, branch_id)
    return count


# ── Cloudbeds Data Insights — Occupancy (v2.1) ────────────────────────────────

INSIGHTS_BASE_URL = "https://api.cloudbeds.com/datainsights/v1.1"
OCCUPANCY_STOCK_REPORT_ID = "110"  # "Rooms Sold, ADR, RevPar and Occupancy"


def fetch_cloudbeds_occupancy(
    property_id: str,
    api_key: str,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
) -> dict[date, dict]:
    """
    Fetch per-day occupancy + revenue from Cloudbeds Data Insights API.

    Uses POST /reports (a temporary custom report on dataset 7) — NOT stock
    report 110. Stock 110's report config has a hardcoded date filter
    (`stay_date >= start_last_month AND stay_date < months_later;2`) so it
    only ever returns a ~95 day window around the current date and ignores
    GET query date params (verified empirically 2026-05-05). Custom reports
    accept explicit `stay_date` filters in the POST body, so this function
    works for ANY date range including historical (2025+) which previously
    came back as rev=0. The temp report is deleted right after fetching.

    Returns: { date: { rooms_sold, occupancy, mfd_occupancy, adr, revpar,
                        room_revenue, capacity_count, blocked, out_of_service } }
    """
    import calendar
    if date_from is None:
        today = date.today()
        date_from = today.replace(day=1)
    if date_to is None:
        today = date.today()
        last_day = calendar.monthrange(today.year, today.month)[1]
        date_to = today.replace(day=last_day)

    headers_post = {
        "Authorization": f"Bearer {api_key}",
        "X-PROPERTY-ID": str(property_id),
        "Content-Type": "application/json",
    }
    headers_get = {
        "Authorization": f"Bearer {api_key}",
        "X-PROPERTY-ID": str(property_id),
    }

    def _f(v) -> float:
        # Cloudbeds responses are inconsistent: some properties return numeric
        # metrics as float, others as string ("85.5"). Cast defensively to
        # avoid 'unsupported operand for /: str/float' downstream.
        if v is None or v == "":
            return 0.0
        try:
            return float(v)
        except (ValueError, TypeError):
            return 0.0

    payload = {
        "title": f"HiD-occ-{date_from.isoformat()}-{date_to.isoformat()}",
        "dataset_id": 7,
        "property_id": str(property_id),
        "property_ids": [str(property_id)],
        "columns": [
            {"cdf": {"type": "default", "column": "rooms_sold"}, "metrics": ["sum"]},
            {"cdf": {"type": "default", "column": "room_revenue"}, "metrics": ["sum"]},
            {"cdf": {"type": "default", "column": "adr"}},
            {"cdf": {"type": "default", "column": "occupancy"}},
            {"cdf": {"type": "default", "column": "mfd_occupancy"}},
            {"cdf": {"type": "default", "column": "revpar"}},
            {"cdf": {"type": "default", "column": "capacity_count"}, "metrics": ["sum"]},
            {"cdf": {"type": "default", "column": "blocked_room_count"}, "metrics": ["sum"]},
            {"cdf": {"type": "default", "column": "out_of_service_count"}, "metrics": ["sum"]},
        ],
        "group_rows": [{"cdf": {"type": "default", "column": "stay_date"}}],
        "filters": {"and": [
            {"cdf": {"type": "default", "column": "stay_date"}, "operator": "greater_than_or_equal", "value": date_from.isoformat()},
            {"cdf": {"type": "default", "column": "stay_date"}, "operator": "less_than_or_equal", "value": date_to.isoformat()},
        ]},
    }

    result: dict[date, dict] = {}
    with httpx.Client(timeout=120) as client:
        resp_create = client.post(f"{INSIGHTS_BASE_URL}/reports", headers=headers_post, json=payload)
        if resp_create.status_code not in (200, 201):
            logger.warning(
                "Occupancy custom-report create failed property=%s status=%d: %s — "
                "falling back to stock report 110 (current ~95 days only)",
                property_id, resp_create.status_code, resp_create.text[:200],
            )
            # Fallback: properties whose API key lacks "Insights: Create Reports"
            # permission can still hit stock 110 (~95 day window). Historical data
            # outside that window is not retrievable for these properties via
            # Insights — would require a full Bookings API sync (full_ingest=true).
            try:
                fb_resp = client.get(
                    f"{INSIGHTS_BASE_URL}/stock_reports/{OCCUPANCY_STOCK_REPORT_ID}/data",
                    headers=headers_get,
                    params={"property_ids": str(property_id)},
                )
                fb_resp.raise_for_status()
                fb_records = fb_resp.json().get("records", {}) or {}
                for ds, m in fb_records.items():
                    try:
                        d_ = date.fromisoformat(ds)
                    except (ValueError, TypeError):
                        continue
                    if date_from and d_ < date_from:
                        continue
                    if date_to and d_ > date_to:
                        continue
                    result[d_] = {
                        "rooms_sold":     _f(m.get("rooms_sold", {}).get("sum")),
                        "occupancy":      _f(m.get("occupancy", {}).get("aggregated")),
                        "mfd_occupancy":  _f(m.get("mfd_occupancy", {}).get("aggregated")),
                        "adr":            _f(m.get("adr", {}).get("aggregated")),
                        "revpar":         _f(m.get("revpar", {}).get("aggregated")),
                        "room_revenue":   _f(m.get("room_revenue", {}).get("sum")),
                        "capacity_count": _f(m.get("capacity_count", {}).get("sum")),
                        "blocked":        _f(m.get("blocked_room_count", {}).get("sum")),
                        "out_of_service": _f(m.get("out_of_service_count", {}).get("sum")),
                    }
                logger.info(
                    "Stock 110 fallback for property %s: %d days [%s → %s]",
                    property_id, len(result), date_from, date_to,
                )
            except Exception as fb_err:
                logger.warning("Stock 110 fallback also failed property=%s: %s", property_id, fb_err)
            return result
        report_id = resp_create.json().get("id")
        try:
            resp = client.get(
                f"{INSIGHTS_BASE_URL}/reports/{report_id}/data",
                headers=headers_get,
                params={"property_ids": str(property_id)},
            )
        finally:
            client.delete(f"{INSIGHTS_BASE_URL}/reports/{report_id}", headers=headers_get)

        if resp.status_code != 200:
            logger.warning(
                "Occupancy report fetch failed property=%s status=%d: %s",
                property_id, resp.status_code, resp.text[:200],
            )
            return result

        body = resp.json()

    records = body.get("records", {}) or {}

    for date_str, metrics in records.items():
        try:
            d = date.fromisoformat(date_str)
        except (ValueError, TypeError):
            continue

        # API filter already constrained range, but re-check defensively
        if date_from and d < date_from:
            continue
        if date_to and d > date_to:
            continue

        result[d] = {
            "rooms_sold":       _f(metrics.get("rooms_sold", {}).get("sum")),
            "occupancy":        _f(metrics.get("occupancy", {}).get("aggregated")),
            "mfd_occupancy":    _f(metrics.get("mfd_occupancy", {}).get("aggregated")),
            "adr":              _f(metrics.get("adr", {}).get("aggregated")),
            "revpar":           _f(metrics.get("revpar", {}).get("aggregated")),
            "room_revenue":     _f(metrics.get("room_revenue", {}).get("sum")),
            "capacity_count":   _f(metrics.get("capacity_count", {}).get("sum")),
            "blocked":          _f(metrics.get("blocked_room_count", {}).get("sum")),
            "out_of_service":   _f(metrics.get("out_of_service_count", {}).get("sum")),
        }

    logger.info(
        "Fetched Cloudbeds occupancy (custom report) for property %s: %d days [%s → %s]",
        property_id, len(result), date_from, date_to,
    )
    return result


def sync_cloudbeds_occupancy(
    db: Session,
    branch_id: str,
    property_id: str,
    currency: str,
    api_key: str,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
) -> dict:
    """
    Sync OCC, total_sold + UNFILTERED revenue/ADR/RevPAR from Cloudbeds Data
    Insights stock report 110 (same source the Cloudbeds Occupancy Report UI
    uses) into daily_metrics.

    NOTE: revenue_native / adr_native / revpar_native written here include ALL
    sources (Blogger / KOL / House Use / Special Case / Work Exchange). They
    are intentionally overridden by sync_cloudbeds_filtered, which applies the
    project ADR rule: filtered revenue / unfiltered rooms_sold. This step still
    writes them so the page has fallback values if the filtered overlay fails.

    Returns: { branch_id, dates_updated, date_from, date_to }
    """
    from app.models.daily_metrics import DailyMetrics

    today = date.today()
    if date_from is None:
        date_from = today.replace(day=1)
    if date_to is None:
        # Sync full month (including future confirmed bookings)
        import calendar
        last_day = calendar.monthrange(today.year, today.month)[1]
        date_to = today.replace(day=last_day)

    occ_data = fetch_cloudbeds_occupancy(property_id, api_key, date_from, date_to)

    updated = 0
    now = datetime.now(timezone.utc)
    rate = get_cached_rate(currency, "VND")

    for d, metrics in sorted(occ_data.items()):
        rooms_sold = int(metrics["rooms_sold"])
        occ_pct = round(metrics["mfd_occupancy"] / 100.0, 4)  # mfd = modified occupancy (excludes blocked/OOS), matches Cloudbeds UI
        adr_native = round(metrics["adr"], 2)
        revpar_native = round(metrics["revpar"], 2)
        revenue_native = round(float(metrics.get("room_revenue") or 0), 2)
        capacity = int(metrics["capacity_count"])

        dm = db.query(DailyMetrics).filter_by(
            branch_id=branch_id, date=d,
        ).first()
        if not dm:
            dm = DailyMetrics(branch_id=branch_id, date=d)
            db.add(dm)

        dm.total_sold = rooms_sold
        dm.occ_pct = occ_pct
        dm.adr_native = adr_native
        dm.revpar_native = revpar_native
        dm.revenue_native = revenue_native
        dm.revenue_vnd = round(revenue_native * rate, 2) if rate else None
        dm.computed_at = now
        updated += 1

    db.commit()
    logger.info(
        "Cloudbeds OCC sync complete branch %s: %d dates updated [%s → %s]",
        branch_id, updated, date_from, date_to,
    )
    return {
        "branch_id": branch_id,
        "dates_updated": updated,
        "date_from": str(date_from),
        "date_to": str(date_to),
    }


def _compute_daily_from_reservation_daily(
    db: Session,
    branch_id: str,
    year: int,
    month: int,
) -> dict:
    """Fallback: compute per-day revenue (filtered) + sold (filtered + unfiltered)
    from reservation_daily when Insights API is unavailable.

    Returns same shape as fetch_filtered_daily:
        {date: {
            total_rev,
            total_sold_excl, total_sold_all,
            room_rev, room_sold_excl, room_sold_all,
            dorm_rev, dorm_sold_excl, dorm_sold_all,
        }}

    ADR rule: revenue excludes Blogger / KOL / House Use / Special Case /
    Work Exchange; rooms_sold (denominator) counts ALL sources.
    """
    from app.models.reservation_daily import ReservationDaily
    from app.services.metrics_engine import EXCLUDED_SOURCES_REVENUE, EXCLUDED_STATUSES
    import calendar

    first_day = date(year, month, 1)
    last_day = date(year, month, calendar.monthrange(year, month)[1])

    rows = db.query(ReservationDaily).filter(
        ReservationDaily.branch_id == branch_id,
        ReservationDaily.date >= first_day,
        ReservationDaily.date <= last_day,
    ).all()

    out: dict = {}
    for rd in rows:
        status = (rd.status or "").lower().strip()
        status_norm = status.replace("-", "_").replace(" ", "_")
        if status in EXCLUDED_STATUSES or status_norm in EXCLUDED_STATUSES:
            # Cancelled / no-show — drop from both filtered and unfiltered
            continue
        d = rd.date
        if d not in out:
            out[d] = {
                "total_rev": 0.0,
                "total_sold_excl": 0.0, "total_sold_all": 0.0,
                "room_rev": 0.0, "room_sold_excl": 0.0, "room_sold_all": 0.0,
                "dorm_rev": 0.0, "dorm_sold_excl": 0.0, "dorm_sold_all": 0.0,
            }

        src = (rd.source or "").lower().strip()
        is_excl_rev = src in EXCLUDED_SOURCES_REVENUE
        rtc = (rd.room_type_category or "").lower()
        night = float(rd.nightly_rate or 0)

        # ALL-sources sold (ADR denominator)
        out[d]["total_sold_all"] += 1
        if rtc == "room":
            out[d]["room_sold_all"] += 1
        elif rtc == "dorm":
            out[d]["dorm_sold_all"] += 1

        if is_excl_rev:
            # Excluded source: counts toward _all only, not toward filtered rev/_excl
            continue

        out[d]["total_rev"] += night
        out[d]["total_sold_excl"] += 1
        if rtc == "room":
            out[d]["room_rev"] += night
            out[d]["room_sold_excl"] += 1
        elif rtc == "dorm":
            out[d]["dorm_rev"] += night
            out[d]["dorm_sold_excl"] += 1

    logger.info(
        "reservation_daily fallback for branch=%s %d/%d: %d days, total_rev=%.0f",
        branch_id, year, month, len(out), sum(v["total_rev"] for v in out.values()),
    )
    return out


def sync_cloudbeds_filtered(
    db: Session,
    branch_id: str,
    property_id: str,
    currency: str,
    api_key: str,
    total_rooms: int,
    total_room_count: int = 0,
    total_dorm_count: int = 0,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
) -> dict:
    """
    Overlay per-day revenue + ADR + RevPAR + room/dorm split into daily_metrics
    using Cloudbeds Data Insights custom reports.

    ADR rule (applied here, overrides values written by sync_cloudbeds_occupancy):
        Revenue   = filtered (excl Blogger / KOL / House Use / Special Case / Work Exchange)
        rooms_sold = ALL sources (no source exclusion — same denominator as OCC)
        ADR       = filtered revenue / unfiltered rooms_sold

    For each month in range, runs 6 custom reports at stay_date granularity
    (total/room/dorm × filtered/unfiltered) and writes per-day:
      - revenue_native, revenue_vnd                    (filtered totals)
      - room_revenue_native, dorm_revenue_native       (filtered split)
      - adr_native, room_adr_native, dorm_adr_native   (rule applied)
      - revpar_native                                  (filtered_rev / total_rooms)
      - rooms_sold, dorms_sold                         (unfiltered split — ADR denominator)

    total_sold / occ_pct are left untouched — sync_cloudbeds_occupancy already
    populated them from stock report 110 (unfiltered, correct for OCC).

    If a property's API key lacks Custom Reports permission (403), falls back
    to reservation_daily-based computation; if that also yields nothing the
    unfiltered values from sync_cloudbeds_occupancy remain (logged warning).

    Returns: { branch_id, months_synced }
    """
    from app.models.daily_metrics import DailyMetrics
    import calendar

    today = date.today()
    if date_from is None:
        date_from = today.replace(day=1)
    if date_to is None:
        last_day = calendar.monthrange(today.year, today.month)[1]
        date_to = today.replace(day=last_day)

    # Collect unique months in the date range
    months_to_sync = set()
    d = date_from.replace(day=1)
    while d <= date_to:
        months_to_sync.add((d.year, d.month))
        if d.month == 12:
            d = d.replace(year=d.year + 1, month=1)
        else:
            d = d.replace(month=d.month + 1)

    months_synced = 0
    now = datetime.now(timezone.utc)
    rate = get_cached_rate(currency, "VND")

    for year, month in sorted(months_to_sync):
        try:
            daily = fetch_filtered_daily(str(property_id), api_key, year, month)
        except Exception as e:
            logger.warning("Filtered daily sync failed branch=%s %d/%d: %s", branch_id, year, month, e)
            continue

        if not daily:
            # Insights 403 (no CREATE permission) — fall back to reservation_daily proration
            logger.warning(
                "Insights returned empty for branch=%s %d/%d — falling back to reservation_daily",
                branch_id, year, month,
            )
            daily = _compute_daily_from_reservation_daily(db, branch_id, year, month)
            if not daily:
                continue

        first_day = date(year, month, 1)
        last_day = date(year, month, calendar.monthrange(year, month)[1])

        existing = {
            dm.date: dm for dm in (
                db.query(DailyMetrics)
                .filter(
                    DailyMetrics.branch_id == branch_id,
                    DailyMetrics.date >= first_day,
                    DailyMetrics.date <= last_day,
                )
                .all()
            )
        }

        for dday, vals in sorted(daily.items()):
            dm = existing.get(dday)
            if dm is None:
                dm = DailyMetrics(branch_id=branch_id, date=dday)
                db.add(dm)
                existing[dday] = dm

            # Filtered revenue (Blogger/KOL/HouseUse/SpecialCase/WorkExchange excluded)
            rev  = round(vals["total_rev"], 2)
            rrev = round(vals["room_rev"], 2)
            drev = round(vals["dorm_rev"], 2)
            # Unfiltered rooms_sold (ALL sources) — ADR denominator per the rule
            rsold_all = int(vals.get("room_sold_all") or 0)
            dsold_all = int(vals.get("dorm_sold_all") or 0)
            tsold_all = int(vals.get("total_sold_all") or 0)

            # SAFETY GUARD: only override total revenue/ADR when the filtered
            # report actually returned data. If rev == 0 we cannot tell whether
            # the property genuinely had zero paying revenue or the custom
            # report failed silently / returned no rows for the property — in
            # either case we'd rather show the unfiltered value from
            # sync_cloudbeds_occupancy (slight rule violation) than wipe the
            # number to 0 (data loss). This caused a regression where Taipei /
            # 1948 / Osaka showed NT$0 / ¥0 across all months.
            if rev > 0:
                dm.revenue_native = rev
                dm.revenue_vnd = round(rev * rate, 2) if rate else None
                if tsold_all > 0:
                    dm.adr_native = round(rev / tsold_all, 2)
                if total_rooms and total_rooms > 0:
                    dm.revpar_native = round(rev / total_rooms, 2)

            # Room split: same guard — only override if filtered room rev > 0
            if rrev > 0 and rsold_all > 0:
                dm.room_revenue_native = rrev
                dm.room_adr_native = round(rrev / rsold_all, 2)
                dm.rooms_sold = rsold_all

            # Dorm split: same guard
            if drev > 0 and dsold_all > 0:
                dm.dorm_revenue_native = drev
                dm.dorm_adr_native = round(drev / dsold_all, 2)
                dm.dorms_sold = dsold_all

            dm.computed_at = now

        months_synced += 1

    db.commit()
    logger.info(
        "Filtered daily sync complete branch %s: %d months synced",
        branch_id, months_synced,
    )
    return {"branch_id": branch_id, "months_synced": months_synced}


# ── Filtered Insights via Custom Reports ──────────────────────────────────────

# Sources excluded from REVENUE (but counted for rooms_sold / OCC)
_REVENUE_EXCLUDED_SOURCES = ["Blogger", "House Use", "KOL", "Special case", "Work Exchange"]


def _make_date_filters(date_from: str, date_to: str) -> list[dict]:
    return [
        {"cdf": {"type": "default", "column": "stay_date"}, "operator": "greater_than_or_equal", "value": date_from},
        {"cdf": {"type": "default", "column": "stay_date"}, "operator": "less_than_or_equal", "value": date_to},
    ]


def _make_source_exclude_filters() -> list[dict]:
    """Filters to exclude Blogger, House Use, KOL, Special case, Work Exchange from revenue.
    Uses not_equals (exact match) to avoid partial-match over-exclusion.

    `multi_level_id: 4` is REQUIRED on the reservation_source CDF for dataset 7.
    Without it Cloudbeds returns 400 'Cdf: reservation_source ...' which
    `_fetch_custom_report_daily` swallows silently → all filtered calls return
    empty → sync_cloudbeds_filtered guards skip writes → room/dorm ADR stay NULL.
    Confirmed empirically 2026-05-05 via /api/sync/debug/cloudbeds: stock 110's
    own config uses the same multi_level_id=4 on this column.
    """
    return [
        {"cdf": {"type": "default", "column": "reservation_source", "multi_level_id": 4},
         "operator": "not_equals", "value": src}
        for src in _REVENUE_EXCLUDED_SOURCES
    ]


def _make_room_type_filter(is_dorm: bool) -> dict:
    """Filter for Dorm (contains 'Dorm') or Room (not contains 'Dorm')."""
    op = "contains" if is_dorm else "not_contains"
    return {"cdf": {"type": "default", "column": "room_type"}, "operator": op, "value": "Dorm"}


def _fetch_custom_report(
    api_key: str,
    property_id: str,
    title: str,
    filters: list[dict],
) -> dict[str, dict]:
    """
    Create a temporary custom report in Cloudbeds Data Insights API,
    fetch data, then delete the report.

    Returns: { "YYYY-MM": {"rev": float, "sold": float} }
    """
    headers = {
        "Authorization": f"Bearer {api_key}",
        "X-PROPERTY-ID": str(property_id),
        "Content-Type": "application/json",
    }
    payload = {
        "title": title,
        "dataset_id": 7,
        "property_id": str(property_id),
        "property_ids": [str(property_id)],
        "columns": [
            {"cdf": {"type": "default", "column": "rooms_sold"}, "metrics": ["sum"]},
            {"cdf": {"type": "default", "column": "room_revenue"}, "metrics": ["sum"]},
            {"cdf": {"type": "default", "column": "adr"}},
        ],
        "group_rows": [{"cdf": {"type": "default", "column": "stay_date"}}],
        "filters": {"and": filters},
    }

    with httpx.Client(timeout=60) as client:
        resp = client.post(f"{INSIGHTS_BASE_URL}/reports", headers=headers, json=payload)
        if resp.status_code not in (200, 201):
            logger.warning("Custom report create failed: %s %s", resp.status_code, resp.text[:200])
            return {}

        report_id = resp.json().get("id")
        try:
            resp2 = client.get(
                f"{INSIGHTS_BASE_URL}/reports/{report_id}/data",
                headers=headers,
                params={"property_ids": str(property_id)},
            )
        finally:
            # Always delete the temporary report
            client.delete(f"{INSIGHTS_BASE_URL}/reports/{report_id}", headers=headers)

        if resp2.status_code != 200:
            return {}

        records = resp2.json().get("records", {})
        months: dict[str, dict] = {}
        for k, v in records.items():
            m = k[:7]
            if m not in months:
                months[m] = {"rev": 0.0, "sold": 0.0}
            months[m]["rev"] += v.get("room_revenue", {}).get("sum", 0)
            months[m]["sold"] += v.get("rooms_sold", {}).get("sum", 0)
        return months


def _fetch_custom_report_daily(
    api_key: str,
    property_id: str,
    title: str,
    filters: list[dict],
) -> dict:
    """
    Like _fetch_custom_report but preserves per-day granularity.
    Returns { date: {"rev": float, "sold": float} }.
    """
    headers = {
        "Authorization": f"Bearer {api_key}",
        "X-PROPERTY-ID": str(property_id),
        "Content-Type": "application/json",
    }
    payload = {
        "title": title,
        "dataset_id": 7,
        "property_id": str(property_id),
        "property_ids": [str(property_id)],
        "columns": [
            {"cdf": {"type": "default", "column": "rooms_sold"}, "metrics": ["sum"]},
            {"cdf": {"type": "default", "column": "room_revenue"}, "metrics": ["sum"]},
            {"cdf": {"type": "default", "column": "adr"}},
        ],
        "group_rows": [{"cdf": {"type": "default", "column": "stay_date"}}],
        "filters": {"and": filters},
    }
    out: dict = {}
    with httpx.Client(timeout=60) as client:
        resp = client.post(f"{INSIGHTS_BASE_URL}/reports", headers=headers, json=payload)
        if resp.status_code not in (200, 201):
            logger.warning("Custom report create failed: %s %s", resp.status_code, resp.text[:200])
            return out
        report_id = resp.json().get("id")
        try:
            resp2 = client.get(
                f"{INSIGHTS_BASE_URL}/reports/{report_id}/data",
                headers=headers,
                params={"property_ids": str(property_id)},
            )
        finally:
            client.delete(f"{INSIGHTS_BASE_URL}/reports/{report_id}", headers=headers)

        if resp2.status_code != 200:
            return out

        for k, v in (resp2.json().get("records", {}) or {}).items():
            try:
                d = date.fromisoformat(k[:10])
            except ValueError:
                continue
            out[d] = {
                "rev": float(v.get("room_revenue", {}).get("sum", 0) or 0),
                "sold": float(v.get("rooms_sold", {}).get("sum", 0) or 0),
            }
    return out


def fetch_filtered_daily(
    property_id: str,
    api_key: str,
    year: int,
    month: int,
) -> dict:
    """
    Per-day revenue (filtered) + rooms_sold (unfiltered), for one branch/month.

    Returns { date: {
        total_rev,                              # excl Blogger/HouseUse/KOL/SpecialCase/WorkExchange
        total_sold_excl, total_sold_all,        # _excl matches filtered rev; _all = ALL sources
        room_rev,
        room_sold_excl, room_sold_all,
        dorm_rev,
        dorm_sold_excl, dorm_sold_all,
    } }

    ADR rule (sync_cloudbeds_filtered uses these fields):
        ADR = revenue (filtered) / rooms_sold (ALL sources)
    so revenue uses *_rev and ADR's denominator uses *_sold_all.
    Returns empty dict on 403 or other failure (caller skips month).
    """
    import calendar
    last_day = calendar.monthrange(year, month)[1]
    dfrom = f"{year}-{month:02d}-01"
    dto = f"{year}-{month:02d}-{last_day:02d}"
    date_f = _make_date_filters(dfrom, dto)
    mkey = f"{year}-{month:02d}"

    total_excl = _fetch_custom_report_daily(
        api_key, property_id, f"HiD-total-{mkey}",
        date_f + _make_source_exclude_filters(),
    )
    room_excl = _fetch_custom_report_daily(
        api_key, property_id, f"HiD-room-{mkey}",
        date_f + [_make_room_type_filter(False)] + _make_source_exclude_filters(),
    )
    dorm_excl = _fetch_custom_report_daily(
        api_key, property_id, f"HiD-dorm-{mkey}",
        date_f + [_make_room_type_filter(True)] + _make_source_exclude_filters(),
    )
    # Unfiltered (ALL sources) — for ADR denominator per the rule
    total_all = _fetch_custom_report_daily(
        api_key, property_id, f"HiD-totalAll-{mkey}",
        date_f,
    )
    room_all = _fetch_custom_report_daily(
        api_key, property_id, f"HiD-roomAll-{mkey}",
        date_f + [_make_room_type_filter(False)],
    )
    dorm_all = _fetch_custom_report_daily(
        api_key, property_id, f"HiD-dormAll-{mkey}",
        date_f + [_make_room_type_filter(True)],
    )

    out: dict = {}
    all_dates = (
        set(total_excl) | set(room_excl) | set(dorm_excl)
        | set(total_all) | set(room_all) | set(dorm_all)
    )
    for d in all_dates:
        t = total_excl.get(d, {"rev": 0, "sold": 0})
        r = room_excl.get(d, {"rev": 0, "sold": 0})
        dm = dorm_excl.get(d, {"rev": 0, "sold": 0})
        ta = total_all.get(d, {"rev": 0, "sold": 0})
        ra = room_all.get(d, {"rev": 0, "sold": 0})
        da = dorm_all.get(d, {"rev": 0, "sold": 0})
        out[d] = {
            "total_rev": t["rev"],
            "total_sold_excl": t["sold"],
            "total_sold_all": ta["sold"],
            "room_rev": r["rev"],
            "room_sold_excl": r["sold"],
            "room_sold_all": ra["sold"],
            "dorm_rev": dm["rev"],
            "dorm_sold_excl": dm["sold"],
            "dorm_sold_all": da["sold"],
        }
    return out


def fetch_occupancy_filtered(
    property_id: str,
    api_key: str,
    year: int,
    month: int,
) -> dict:
    """
    Fetch occupancy metrics with proper filtering from Cloudbeds Insights API.

    Uses custom reports to get:
    - Revenue with excluded sources (Blogger, House Use, KOL, Special case, Work Exchange)
    - Rooms sold with ALL sources (no exclusions)
    - Room vs Dorm split

    Returns: {
        "total_rev": float, "total_sold": int, "total_adr": float,
        "room_rev": float, "room_sold": int, "room_adr": float,
        "dorm_rev": float, "dorm_sold": int, "dorm_adr": float,
        "has_dorm": bool,
    }
    """
    import calendar
    last_day = calendar.monthrange(year, month)[1]
    dfrom = f"{year}-{month:02d}-01"
    dto = f"{year}-{month:02d}-{last_day:02d}"
    date_f = _make_date_filters(dfrom, dto)
    month_key = f"{year}-{month:02d}"

    # 1. ALL sources (for rooms_sold / OCC)
    all_data = _fetch_custom_report(
        api_key, property_id, f"HiD-all-{month_key}", date_f,
    )
    # 2. Excluded sources (for revenue)
    excl_data = _fetch_custom_report(
        api_key, property_id, f"HiD-excl-{month_key}",
        date_f + _make_source_exclude_filters(),
    )

    all_m = all_data.get(month_key, {"rev": 0, "sold": 0})
    excl_m = excl_data.get(month_key, {"rev": 0, "sold": 0})

    total_rev = excl_m["rev"]
    total_sold = int(all_m["sold"])
    total_adr = round(total_rev / total_sold, 2) if total_sold > 0 else 0

    result = {
        "total_rev": total_rev,
        "total_sold": total_sold,
        "total_adr": total_adr,
        "room_rev": 0, "room_sold": 0, "room_adr": 0,
        "dorm_rev": 0, "dorm_sold": 0, "dorm_adr": 0,
        "has_dorm": False,
    }

    # 3. Room / Dorm split (only if custom reports work — try Room first)
    room_excl = _fetch_custom_report(
        api_key, property_id, f"HiD-roomExcl-{month_key}",
        date_f + [_make_room_type_filter(False)] + _make_source_exclude_filters(),
    )

    if room_excl:  # Custom reports work for this property
        room_all = _fetch_custom_report(
            api_key, property_id, f"HiD-roomAll-{month_key}",
            date_f + [_make_room_type_filter(False)],
        )
        dorm_excl = _fetch_custom_report(
            api_key, property_id, f"HiD-dormExcl-{month_key}",
            date_f + [_make_room_type_filter(True)] + _make_source_exclude_filters(),
        )
        dorm_all = _fetch_custom_report(
            api_key, property_id, f"HiD-dormAll-{month_key}",
            date_f + [_make_room_type_filter(True)],
        )

        rm_excl = room_excl.get(month_key, {"rev": 0, "sold": 0})
        rm_all = room_all.get(month_key, {"rev": 0, "sold": 0})
        dm_excl = dorm_excl.get(month_key, {"rev": 0, "sold": 0})
        dm_all = dorm_all.get(month_key, {"rev": 0, "sold": 0})

        room_rev = rm_excl["rev"]
        room_sold = int(rm_all["sold"])
        dorm_rev = dm_excl["rev"]
        dorm_sold = int(dm_all["sold"])

        result["room_rev"] = room_rev
        result["room_sold"] = room_sold
        result["room_adr"] = round(room_rev / room_sold, 2) if room_sold > 0 else 0
        result["dorm_rev"] = dorm_rev
        result["dorm_sold"] = dorm_sold
        result["dorm_adr"] = round(dorm_rev / dorm_sold, 2) if dorm_sold > 0 else 0
        result["has_dorm"] = dorm_sold > 0

    logger.info(
        "Filtered occupancy %s %d/%d: total_rev=%.0f sold=%d adr=%.2f "
        "room_adr=%.2f dorm_adr=%.2f",
        property_id, year, month,
        result["total_rev"], result["total_sold"], result["total_adr"],
        result["room_adr"], result["dorm_adr"],
    )
    return result


async def sync_all_branches(incremental: bool = True) -> list[dict]:
    """Sync all active branches — uses per-property API key from config.

    incremental=True  (default): only pull reservations modified in last 2 days (fast).
    incremental=False: full sync — pull all reservations in the lookback window (thorough).
    """
    from app.models.branch import Branch

    db = SessionLocal()
    results = []
    try:
        branches = db.query(Branch).filter_by(is_active=True).all()
        for branch in branches:
            pid = branch.cloudbeds_property_id
            if not pid:
                logger.warning("No property_id for branch %s — skipping", branch.name)
                results.append({"branch_id": str(branch.id), "branch": branch.name, "error": "no property_id"})
                continue
            api_key = settings.get_api_key_for_property(str(pid))
            if not api_key:
                logger.warning("No API key for property %s (%s) — skipping", pid, branch.name)
                results.append({"branch_id": str(branch.id), "branch": branch.name, "error": "no api_key configured"})
                continue
            try:
                result = sync_branch(str(branch.id), pid, branch.currency, api_key=api_key, incremental=incremental)
                result["branch"] = branch.name
                results.append(result)
            except Exception as exc:
                logger.error("Branch %s sync error: %s", branch.name, exc)
                results.append({"branch_id": str(branch.id), "branch": branch.name, "error": str(exc)})
    finally:
        db.close()

    return results


# ── Country Insights via Custom Reports (Dataset #3 — Reservations) ───────────

def fetch_country_insights(
    property_id: str,
    api_key: str,
    year: int,
    month: int,
) -> dict[str, dict]:
    """
    Fetch country-level reservation data from Cloudbeds Data Insights API.

    Uses a temporary custom report on dataset #3 (Reservations) grouped by
    primary_guest_residence_country. This is much lighter than pulling raw
    reservation data.

    Returns: {
        "Australia": {"nights": 101, "revenue": 72287486, "guests": 45},
        "China":     {"nights": 188, "revenue": 163195287, "guests": 163},
        ...
    }
    """
    import calendar

    first_day = f"{year}-{month:02d}-01"
    last_day_num = calendar.monthrange(year, month)[1]
    last_day = f"{year}-{month:02d}-{last_day_num:02d}"

    headers = {
        "Authorization": f"Bearer {api_key}",
        "X-PROPERTY-ID": str(property_id),
        "Content-Type": "application/json",
    }

    payload = {
        "title": f"HiD-country-{year}{month:02d}",
        "dataset_id": 3,
        "property_id": str(property_id),
        "property_ids": [str(property_id)],
        "columns": [
            {"cdf": {"type": "default", "column": "room_nights_count"}, "metrics": ["sum"]},
            {"cdf": {"type": "default", "column": "room_revenue_total_amount"}, "metrics": ["sum"]},
            {"cdf": {"type": "default", "column": "guest_count"}, "metrics": ["sum"]},
        ],
        "group_rows": [
            {"cdf": {"type": "default", "column": "primary_guest_residence_country"}},
        ],
        "filters": {
            "and": [
                {
                    "cdf": {"type": "default", "column": "checkin_date"},
                    "operator": "greater_than_or_equal",
                    "value": first_day,
                },
                {
                    "cdf": {"type": "default", "column": "checkin_date"},
                    "operator": "less_than_or_equal",
                    "value": last_day,
                },
            ]
        },
    }

    with httpx.Client(timeout=60) as client:
        resp = client.post(
            f"{INSIGHTS_BASE_URL}/reports", headers=headers, json=payload,
        )
        if resp.status_code not in (200, 201):
            logger.warning(
                "Country insights report create failed for %s %d-%02d: %s %s",
                property_id, year, month, resp.status_code, resp.text[:200],
            )
            return {}

        report_id = resp.json().get("id")
        try:
            resp2 = client.get(
                f"{INSIGHTS_BASE_URL}/reports/{report_id}/data",
                headers=headers,
                params={"property_ids": str(property_id)},
            )
        finally:
            client.delete(f"{INSIGHTS_BASE_URL}/reports/{report_id}", headers=headers)

        if resp2.status_code != 200:
            logger.warning(
                "Country insights data fetch failed for %s %d-%02d: %s",
                property_id, year, month, resp2.text[:200],
            )
            return {}

        records = resp2.json().get("records", {})
        result: dict[str, dict] = {}
        for country_name, metrics in records.items():
            if not country_name or country_name == "-":
                continue
            result[country_name] = {
                "nights": int(metrics.get("room_nights_count", {}).get("sum", 0)),
                "revenue": float(metrics.get("room_revenue_total_amount", {}).get("sum", 0)),
                "guests": int(metrics.get("guest_count", {}).get("sum", 0)),
            }

        logger.info(
            "Country insights fetched for property %s %d-%02d: %d countries",
            property_id, year, month, len(result),
        )
        return result
