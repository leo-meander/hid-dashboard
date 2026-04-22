"""Ad Combos router — the verdict layer. Copy × Material = one ad unit."""
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from app.database import get_db
from app.models.ad_combo import AdCombo
from app.models.creative_copy import CreativeCopy
from app.models.creative_material import CreativeMaterial
from app.services.id_generator import generate_code
from app.services.verdict_sync import sync_combo_performance, compute_derived_verdicts
from app.services.email_service import send_approval_email
from app.services import angle_classifier
from app.models.user import User
from app.models.creative_angle import CreativeAngle

router = APIRouter()


def _envelope(data):
    return {"success": True, "data": data, "error": None,
            "timestamp": datetime.now(timezone.utc).isoformat()}


def _combo_dict(cb: AdCombo, include_detail=False) -> dict:
    result = {
        "id": str(cb.id),
        "combo_code": cb.combo_code,
        "copy_id": str(cb.copy_id),
        "material_id": str(cb.material_id),
        "branch_id": str(cb.branch_id),
        "target_audience": cb.target_audience,
        "channel": cb.channel,
        "language": cb.language,
        "country_target": cb.country_target,
        "angle_id": str(cb.angle_id) if cb.angle_id else None,
        "meta_ad_name": cb.meta_ad_name,
        "verdict": cb.verdict,
        "verdict_source": cb.verdict_source,
        "verdict_notes": cb.verdict_notes,
        "spend_vnd": float(cb.spend_vnd) if cb.spend_vnd else None,
        "revenue_vnd": float(cb.revenue_vnd) if cb.revenue_vnd else None,
        "roas": float(cb.roas) if cb.roas else None,
        "impressions": cb.impressions,
        "clicks": cb.clicks,
        "leads": cb.leads,
        "purchases": cb.purchases,
        "lp_views": cb.lp_views,
        "add_to_cart": cb.add_to_cart,
        "initiate_checkout": cb.initiate_checkout,
        "date_first_run": cb.date_first_run.isoformat() if cb.date_first_run else None,
        "date_last_run": cb.date_last_run.isoformat() if cb.date_last_run else None,
        "run_status": cb.run_status,
        "last_synced_at": cb.last_synced_at.isoformat() if cb.last_synced_at else None,
        "kol_id": str(cb.kol_id) if cb.kol_id else None,
        "approval_status": cb.approval_status,
        "submitted_by": cb.submitted_by,
        "reviewer_id": str(cb.reviewer_id) if cb.reviewer_id else None,
        "reviewer_name": cb.reviewer.name if cb.reviewer else None,
        "approval_deadline": cb.approval_deadline.isoformat() if cb.approval_deadline else None,
        "approval_feedback": cb.approval_feedback,
        "approved_at": cb.approved_at.isoformat() if cb.approved_at else None,
        "is_active": cb.is_active,
        "created_at": cb.created_at.isoformat() if cb.created_at else None,
    }
    if cb.kol:
        result["kol"] = {
            "kol_name": cb.kol.kol_name,
            "kol_nationality": cb.kol.kol_nationality,
            "paid_ads_eligible": cb.kol.paid_ads_eligible,
        }
    # Always include copy + material summary for card display
    if cb.copy:
        result["copy"] = {
            "copy_code": cb.copy.copy_code,
            "headline": cb.copy.headline,
            "primary_text": cb.copy.primary_text if include_detail else (cb.copy.primary_text[:120] + "..." if cb.copy.primary_text and len(cb.copy.primary_text) > 120 else cb.copy.primary_text),
            "ad_format": cb.copy.ad_format,
            "landing_page_url": cb.copy.landing_page_url if include_detail else None,
        }
    if cb.material:
        result["material"] = {
            "material_code": cb.material.material_code,
            "material_type": cb.material.material_type,
            "file_link": cb.material.file_link,
            "kol_name": cb.material.kol_name,
            "design_type": cb.material.design_type,
        }
    if cb.angle:
        result["angle"] = {
            "angle_code": cb.angle.angle_code,
            "name": cb.angle.name,
            "hook_type": cb.angle.hook_type,
        }
    return result


class ComboIn(BaseModel):
    copy_id: UUID
    material_id: UUID
    kol_id: Optional[UUID] = None
    meta_ad_name: Optional[str] = None
    run_status: Optional[str] = None
    verdict: Optional[str] = None
    verdict_notes: Optional[str] = None
    date_first_run: Optional[str] = None
    # Approval fields (optional — submit for approval after creation)
    submit_approval: Optional[bool] = False
    reviewer_id: Optional[UUID] = None
    approval_deadline: Optional[str] = None
    submitted_by: Optional[str] = None


class ComboUpdate(BaseModel):
    meta_ad_name: Optional[str] = None
    run_status: Optional[str] = None
    verdict: Optional[str] = None
    verdict_notes: Optional[str] = None
    date_first_run: Optional[str] = None
    date_last_run: Optional[str] = None


@router.get("")
def list_combos(
    branch_id: Optional[UUID] = Query(None),
    target_audience: Optional[str] = Query(None),
    channel: Optional[str] = Query(None),
    language: Optional[str] = Query(None),
    country_target: Optional[str] = Query(None),
    verdict: Optional[str] = Query(None),
    angle_id: Optional[UUID] = Query(None),
    run_status: Optional[str] = Query(None),
    copy_id: Optional[UUID] = Query(None),
    material_id: Optional[UUID] = Query(None),
    db: Session = Depends(get_db),
):
    q = db.query(AdCombo).filter(AdCombo.is_active == True)
    if branch_id:
        q = q.filter(AdCombo.branch_id == branch_id)
    if target_audience:
        q = q.filter(AdCombo.target_audience.any(target_audience))
    if channel:
        q = q.filter(AdCombo.channel == channel)
    if language:
        q = q.filter(AdCombo.language == language)
    if country_target:
        q = q.filter(AdCombo.country_target == country_target)
    if verdict:
        q = q.filter(AdCombo.verdict == verdict)
    if angle_id:
        q = q.filter(AdCombo.angle_id == angle_id)
    if run_status:
        q = q.filter(AdCombo.run_status == run_status)
    if copy_id:
        q = q.filter(AdCombo.copy_id == copy_id)
    if material_id:
        q = q.filter(AdCombo.material_id == material_id)
    combos = q.order_by(AdCombo.created_at.desc()).all()
    return _envelope([_combo_dict(cb) for cb in combos])


@router.post("")
def create_combo(body: ComboIn, db: Session = Depends(get_db)):
    # Validate copy and material exist
    copy = db.query(CreativeCopy).filter(CreativeCopy.id == body.copy_id, CreativeCopy.is_active == True).first()
    if not copy:
        raise HTTPException(404, "Copy not found")
    material = db.query(CreativeMaterial).filter(CreativeMaterial.id == body.material_id, CreativeMaterial.is_active == True).first()
    if not material:
        raise HTTPException(404, "Material not found")

    # Validate same branch
    if str(copy.branch_id) != str(material.branch_id):
        raise HTTPException(422, "Copy and material must belong to the same branch")

    combo_code = generate_code(db, "CMB", "ad_combos", "combo_code")

    data = body.model_dump()
    if data.get("date_first_run"):
        from datetime import date as dt
        data["date_first_run"] = dt.fromisoformat(data["date_first_run"])

    # If verdict is manually set, mark source as manual
    verdict_source = None
    if data.get("verdict"):
        verdict_source = "manual"

    try:
        obj = AdCombo(
            combo_code=combo_code,
            copy_id=body.copy_id,
            material_id=body.material_id,
            branch_id=copy.branch_id,
            target_audience=copy.target_audience,
            channel=copy.channel,
            language=copy.language,
            country_target=copy.country_target,
            angle_id=copy.angle_id,
            kol_id=body.kol_id,
            meta_ad_name=data.get("meta_ad_name"),
            verdict=data.get("verdict"),
            verdict_source=verdict_source,
            verdict_notes=data.get("verdict_notes"),
            run_status=data.get("run_status"),
            date_first_run=data.get("date_first_run"),
        )
        db.add(obj)
        db.commit()
        db.refresh(obj)
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "This copy + material combination already exists")

    # Handle approval submission
    if body.submit_approval and body.reviewer_id:
        reviewer = db.query(User).filter(User.id == body.reviewer_id).first()
        if reviewer:
            from app.models.branch import Branch
            branch = db.query(Branch).filter(Branch.id == copy.branch_id).first()
            deadline = body.approval_deadline
            obj.approval_status = "Pending"
            obj.submitted_by = data.get("submitted_by") or "Unknown"
            obj.reviewer_id = body.reviewer_id
            if deadline:
                from datetime import date as dt
                obj.approval_deadline = dt.fromisoformat(deadline)
            db.commit()
            db.refresh(obj)

            # Look up submitter email for CC
            submitter_user = db.query(User).filter(User.name == obj.submitted_by).first()
            submitter_email = submitter_user.email if submitter_user else None

            # Send email in background thread so it doesn't block the response
            import threading
            threading.Thread(
                target=send_approval_email,
                kwargs={
                    "reviewer_email": reviewer.email,
                    "reviewer_name": reviewer.name,
                    "combo_code": combo_code,
                    "combo_id": str(obj.id),
                    "branch_name": branch.name if branch else "Unknown",
                    "material_type": material.material_type,
                    "submitted_by": obj.submitted_by,
                    "approval_deadline": deadline,
                    "material_link": material.file_link,
                    "kol_name": material.kol_name,
                    "submitter_email": submitter_email,
                },
                daemon=True,
            ).start()

    return _envelope(_combo_dict(obj))


@router.get("/insights")
def combo_insights(
    branch_id: Optional[UUID] = Query(None),
    db: Session = Depends(get_db),
):
    """Aggregated: top combos by ROAS per audience per branch."""
    q = db.query(AdCombo).filter(AdCombo.is_active == True, AdCombo.verdict.isnot(None))
    if branch_id:
        q = q.filter(AdCombo.branch_id == branch_id)

    total = q.count()
    winning = q.filter(AdCombo.verdict == "WIN").count()
    top_roas = q.filter(AdCombo.roas.isnot(None)).order_by(AdCombo.roas.desc()).limit(5).all()

    return _envelope({
        "total_combos": total,
        "win_count": winning,
        "top_by_roas": [
            {"combo_code": cb.combo_code, "roas": float(cb.roas), "target_audience": cb.target_audience,
             "channel": cb.channel}
            for cb in top_roas
        ],
    })


@router.get("/pending")
def list_pending(
    reviewer_id: Optional[UUID] = Query(None),
    db: Session = Depends(get_db),
):
    """List combos pending approval, optionally filtered by reviewer."""
    q = db.query(AdCombo).filter(
        AdCombo.is_active == True,
        AdCombo.approval_status == "Pending",
    )
    if reviewer_id:
        q = q.filter(AdCombo.reviewer_id == reviewer_id)
    combos = q.order_by(AdCombo.created_at.desc()).all()
    return _envelope([_combo_dict(cb) for cb in combos])


class SubmitApprovalBody(BaseModel):
    reviewer_id: UUID
    submitted_by: str
    approval_deadline: Optional[str] = None


@router.post("/{combo_id}/submit-approval")
def submit_for_approval(combo_id: UUID, body: SubmitApprovalBody, db: Session = Depends(get_db)):
    """Submit a combo for approval — sends email to reviewer."""
    obj = db.query(AdCombo).filter(AdCombo.id == combo_id, AdCombo.is_active == True).first()
    if not obj:
        raise HTTPException(404, "Combo not found")

    reviewer = db.query(User).filter(User.id == body.reviewer_id).first()
    if not reviewer:
        raise HTTPException(404, "Reviewer not found")

    from app.models.branch import Branch
    branch = db.query(Branch).filter(Branch.id == obj.branch_id).first()

    obj.approval_status = "Pending"
    obj.submitted_by = body.submitted_by
    obj.reviewer_id = body.reviewer_id
    if body.approval_deadline:
        from datetime import date as dt
        obj.approval_deadline = dt.fromisoformat(body.approval_deadline)
    obj.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(obj)

    # Look up submitter email for CC
    submitter_user = db.query(User).filter(User.name == body.submitted_by).first()
    submitter_email = submitter_user.email if submitter_user else None

    # Send email in background thread so it doesn't block the response
    mat = obj.material
    import threading
    threading.Thread(
        target=send_approval_email,
        kwargs={
            "reviewer_email": reviewer.email,
            "reviewer_name": reviewer.name,
            "combo_code": obj.combo_code,
            "combo_id": str(obj.id),
            "branch_name": branch.name if branch else "Unknown",
            "material_type": mat.material_type if mat else "Unknown",
            "submitted_by": body.submitted_by,
            "approval_deadline": body.approval_deadline,
            "material_link": mat.file_link if mat else None,
            "kol_name": mat.kol_name if mat else None,
            "submitter_email": submitter_email,
        },
        daemon=True,
    ).start()
    return _envelope(_combo_dict(obj, include_detail=True))


class ReviewBody(BaseModel):
    approval_status: str  # Approved / Rejected / Needs Revision
    feedback: Optional[str] = None


@router.patch("/{combo_id}/review")
def review_combo(combo_id: UUID, body: ReviewBody, db: Session = Depends(get_db)):
    """Approve or reject a combo."""
    obj = db.query(AdCombo).filter(AdCombo.id == combo_id, AdCombo.is_active == True).first()
    if not obj:
        raise HTTPException(404, "Combo not found")
    if body.approval_status not in ("Approved", "Rejected", "Needs Revision"):
        raise HTTPException(422, "Invalid approval_status")

    obj.approval_status = body.approval_status
    obj.approval_feedback = body.feedback
    obj.approved_at = datetime.now(timezone.utc)
    obj.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(obj)
    return _envelope(_combo_dict(obj, include_detail=True))


@router.get("/{combo_id}")
def get_combo(combo_id: UUID, db: Session = Depends(get_db)):
    obj = db.query(AdCombo).filter(AdCombo.id == combo_id, AdCombo.is_active == True).first()
    if not obj:
        raise HTTPException(404, "Combo not found")
    return _envelope(_combo_dict(obj, include_detail=True))


@router.patch("/{combo_id}")
def update_combo(combo_id: UUID, body: ComboUpdate, db: Session = Depends(get_db)):
    obj = db.query(AdCombo).filter(AdCombo.id == combo_id, AdCombo.is_active == True).first()
    if not obj:
        raise HTTPException(404, "Combo not found")

    data = body.model_dump(exclude_unset=True)

    # If verdict is being set manually, lock verdict_source
    if "verdict" in data and data["verdict"] is not None:
        data["verdict_source"] = "manual"

    for k, v in data.items():
        if k in ("date_first_run", "date_last_run") and v:
            from datetime import date as dt
            v = dt.fromisoformat(v)
        setattr(obj, k, v)

    obj.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(obj)
    return _envelope(_combo_dict(obj, include_detail=True))


@router.delete("/{combo_id}")
def delete_combo(combo_id: UUID, db: Session = Depends(get_db)):
    obj = db.query(AdCombo).filter(AdCombo.id == combo_id).first()
    if not obj:
        raise HTTPException(404, "Combo not found")
    obj.is_active = False
    db.commit()
    return _envelope({"deleted": str(combo_id)})


@router.post("/sync")
def manual_sync(db: Session = Depends(get_db)):
    """Manual trigger: sync performance + recompute derived verdicts."""
    synced = sync_combo_performance(db)
    derived = compute_derived_verdicts(db)
    return _envelope({
        "combos_synced": synced,
        "components_updated": derived,
        "synced_at": datetime.now(timezone.utc).isoformat(),
    })


class AutoClassifyRequest(BaseModel):
    branch_id: Optional[UUID] = None
    force: Optional[bool] = False  # re-classify even if angle already set


@router.post("/auto-classify-angles")
def auto_classify_angles(body: AutoClassifyRequest = AutoClassifyRequest(), db: Session = Depends(get_db)):
    """AI reads copy content and auto-assigns creative_angle to combos without one.

    Uses Claude Haiku to detect hook_type, then matches to existing CreativeAngle
    or creates a new one. Sets angle_id on both the combo and its copy.
    """
    from app.config import settings
    from app.services.id_generator import generate_code as gen_code

    if not settings.ANTHROPIC_API_KEY:
        raise HTTPException(400, "ANTHROPIC_API_KEY not configured")

    q = db.query(AdCombo).filter(AdCombo.is_active == True)
    if body.branch_id:
        q = q.filter(AdCombo.branch_id == body.branch_id)
    if not body.force:
        q = q.filter(AdCombo.angle_id.is_(None))

    combos = q.all()
    if not combos:
        return _envelope({"classified": 0, "skipped": 0, "total": 0, "message": "No combos need classification"})

    import logging
    logger = logging.getLogger(__name__)
    logger.info("Auto-classify: processing %d combos", len(combos))

    classified = 0
    skipped = 0
    errors = []

    for combo in combos:
        copy = combo.copy
        if not copy or (not copy.primary_text and not copy.headline):
            skipped += 1
            errors.append(f"{combo.combo_code}: no copy text")
            continue

        text_for_ai = f"{copy.headline or ''}\n{copy.primary_text or ''}"
        logger.info("Classifying %s: %s", combo.combo_code, text_for_ai[:80])

        try:
            result = angle_classifier.classify(text_for_ai, settings.ANTHROPIC_API_KEY)
        except Exception as e:
            skipped += 1
            errors.append(f"{combo.combo_code}: AI error — {str(e)[:100]}")
            logger.error("Classify error for %s: %s", combo.combo_code, e)
            continue

        if not result["hook_type"]:
            skipped += 1
            errors.append(f"{combo.combo_code}: AI returned no hook_type (text length: {len(text_for_ai)})")
            continue

        logger.info("Classified %s → %s", combo.combo_code, result["hook_type"])

        hook = result["hook_type"]
        keypoints = result["keypoints"]

        # Find existing CreativeAngle with same hook_type in same branch
        angle = db.query(CreativeAngle).filter(
            CreativeAngle.branch_id == combo.branch_id,
            CreativeAngle.hook_type == hook,
            CreativeAngle.is_active == True,
        ).first()

        if not angle:
            # Create new CreativeAngle
            angle_code = gen_code(db, "ANG", "creative_angles", "angle_code")
            from app.models.branch import Branch
            branch = db.query(Branch).filter(Branch.id == combo.branch_id).first()
            branch_label = branch.name if branch else "Unknown"

            angle = CreativeAngle(
                angle_code=angle_code,
                branch_id=combo.branch_id,
                name=f"{hook} — {branch_label}",
                hook_type=hook,
                keypoint_1=keypoints[0] if len(keypoints) > 0 else None,
                keypoint_2=keypoints[1] if len(keypoints) > 1 else None,
                keypoint_3=keypoints[2] if len(keypoints) > 2 else None,
                keypoint_4=keypoints[3] if len(keypoints) > 3 else None,
                keypoint_5=keypoints[4] if len(keypoints) > 4 else None,
            )
            db.add(angle)
            db.flush()

        # Assign angle to combo and its copy
        combo.angle_id = angle.id
        if copy and not copy.angle_id:
            copy.angle_id = angle.id
        classified += 1

    db.commit()
    logger.info("Auto-classify done: %d classified, %d skipped out of %d", classified, skipped, len(combos))
    return _envelope({
        "classified": classified,
        "skipped": skipped,
        "total": len(combos),
        "errors": errors[:10],  # first 10 errors for debugging
        "message": f"AI classified {classified} combos, skipped {skipped}",
    })
