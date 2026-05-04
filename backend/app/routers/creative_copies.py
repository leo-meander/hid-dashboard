"""Creative Copies router — copy component library with derived verdict"""
from datetime import datetime, timezone
from typing import Optional, List
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.creative_copy import CreativeCopy
from app.models.ad_combo import AdCombo
from app.services.id_generator import generate_code

router = APIRouter()


def _envelope(data):
    return {"success": True, "data": data, "error": None,
            "timestamp": datetime.now(timezone.utc).isoformat()}


def _last_combo_synced_at(db: Session, branch_id: Optional[UUID]) -> Optional[str]:
    """Latest verdict_sync timestamp from ad_combos — represents creative-library
    data freshness for derived_verdict on copies/materials/angles."""
    q = db.query(func.max(AdCombo.last_synced_at))
    if branch_id:
        q = q.filter(AdCombo.branch_id == branch_id)
    ts = q.scalar()
    return ts.isoformat() if ts else None


def _copy_dict(c: CreativeCopy) -> dict:
    return {
        "id": str(c.id),
        "copy_code": c.copy_code,
        "angle_id": str(c.angle_id) if c.angle_id else None,
        "branch_id": str(c.branch_id),
        "channel": c.channel,
        "ad_format": c.ad_format,
        "target_audience": c.target_audience,
        "country_target": c.country_target,
        "language": c.language,
        "headline": c.headline,
        "primary_text": c.primary_text,
        "landing_page_url": c.landing_page_url,
        "derived_verdict": c.derived_verdict,
        "combo_count": c.combo_count,
        "tags": c.tags,
        "is_active": c.is_active,
        "created_at": c.created_at.isoformat() if c.created_at else None,
        "updated_at": c.updated_at.isoformat() if c.updated_at else None,
    }


class CopyIn(BaseModel):
    angle_id: Optional[UUID] = None
    branch_id: UUID
    channel: str
    ad_format: Optional[str] = None
    target_audience: List[str]
    country_target: Optional[str] = None
    language: str
    headline: Optional[str] = None
    primary_text: str
    landing_page_url: Optional[str] = None
    tags: Optional[List[str]] = None


class CopyUpdate(BaseModel):
    angle_id: Optional[UUID] = None
    channel: Optional[str] = None
    ad_format: Optional[str] = None
    target_audience: Optional[List[str]] = None
    country_target: Optional[str] = None
    language: Optional[str] = None
    headline: Optional[str] = None
    primary_text: Optional[str] = None
    landing_page_url: Optional[str] = None
    tags: Optional[List[str]] = None


@router.get("")
def list_copies(
    branch_id: Optional[UUID] = Query(None),
    angle_id: Optional[UUID] = Query(None),
    channel: Optional[str] = Query(None),
    language: Optional[str] = Query(None),
    target_audience: Optional[str] = Query(None),
    derived_verdict: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    q = db.query(CreativeCopy).filter(CreativeCopy.is_active == True)
    if branch_id:
        q = q.filter(CreativeCopy.branch_id == branch_id)
    if angle_id:
        q = q.filter(CreativeCopy.angle_id == angle_id)
    if channel:
        q = q.filter(CreativeCopy.channel == channel)
    if language:
        q = q.filter(CreativeCopy.language == language)
    if target_audience:
        q = q.filter(CreativeCopy.target_audience.any(target_audience))
    if derived_verdict:
        q = q.filter(CreativeCopy.derived_verdict == derived_verdict)
    last_synced_iso = _last_combo_synced_at(db, branch_id)
    out = []
    for c in q.order_by(CreativeCopy.created_at.desc()).all():
        d = _copy_dict(c)
        d["data_synced_at"] = last_synced_iso
        out.append(d)
    return _envelope(out)


@router.post("")
def create_copy(body: CopyIn, db: Session = Depends(get_db)):
    copy_code = generate_code(db, "CPY", "creative_copies", "copy_code")
    obj = CreativeCopy(copy_code=copy_code, **body.model_dump())
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return _envelope(_copy_dict(obj))


@router.get("/{copy_id}")
def get_copy(copy_id: UUID, db: Session = Depends(get_db)):
    obj = db.query(CreativeCopy).filter(CreativeCopy.id == copy_id, CreativeCopy.is_active == True).first()
    if not obj:
        raise HTTPException(404, "Copy not found")
    result = _copy_dict(obj)
    if obj.angle:
        result["angle_info"] = {
            "angle_code": obj.angle.angle_code,
            "name": obj.angle.name,
            "hook_type": obj.angle.hook_type,
        }
    # List combos using this copy with their verdicts
    result["combos"] = [
        {"id": str(cb.id), "combo_code": cb.combo_code, "verdict": cb.verdict,
         "roas": float(cb.roas) if cb.roas else None,
         "material_code": cb.material.material_code if cb.material else None,
         "material_type": cb.material.material_type if cb.material else None}
        for cb in obj.combos if cb.is_active
    ]
    return _envelope(result)


@router.patch("/{copy_id}")
def update_copy(copy_id: UUID, body: CopyUpdate, db: Session = Depends(get_db)):
    obj = db.query(CreativeCopy).filter(CreativeCopy.id == copy_id, CreativeCopy.is_active == True).first()
    if not obj:
        raise HTTPException(404, "Copy not found")
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(obj, k, v)
    obj.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(obj)
    return _envelope(_copy_dict(obj))


@router.delete("/{copy_id}")
def delete_copy(copy_id: UUID, db: Session = Depends(get_db)):
    obj = db.query(CreativeCopy).filter(CreativeCopy.id == copy_id).first()
    if not obj:
        raise HTTPException(404, "Copy not found")
    active_combos = db.query(AdCombo).filter(AdCombo.copy_id == copy_id, AdCombo.is_active == True).count()
    if active_combos > 0:
        raise HTTPException(400, f"Cannot delete: copy is used in {active_combos} active combo(s)")
    obj.is_active = False
    db.commit()
    return _envelope({"deleted": str(copy_id)})
