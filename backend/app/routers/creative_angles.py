"""Creative Angles router — strategic frameworks for the Creative Library"""
from datetime import datetime, timezone
from typing import Optional, List
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.ad_combo import AdCombo
from app.models.creative_angle import CreativeAngle
from app.services.id_generator import generate_code

router = APIRouter()


def _envelope(data):
    return {"success": True, "data": data, "error": None,
            "timestamp": datetime.now(timezone.utc).isoformat()}


def _last_combo_synced_at(db: Session, branch_id: Optional[UUID]) -> Optional[str]:
    q = db.query(func.max(AdCombo.last_synced_at))
    if branch_id:
        q = q.filter(AdCombo.branch_id == branch_id)
    ts = q.scalar()
    return ts.isoformat() if ts else None


def _angle_dict(a: CreativeAngle) -> dict:
    return {
        "id": str(a.id),
        "angle_code": a.angle_code,
        "branch_id": str(a.branch_id) if a.branch_id else None,
        "name": a.name,
        "hook_type": a.hook_type,
        "keypoint_1": a.keypoint_1,
        "keypoint_2": a.keypoint_2,
        "keypoint_3": a.keypoint_3,
        "keypoint_4": a.keypoint_4,
        "keypoint_5": a.keypoint_5,
        "target_audience": a.target_audience,
        "notes": a.notes,
        "is_active": a.is_active,
        "created_at": a.created_at.isoformat() if a.created_at else None,
        "updated_at": a.updated_at.isoformat() if a.updated_at else None,
    }


class AngleIn(BaseModel):
    branch_id: Optional[UUID] = None
    name: str
    hook_type: str
    keypoint_1: str
    keypoint_2: Optional[str] = None
    keypoint_3: Optional[str] = None
    keypoint_4: Optional[str] = None
    keypoint_5: Optional[str] = None
    target_audience: Optional[List[str]] = None
    notes: Optional[str] = None


class AngleUpdate(BaseModel):
    name: Optional[str] = None
    hook_type: Optional[str] = None
    keypoint_1: Optional[str] = None
    keypoint_2: Optional[str] = None
    keypoint_3: Optional[str] = None
    keypoint_4: Optional[str] = None
    keypoint_5: Optional[str] = None
    target_audience: Optional[List[str]] = None
    notes: Optional[str] = None


@router.get("")
def list_angles(
    branch_id: Optional[UUID] = Query(None),
    hook_type: Optional[str] = Query(None),
    target_audience: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    q = db.query(CreativeAngle).filter(CreativeAngle.is_active == True)
    if branch_id:
        q = q.filter(CreativeAngle.branch_id == branch_id)
    if hook_type:
        q = q.filter(CreativeAngle.hook_type == hook_type)
    if target_audience:
        q = q.filter(CreativeAngle.target_audience.any(target_audience))
    last_synced_iso = _last_combo_synced_at(db, branch_id)
    out = []
    for a in q.order_by(CreativeAngle.created_at.desc()).all():
        d = _angle_dict(a)
        d["data_synced_at"] = last_synced_iso
        out.append(d)
    return _envelope(out)


@router.post("")
def create_angle(body: AngleIn, db: Session = Depends(get_db)):
    angle_code = generate_code(db, "ANG", "creative_angles", "angle_code")
    obj = CreativeAngle(angle_code=angle_code, **body.model_dump())
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return _envelope(_angle_dict(obj))


@router.get("/{angle_id}")
def get_angle(angle_id: UUID, db: Session = Depends(get_db)):
    obj = db.query(CreativeAngle).filter(CreativeAngle.id == angle_id, CreativeAngle.is_active == True).first()
    if not obj:
        raise HTTPException(404, "Angle not found")
    result = _angle_dict(obj)
    result["copies"] = [
        {"id": str(c.id), "copy_code": c.copy_code, "headline": c.headline, "derived_verdict": c.derived_verdict}
        for c in obj.copies if c.is_active
    ]
    result["combos"] = [
        {"id": str(cb.id), "combo_code": cb.combo_code, "verdict": cb.verdict,
         "roas": float(cb.roas) if cb.roas else None}
        for cb in obj.combos if cb.is_active
    ]
    return _envelope(result)


@router.patch("/{angle_id}")
def update_angle(angle_id: UUID, body: AngleUpdate, db: Session = Depends(get_db)):
    obj = db.query(CreativeAngle).filter(CreativeAngle.id == angle_id, CreativeAngle.is_active == True).first()
    if not obj:
        raise HTTPException(404, "Angle not found")
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(obj, k, v)
    obj.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(obj)
    return _envelope(_angle_dict(obj))


@router.delete("/{angle_id}")
def delete_angle(angle_id: UUID, db: Session = Depends(get_db)):
    obj = db.query(CreativeAngle).filter(CreativeAngle.id == angle_id).first()
    if not obj:
        raise HTTPException(404, "Angle not found")
    obj.is_active = False
    db.commit()
    return _envelope({"deleted": str(angle_id)})
