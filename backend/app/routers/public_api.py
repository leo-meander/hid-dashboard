"""
Public API router — external systems access reservation data via API key.
Auth: X-API-Key header.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional
from uuid import UUID

import bcrypt
from fastapi import APIRouter, Depends, HTTPException, Header
from sqlalchemy.orm import Session, undefer

from app.database import get_db
from app.models.api_key import ApiKey
from app.models.reservation import Reservation
from app.models.branch import Branch

router = APIRouter()


# ── API Key Auth Dependency ──────────────────────────────────────────────────

def verify_api_key(
    x_api_key: str = Header(..., alias="X-API-Key"),
    db: Session = Depends(get_db),
) -> ApiKey:
    """Validate API key from X-API-Key header."""
    if not x_api_key or not x_api_key.startswith("hid_"):
        raise HTTPException(status_code=401, detail="Invalid API key format")

    prefix = x_api_key[:12]
    candidates = db.query(ApiKey).filter_by(key_prefix=prefix, is_active=True).all()

    for candidate in candidates:
        try:
            if bcrypt.checkpw(x_api_key.encode(), candidate.key_hash.encode()):
                candidate.last_used_at = datetime.now(timezone.utc)
                db.commit()
                return candidate
        except Exception:
            continue

    raise HTTPException(status_code=401, detail="Invalid or revoked API key")


# ── Helpers ──────────────────────────────────────────────────────────────────

def _extract_raw(raw: dict | None, key: str):
    """Safely extract a value from raw_data JSONB."""
    if not raw:
        return None
    return raw.get(key)


def _reservation_out(r: Reservation, branch_name: str | None) -> dict:
    """Map reservation to the exact field list requested by the user."""
    raw = r.raw_data or {}
    return {
        "name": _extract_raw(raw, "guestName"),
        "email": _extract_raw(raw, "guestEmail"),
        "phone_number": _extract_raw(raw, "guestPhone"),
        "mobile": _extract_raw(raw, "guestCellPhone"),
        "gender": _extract_raw(raw, "guestGender"),
        "date_of_birth": _extract_raw(raw, "guestBirthday"),
        "reservation_number": r.cloudbeds_reservation_id,
        "third_party_confirmation_number": _extract_raw(raw, "thirdPartyIdentifier"),
        "type_of_document": _extract_raw(raw, "documentType"),
        "document_number": _extract_raw(raw, "documentNumber"),
        "document_issue_date": _extract_raw(raw, "documentIssueDate"),
        "document_issuing_country": _extract_raw(raw, "documentIssuingCountry"),
        "document_expiration_date": _extract_raw(raw, "documentExpirationDate"),
        "street_address": _extract_raw(raw, "guestAddress"),
        "apt_suite_floor": _extract_raw(raw, "guestAddress2"),
        "city": _extract_raw(raw, "guestCity"),
        "state": _extract_raw(raw, "guestState"),
        "postal_zip_code": _extract_raw(raw, "guestZip"),
        "adults": r.adults,
        "children": _extract_raw(raw, "children"),
        "room_number": r.room_number,
        "accommodation_total": _extract_raw(raw, "accommodationTotal"),
        "amount_paid": _extract_raw(raw, "amountPaid"),
        "check_in_date": r.check_in_date.isoformat() if r.check_in_date else None,
        "check_out_date": r.check_out_date.isoformat() if r.check_out_date else None,
        "nights": r.nights,
        "room_type": r.room_type,
        "grand_total": float(r.grand_total_native) if r.grand_total_native else None,
        "deposit": _extract_raw(raw, "depositAmount"),
        "products": _extract_raw(raw, "productsTotal"),
        "balance_due": _extract_raw(raw, "balanceDue"),
        "credit_card_type": _extract_raw(raw, "creditCardType"),
        "reservation_date": r.reservation_date.isoformat() if r.reservation_date else None,
        "source": r.source,
        "meal_plan": _extract_raw(raw, "mealPlan"),
        "status": r.status,
        "country": r.guest_country,
        "guest_status": _extract_raw(raw, "guestStatus"),
        "cancellation_date": r.cancellation_date.isoformat() if r.cancellation_date else None,
        "branch": branch_name,
    }


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.get("/reservations")
def get_reservations(
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    branch_id: Optional[UUID] = None,
    status: Optional[str] = None,
    limit: int = 200,
    offset: int = 0,
    _key: ApiKey = Depends(verify_api_key),
    db: Session = Depends(get_db),
):
    """
    Fetch reservation data for external systems.
    Authenticate with X-API-Key header.

    Query params:
    - date_from / date_to: filter by check_in_date range (YYYY-MM-DD)
    - branch_id: filter by branch UUID
    - status: filter by reservation status
    - limit: max results (default 200, max 1000)
    - offset: pagination offset
    """
    try:
        limit = min(limit, 1000)
        # raw_data is deferred on the model; this endpoint exposes JSONB fields,
        # so undefer explicitly for these rows only.
        q = db.query(Reservation).options(undefer(Reservation.raw_data))

        if date_from:
            q = q.filter(Reservation.check_in_date >= date_from)
        if date_to:
            q = q.filter(Reservation.check_in_date <= date_to)
        if branch_id:
            q = q.filter(Reservation.branch_id == branch_id)
        if status:
            q = q.filter(Reservation.status == status)

        total = q.count()
        reservations = (
            q.order_by(Reservation.check_in_date.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )

        # Pre-load branch names
        branch_ids = {r.branch_id for r in reservations}
        branches = {
            b.id: b.name
            for b in db.query(Branch).filter(Branch.id.in_(branch_ids)).all()
        } if branch_ids else {}

        return {
            "success": True,
            "data": {
                "reservations": [
                    _reservation_out(r, branches.get(r.branch_id))
                    for r in reservations
                ],
                "total": total,
                "limit": limit,
                "offset": offset,
            },
            "error": None,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
