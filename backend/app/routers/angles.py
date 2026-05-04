"""Ad Angles router — WIN/TEST/LOSE based on TOF Sales campaign performance.

Logic:
1. Only consider combos linked to TOF Sales campaigns
2. Benchmark_TOF = AVG ROAS of ALL TOF Sales ads in that branch
3. IF impressions < 20K OR bookings < 5 → TEST (insufficient data)
4. IF ROAS >= Benchmark_TOF → WIN
5. IF ROAS <= 0.6 × Benchmark_TOF → LOSE
6. ELSE → TEST
"""
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.database import get_db
from app.models.creative_angle import CreativeAngle
from app.models.ad_combo import AdCombo
from app.models.ads import AdsPerformance

router = APIRouter()


def _envelope(data):
    return {"success": True, "data": data, "error": None,
            "timestamp": datetime.now(timezone.utc).isoformat()}


class AngleIn(BaseModel):
    name: str
    description: Optional[str] = None
    status: Optional[str] = None
    branch_id: Optional[UUID] = None
    created_by: Optional[str] = None


# ── Thresholds ────────────────────────────────────────────────
MIN_IMPRESSIONS = 20_000
MIN_BOOKINGS = 5


def _compute_score(roas, ctr_pct, cpb_native, bookings):
    """Score 0-100: ROAS 40% + CTR 25% + CPB(inv) 25% + Volume 10%."""
    if roas is None and ctr_pct is None and cpb_native is None and bookings == 0:
        return None
    roas_score = min((roas or 0) / 5.0, 1.0) * 0.40
    ctr_score = min((ctr_pct or 0) / 5.0, 1.0) * 0.25
    cpb_score = max(0, 1 - ((cpb_native or 500) / 500)) * 0.25 if cpb_native and cpb_native > 0 else 0.0
    vol_score = min((bookings or 0) / 50.0, 1.0) * 0.10
    return round((roas_score + ctr_score + cpb_score + vol_score) * 100, 1)


def _compute_branch_benchmarks(db: Session, branch_ids: list[str]) -> dict[str, float]:
    """Compute AVG ROAS for TOF Sales campaigns per branch from ads_performance."""
    if not branch_ids:
        return {}

    rows = (
        db.query(
            AdsPerformance.branch_id,
            func.sum(func.coalesce(AdsPerformance.revenue_vnd, AdsPerformance.revenue_native)).label("total_rev"),
            func.sum(func.coalesce(AdsPerformance.cost_vnd, AdsPerformance.cost_native)).label("total_cost"),
        )
        .filter(
            AdsPerformance.funnel_stage == "TOF",
            AdsPerformance.campaign_name.ilike("%Sales%"),
            func.coalesce(AdsPerformance.cost_vnd, AdsPerformance.cost_native) > 0,
        )
        .group_by(AdsPerformance.branch_id)
        .all()
    )

    benchmarks = {}
    for r in rows:
        bid = str(r.branch_id)
        total_rev = float(r.total_rev or 0)
        total_cost = float(r.total_cost or 0)
        if total_cost > 0:
            benchmarks[bid] = round(total_rev / total_cost, 2)
    return benchmarks


def _get_tof_sales_stats_per_angle(db: Session, branch_id: Optional[UUID] = None) -> dict:
    """
    For each angle, aggregate performance ONLY from combos whose
    matched ads_performance rows are TOF Sales campaigns.

    Returns: { angle_id_str: { impressions, bookings, spend, revenue, roas, qualifying_combos } }
    """
    # Join ad_combos → ads_performance via meta_ad_name = ad_name
    # Filter: TOF + Sales in campaign_name
    q = (
        db.query(
            AdCombo.angle_id,
            func.sum(AdsPerformance.impressions).label("impressions"),
            func.sum(AdsPerformance.clicks).label("clicks"),
            func.sum(AdsPerformance.bookings).label("bookings"),
            func.sum(func.coalesce(AdsPerformance.cost_vnd, AdsPerformance.cost_native)).label("spend"),
            func.sum(func.coalesce(AdsPerformance.revenue_vnd, AdsPerformance.revenue_native)).label("revenue"),
            func.count(AdCombo.id).label("qualifying_combos"),
        )
        .join(AdsPerformance, AdsPerformance.ad_name == AdCombo.meta_ad_name)
        .filter(
            AdCombo.angle_id.isnot(None),
            AdCombo.is_active == True,
            AdCombo.meta_ad_name.isnot(None),
            AdsPerformance.funnel_stage == "TOF",
            AdsPerformance.campaign_name.ilike("%Sales%"),
        )
    )
    if branch_id:
        q = q.filter(AdCombo.branch_id == branch_id)

    q = q.group_by(AdCombo.angle_id)

    result = {}
    for r in q.all():
        aid = str(r.angle_id)
        spend = float(r.spend or 0)
        rev = float(r.revenue or 0)
        impr = int(r.impressions or 0)
        clicks = int(r.clicks or 0)
        bk = int(r.bookings or 0)
        result[aid] = {
            "impressions": impr,
            "clicks": clicks,
            "bookings": bk,
            "spend": spend,
            "revenue": rev,
            "roas": round(rev / spend, 2) if spend > 0 else None,
            "ctr_pct": round(clicks / impr * 100, 2) if impr > 0 else None,
            "cpb": round(spend / bk, 2) if bk > 0 else None,
            "qualifying_combos": int(r.qualifying_combos),
        }
    return result


def _derive_status(stats: dict | None, benchmark_tof: float | None) -> tuple[str, str | None]:
    """
    Apply TOF Sales verdict logic.
    Returns (status, reason).
    """
    if not stats or stats["qualifying_combos"] == 0:
        return "TEST", "no_tof_sales_data"

    impr = stats["impressions"]
    bk = stats["bookings"]
    roas = stats["roas"]

    # Insufficient data check
    if impr < MIN_IMPRESSIONS or bk < MIN_BOOKINGS:
        return "TEST", "insufficient_data"

    # No benchmark available for this branch
    if benchmark_tof is None or benchmark_tof == 0:
        return "TEST", "no_benchmark"

    # Apply threshold
    if roas is not None and roas >= benchmark_tof:
        return "WIN", None
    elif roas is not None and roas <= 0.6 * benchmark_tof:
        return "LOSE", None
    else:
        return "TEST", None


@router.get("")
def list_angles(
    branch_id: Optional[UUID] = Query(None),
    status: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    # ── 1. Fetch creative angles ──────────────────────────────
    q = db.query(CreativeAngle).filter(CreativeAngle.is_active == True)
    if branch_id:
        q = q.filter(CreativeAngle.branch_id == branch_id)
    angles = q.order_by(CreativeAngle.created_at.desc()).all()

    last_synced_q = db.query(func.max(AdCombo.last_synced_at))
    if branch_id:
        last_synced_q = last_synced_q.filter(AdCombo.branch_id == branch_id)
    last_synced_ts = last_synced_q.scalar()
    last_synced_iso = last_synced_ts.isoformat() if last_synced_ts else None

    if not angles:
        return _envelope([])

    # ── 2. Compute branch benchmarks (AVG ROAS of TOF Sales) ─
    branch_ids = list({str(a.branch_id) for a in angles if a.branch_id})
    benchmarks = _compute_branch_benchmarks(db, branch_ids)

    # ── 3. Get TOF Sales stats per angle ──────────────────────
    tof_stats = _get_tof_sales_stats_per_angle(db, branch_id)

    # ── 4. Also get ALL combo stats (for display — total spend/ROAS across all campaigns)
    all_stats_q = (
        db.query(
            AdCombo.angle_id,
            func.coalesce(func.sum(AdCombo.spend_vnd), 0).label("cost"),
            func.coalesce(func.sum(AdCombo.revenue_vnd), 0).label("revenue"),
            func.coalesce(func.sum(AdCombo.impressions), 0).label("impressions"),
            func.coalesce(func.sum(AdCombo.clicks), 0).label("clicks"),
            func.coalesce(func.sum(AdCombo.purchases), 0).label("bookings"),
            func.count(AdCombo.id).label("combo_count"),
        )
        .filter(AdCombo.angle_id.isnot(None), AdCombo.is_active == True)
    )
    if branch_id:
        all_stats_q = all_stats_q.filter(AdCombo.branch_id == branch_id)
    all_stats_map = {str(r.angle_id): r for r in all_stats_q.group_by(AdCombo.angle_id).all()}

    # ── 5. Build response ─────────────────────────────────────
    result = []
    for a in angles:
        aid = str(a.id)
        bid = str(a.branch_id) if a.branch_id else None
        benchmark = benchmarks.get(bid) if bid else None

        # TOF Sales stats for verdict
        tof = tof_stats.get(aid)
        angle_status, status_reason = _derive_status(tof, benchmark)

        # All-campaign stats for display
        s = all_stats_map.get(aid)
        total_cost = float(s.cost) if s else 0
        total_rev = float(s.revenue) if s else 0
        total_impr = int(s.impressions) if s else 0
        total_clicks = int(s.clicks) if s else 0
        total_bookings = int(s.bookings) if s else 0
        total_combos = int(s.combo_count) if s else 0

        total_roas = round(total_rev / total_cost, 2) if total_cost > 0 else None
        ctr_pct = round(total_clicks / total_impr * 100, 2) if total_impr > 0 else None
        cpb = round(total_cost / total_bookings, 2) if total_bookings > 0 else None
        score = _compute_score(total_roas, ctr_pct, cpb, total_bookings)

        result.append({
            "id": aid,
            "angle_code": a.angle_code,
            "name": a.name,
            "description": a.notes,
            "hook_type": a.hook_type,
            "keypoint_1": a.keypoint_1,
            "keypoint_2": a.keypoint_2,
            "keypoint_3": a.keypoint_3,
            "keypoint_4": a.keypoint_4,
            "keypoint_5": a.keypoint_5,
            "status": angle_status,
            "status_reason": status_reason,
            "branch_id": bid,
            "created_by": None,
            "created_at": a.created_at.isoformat() if a.created_at else None,

            # TOF Sales metrics (used for verdict)
            "tof_impressions": tof["impressions"] if tof else 0,
            "tof_bookings": tof["bookings"] if tof else 0,
            "tof_roas": tof["roas"] if tof else None,
            "tof_spend": tof["spend"] if tof else 0,
            "tof_revenue": tof["revenue"] if tof else 0,
            "qualifying_combos": tof["qualifying_combos"] if tof else 0,
            "benchmark_tof": benchmark,

            # All-campaign metrics (for display)
            "cost_native": total_cost,
            "revenue_native": total_rev,
            "impressions": total_impr,
            "clicks": total_clicks,
            "bookings": total_bookings,
            "roas": total_roas,
            "ctr_pct": ctr_pct,
            "cpb_native": cpb,
            "score": score,
            "combo_count": total_combos,
            "data_synced_at": last_synced_iso,
        })

    # Filter by status if requested
    if status:
        result = [r for r in result if r["status"] == status]

    # Sort: WIN > TEST > LOSE, then by score desc
    status_order = {"WIN": 0, "TEST": 1, "LOSE": 2, None: 3}
    result.sort(key=lambda x: (status_order.get(x["status"], 3), -(x["score"] or 0)))
    return _envelope(result)


@router.post("")
def create_angle(body: AngleIn, db: Session = Depends(get_db)):
    """Create a new creative angle."""
    from app.services.id_generator import generate_code
    angle_code = generate_code(db, "ANG", "creative_angles", "angle_code")
    obj = CreativeAngle(
        angle_code=angle_code,
        name=body.name,
        hook_type="Story",
        keypoint_1=body.description or body.name,
        branch_id=body.branch_id,
        notes=body.description,
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return _envelope({"id": str(obj.id), "name": obj.name, "status": "TEST"})


@router.put("/{angle_id}")
def update_angle(angle_id: UUID, body: AngleIn, db: Session = Depends(get_db)):
    obj = db.query(CreativeAngle).filter(CreativeAngle.id == angle_id, CreativeAngle.is_active == True).first()
    if not obj:
        raise HTTPException(404, "Angle not found")
    obj.name = body.name
    if body.description:
        obj.notes = body.description
    obj.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(obj)
    return _envelope({"id": str(obj.id), "name": obj.name, "status": "TEST"})


@router.delete("/{angle_id}")
def delete_angle(angle_id: UUID, db: Session = Depends(get_db)):
    obj = db.query(CreativeAngle).filter(CreativeAngle.id == angle_id).first()
    if not obj:
        raise HTTPException(404, "Angle not found")
    obj.is_active = False
    db.commit()
    return _envelope({"deleted": str(angle_id)})
