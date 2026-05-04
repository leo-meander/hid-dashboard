"""Ad Analyzer router — AI-powered ad analysis with funnel diagnostics."""
import threading
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import func

from app.database import get_db
from app.models.ad_combo import AdCombo
from app.models.ad_analysis import AdAnalysisResult
from app.models.ads import AdsPerformance
from app.services.ad_analyzer_service import run_analysis

router = APIRouter()


def _envelope(data):
    return {"success": True, "data": data, "error": None,
            "timestamp": datetime.now(timezone.utc).isoformat()}


def _result_dict(r: AdAnalysisResult) -> dict:
    return {
        "id": str(r.id),
        "combo_id": str(r.combo_id),
        "detected_angles": r.detected_angles,
        "detected_ta": r.detected_ta,
        "keypoints": r.keypoints,
        "visual_summary": r.visual_summary,
        "funnel_analysis": r.funnel_analysis,
        "ai_recommendation": r.ai_recommendation,
        "recommendation_type": r.recommendation_type,
        "confidence_score": float(r.confidence_score) if r.confidence_score else None,
        "model_used": r.model_used,
        "analyzed_at": r.analyzed_at.isoformat() if r.analyzed_at else None,
        # Include combo info for display
        "combo_code": r.combo.combo_code if r.combo else None,
        "combo": {
            "combo_code": r.combo.combo_code,
            "meta_ad_name": r.combo.meta_ad_name,
            "target_audience": r.combo.target_audience,
            "channel": r.combo.channel,
            "roas": float(r.combo.roas) if r.combo.roas else None,
            "spend_vnd": float(r.combo.spend_vnd) if r.combo.spend_vnd else None,
            "revenue_vnd": float(r.combo.revenue_vnd) if r.combo.revenue_vnd else None,
            "impressions": r.combo.impressions,
            "clicks": r.combo.clicks,
            "purchases": r.combo.purchases,
            "lp_views": r.combo.lp_views,
            "add_to_cart": r.combo.add_to_cart,
            "verdict": r.combo.verdict,
            "run_status": r.combo.run_status,
            "copy_headline": r.combo.copy.headline if r.combo.copy else None,
            "copy_primary_text": (r.combo.copy.primary_text[:150] + "...") if r.combo.copy and r.combo.copy.primary_text and len(r.combo.copy.primary_text) > 150 else (r.combo.copy.primary_text if r.combo.copy else None),
            "material_type": r.combo.material.material_type if r.combo.material else None,
            "material_link": r.combo.material.file_link if r.combo.material else None,
        } if r.combo else None,
    }


def _get_branch_benchmark(db: Session, branch_id) -> float | None:
    """Compute AVG ROAS for TOF Sales campaigns for a branch."""
    row = (
        db.query(
            func.sum(func.coalesce(AdsPerformance.revenue_vnd, AdsPerformance.revenue_native)).label("rev"),
            func.sum(func.coalesce(AdsPerformance.cost_vnd, AdsPerformance.cost_native)).label("cost"),
        )
        .filter(
            AdsPerformance.branch_id == branch_id,
            AdsPerformance.funnel_stage == "TOF",
            AdsPerformance.campaign_name.ilike("%Sales%"),
            func.coalesce(AdsPerformance.cost_vnd, AdsPerformance.cost_native) > 0,
        )
        .first()
    )
    if row and row.cost and float(row.cost) > 0:
        return round(float(row.rev or 0) / float(row.cost), 2)
    return None


class AnalyzeRequest(BaseModel):
    combo_id: UUID


@router.post("/analyze")
def analyze_single(body: AnalyzeRequest, db: Session = Depends(get_db)):
    """Trigger AI analysis for a single combo. Runs in background thread."""
    combo = db.query(AdCombo).filter(
        AdCombo.id == body.combo_id, AdCombo.is_active == True
    ).first()
    if not combo:
        raise HTTPException(404, "Combo not found")

    # Check if already analyzed
    existing = db.query(AdAnalysisResult).filter(
        AdAnalysisResult.combo_id == body.combo_id
    ).first()

    benchmark = _get_branch_benchmark(db, combo.branch_id)

    # Run analysis (synchronous for single combo — fast enough)
    result_data = run_analysis(combo, db, benchmark)

    if existing:
        for k, v in result_data.items():
            if k != "combo_id":
                setattr(existing, k, v)
        db.commit()
        db.refresh(existing)
        return _envelope(_result_dict(existing))
    else:
        obj = AdAnalysisResult(**result_data)
        db.add(obj)
        db.commit()
        db.refresh(obj)
        return _envelope(_result_dict(obj))


class BatchRequest(BaseModel):
    branch_id: UUID
    force_reanalyze: Optional[bool] = False


@router.post("/analyze-batch")
def analyze_batch(body: BatchRequest, db: Session = Depends(get_db)):
    """Trigger AI analysis for all combos in a branch. Runs in background."""
    q = db.query(AdCombo).filter(
        AdCombo.branch_id == body.branch_id,
        AdCombo.is_active == True,
    )
    combos = q.all()
    if not combos:
        return _envelope({"queued": 0, "message": "No combos found for this branch"})

    # Filter out already-analyzed unless force
    if not body.force_reanalyze:
        analyzed_ids = {
            r.combo_id for r in
            db.query(AdAnalysisResult.combo_id).filter(
                AdAnalysisResult.combo_id.in_([c.id for c in combos])
            ).all()
        }
        combos = [c for c in combos if c.id not in analyzed_ids]

    if not combos:
        return _envelope({"queued": 0, "message": "All combos already analyzed"})

    combo_ids = [c.id for c in combos]
    branch_id = body.branch_id

    def _run_batch():
        from app.database import SessionLocal
        sess = SessionLocal()
        try:
            benchmark = _get_branch_benchmark(sess, branch_id)
            for cid in combo_ids:
                combo = sess.query(AdCombo).filter(AdCombo.id == cid).first()
                if not combo:
                    continue
                try:
                    result_data = run_analysis(combo, sess, benchmark)
                    existing = sess.query(AdAnalysisResult).filter(
                        AdAnalysisResult.combo_id == cid
                    ).first()
                    if existing:
                        for k, v in result_data.items():
                            if k != "combo_id":
                                setattr(existing, k, v)
                    else:
                        sess.add(AdAnalysisResult(**result_data))
                    sess.commit()
                except Exception as e:
                    sess.rollback()
                    import logging
                    logging.getLogger(__name__).error("Batch analysis failed for %s: %s", cid, e)
        finally:
            sess.close()

    threading.Thread(target=_run_batch, daemon=True).start()
    return _envelope({"queued": len(combo_ids), "message": f"Analyzing {len(combo_ids)} combos in background"})


@router.get("/results")
def list_results(
    branch_id: Optional[UUID] = Query(None),
    combo_id: Optional[UUID] = Query(None),
    recommendation_type: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    detected_angle: Optional[str] = Query(None),
    detected_ta: Optional[str] = Query(None),
    verdict: Optional[str] = Query(None),
    limit: int = Query(200, ge=1, le=500),
    db: Session = Depends(get_db),
):
    """Get analysis results with search + filters."""
    from app.models.creative_copy import CreativeCopy

    q = (db.query(AdAnalysisResult)
         .join(AdCombo)
         .options(
             joinedload(AdAnalysisResult.combo)
             .joinedload(AdCombo.copy),
             joinedload(AdAnalysisResult.combo)
             .joinedload(AdCombo.material),
         ))
    if branch_id:
        q = q.filter(AdCombo.branch_id == branch_id)
    if combo_id:
        q = q.filter(AdAnalysisResult.combo_id == combo_id)
    if recommendation_type:
        q = q.filter(AdAnalysisResult.recommendation_type == recommendation_type)
    if verdict:
        q = q.filter(AdCombo.verdict == verdict)
    if detected_angle:
        q = q.filter(AdAnalysisResult.detected_angles.any(detected_angle))
    if detected_ta:
        q = q.filter(AdAnalysisResult.detected_ta.any(detected_ta))
    if search:
        pattern = f"%{search}%"
        q = q.outerjoin(CreativeCopy, AdCombo.copy_id == CreativeCopy.id).filter(
            (AdCombo.combo_code.ilike(pattern)) |
            (AdCombo.meta_ad_name.ilike(pattern)) |
            (CreativeCopy.headline.ilike(pattern)) |
            (AdAnalysisResult.ai_recommendation.ilike(pattern))
        )
    results = q.order_by(AdAnalysisResult.analyzed_at.desc()).limit(limit).all()
    return _envelope([_result_dict(r) for r in results])


@router.get("/insights")
def analyzer_insights(
    branch_id: Optional[UUID] = Query(None),
    db: Session = Depends(get_db),
):
    """Aggregated insights for charts: angle performance, TA×Angle matrix, funnel summary."""
    q = (db.query(AdAnalysisResult)
         .join(AdCombo)
         .options(joinedload(AdAnalysisResult.combo))
         .filter(AdCombo.is_active == True))
    if branch_id:
        q = q.filter(AdCombo.branch_id == branch_id)
    results = q.all()

    last_synced = max(
        (r.analyzed_at for r in results if r.analyzed_at), default=None
    )
    last_synced_iso = last_synced.isoformat() if last_synced else None

    if not results:
        return _envelope({
            "total_analyzed": 0,
            "angle_performance": [],
            "ta_angle_matrix": [],
            "recommendation_summary": {},
            "funnel_aggregate": {},
            "data_synced_at": None,
        })

    # Angle performance: avg CTR per angle
    angle_stats = {}
    for r in results:
        combo = r.combo
        if not combo or not r.detected_angles:
            continue
        ctr = (combo.clicks / combo.impressions * 100) if combo.impressions and combo.impressions > 0 else 0
        roas = float(combo.roas) if combo.roas else 0
        for angle in r.detected_angles:
            if angle not in angle_stats:
                angle_stats[angle] = {"ctrs": [], "roas_values": [], "count": 0}
            angle_stats[angle]["ctrs"].append(ctr)
            angle_stats[angle]["roas_values"].append(roas)
            angle_stats[angle]["count"] += 1

    angle_performance = [
        {
            "angle": angle,
            "avg_ctr": round(sum(s["ctrs"]) / len(s["ctrs"]), 2) if s["ctrs"] else 0,
            "avg_roas": round(sum(s["roas_values"]) / len(s["roas_values"]), 2) if s["roas_values"] else 0,
            "count": s["count"],
        }
        for angle, s in sorted(angle_stats.items(), key=lambda x: -sum(x[1]["ctrs"]) / max(len(x[1]["ctrs"]), 1))
    ]

    # TA × Angle matrix
    ta_angle = {}
    for r in results:
        combo = r.combo
        if not combo or not r.detected_angles or not r.detected_ta:
            continue
        roas = float(combo.roas) if combo.roas else 0
        for ta in r.detected_ta:
            for angle in r.detected_angles:
                key = (ta, angle)
                if key not in ta_angle:
                    ta_angle[key] = {"roas_values": [], "count": 0}
                ta_angle[key]["roas_values"].append(roas)
                ta_angle[key]["count"] += 1

    ta_angle_matrix = [
        {
            "ta": ta,
            "angle": angle,
            "avg_roas": round(sum(s["roas_values"]) / len(s["roas_values"]), 2) if s["roas_values"] else 0,
            "count": s["count"],
        }
        for (ta, angle), s in ta_angle.items()
    ]

    # Recommendation summary
    rec_counts = {}
    for r in results:
        rt = r.recommendation_type or "unknown"
        rec_counts[rt] = rec_counts.get(rt, 0) + 1

    # Aggregate funnel
    total_imp = sum(r.combo.impressions or 0 for r in results if r.combo)
    total_clicks = sum(r.combo.clicks or 0 for r in results if r.combo)
    total_lp = sum(r.combo.lp_views or 0 for r in results if r.combo)
    total_atc = sum(r.combo.add_to_cart or 0 for r in results if r.combo)
    total_co = sum(r.combo.initiate_checkout or 0 for r in results if r.combo)
    total_pur = sum(r.combo.purchases or 0 for r in results if r.combo)

    funnel_aggregate = {
        "impressions": total_imp,
        "clicks": total_clicks,
        "ctr": round(total_clicks / total_imp * 100, 2) if total_imp > 0 else 0,
        "lp_views": total_lp,
        "lp_view_rate": round(total_lp / total_clicks * 100, 1) if total_clicks > 0 else 0,
        "add_to_cart": total_atc,
        "atc_rate": round(total_atc / total_lp * 100, 1) if total_lp > 0 else 0,
        "checkout": total_co,
        "checkout_rate": round(total_co / total_atc * 100, 1) if total_atc > 0 else 0,
        "purchases": total_pur,
        "purchase_rate": round(total_pur / total_co * 100, 1) if total_co > 0 else 0,
    }

    return _envelope({
        "total_analyzed": len(results),
        "angle_performance": angle_performance,
        "ta_angle_matrix": ta_angle_matrix,
        "recommendation_summary": rec_counts,
        "funnel_aggregate": funnel_aggregate,
        "data_synced_at": last_synced_iso,
    })
