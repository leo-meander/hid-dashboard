"""
CSV import service — reads exported Cloudbeds Raw_data CSV files and upserts
reservations into the local DB, filling in grand_total_native and all other fields.
"""
from __future__ import annotations

import csv
import logging
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Optional

from app.database import SessionLocal
from app.models.reservation import Reservation
from app.services.cloudbeds import (
    map_room_type_category,
    map_source_category,
    normalize_source,
    map_country_code,
    get_cached_rate,
)

logger = logging.getLogger(__name__)

# ── Branch config ────────────────────────────────────────────────────────────

CSV_CONFIGS: dict[str, dict] = {
    "SGN_Data Analysis - Raw_data.csv": {
        "branch_id": "11111111-1111-1111-1111-111111111102",
        "currency": "VND",
        "european_numbers": False,   # plain integers / decimal
    },
    "1948_Data Analysis - Raw_data.csv": {
        "branch_id": "11111111-1111-1111-1111-111111111103",
        "currency": "TWD",
        "european_numbers": False,   # Grand Total column is standard decimal
    },
    "Taipei_Data Analysis - Raw_data.csv": {
        "branch_id": "11111111-1111-1111-1111-111111111101",
        "currency": "TWD",
        "european_numbers": True,    # period = thousands separator
    },
    "Oani_Data Analysis - Raw_data.csv": {
        "branch_id": "11111111-1111-1111-1111-111111111104",
        "currency": "TWD",
        "european_numbers": True,
    },
    "OSK_Data Analysis - Raw_data.csv": {
        "branch_id": "11111111-1111-1111-1111-111111111105",
        "currency": "JPY",
        "european_numbers": False,
    },
}

# ── Status normalization ─────────────────────────────────────────────────────

_STATUS_MAP: dict[str, str] = {
    "confirmed": "confirmed",
    "cancelled": "cancelled",
    "canceled": "cancelled",
    "in-house": "in_house",
    "in_house": "in_house",
    "checked out": "checked_out",
    "checked_out": "checked_out",
    "checkedout": "checked_out",
    "no show": "no_show",
    "noshow": "no_show",
    "no_show": "no_show",
    "not_checked_in": "confirmed",
}

def _norm_status(raw: str) -> Optional[str]:
    if not raw:
        return None
    return _STATUS_MAP.get(raw.lower().strip(), raw.lower().strip())


# ── Number parsing ────────────────────────────────────────────────────────────

def _parse_amount(raw: str, european: bool = False) -> Optional[Decimal]:
    """
    Auto-detects number format per value:
      - Has both period and comma → European (period=thousands, comma=decimal)
      - Period followed by exactly 3 digits at end, no comma → European thousands separator
        e.g. "10.800" → 10800,  "2.160" → 2160
      - Otherwise → standard decimal (period=decimal, comma=thousands)
        e.g. "2400.00" → 2400,  "85226.00" → 85226
    The `european` parameter is kept for backward compatibility but ignored.
    """
    if not raw or not raw.strip():
        return None
    s = raw.strip()
    # Remove any currency symbols / whitespace
    s = re.sub(r"[^\d.,\-]", "", s)
    if not s or s in ("-",):
        return None
    if "." in s and "," in s:
        # Explicit European format: "1.234,56" or "1,234.56"
        if s.index(".") < s.index(","):
            # period before comma → European (1.234,56)
            s = s.replace(".", "").replace(",", ".")
        else:
            # comma before period → standard (1,234.56)
            s = s.replace(",", "")
    elif re.search(r"\.\d{3}$", s):
        # Period with exactly 3 trailing digits and no comma → European thousands
        s = s.replace(".", "")
    else:
        # Standard decimal or plain integer
        s = s.replace(",", "")
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


# ── Date parsing ──────────────────────────────────────────────────────────────

def _parse_date(raw: str) -> Optional[date]:
    """
    Handles:
      - DD/MM/YYYY  (most branches)
      - M/D/YYYY or D/M/YYYY ambiguous — resolve by validity
      - YYYY-MM-DD  (ISO — Oani reservation dates)
      - YYYY-MM-DD HH:MM:SS (Oani datetime)
    """
    if not raw or not raw.strip() or raw.strip() in ("N/A", "-", ""):
        return None
    s = raw.strip().split()[0]  # drop time portion if present

    # ISO format
    if re.match(r"\d{4}-\d{2}-\d{2}", s):
        try:
            parts = s.split("-")
            return date(int(parts[0]), int(parts[1]), int(parts[2]))
        except Exception:
            return None

    # Slash-separated
    if "/" in s:
        parts = s.split("/")
        if len(parts) == 3:
            a, b, y = parts
            try:
                ia, ib, iy = int(a), int(b), int(y)
            except ValueError:
                return None
            # If first part > 12 → must be day (D/M/YYYY)
            # If second part > 12 → must be day in month position (M/D/YYYY)
            if ia > 12:
                # D/M/YYYY
                try:
                    return date(iy, ib, ia)
                except ValueError:
                    return None
            elif ib > 12:
                # M/D/YYYY
                try:
                    return date(iy, ia, ib)
                except ValueError:
                    return None
            else:
                # Ambiguous — assume D/M/YYYY (consistent with Cloudbeds export)
                try:
                    return date(iy, ib, ia)
                except ValueError:
                    # Fallback M/D
                    try:
                        return date(iy, ia, ib)
                    except ValueError:
                        return None
    return None


def _parse_int(raw: str) -> Optional[int]:
    """Parse integers that may have comma as decimal sep (e.g. '5,00' → 5)."""
    if not raw or not raw.strip():
        return None
    s = raw.strip().split(",")[0].split(".")[0]
    try:
        return int(s)
    except ValueError:
        return None


# ── Core import ───────────────────────────────────────────────────────────────

def import_csv_file(csv_path: Path, branch_id: str, currency: str,
                    european_numbers: bool) -> dict:
    """
    Read one CSV file and upsert reservations.
    Bulk approach: pre-loads all existing reservation IDs in one query,
    then processes all rows in Python. DB round trips: O(1) pre-load + O(n/200) commits.
    Returns {"created": N, "updated": N, "skipped": N}
    """
    rate = get_cached_rate(currency, "VND")
    db = SessionLocal()
    created = updated = skipped = 0

    try:
        with open(csv_path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            rows = list(reader)

        logger.info("CSV import %s — %d rows, currency=%s", csv_path.name, len(rows), currency)

        # Pre-load all existing reservation IDs for this branch in one query
        existing_map: dict[str, Reservation] = {
            r.cloudbeds_reservation_id: r
            for r in db.query(Reservation).filter_by(branch_id=branch_id).all()
        }
        logger.info("Pre-loaded %d existing reservations for branch %s", len(existing_map), branch_id)

        for row in rows:
            res_num = (row.get("Reservation Number") or "").strip()
            if not res_num:
                skipped += 1
                continue

            # Revenue = accommodation only, NOT grand total (which includes tax/fees/extras).
            # Cloudbeds CSV export has an "Accommodation Total" column for this.
            accom_raw = row.get("Accommodation Total", "")
            grand_total = _parse_amount(accom_raw, european_numbers)
            if grand_total is not None and grand_total < 0:
                grand_total = None  # ignore negative / refund rows

            check_in  = _parse_date(row.get("Check in Date", ""))
            check_out = _parse_date(row.get("Check out Date", ""))
            if not check_in:
                skipped += 1
                continue

            nights     = _parse_int(row.get("Nights", ""))
            adults     = _parse_int(row.get("Adults", ""))
            status     = _norm_status(row.get("Status", ""))
            source_raw = (row.get("Source") or "").strip()
            source     = normalize_source(source_raw) if source_raw else None
            room_type  = ((row.get("Room Type") or "").strip() or None)
            if room_type and len(room_type) > 100:
                room_type = room_type[:100]
            room_number = ((row.get("Room Number") or "").strip() or None)
            if room_number and len(room_number) > 50:
                room_number = room_number[:50]
            country    = (row.get("Country") or "").strip() or None
            res_date   = _parse_date(row.get("Reservation Date", ""))
            cancel_date = _parse_date(row.get("Cancelation Date", "")
                                      or row.get("Cancellation Date", ""))

            grand_total_vnd = (
                round(float(grand_total) * rate, 2)
                if grand_total is not None and rate is not None else None
            )

            payload = dict(
                branch_id=branch_id,
                check_in_date=check_in,
                check_out_date=check_out,
                nights=nights,
                adults=adults,
                status=status,
                source=source,
                source_category=map_source_category(source) if source else None,
                room_type=room_type,
                room_type_category=map_room_type_category(room_type) if room_type else None,
                room_number=room_number,
                # Always set — map_country_code handles None/empty → "Unknown"
                guest_country=map_country_code(country),
                guest_country_code=map_country_code(country),
                reservation_date=res_date,
                cancellation_date=cancel_date,
            )
            if grand_total is not None:
                payload["grand_total_native"] = float(grand_total)
                payload["grand_total_vnd"] = grand_total_vnd

            existing = existing_map.get(res_num)
            if existing:
                for k, v in payload.items():
                    if v is not None:
                        setattr(existing, k, v)
                updated += 1
            else:
                new_res = Reservation(cloudbeds_reservation_id=res_num, **payload)
                db.add(new_res)
                existing_map[res_num] = new_res  # prevent duplicate inserts
                created += 1

            if (created + updated) % 500 == 0:
                db.commit()
                logger.info("CSV import progress — created=%d updated=%d skipped=%d",
                            created, updated, skipped)

        db.commit()
        logger.info("CSV import done — created=%d updated=%d skipped=%d",
                    created, updated, skipped)
        return {"created": created, "updated": updated, "skipped": skipped}

    except Exception as exc:
        db.rollback()
        logger.error("CSV import failed for %s: %s", csv_path.name, exc)
        raise
    finally:
        db.close()


def import_all_csvs(csv_dir: Path) -> dict:
    """Run import for all known CSV files found in csv_dir."""
    results = {}
    for filename, config in CSV_CONFIGS.items():
        path = csv_dir / filename
        if not path.exists():
            logger.warning("CSV not found: %s", path)
            results[filename] = {"error": "file not found"}
            continue
        try:
            result = import_csv_file(
                path,
                branch_id=config["branch_id"],
                currency=config["currency"],
                european_numbers=config["european_numbers"],
            )
            results[filename] = result
        except Exception as exc:
            results[filename] = {"error": str(exc)}
    return results
