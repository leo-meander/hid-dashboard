"""Countries router — Phase 2: ranking + trend endpoints"""
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.reservation import Reservation
from app.services.country_scorer import score_countries

router = APIRouter()


def _envelope(data):
    return {"success": True, "data": data, "error": None,
            "timestamp": datetime.now(timezone.utc).isoformat()}


def _last_reservations_synced_at(db: Session, branch_id: Optional[UUID]) -> Optional[str]:
    q = db.query(func.max(Reservation.updated_at))
    if branch_id:
        q = q.filter(Reservation.branch_id == branch_id)
    ts = q.scalar()
    return ts.isoformat() if ts else None


@router.get("/ranking")
def country_ranking(
    branch_id: Optional[UUID] = Query(None),
    top_n: int = Query(30, ge=5, le=100),
    db: Session = Depends(get_db),
):
    """
    Country potential ranking with Hot/Warm/Cold tiers (v1.3 WoW/MoM scoring).
    Sorted by potential score descending.
    """
    results = score_countries(
        db=db,
        branch_id=branch_id,
        top_n=top_n,
    )
    last_synced_iso = _last_reservations_synced_at(db, branch_id)
    # Add rank
    for i, r in enumerate(results, 1):
        r["rank"] = i
        r["data_synced_at"] = last_synced_iso
    return _envelope(results)


@router.get("/{country_code}/trend")
def country_trend(
    country_code: str,
    branch_id: Optional[UUID] = Query(None),
    months: int = Query(24, ge=3, le=36),
    db: Session = Depends(get_db),
):
    """
    Monthly booking trend for a specific country over the past N months.
    Returns month-by-month counts + revenue for YoY chart.
    """
    today = datetime.now(timezone.utc).date()
    date_from = date(today.year - (months // 12 + 1), today.month, 1)

    q = (
        db.query(
            func.extract("year", Reservation.check_in_date).label("year"),
            func.extract("month", Reservation.check_in_date).label("month"),
            func.count(Reservation.id).label("count"),
            func.coalesce(func.sum(Reservation.grand_total_native), 0).label("revenue_native"),
            func.coalesce(func.sum(Reservation.grand_total_vnd), 0).label("revenue_vnd"),
        )
        .filter(
            Reservation.guest_country_code == country_code.upper(),
            Reservation.check_in_date >= date_from,
            Reservation.status.notin_(["cancelled", "canceled", "no_show"]),
        )
    )
    if branch_id:
        q = q.filter(Reservation.branch_id == branch_id)

    rows = q.group_by("year", "month").order_by("year", "month").all()

    # Also get country name
    name_row = (
        db.query(Reservation.guest_country)
        .filter(Reservation.guest_country_code == country_code.upper())
        .first()
    )
    country_name = name_row[0] if name_row else country_code.upper()

    trend = [
        {
            "year": int(r.year),
            "month": int(r.month),
            "count": r.count,
            "revenue_native": float(r.revenue_native),
            "revenue_vnd": float(r.revenue_vnd),
        }
        for r in rows
    ]

    return _envelope({
        "country_code": country_code.upper(),
        "country": country_name,
        "trend": trend,
        "data_synced_at": _last_reservations_synced_at(db, branch_id),
    })
