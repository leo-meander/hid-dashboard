"""Personas router — per-branch guest persona derived from reservation data."""
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.reservation import Reservation
from app.services.persona_engine import build_all_personas

router = APIRouter()


def _envelope(data):
    return {"success": True, "data": data, "error": None,
            "timestamp": datetime.now(timezone.utc).isoformat()}


@router.get("")
def get_personas(
    branch_id: Optional[UUID] = Query(None, description="One branch; omit for all active branches"),
    months: int = Query(12, ge=1, le=36, description="Trailing window in months (default 12)"),
    db: Session = Depends(get_db),
):
    """Guest persona per branch: demographics (gender/age/country), behaviour
    (lead time, length of stay, channel mix, party size, room vs dorm,
    cancellation rate), value (ADR, avg booking value), plus a synthesised
    headline. Demographic coverage is reported per dimension — gender/age are
    still backfilling from Cloudbeds."""
    data = build_all_personas(db, str(branch_id) if branch_id else None, months)
    last_synced = db.query(func.max(Reservation.updated_at))
    if branch_id:
        last_synced = last_synced.filter(Reservation.branch_id == branch_id)
    ts = last_synced.scalar()
    data["data_synced_at"] = ts.isoformat() if ts else None
    return _envelope(data)
