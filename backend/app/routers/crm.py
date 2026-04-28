"""
CRM Dashboard router — Revenue & Booking analytics for CRM room types.
Queries reservations where room_type contains 'CRM', "MEANDER'S FRIEND", or 'Travel guide'.
"""
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from uuid import UUID
from collections import defaultdict

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, case, extract, or_
from sqlalchemy.orm import Session, undefer

from app.database import get_db
from app.models.reservation import Reservation

router = APIRouter()


def _envelope(data):
    return {
        "success": True,
        "data": data,
        "error": None,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _crm_filter():
    """Filter for CRM-related room types/rate plans: CRM, MEANDER'S FRIEND, Travel guide, Grand Open."""
    return or_(
        Reservation.room_type.ilike("%CRM%"),
        Reservation.rate_plan_name.ilike("%CRM%"),
        Reservation.room_type.ilike("%MEANDER'S FRIEND%"),
        Reservation.rate_plan_name.ilike("%MEANDER'S FRIEND%"),
        Reservation.room_type.ilike("%Travel guide%"),
        Reservation.rate_plan_name.ilike("%Travel guide%"),
        Reservation.room_type.ilike("%Grand Open%"),
        Reservation.rate_plan_name.ilike("%Grand Open%"),
    )


def _crm_base_query(db: Session, branch_id: Optional[UUID] = None):
    """Base query: reservations with CRM-related room types."""
    q = db.query(Reservation).filter(_crm_filter())
    if branch_id:
        q = q.filter(Reservation.branch_id == branch_id)
    return q


# ── Summary KPIs ──────────────────────────────────────────────────────────────

@router.get("/summary")
def crm_summary(
    branch_id: Optional[UUID] = Query(None),
    date_from: Optional[date] = Query(None),
    date_to: Optional[date] = Query(None),
    db: Session = Depends(get_db),
):
    """High-level CRM KPIs: total bookings, revenue, avg nights, cancel rate."""
    try:
        today = datetime.now(timezone.utc).date()
        if date_to is None:
            date_to = today
        if date_from is None:
            date_from = date_to - timedelta(days=29)

        q = db.query(
            func.count(Reservation.id).label("total_bookings"),
            func.sum(Reservation.grand_total_vnd).label("total_revenue_vnd"),
            func.sum(Reservation.grand_total_native).label("total_revenue_native"),
            func.avg(Reservation.nights).label("avg_nights"),
            func.sum(Reservation.nights).label("total_nights"),
            func.sum(Reservation.adults).label("total_guests"),
            func.sum(case((Reservation.status == "Cancelled", 1), else_=0)).label("cancellations"),
        ).filter(
            _crm_filter(),
            Reservation.check_in_date >= date_from,
            Reservation.check_in_date <= date_to,
        )

        if branch_id:
            q = q.filter(Reservation.branch_id == branch_id)

        row = q.one()

        total = row.total_bookings or 0
        cancellations = row.cancellations or 0
        cancel_rate = round(cancellations / total, 4) if total > 0 else 0

        return _envelope({
            "total_bookings": total,
            "total_revenue_vnd": float(row.total_revenue_vnd or 0),
            "total_revenue_native": float(row.total_revenue_native or 0),
            "avg_nights": round(float(row.avg_nights or 0), 1),
            "total_nights": row.total_nights or 0,
            "total_guests": row.total_guests or 0,
            "cancellations": cancellations,
            "cancellation_rate": cancel_rate,
            "confirmed_bookings": total - cancellations,
        })
    except Exception as e:
        import logging
        logging.getLogger(__name__).exception("crm_summary failed")
        return {"success": False, "data": None, "error": str(e),
                "timestamp": datetime.now(timezone.utc).isoformat()}


# ── Daily Trend ───────────────────────────────────────────────────────────────

@router.get("/daily")
def crm_daily(
    branch_id: Optional[UUID] = Query(None),
    date_from: Optional[date] = Query(None),
    date_to: Optional[date] = Query(None),
    db: Session = Depends(get_db),
):
    """Daily CRM bookings and revenue trend."""
    try:
        today = datetime.now(timezone.utc).date()
        if date_to is None:
            date_to = today
        if date_from is None:
            date_from = date_to - timedelta(days=29)

        rows = (
            db.query(
                Reservation.check_in_date.label("date"),
                func.count(Reservation.id).label("bookings"),
                func.sum(Reservation.grand_total_vnd).label("revenue_vnd"),
                func.sum(Reservation.grand_total_native).label("revenue_native"),
                func.sum(Reservation.nights).label("nights"),
                func.sum(Reservation.adults).label("guests"),
            )
            .filter(
                _crm_filter(),
                Reservation.check_in_date >= date_from,
                Reservation.check_in_date <= date_to,
                Reservation.status != "Cancelled",
            )
            .group_by(Reservation.check_in_date)
            .order_by(Reservation.check_in_date)
            .all()
        )

        result = []
        for r in rows:
            adr = round(float(r.revenue_native or 0) / r.nights, 2) if r.nights and r.nights > 0 else 0
            result.append({
                "date": r.date.isoformat(),
                "bookings": r.bookings,
                "revenue_vnd": float(r.revenue_vnd or 0),
                "revenue_native": float(r.revenue_native or 0),
                "nights": r.nights or 0,
                "guests": r.guests or 0,
                "adr_native": adr,
            })

        return _envelope(result)
    except Exception as e:
        import logging
        logging.getLogger(__name__).exception("crm_daily failed")
        return {"success": False, "data": None, "error": str(e),
                "timestamp": datetime.now(timezone.utc).isoformat()}


# ── Monthly Trend ─────────────────────────────────────────────────────────────

@router.get("/monthly")
def crm_monthly(
    branch_id: Optional[UUID] = Query(None),
    date_from: Optional[date] = Query(None),
    date_to: Optional[date] = Query(None),
    db: Session = Depends(get_db),
):
    """Monthly CRM bookings and revenue aggregation."""
    try:
        today = datetime.now(timezone.utc).date()
        if date_to is None:
            date_to = today
        if date_from is None:
            date_from = date(today.year - 1, 1, 1)

        rows = (
            db.query(
                extract("year", Reservation.check_in_date).label("year"),
                extract("month", Reservation.check_in_date).label("month"),
                func.count(Reservation.id).label("bookings"),
                func.sum(Reservation.grand_total_vnd).label("revenue_vnd"),
                func.sum(Reservation.grand_total_native).label("revenue_native"),
                func.sum(Reservation.nights).label("nights"),
                func.sum(Reservation.adults).label("guests"),
                func.sum(case((Reservation.status == "Cancelled", 1), else_=0)).label("cancellations"),
            )
            .filter(
                _crm_filter(),
                Reservation.check_in_date >= date_from,
                Reservation.check_in_date <= date_to,
            )
            .group_by("year", "month")
            .order_by("year", "month")
            .all()
        )

        result = []
        for r in rows:
            total = r.bookings or 0
            cancel = r.cancellations or 0
            nights = r.nights or 0
            rev = float(r.revenue_native or 0)
            adr = round(rev / nights, 2) if nights > 0 else 0
            result.append({
                "year": int(r.year),
                "month": int(r.month),
                "bookings": total,
                "confirmed": total - cancel,
                "cancellations": cancel,
                "cancellation_rate": round(cancel / total, 4) if total > 0 else 0,
                "revenue_vnd": float(r.revenue_vnd or 0),
                "revenue_native": rev,
                "nights": nights,
                "guests": r.guests or 0,
                "adr_native": adr,
            })

        return _envelope(result)
    except Exception as e:
        import logging
        logging.getLogger(__name__).exception("crm_monthly failed")
        return {"success": False, "data": None, "error": str(e),
                "timestamp": datetime.now(timezone.utc).isoformat()}


# ── By Branch ─────────────────────────────────────────────────────────────────

@router.get("/by-branch")
def crm_by_branch(
    date_from: Optional[date] = Query(None),
    date_to: Optional[date] = Query(None),
    db: Session = Depends(get_db),
):
    """CRM performance broken down by branch."""
    try:
        today = datetime.now(timezone.utc).date()
        if date_to is None:
            date_to = today
        if date_from is None:
            date_from = date_to - timedelta(days=29)

        rows = (
            db.query(
                Reservation.branch_id,
                func.count(Reservation.id).label("bookings"),
                func.sum(Reservation.grand_total_vnd).label("revenue_vnd"),
                func.sum(Reservation.grand_total_native).label("revenue_native"),
                func.sum(Reservation.nights).label("nights"),
                func.sum(case((Reservation.status == "Cancelled", 1), else_=0)).label("cancellations"),
            )
            .filter(
                _crm_filter(),
                Reservation.check_in_date >= date_from,
                Reservation.check_in_date <= date_to,
            )
            .group_by(Reservation.branch_id)
            .all()
        )

        result = []
        for r in rows:
            total = r.bookings or 0
            cancel = r.cancellations or 0
            nights = r.nights or 0
            rev = float(r.revenue_native or 0)
            result.append({
                "branch_id": str(r.branch_id),
                "bookings": total,
                "confirmed": total - cancel,
                "cancellations": cancel,
                "cancellation_rate": round(cancel / total, 4) if total > 0 else 0,
                "revenue_vnd": float(r.revenue_vnd or 0),
                "revenue_native": rev,
                "nights": nights,
                "adr_native": round(rev / nights, 2) if nights > 0 else 0,
            })

        result.sort(key=lambda x: x["revenue_vnd"], reverse=True)
        return _envelope(result)
    except Exception as e:
        import logging
        logging.getLogger(__name__).exception("crm_by_branch failed")
        return {"success": False, "data": None, "error": str(e),
                "timestamp": datetime.now(timezone.utc).isoformat()}


# ── By Source ─────────────────────────────────────────────────────────────────

@router.get("/by-source")
def crm_by_source(
    branch_id: Optional[UUID] = Query(None),
    date_from: Optional[date] = Query(None),
    date_to: Optional[date] = Query(None),
    db: Session = Depends(get_db),
):
    """CRM bookings broken down by booking source."""
    try:
        today = datetime.now(timezone.utc).date()
        if date_to is None:
            date_to = today
        if date_from is None:
            date_from = date_to - timedelta(days=29)

        q = (
            db.query(
                Reservation.source,
                Reservation.source_category,
                func.count(Reservation.id).label("bookings"),
                func.sum(Reservation.grand_total_vnd).label("revenue_vnd"),
                func.sum(Reservation.grand_total_native).label("revenue_native"),
                func.sum(Reservation.nights).label("nights"),
            )
            .filter(
                _crm_filter(),
                Reservation.check_in_date >= date_from,
                Reservation.check_in_date <= date_to,
                Reservation.status != "Cancelled",
            )
            .group_by(Reservation.source, Reservation.source_category)
        )

        if branch_id:
            q = q.filter(Reservation.branch_id == branch_id)

        rows = q.all()

        result = []
        for r in rows:
            result.append({
                "source": r.source or "Unknown",
                "source_category": r.source_category or "Unknown",
                "bookings": r.bookings,
                "revenue_vnd": float(r.revenue_vnd or 0),
                "revenue_native": float(r.revenue_native or 0),
                "nights": r.nights or 0,
            })

        result.sort(key=lambda x: x["bookings"], reverse=True)
        return _envelope(result)
    except Exception as e:
        import logging
        logging.getLogger(__name__).exception("crm_by_source failed")
        return {"success": False, "data": None, "error": str(e),
                "timestamp": datetime.now(timezone.utc).isoformat()}


# ── Reservation List ──────────────────────────────────────────────────────────

@router.get("/reservations")
def crm_reservations(
    branch_id: Optional[UUID] = Query(None),
    date_from: Optional[date] = Query(None),
    date_to: Optional[date] = Query(None),
    status: Optional[str] = Query(None),
    limit: int = Query(50, le=200),
    offset: int = Query(0),
    db: Session = Depends(get_db),
):
    """List individual CRM reservations with pagination."""
    try:
        today = datetime.now(timezone.utc).date()
        if date_to is None:
            date_to = today
        if date_from is None:
            date_from = date_to - timedelta(days=29)

        q = _crm_base_query(db, branch_id).filter(
            Reservation.check_in_date >= date_from,
            Reservation.check_in_date <= date_to,
        )

        if status:
            q = q.filter(Reservation.status == status)

        total = q.count()
        rows = q.order_by(Reservation.check_in_date.desc()).offset(offset).limit(limit).all()

        result = []
        for r in rows:
            result.append({
                "id": str(r.id),
                "branch_id": str(r.branch_id),
                "room_type": r.room_type,
                "rate_plan_name": r.rate_plan_name,
                "source": r.source,
                "source_category": r.source_category,
                "guest_country": r.guest_country,
                "check_in_date": r.check_in_date.isoformat(),
                "check_out_date": r.check_out_date.isoformat(),
                "nights": r.nights,
                "adults": r.adults,
                "grand_total_native": float(r.grand_total_native or 0),
                "grand_total_vnd": float(r.grand_total_vnd or 0),
                "status": r.status,
                "reservation_date": r.reservation_date.isoformat() if r.reservation_date else None,
            })

        return _envelope({"items": result, "total": total, "limit": limit, "offset": offset})
    except Exception as e:
        import logging
        logging.getLogger(__name__).exception("crm_reservations failed")
        return {"success": False, "data": None, "error": str(e),
                "timestamp": datetime.now(timezone.utc).isoformat()}


# ── Room Type Breakdown ───────────────────────────────────────────────────────

@router.get("/room-types")
def crm_room_types(
    branch_id: Optional[UUID] = Query(None),
    date_from: Optional[date] = Query(None),
    date_to: Optional[date] = Query(None),
    db: Session = Depends(get_db),
):
    """Breakdown by specific CRM room type names."""
    try:
        today = datetime.now(timezone.utc).date()
        if date_to is None:
            date_to = today
        if date_from is None:
            date_from = date_to - timedelta(days=29)

        q = (
            db.query(
                Reservation.room_type,
                func.count(Reservation.id).label("bookings"),
                func.sum(Reservation.grand_total_vnd).label("revenue_vnd"),
                func.sum(Reservation.grand_total_native).label("revenue_native"),
                func.sum(Reservation.nights).label("nights"),
            )
            .filter(
                _crm_filter(),
                Reservation.check_in_date >= date_from,
                Reservation.check_in_date <= date_to,
                Reservation.status != "Cancelled",
            )
            .group_by(Reservation.room_type)
        )

        if branch_id:
            q = q.filter(Reservation.branch_id == branch_id)

        rows = q.all()

        result = []
        for r in rows:
            result.append({
                "room_type": r.room_type,
                "bookings": r.bookings,
                "revenue_vnd": float(r.revenue_vnd or 0),
                "revenue_native": float(r.revenue_native or 0),
                "nights": r.nights or 0,
            })

        result.sort(key=lambda x: x["bookings"], reverse=True)
        return _envelope(result)
    except Exception as e:
        import logging
        logging.getLogger(__name__).exception("crm_room_types failed")
        return {"success": False, "data": None, "error": str(e),
                "timestamp": datetime.now(timezone.utc).isoformat()}


# ── Reservation Contacts (email + cb_id from raw_data) ────────────────────────

@router.get("/reservation-contacts")
def crm_reservation_contacts(
    rate_plan_contains: Optional[str] = Query(None, description="Filter rate_plan_name OR room_type contains this string"),
    branch_id: Optional[UUID] = Query(None),
    date_from: Optional[date] = Query(None),
    date_to: Optional[date] = Query(None),
    db: Session = Depends(get_db),
):
    """Return CRM reservation contacts (id, cb_id, email, name) per branch.

    Filters: optional rate_plan_contains (default = all CRM-related), branch_id,
    and check_in_date range. raw_data is undeferred to extract guestEmail/guestName.
    """
    try:
        today = datetime.now(timezone.utc).date()
        if date_to is None:
            date_to = today
        if date_from is None:
            date_from = date_to - timedelta(days=365)

        if rate_plan_contains:
            like = f"%{rate_plan_contains}%"
            base_filter = or_(
                Reservation.rate_plan_name.ilike(like),
                Reservation.room_type.ilike(like),
            )
        else:
            base_filter = _crm_filter()

        q = db.query(Reservation).options(undefer(Reservation.raw_data)).filter(
            base_filter,
            Reservation.check_in_date >= date_from,
            Reservation.check_in_date <= date_to,
        )
        if branch_id:
            q = q.filter(Reservation.branch_id == branch_id)

        rows = q.order_by(Reservation.branch_id, Reservation.check_in_date).all()

        result = []
        for r in rows:
            raw = r.raw_data or {}
            result.append({
                "reservation_uid": str(r.id),
                "cloudbeds_reservation_id": r.cloudbeds_reservation_id,
                "branch_id": str(r.branch_id),
                "guest_name": raw.get("guestName"),
                "guest_email": raw.get("guestEmail"),
                "guest_phone": raw.get("guestPhone") or raw.get("guestCellPhone"),
                "rate_plan_name": r.rate_plan_name,
                "room_type": r.room_type,
                "status": r.status,
                "check_in_date": r.check_in_date.isoformat() if r.check_in_date else None,
                "check_out_date": r.check_out_date.isoformat() if r.check_out_date else None,
                "nights": r.nights,
                "grand_total_native": float(r.grand_total_native or 0),
                "grand_total_vnd": float(r.grand_total_vnd or 0),
                "reservation_date": r.reservation_date.isoformat() if r.reservation_date else None,
            })
        return _envelope({"items": result, "total": len(result)})
    except Exception as e:
        import logging
        logging.getLogger(__name__).exception("crm_reservation_contacts failed")
        return {"success": False, "data": None, "error": str(e),
                "timestamp": datetime.now(timezone.utc).isoformat()}
