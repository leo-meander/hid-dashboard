from datetime import datetime, date, timedelta, timezone
from typing import Optional
from uuid import UUID
from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, extract
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.branch import Branch
from app.models.kpi import KPITarget
from app.models.daily_metrics import DailyMetrics
from app.services.kpi_engine import compute_kpi_summary, compute_next_month_forecast

router = APIRouter()


# ── Schemas ────────────────────────────────────────────────────────────────────

class KPITargetCreate(BaseModel):
    branch_id: UUID
    year: int
    month: int
    target_revenue_native: float
    target_revenue_vnd: float
    predicted_occ_pct: Optional[float] = None


class KPITargetPatch(BaseModel):
    target_revenue_native: Optional[float] = None
    target_revenue_vnd: Optional[float] = None
    predicted_occ_pct: Optional[float] = None
    predicted_room_occ_pct: Optional[float] = None
    predicted_dorm_occ_pct: Optional[float] = None
    deduction_pct: Optional[float] = None


class KPITargetUpsert(BaseModel):
    branch_id: UUID
    year: int
    month: int
    target_revenue_native: float
    predicted_occ_pct: Optional[float] = None
    predicted_room_occ_pct: Optional[float] = None
    predicted_dorm_occ_pct: Optional[float] = None
    deduction_pct: Optional[float] = None


class KPITargetOut(BaseModel):
    id: UUID
    branch_id: UUID
    year: int
    month: int
    target_revenue_native: float
    target_revenue_vnd: float
    predicted_occ_pct: Optional[float]
    predicted_room_occ_pct: Optional[float] = None
    predicted_dorm_occ_pct: Optional[float] = None
    deduction_pct: Optional[float] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class DeductionUpdate(BaseModel):
    branch_id: UUID
    year: int
    month: int
    deduction_pct: float


class OtherRevenueUpdate(BaseModel):
    branch_id: UUID
    year: int
    month: int
    other_revenue_native: float


def _envelope(data):
    return {
        "success": True,
        "data": data,
        "error": None,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ── CRUD Endpoints ─────────────────────────────────────────────────────────────

@router.post("/targets", status_code=201)
def create_kpi_target(payload: KPITargetCreate, db: Session = Depends(get_db)):
    existing = (
        db.query(KPITarget)
        .filter_by(branch_id=payload.branch_id, year=payload.year, month=payload.month)
        .first()
    )
    if existing:
        raise HTTPException(status_code=409, detail="KPI target already exists for this branch/year/month")

    target = KPITarget(**payload.model_dump())
    db.add(target)
    db.commit()
    db.refresh(target)
    return _envelope(KPITargetOut.model_validate(target).model_dump())


@router.get("/targets")
def list_kpi_targets(
    branch_id: Optional[UUID] = Query(None),
    year: Optional[int] = Query(None),
    db: Session = Depends(get_db),
):
    q = db.query(KPITarget)
    if branch_id:
        q = q.filter(KPITarget.branch_id == branch_id)
    if year:
        q = q.filter(KPITarget.year == year)
    targets = q.order_by(KPITarget.year, KPITarget.month).all()
    return _envelope([KPITargetOut.model_validate(t).model_dump() for t in targets])


@router.put("/targets/upsert")
def upsert_kpi_target(payload: KPITargetUpsert, db: Session = Depends(get_db)):
    """Create or update a KPI target for a branch/year/month."""
    existing = (
        db.query(KPITarget)
        .filter_by(branch_id=payload.branch_id, year=payload.year, month=payload.month)
        .first()
    )
    if existing:
        existing.target_revenue_native = payload.target_revenue_native
        existing.target_revenue_vnd = payload.target_revenue_native  # placeholder
        if payload.predicted_occ_pct is not None:
            existing.predicted_occ_pct = payload.predicted_occ_pct
        if payload.predicted_room_occ_pct is not None:
            existing.predicted_room_occ_pct = payload.predicted_room_occ_pct
        if payload.predicted_dorm_occ_pct is not None:
            existing.predicted_dorm_occ_pct = payload.predicted_dorm_occ_pct
        if payload.deduction_pct is not None:
            existing.deduction_pct = payload.deduction_pct
        db.commit()
        db.refresh(existing)
        return _envelope(KPITargetOut.model_validate(existing).model_dump())
    else:
        target = KPITarget(
            branch_id=payload.branch_id,
            year=payload.year,
            month=payload.month,
            target_revenue_native=payload.target_revenue_native,
            target_revenue_vnd=payload.target_revenue_native,
            predicted_occ_pct=payload.predicted_occ_pct,
            predicted_room_occ_pct=payload.predicted_room_occ_pct,
            predicted_dorm_occ_pct=payload.predicted_dorm_occ_pct,
            deduction_pct=payload.deduction_pct or 0,
        )
        db.add(target)
        db.commit()
        db.refresh(target)
        return _envelope(KPITargetOut.model_validate(target).model_dump())


@router.patch("/targets/{target_id}")
def update_kpi_target(target_id: UUID, payload: KPITargetPatch, db: Session = Depends(get_db)):
    target = db.query(KPITarget).filter_by(id=target_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="KPI target not found")

    update_data = payload.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(target, field, value)

    db.commit()
    db.refresh(target)
    return _envelope(KPITargetOut.model_validate(target).model_dump())


@router.put("/deduction")
def save_deduction(payload: DeductionUpdate, db: Session = Depends(get_db)):
    """Save deduction % for a branch/year/month. Creates KPI target if not exists."""
    existing = (
        db.query(KPITarget)
        .filter_by(branch_id=payload.branch_id, year=payload.year, month=payload.month)
        .first()
    )
    if existing:
        existing.deduction_pct = max(0, min(100, payload.deduction_pct))
        db.commit()
        db.refresh(existing)
        return _envelope({"saved": True, "deduction_pct": float(existing.deduction_pct)})
    else:
        # Create minimal target so deduction can be stored
        target = KPITarget(
            branch_id=payload.branch_id,
            year=payload.year,
            month=payload.month,
            target_revenue_native=0,
            target_revenue_vnd=0,
            deduction_pct=max(0, min(100, payload.deduction_pct)),
        )
        db.add(target)
        db.commit()
        db.refresh(target)
        return _envelope({"saved": True, "deduction_pct": float(target.deduction_pct)})


@router.put("/other-revenue")
def save_other_revenue(payload: OtherRevenueUpdate, db: Session = Depends(get_db)):
    """Save manual other revenue (native currency) for a branch/year/month.
    Added on top of the deducted forecast in the All Branches summary."""
    val = max(0, float(payload.other_revenue_native or 0))
    existing = (
        db.query(KPITarget)
        .filter_by(branch_id=payload.branch_id, year=payload.year, month=payload.month)
        .first()
    )
    if existing:
        existing.other_revenue_native = val
        db.commit()
        db.refresh(existing)
        return _envelope({"saved": True, "other_revenue_native": float(existing.other_revenue_native)})
    else:
        target = KPITarget(
            branch_id=payload.branch_id,
            year=payload.year,
            month=payload.month,
            target_revenue_native=0,
            target_revenue_vnd=0,
            other_revenue_native=val,
        )
        db.add(target)
        db.commit()
        db.refresh(target)
        return _envelope({"saved": True, "other_revenue_native": float(target.other_revenue_native)})


# ── Summary Endpoints (Phase 2) ────────────────────────────────────────────────

def _branch_summary(db, branch, year, month):
    total_room_count = branch.total_room_count or 0
    total_dorm_count = branch.total_dorm_count or 0

    summary = compute_kpi_summary(
        db=db,
        branch_id=branch.id,
        year=year,
        month=month,
        total_rooms=branch.total_rooms or 0,
        total_room_count=total_room_count,
        total_dorm_count=total_dorm_count,
    )
    summary["branch_id"]   = str(branch.id)
    summary["branch_name"] = branch.name
    summary["branch_city"] = branch.city
    summary["currency"]    = branch.currency or branch.native_currency or "VND"
    summary["total_room_count"] = total_room_count
    summary["total_dorm_count"] = total_dorm_count

    # Include saved deduction_pct from KPI target
    target = (
        db.query(KPITarget)
        .filter_by(branch_id=branch.id, year=year, month=month)
        .first()
    )
    summary["deduction_pct"] = float(target.deduction_pct or 0) if target else 0
    summary["other_revenue_native"] = float(target.other_revenue_native or 0) if target else 0

    # Always include next-month forecast
    next_data = compute_next_month_forecast(
        db=db,
        branch_id=branch.id,
        total_rooms=branch.total_rooms or 0,
        cur_year=year,
        cur_month=month,
        total_room_count=total_room_count,
        total_dorm_count=total_dorm_count,
    )
    summary.update(next_data)
    return summary


@router.get("/summary")
def kpi_summary_all(
    year: int = Query(...),
    month: int = Query(...),
    months: Optional[str] = Query(None, description="'current,next' for All Branches table"),
    branch_id: Optional[UUID] = Query(None),
    db: Session = Depends(get_db),
):
    """
    KPI achievement summary for all active branches (or a single branch if branch_id provided).
    ?months=current,next also returns next-month OCC-based forecast per branch.
    """
    now = datetime.now(timezone.utc)
    q = db.query(Branch).filter_by(is_active=True)
    if branch_id:
        q = q.filter(Branch.id == branch_id)
    branches = q.all()
    results = []

    for branch in branches:
        row = _branch_summary(db, branch, year, month)
        results.append(row)

    return _envelope(results)


@router.get("/summary/{branch_id}")
def kpi_summary_branch(
    branch_id: UUID,
    year: int = Query(...),
    month: int = Query(...),
    db: Session = Depends(get_db),
):
    """KPI achievement summary for a single branch."""
    branch = db.query(Branch).filter_by(id=branch_id, is_active=True).first()
    if not branch:
        raise HTTPException(status_code=404, detail="Branch not found")

    return _envelope(_branch_summary(db, branch, year, month))


# ── Yearly Grid (Target vs Actual vs Hit Rate) ──────────────────────────────

@router.get("/yearly-grid")
def kpi_yearly_grid(
    year: int = Query(None),
    branch_id: Optional[UUID] = Query(None),
    db: Session = Depends(get_db),
):
    """
    Full-year KPI grid: Target, Actual, Hit% per branch per month.
    Returns branches + 12-month grid + totals row.
    Optionally filter to a single branch.
    """
    if year is None:
        year = datetime.now(timezone.utc).year

    # Get active branches
    q = db.query(Branch).filter_by(is_active=True)
    if branch_id:
        q = q.filter(Branch.id == branch_id)
    branches = q.order_by(Branch.name).all()
    branch_list = [
        {"id": str(b.id), "name": b.name, "currency": b.currency or "VND"}
        for b in branches
    ]
    branch_ids = [b.id for b in branches]

    # 1. Query all KPI targets for the year
    targets = db.query(KPITarget).filter(
        KPITarget.year == year,
        KPITarget.branch_id.in_(branch_ids),
    ).all()

    # target_map[(branch_id, month)] = {target, override}
    target_map = {}
    for t in targets:
        target_map[(str(t.branch_id), t.month)] = {
            "target": float(t.target_revenue_native or 0),
            "override": float(t.actual_revenue_override) if t.actual_revenue_override is not None else None,
        }

    # 2. Query actual revenue from daily_metrics, grouped by branch + month
    actuals = db.query(
        DailyMetrics.branch_id,
        extract("month", DailyMetrics.date).label("mo"),
        func.coalesce(func.sum(DailyMetrics.revenue_native), 0).label("revenue"),
    ).filter(
        extract("year", DailyMetrics.date) == year,
        DailyMetrics.branch_id.in_(branch_ids),
    ).group_by(
        DailyMetrics.branch_id, "mo",
    ).all()

    # actual_map[(branch_id, month)] = actual_revenue (from Cloudbeds)
    actual_map = {}
    for a in actuals:
        actual_map[(str(a.branch_id), int(a.mo))] = float(a.revenue)

    # 3. Build grid — override takes precedence over Cloudbeds actual
    months = []
    totals = defaultdict(lambda: {"target": 0, "actual": 0})

    for mo in range(1, 13):
        row = {"month": mo}
        branch_data = []
        for b in branch_list:
            bid = b["id"]
            kpi = target_map.get((bid, mo), {})
            target = kpi.get("target", 0)
            override = kpi.get("override")
            cloudbeds_actual = actual_map.get((bid, mo), 0)
            actual = override if override is not None else cloudbeds_actual
            hit_pct = round(actual / target * 100, 1) if target > 0 else None

            branch_data.append({
                "branch_id": bid,
                "target": target,
                "actual": actual,
                "cloudbeds_actual": cloudbeds_actual,
                "is_override": override is not None,
                "hit_pct": hit_pct,
            })

            totals[bid]["target"] += target
            totals[bid]["actual"] += actual

        row["branches"] = branch_data
        months.append(row)

    # 4. Totals row
    total_row = []
    for b in branch_list:
        bid = b["id"]
        t = totals[bid]["target"]
        a = totals[bid]["actual"]
        total_row.append({
            "branch_id": bid,
            "target": t,
            "actual": a,
            "hit_pct": round(a / t * 100, 1) if t > 0 else None,
        })

    return _envelope({
        "year": year,
        "branches": branch_list,
        "months": months,
        "totals": total_row,
    })


class ActualOverride(BaseModel):
    branch_id: UUID
    year: int
    month: int
    actual_revenue: Optional[float] = None  # None = clear override, use Cloudbeds


@router.put("/actual-override")
def save_actual_override(payload: ActualOverride, db: Session = Depends(get_db)):
    """Save or clear a manual actual revenue override for a branch/month."""
    existing = (
        db.query(KPITarget)
        .filter_by(branch_id=payload.branch_id, year=payload.year, month=payload.month)
        .first()
    )
    if existing:
        existing.actual_revenue_override = payload.actual_revenue
        db.commit()
        return _envelope({"saved": True, "override": payload.actual_revenue})
    elif payload.actual_revenue is not None:
        # Create minimal KPI target row to store override
        target = KPITarget(
            branch_id=payload.branch_id,
            year=payload.year,
            month=payload.month,
            target_revenue_native=0,
            target_revenue_vnd=0,
            actual_revenue_override=payload.actual_revenue,
        )
        db.add(target)
        db.commit()
        return _envelope({"saved": True, "override": payload.actual_revenue})
    return _envelope({"saved": False, "message": "No target row exists and no override value provided"})


# ── Period Achievement ────────────────────────────────────────────────────────

@router.get("/period-achievement")
def kpi_period_achievement(
    date_from: date = Query(...),
    date_to: date = Query(...),
    branch_id: Optional[UUID] = Query(None),
    db: Session = Depends(get_db),
):
    """
    KPI achievement for an arbitrary date range.
    Daily Goal = monthly target / days_in_month for each day.
    Period target = sum of daily goals across the range.
    Actual revenue = sum of daily_metrics.revenue_native for the range.
    """
    import calendar

    q = db.query(Branch).filter_by(is_active=True)
    if branch_id:
        q = q.filter(Branch.id == branch_id)
    branches = q.all()

    # Pre-load all KPI targets covering the date range months
    # Determine unique (year, month) pairs in the range
    ym_pairs = set()
    d = date_from
    while d <= date_to:
        ym_pairs.add((d.year, d.month))
        # Jump to next month
        if d.month == 12:
            d = date(d.year + 1, 1, 1)
        else:
            d = date(d.year, d.month + 1, 1)

    branch_ids = [b.id for b in branches]

    # Load targets for all relevant months
    targets = db.query(KPITarget).filter(
        KPITarget.branch_id.in_(branch_ids),
    ).all()

    # target_map[(branch_id, year, month)] = target_revenue_native
    target_map = {}
    for t in targets:
        key = (str(t.branch_id), t.year, t.month)
        target_map[key] = float(t.target_revenue_native or 0)

    # Load actual revenue from daily_metrics grouped by branch
    actuals = db.query(
        DailyMetrics.branch_id,
        func.coalesce(func.sum(DailyMetrics.revenue_native), 0).label("revenue"),
    ).filter(
        DailyMetrics.branch_id.in_(branch_ids),
        DailyMetrics.date >= date_from,
        DailyMetrics.date <= date_to,
    ).group_by(DailyMetrics.branch_id).all()

    actual_map = {str(a.branch_id): float(a.revenue) for a in actuals}

    # Count total days in range
    total_days = (date_to - date_from).days + 1

    results = []
    for branch in branches:
        bid = str(branch.id)
        cur = branch.currency or branch.native_currency or "VND"

        # Calculate period target by summing daily goals
        period_target = 0.0
        d = date_from
        while d <= date_to:
            yr, mo = d.year, d.month
            monthly_target = target_map.get((bid, yr, mo), 0)
            dim = calendar.monthrange(yr, mo)[1]
            daily_goal = monthly_target / dim if dim > 0 else 0
            period_target += daily_goal
            d += timedelta(days=1)

        actual_revenue = actual_map.get(bid, 0)
        achievement_pct = round(actual_revenue / period_target, 4) if period_target > 0 else None

        avg_daily_goal = round(period_target / total_days, 2) if total_days > 0 else 0
        avg_daily_actual = round(actual_revenue / total_days, 2) if total_days > 0 else 0

        results.append({
            "branch_id": bid,
            "branch_name": branch.name,
            "currency": cur,
            "date_from": date_from.isoformat(),
            "date_to": date_to.isoformat(),
            "actual_revenue": round(actual_revenue, 2),
            "target_revenue": round(period_target, 2),
            "achievement_pct": achievement_pct,
            "daily_goal": avg_daily_goal,
            "daily_actual": avg_daily_actual,
            "total_days": total_days,
        })

    return _envelope(results)
