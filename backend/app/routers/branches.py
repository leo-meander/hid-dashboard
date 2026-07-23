from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import text

from ..database import get_db
from ..models.branch import Branch

router = APIRouter(prefix="/api/branches", tags=["branches"])


@router.get("")
def list_branches(db: Session = Depends(get_db)):
    rows = db.execute(text("""
        SELECT
            id,
            name,
            city,
            country  AS country_code,
            currency AS native_currency,
            total_rooms,
            total_room_count,
            total_dorm_count,
            timezone,
            is_active,
            cloudbeds_property_id
        FROM branches
        WHERE is_active = true
    """)).mappings().all()

    _ORDER = ("taipei", "1948", "oani", "osaka", "saigon")
    def _branch_rank(r):
        name_lower = r["name"].lower()
        for i, key in enumerate(_ORDER):
            if key in name_lower:
                return i
        return 99

    sorted_rows = sorted(rows, key=_branch_rank)
    return {"success": True, "data": [dict(r) for r in sorted_rows]}


class BranchCapacityUpdate(BaseModel):
    total_rooms: Optional[int] = None
    total_room_count: Optional[int] = None
    total_dorm_count: Optional[int] = None


@router.patch("/{branch_id}/capacity")
def update_branch_capacity(
    branch_id: UUID,
    payload: BranchCapacityUpdate,
    db: Session = Depends(get_db),
):
    """Update sellable room/dorm capacity for a branch."""
    branch = db.query(Branch).filter_by(id=branch_id).first()
    if not branch:
        raise HTTPException(status_code=404, detail="Branch not found")

    if payload.total_rooms is not None:
        branch.total_rooms = payload.total_rooms
    if payload.total_room_count is not None:
        branch.total_room_count = payload.total_room_count
    if payload.total_dorm_count is not None:
        branch.total_dorm_count = payload.total_dorm_count

    db.commit()
    db.refresh(branch)
    return {
        "success": True,
        "data": {
            "id": str(branch.id),
            "name": branch.name,
            "total_rooms": branch.total_rooms,
            "total_room_count": branch.total_room_count,
            "total_dorm_count": branch.total_dorm_count,
        },
    }
