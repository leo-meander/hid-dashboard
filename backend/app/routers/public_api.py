"""
Public API router — external systems access reservation data via API key.
Auth: X-API-Key header.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

import bcrypt
from fastapi import APIRouter, Depends, HTTPException, Header, Query
from sqlalchemy import func
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


# ── Shared envelope ──────────────────────────────────────────────────────────

def _envelope(data):
    return {
        "success": True,
        "data": data,
        "error": None,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ── Branches ─────────────────────────────────────────────────────────────────

@router.get("/branches")
def public_branches(
    _key: ApiKey = Depends(verify_api_key),
    db: Session = Depends(get_db),
):
    """Active branches with capacity (rooms / dorms / total)."""
    rows = (
        db.query(Branch)
        .filter(Branch.is_active.is_(True))
        .order_by(Branch.name)
        .all()
    )
    data = [
        {
            "id": str(b.id),
            "name": b.name,
            "city": b.city,
            "country_code": b.country,
            "currency": b.currency,
            "total_rooms": b.total_rooms,
            "total_room_count": b.total_room_count,
            "total_dorm_count": b.total_dorm_count,
            "timezone": b.timezone,
        }
        for b in rows
    ]
    return _envelope(data)


# ── Metrics: Daily / Weekly / Monthly ────────────────────────────────────────

@router.get("/metrics/daily")
def public_metrics_daily(
    branch_id: Optional[UUID] = Query(None),
    date_from: Optional[date] = Query(None),
    date_to: Optional[date] = Query(None),
    _key: ApiKey = Depends(verify_api_key),
    db: Session = Depends(get_db),
):
    """Daily OCC, ADR, RevPAR, revenue per branch. Defaults to last 30 days."""
    from app.routers.metrics import get_daily as _get_daily
    return _get_daily(
        branch_id=branch_id, date_from=date_from, date_to=date_to, db=db,
    )


@router.get("/metrics/weekly")
def public_metrics_weekly(
    branch_id: Optional[UUID] = Query(None),
    date_from: Optional[date] = Query(None),
    date_to: Optional[date] = Query(None),
    _key: ApiKey = Depends(verify_api_key),
    db: Session = Depends(get_db),
):
    """Weekly aggregation. Defaults to last 12 weeks."""
    from app.routers.metrics import get_weekly as _get_weekly
    return _get_weekly(
        branch_id=branch_id, date_from=date_from, date_to=date_to, db=db,
    )


@router.get("/metrics/monthly")
def public_metrics_monthly(
    branch_id: Optional[UUID] = Query(None),
    year_from: Optional[int] = Query(None),
    year_to: Optional[int] = Query(None),
    _key: ApiKey = Depends(verify_api_key),
    db: Session = Depends(get_db),
):
    """Monthly aggregation with country breakdown."""
    from app.routers.metrics import get_monthly as _get_monthly
    return _get_monthly(
        branch_id=branch_id, year_from=year_from, year_to=year_to, db=db,
    )


@router.get("/metrics/ota-mix")
def public_metrics_ota_mix(
    branch_id: Optional[UUID] = Query(None),
    date_from: Optional[date] = Query(None),
    date_to: Optional[date] = Query(None),
    _key: ApiKey = Depends(verify_api_key),
    db: Session = Depends(get_db),
):
    """Channel mix (Direct vs OTAs) by booking count and revenue."""
    from app.routers.metrics import get_ota_mix_endpoint as _get_ota
    return _get_ota(
        branch_id=branch_id, date_from=date_from, date_to=date_to, db=db,
    )


@router.get("/metrics/country-yoy-insights")
def public_metrics_country_yoy(
    year: Optional[int] = Query(None),
    month: Optional[int] = Query(None, ge=1, le=12),
    branch_id: Optional[UUID] = Query(None),
    date_type: str = Query("check_in", regex="^(check_in|booked)$"),
    _key: ApiKey = Depends(verify_api_key),
    db: Session = Depends(get_db),
):
    """Country YoY comparison — current year vs previous year."""
    from app.routers.metrics import get_country_yoy_insights as _yoy
    return _yoy(
        year=year, month=month, branch_id=branch_id,
        date_type=date_type, db=db,
    )


@router.get("/metrics/country-reservations")
def public_metrics_country_reservations(
    view: str = Query("monthly", regex="^(weekly|monthly)$"),
    branch_id: Optional[UUID] = Query(None),
    limit: int = Query(500, ge=1, le=500),
    date_type: str = Query("check_in", regex="^(check_in|booked)$"),
    _key: ApiKey = Depends(verify_api_key),
    db: Session = Depends(get_db),
):
    """Top countries with reservation trend over 7 weeks/months."""
    from app.routers.metrics import get_country_reservations as _cr
    return _cr(
        view=view, branch_id=branch_id, limit=limit,
        date_type=date_type, db=db,
    )


# ── Lead Time (computed from reservations) ───────────────────────────────────

@router.get("/lead-time")
def public_lead_time(
    branch_id: Optional[UUID] = Query(None),
    country_code: Optional[str] = Query(None, min_length=2, max_length=2),
    days_back: int = Query(180, ge=30, le=730),
    _key: ApiKey = Depends(verify_api_key),
    db: Session = Depends(get_db),
):
    """
    Average booking lead time (days from reservation_date to check_in_date)
    over the past N days. Group by country if country_code is omitted —
    returns per-country breakdown for the branch (or all branches).

    Used by the ads-platform recommendation framework: lead-time defines the
    target stay window for ads aimed at a specific source country.
    """
    today = datetime.now(timezone.utc).date()
    date_from = today - timedelta(days=days_back)

    EXCLUDED = (
        "canceled", "cancelled", "no_show", "no-show", "cancelled_by_guest",
    )

    avg_expr = func.avg(
        Reservation.check_in_date - Reservation.reservation_date
    )
    median_expr = func.percentile_cont(0.5).within_group(
        (Reservation.check_in_date - Reservation.reservation_date).asc()
    )

    q = db.query(
        Reservation.guest_country_code.label("code"),
        func.coalesce(Reservation.guest_country, "Unknown").label("country"),
        func.count(Reservation.id).label("samples"),
        avg_expr.label("avg_days"),
        median_expr.label("median_days"),
    ).filter(
        Reservation.check_in_date >= date_from,
        Reservation.reservation_date.isnot(None),
        ~func.lower(func.coalesce(Reservation.status, "")).in_(EXCLUDED),
    )

    if branch_id:
        q = q.filter(Reservation.branch_id == branch_id)
    if country_code:
        q = q.filter(
            func.upper(Reservation.guest_country_code) == country_code.upper()
        )

    rows = q.group_by(
        Reservation.guest_country_code, Reservation.guest_country,
    ).order_by(func.count(Reservation.id).desc()).all()

    def _to_days(val):
        if val is None:
            return None
        if hasattr(val, "days"):
            return float(val.days) + (val.seconds / 86400)
        try:
            return float(val)
        except (TypeError, ValueError):
            return None

    data = [
        {
            "country_code": r.code,
            "country": r.country,
            "samples": int(r.samples),
            "avg_lead_days": round(_to_days(r.avg_days), 1)
                if r.avg_days is not None else None,
            "median_lead_days": round(_to_days(r.median_days), 1)
                if r.median_days is not None else None,
        }
        for r in rows
    ]

    return _envelope({
        "branch_id": str(branch_id) if branch_id else None,
        "country_code": country_code.upper() if country_code else None,
        "date_from": date_from.isoformat(),
        "date_to": today.isoformat(),
        "days_back": days_back,
        "countries": data,
    })


# ── Events (city / branch demand drivers) ────────────────────────────────────

@router.get("/events")
def public_events(
    branch_id: Optional[UUID] = Query(None),
    city: Optional[str] = Query(None),
    date_from: Optional[date] = Query(None),
    date_to: Optional[date] = Query(None),
    _key: ApiKey = Depends(verify_api_key),
    db: Session = Depends(get_db),
):
    """Local events that drive demand (festivals, conferences, concerts)."""
    from app.routers.events import list_events as _list_events
    return _list_events(
        branch_id=branch_id, city=city,
        date_from=date_from, date_to=date_to, db=db,
    )


# ── Holiday Intelligence ─────────────────────────────────────────────────────

@router.get("/holidays/upcoming")
def public_holidays_upcoming(
    days: int = Query(60, ge=1, le=365),
    _key: ApiKey = Depends(verify_api_key),
    db: Session = Depends(get_db),
):
    """Upcoming holiday windows across all source markets — next N days."""
    from app.routers.holiday_intel import upcoming_windows as _upcoming
    return _upcoming(days=days, db=db)


@router.get("/holidays/calendar")
def public_holidays_calendar(
    country_code: Optional[str] = Query(None),
    month: Optional[int] = Query(None, ge=1, le=12),
    travel_propensity: Optional[str] = Query(None),
    _key: ApiKey = Depends(verify_api_key),
    db: Session = Depends(get_db),
):
    """Full holiday calendar — filter by country, month, propensity."""
    from app.routers.holiday_intel import list_holidays as _list_h
    return _list_h(
        country_code=country_code, month=month,
        travel_propensity=travel_propensity, db=db,
    )


@router.get("/holidays/country/{code}")
def public_holidays_country(
    code: str,
    _key: ApiKey = Depends(verify_api_key),
    db: Session = Depends(get_db),
):
    """Holiday detail for a specific source country (e.g. VN, JP, TW)."""
    from app.routers.holiday_intel import country_holidays as _country_h
    return _country_h(code=code, db=db)


# ── Countries: ranking + trend ───────────────────────────────────────────────

@router.get("/countries/ranking")
def public_countries_ranking(
    branch_id: Optional[UUID] = Query(None),
    top_n: int = Query(30, ge=5, le=100),
    _key: ApiKey = Depends(verify_api_key),
    db: Session = Depends(get_db),
):
    """Country potential ranking with Hot/Warm/Cold tiers."""
    from app.routers.countries import country_ranking as _cr
    return _cr(branch_id=branch_id, top_n=top_n, db=db)


@router.get("/countries/{country_code}/trend")
def public_country_trend(
    country_code: str,
    branch_id: Optional[UUID] = Query(None),
    months: int = Query(24, ge=3, le=36),
    _key: ApiKey = Depends(verify_api_key),
    db: Session = Depends(get_db),
):
    """Monthly booking trend for a specific country over the past N months."""
    from app.routers.countries import country_trend as _ct
    return _ct(
        country_code=country_code, branch_id=branch_id,
        months=months, db=db,
    )


# ── Country Intelligence (single best-source for framework step 5) ───────────

@router.get("/insights/country-intel")
def public_country_intel(
    branch_id: Optional[UUID] = Query(None),
    _key: ApiKey = Depends(verify_api_key),
    db: Session = Depends(get_db),
):
    """
    Per-branch country intelligence: top volume + top growth + KOL/Ads
    coverage + government visitor forecast + room-type stats. This is the
    single richest endpoint for the recommendation framework.
    """
    from app.routers.insights import country_intelligence as _ci
    return _ci(branch_id=branch_id, db=db)


# ── KPI Achievement (framework step 3 — high/low OCC analog) ─────────────────

@router.get("/kpi/period-achievement")
def public_kpi_achievement(
    date_from: date = Query(...),
    date_to: date = Query(...),
    branch_id: Optional[UUID] = Query(None),
    _key: ApiKey = Depends(verify_api_key),
    db: Session = Depends(get_db),
):
    """
    Revenue actual vs target for an arbitrary date range. Used to gauge
    whether the branch is on/ahead/behind plan during the target period —
    direct input for budget allocation logic.
    """
    from app.routers.kpi import kpi_period_achievement as _ka
    return _ka(
        date_from=date_from, date_to=date_to,
        branch_id=branch_id, db=db,
    )
