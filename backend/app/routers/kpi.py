from datetime import datetime, date, timezone
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
from app.services.kpi_engine import (
    compute_kpi_summary,
    compute_next_month_forecast,
    month_actual_and_target,
    period_achievement_row,
)
from app.services.currency import get_cached_rate
from app.services.report_common import ict_today
from app.services.year_forecast import forecast_year

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
    branch = db.query(Branch).filter_by(id=payload.branch_id).first()
    if not branch:
        raise HTTPException(status_code=404, detail="Branch not found")
    cur = branch.currency or branch.native_currency or "VND"
    fx = get_cached_rate(cur, "VND") or 1.0

    existing = (
        db.query(KPITarget)
        .filter_by(branch_id=payload.branch_id, year=payload.year, month=payload.month)
        .first()
    )
    if existing:
        existing.target_revenue_native = payload.target_revenue_native
        existing.target_revenue_vnd = round(payload.target_revenue_native * fx, 2)
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
            target_revenue_vnd=round(payload.target_revenue_native * fx, 2),
            predicted_occ_pct=payload.predicted_occ_pct,
            predicted_room_occ_pct=payload.predicted_room_occ_pct,
            predicted_dorm_occ_pct=payload.predicted_dorm_occ_pct,
            deduction_pct=payload.deduction_pct or 0,
        )
        db.add(target)
        db.commit()
        db.refresh(target)
        return _envelope(KPITargetOut.model_validate(target).model_dump())


@router.post("/targets/backfill-vnd")
def backfill_target_vnd(db: Session = Depends(get_db)):
    """One-time fix: recompute target_revenue_vnd for all existing KPI targets using current FX rates."""
    branches = {str(b.id): b for b in db.query(Branch).all()}
    targets = db.query(KPITarget).all()
    updated = 0
    for t in targets:
        branch = branches.get(str(t.branch_id))
        if not branch:
            continue
        cur = branch.currency or branch.native_currency or "VND"
        fx = get_cached_rate(cur, "VND") or 1.0
        correct_vnd = round(float(t.target_revenue_native or 0) * fx, 2)
        if abs(correct_vnd - float(t.target_revenue_vnd or 0)) > 1:
            t.target_revenue_vnd = correct_vnd
            updated += 1
    db.commit()
    return _envelope({"updated": updated, "total": len(targets)})


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
    """Save fixed deduction %% for a branch. Applies to every month (no monthly reset).
    year/month are accepted for backward compatibility but ignored."""
    branch = db.query(Branch).filter_by(id=payload.branch_id).first()
    if not branch:
        raise HTTPException(status_code=404, detail="Branch not found")
    branch.deduction_pct = max(0, min(100, payload.deduction_pct))
    db.commit()
    db.refresh(branch)
    return _envelope({"saved": True, "deduction_pct": float(branch.deduction_pct)})


@router.put("/other-revenue")
def save_other_revenue(payload: OtherRevenueUpdate, db: Session = Depends(get_db)):
    """Save fixed manual other revenue (native currency) for a branch. Applies to every
    month (no monthly reset). Added on top of the deducted forecast in the All Branches
    summary. year/month are accepted for backward compatibility but ignored."""
    branch = db.query(Branch).filter_by(id=payload.branch_id).first()
    if not branch:
        raise HTTPException(status_code=404, detail="Branch not found")
    branch.other_revenue_native = max(0, float(payload.other_revenue_native or 0))
    db.commit()
    db.refresh(branch)
    return _envelope({"saved": True, "other_revenue_native": float(branch.other_revenue_native)})


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

    # Fixed per-branch adjustments — same value every month (no monthly reset)
    summary["deduction_pct"] = float(branch.deduction_pct or 0)
    summary["other_revenue_native"] = float(branch.other_revenue_native or 0)

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
    _ORDER = ("taipei", "1948", "oani", "osaka", "saigon")
    def _rank(b):
        n = b.name.lower()
        for i, key in enumerate(_ORDER):
            if key in n:
                return i
        return 99

    branches = sorted(q.all(), key=_rank)
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


# ── Pace forecast (will the year's target be hit) ───────────────────────────

@router.get("/pace-forecast")
def kpi_pace_forecast(
    year: int = Query(None, description="Defaults to the current year."),
    branch_id: Optional[UUID] = Query(None),
    days: int = Query(60, ge=1, le=365,
                      description="Booking window the pace is read over."),
    db: Session = Depends(get_db),
):
    """Where the year's revenue lands against its target, on current pace.

    The yearly grid answers "how much has been earned" — in September that is
    79% of a twelve-month target, because four of the months are still zero.
    This answers the question that was meant: months that have finished
    counted as they happened, every month still open projected from booking
    pace, and the two halves reported separately so neither can be mistaken
    for the other.

    `q4_*` is the same arithmetic over October to December alone.
    """
    return _envelope(forecast_year(
        db,
        branch_id=branch_id,
        year=year or ict_today().year,
        as_of=ict_today(),
        days=days,
    ))


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

    branch_by_id = {str(b.id): b for b in branches}

    for mo in range(1, 13):
        row = {"month": mo}
        branch_data = []
        for b in branch_list:
            bid = b["id"]
            kpi = target_map.get((bid, mo), {})
            target = kpi.get("target", 0)
            override = kpi.get("override")
            cloudbeds_actual = actual_map.get((bid, mo), 0)
            # Shared with the Bi-Weekly Branch Manager report's month gauges —
            # see kpi_engine.month_actual_and_target for why this is a whole-
            # month, un-prorated sum rather than capped at "today".
            result = month_actual_and_target(branch_by_id[bid], target, override, cloudbeds_actual)
            actual = result["actual_revenue"]

            branch_data.append({
                "branch_id": bid,
                "target": target,
                "actual": actual,
                "raw_actual": override if override is not None else cloudbeds_actual,
                "cloudbeds_actual": cloudbeds_actual,
                "is_override": result["is_override"],
                "hit_pct": result["achievement_pct"],
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


# ── Multi-Year Comparison (same Target/Actual/Hit% logic, N years side by side) ──

#: More than this many year columns stops being readable on one screen.
MAX_COMPARE_YEARS = 6


def _parse_years(raw: str) -> list[int]:
    """'2026,2025' -> [2025, 2026]. Always ascending, deduped, sanity-bounded."""
    out = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            y = int(part)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Not a year: {part!r}")
        if not 2000 <= y <= 2100:
            raise HTTPException(status_code=400, detail=f"Year out of range: {y}")
        out.add(y)
    if not out:
        raise HTTPException(status_code=400, detail="No years given")
    if len(out) > MAX_COMPARE_YEARS:
        raise HTTPException(
            status_code=400,
            detail=f"At most {MAX_COMPARE_YEARS} years can be compared at once",
        )
    return sorted(out)


def _cutoff_month(years: list[int]) -> int:
    """Last month that is fully in the past, across the compared years.

    A year still running carries forward bookings in `daily_metrics` — Oct-Dec
    2026 read as a fraction of target back in September because those nights
    simply had not happened yet. Comparing such a year's full-year total
    against a closed year measures the calendar, not the business, so
    ``basis=ytd`` clips every year to the same completed months.

    In January nothing has closed yet in the running year, so there is no
    honest YTD window: the full year is returned and the response reports the
    basis it actually got.
    """
    today = ict_today()
    if today.year not in years:
        return 12
    return today.month - 1 if today.month > 1 else 12


def chain_year_deltas(year_list: list[int], by_year: dict) -> dict:
    """Attach each year's move against the previous year *in the selection*.

    Chaining against the selection rather than against `year - 1` means
    dropping 2025 from a 2024/2025/2026 compare re-bases 2026 onto 2024
    instead of leaving a gap.

    A year the branch was not on KPI for is not a baseline: `daily_metrics`
    holds rows for periods nobody was tracking (a branch still fitting out,
    or seeded placeholder figures), and dividing by those manufactures a
    triple-digit move out of nothing. Such a pair gets no percentage.
    """
    prev_year = None
    for y in year_list:
        cur = by_year[y]
        prev = by_year[prev_year] if prev_year is not None else None
        delta = None
        if prev and prev["has_kpi"] and prev["actual"] > 0 and cur["has_kpi"]:
            delta = round((cur["actual"] - prev["actual"]) / prev["actual"] * 100, 1)
        cur["vs_prev_pct"] = delta
        cur["vs_prev_year"] = prev_year
        prev_year = y
    return {str(y): by_year[y] for y in year_list}


@router.get("/multi-year")
def kpi_multi_year(
    years: str = Query(..., description="Comma-separated, e.g. '2025,2026'"),
    branch_id: Optional[UUID] = Query(None),
    basis: str = Query("ytd", pattern="^(ytd|full)$"),
    db: Session = Depends(get_db),
):
    """Target / Actual / Hit% per month for several years at once.

    Same per-month rule as the single-year grid — `month_actual_and_target`
    decides override-vs-Cloudbeds for both — so a month reads identically
    whichever table you open it in.

    One branch reports in its own currency. Without `branch_id` the branches
    are summed, which only works on a single currency, so the group view is
    VND and says so via `scope.currency`.
    """
    year_list = _parse_years(years)

    q = db.query(Branch).filter_by(is_active=True)
    if branch_id:
        q = q.filter(Branch.id == branch_id)
    branches = q.order_by(Branch.name).all()
    if not branches:
        raise HTTPException(status_code=404, detail="No active branch matched")

    single = branches[0] if branch_id else None
    currency = single.currency if single else "VND"

    # FX per branch, only needed to fold several currencies into a group total.
    fx = {
        str(b.id): 1.0 if single else (get_cached_rate(b.currency, "VND") or 1.0)
        for b in branches
    }
    branch_ids = [b.id for b in branches]

    # Targets + overrides for every compared year, one query.
    targets = db.query(KPITarget).filter(
        KPITarget.year.in_(year_list),
        KPITarget.branch_id.in_(branch_ids),
    ).all()
    target_map = {
        (str(t.branch_id), t.year, t.month): (
            float(t.target_revenue_native or 0),
            float(t.actual_revenue_override) if t.actual_revenue_override is not None else None,
        )
        for t in targets
    }

    # Cloudbeds actuals for every compared year, one query.
    rows = db.query(
        DailyMetrics.branch_id,
        extract("year", DailyMetrics.date).label("yr"),
        extract("month", DailyMetrics.date).label("mo"),
        func.coalesce(func.sum(DailyMetrics.revenue_native), 0).label("revenue"),
    ).filter(
        extract("year", DailyMetrics.date).in_(year_list),
        DailyMetrics.branch_id.in_(branch_ids),
    ).group_by(DailyMetrics.branch_id, "yr", "mo").all()
    actual_map = {
        (str(r.branch_id), int(r.yr), int(r.mo)): float(r.revenue) for r in rows
    }

    cutoff = _cutoff_month(year_list) if basis == "ytd" else 12
    effective_basis = "full" if cutoff == 12 else basis

    def cell(year: int, month: int) -> dict:
        target = actual = 0.0
        has_kpi = is_override = False
        for b in branches:
            bid = str(b.id)
            t, override = target_map.get((bid, year, month), (0.0, None))
            res = month_actual_and_target(
                b, t, override, actual_map.get((bid, year, month), 0.0)
            )
            rate = fx[bid]
            target += res["target_revenue"] * rate
            actual += res["actual_revenue"] * rate
            # A branch counts as "on KPI" for a month only once someone set a
            # target or typed an actual. Without either, the figure is whatever
            # Cloudbeds happens to hold for a period nobody was tracking — real
            # enough to show, not solid enough to move a year-on-year % off.
            if t > 0 or res["is_override"]:
                has_kpi = True
            if res["is_override"]:
                is_override = True
        return {
            "target": target,
            "actual": actual,
            "hit_pct": round(actual / target * 100, 1) if target > 0 else None,
            "is_override": is_override,
            "has_kpi": has_kpi,
            "in_basis": month <= cutoff,
        }

    def with_deltas(by_year: dict) -> dict:
        return chain_year_deltas(year_list, by_year)

    months = []
    totals_acc = {
        y: {"target": 0.0, "actual": 0.0, "has_kpi": False,
            "is_override": False, "in_basis": True}
        for y in year_list
    }
    for mo in range(1, 13):
        by_year = {y: cell(y, mo) for y in year_list}
        for y in year_list:
            c = by_year[y]
            if not c["in_basis"]:
                continue
            acc = totals_acc[y]
            acc["target"] += c["target"]
            acc["actual"] += c["actual"]
            acc["has_kpi"] = acc["has_kpi"] or c["has_kpi"]
            acc["is_override"] = acc["is_override"] or c["is_override"]
        months.append({"month": mo, "years": with_deltas(by_year)})

    for y in year_list:
        acc = totals_acc[y]
        acc["hit_pct"] = (
            round(acc["actual"] / acc["target"] * 100, 1) if acc["target"] > 0 else None
        )

    return _envelope({
        "years": year_list,
        "basis": effective_basis,
        "requested_basis": basis,
        "cutoff_month": cutoff,
        "scope": {
            "mode": "branch" if single else "group",
            "branch_id": str(single.id) if single else None,
            "branch_name": single.name if single else "All branches",
            "currency": currency,
        },
        "months": months,
        "totals": with_deltas(totals_acc),
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

    The per-branch math lives in `kpi_engine.period_achievement_row` so the
    Bi-Weekly Branch Manager report reports achievement identically. Loading
    stays batched here — one targets query and one revenue query for all
    branches, rather than two per branch.
    """
    q = db.query(Branch).filter_by(is_active=True)
    if branch_id:
        q = q.filter(Branch.id == branch_id)
    branches = q.all()

    branch_ids = [b.id for b in branches]

    # Load targets for all relevant months
    targets = db.query(KPITarget).filter(
        KPITarget.branch_id.in_(branch_ids),
    ).all()

    # target_map[branch_id][(year, month)] = (target_revenue_native, target_revenue_vnd)
    target_map: dict[str, dict] = {}
    for t in targets:
        target_map.setdefault(str(t.branch_id), {})[(t.year, t.month)] = (
            float(t.target_revenue_native or 0),
            float(t.target_revenue_vnd or 0),
        )

    # Load actual revenue from daily_metrics grouped by branch (native + VND)
    actuals = db.query(
        DailyMetrics.branch_id,
        func.coalesce(func.sum(DailyMetrics.revenue_native), 0).label("revenue"),
        func.coalesce(func.sum(DailyMetrics.revenue_vnd), 0).label("revenue_vnd"),
    ).filter(
        DailyMetrics.branch_id.in_(branch_ids),
        DailyMetrics.date >= date_from,
        DailyMetrics.date <= date_to,
    ).group_by(DailyMetrics.branch_id).all()

    actual_map = {str(a.branch_id): float(a.revenue) for a in actuals}
    actual_vnd_map = {str(a.branch_id): float(a.revenue_vnd) for a in actuals}

    results = []
    for branch in branches:
        bid = str(branch.id)
        cur = branch.currency or branch.native_currency or "VND"
        # FX for normalising native amounts to VND (so multi-currency branches can
        # be summed into a single group total). other_revenue is stored native only.
        fx = get_cached_rate(cur, "VND") or 1.0
        results.append(period_achievement_row(
            branch, date_from, date_to,
            monthly_targets=target_map.get(bid, {}),
            actual_revenue=actual_map.get(bid, 0),
            actual_revenue_vnd=actual_vnd_map.get(bid, 0),
            fx=fx,
        ))

    return _envelope(results)
