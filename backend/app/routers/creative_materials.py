"""Creative Materials router — visual assets + KOL videos component library"""
from datetime import datetime, timezone, date as date_type
from typing import Optional, List
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.creative_material import CreativeMaterial
from app.models.ad_combo import AdCombo
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


def _mat_dict(m: CreativeMaterial) -> dict:
    return {
        "id": str(m.id),
        "material_code": m.material_code,
        "angle_id": str(m.angle_id) if m.angle_id else None,
        "branch_id": str(m.branch_id),
        "material_type": m.material_type,
        "design_type": m.design_type,
        "format_ratio": m.format_ratio,
        "channel": m.channel,
        "target_audience": m.target_audience,
        "language": m.language,
        "file_link": m.file_link,
        "kol_name": m.kol_name,
        "kol_nationality": m.kol_nationality,
        "paid_ads_eligible": m.paid_ads_eligible,
        "paid_ads_channel": m.paid_ads_channel,
        "usage_rights_until": m.usage_rights_until.isoformat() if m.usage_rights_until else None,
        "assigned_to": m.assigned_to,
        "order_status": m.order_status,
        "derived_verdict": m.derived_verdict,
        "combo_count": m.combo_count,
        "tags": m.tags,
        "is_active": m.is_active,
        "created_at": m.created_at.isoformat() if m.created_at else None,
        "updated_at": m.updated_at.isoformat() if m.updated_at else None,
    }


class MaterialIn(BaseModel):
    angle_id: Optional[UUID] = None
    branch_id: UUID
    material_type: str
    design_type: Optional[str] = None
    format_ratio: Optional[str] = None
    channel: Optional[str] = None
    target_audience: List[str]
    language: Optional[str] = None
    file_link: Optional[str] = None
    kol_name: Optional[str] = None
    kol_nationality: Optional[str] = None
    paid_ads_eligible: Optional[bool] = False
    paid_ads_channel: Optional[str] = None
    usage_rights_until: Optional[str] = None
    assigned_to: Optional[str] = None
    order_status: Optional[str] = None
    tags: Optional[List[str]] = None


class MaterialUpdate(BaseModel):
    angle_id: Optional[UUID] = None
    material_type: Optional[str] = None
    design_type: Optional[str] = None
    format_ratio: Optional[str] = None
    channel: Optional[str] = None
    target_audience: Optional[List[str]] = None
    language: Optional[str] = None
    file_link: Optional[str] = None
    kol_name: Optional[str] = None
    kol_nationality: Optional[str] = None
    paid_ads_eligible: Optional[bool] = None
    paid_ads_channel: Optional[str] = None
    usage_rights_until: Optional[str] = None
    assigned_to: Optional[str] = None
    order_status: Optional[str] = None
    tags: Optional[List[str]] = None


@router.get("")
def list_materials(
    branch_id: Optional[UUID] = Query(None),
    material_type: Optional[str] = Query(None),
    target_audience: Optional[str] = Query(None),
    language: Optional[str] = Query(None),
    derived_verdict: Optional[str] = Query(None),
    paid_ads_eligible: Optional[bool] = Query(None),
    db: Session = Depends(get_db),
):
    q = db.query(CreativeMaterial).filter(CreativeMaterial.is_active == True)
    if branch_id:
        q = q.filter(CreativeMaterial.branch_id == branch_id)
    if material_type:
        q = q.filter(CreativeMaterial.material_type == material_type)
    if target_audience:
        q = q.filter(CreativeMaterial.target_audience.any(target_audience))
    if language:
        q = q.filter(CreativeMaterial.language == language)
    if derived_verdict:
        q = q.filter(CreativeMaterial.derived_verdict == derived_verdict)
    if paid_ads_eligible is not None:
        q = q.filter(CreativeMaterial.paid_ads_eligible == paid_ads_eligible)
    last_synced_iso = _last_combo_synced_at(db, branch_id)
    out = []
    for m in q.order_by(CreativeMaterial.created_at.desc()).all():
        d = _mat_dict(m)
        d["data_synced_at"] = last_synced_iso
        out.append(d)
    return _envelope(out)


@router.post("")
def create_material(body: MaterialIn, db: Session = Depends(get_db)):
    material_code = generate_code(db, "MAT", "creative_materials", "material_code")
    data = body.model_dump()
    if data.get("usage_rights_until"):
        data["usage_rights_until"] = date_type.fromisoformat(data["usage_rights_until"])
    obj = CreativeMaterial(material_code=material_code, **data)
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return _envelope(_mat_dict(obj))


@router.get("/{material_id}")
def get_material(material_id: UUID, db: Session = Depends(get_db)):
    obj = db.query(CreativeMaterial).filter(CreativeMaterial.id == material_id, CreativeMaterial.is_active == True).first()
    if not obj:
        raise HTTPException(404, "Material not found")
    result = _mat_dict(obj)
    if obj.angle:
        result["angle_info"] = {
            "angle_code": obj.angle.angle_code,
            "name": obj.angle.name,
            "hook_type": obj.angle.hook_type,
        }
    result["combos"] = [
        {"id": str(cb.id), "combo_code": cb.combo_code, "verdict": cb.verdict,
         "roas": float(cb.roas) if cb.roas else None,
         "copy_code": cb.copy.copy_code if cb.copy else None,
         "headline": cb.copy.headline if cb.copy else None}
        for cb in obj.combos if cb.is_active
    ]
    return _envelope(result)


@router.patch("/{material_id}")
def update_material(material_id: UUID, body: MaterialUpdate, db: Session = Depends(get_db)):
    obj = db.query(CreativeMaterial).filter(CreativeMaterial.id == material_id, CreativeMaterial.is_active == True).first()
    if not obj:
        raise HTTPException(404, "Material not found")
    for k, v in body.model_dump(exclude_unset=True).items():
        if k == "usage_rights_until" and v:
            v = date_type.fromisoformat(v)
        setattr(obj, k, v)
    obj.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(obj)
    return _envelope(_mat_dict(obj))


@router.delete("/{material_id}")
def delete_material(material_id: UUID, db: Session = Depends(get_db)):
    obj = db.query(CreativeMaterial).filter(CreativeMaterial.id == material_id).first()
    if not obj:
        raise HTTPException(404, "Material not found")
    active_combos = db.query(AdCombo).filter(AdCombo.material_id == material_id, AdCombo.is_active == True).count()
    if active_combos > 0:
        raise HTTPException(400, f"Cannot delete: material is used in {active_combos} active combo(s)")
    obj.is_active = False
    db.commit()
    return _envelope({"deleted": str(material_id)})
