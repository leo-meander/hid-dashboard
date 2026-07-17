"""Team KPI router — /api/team-kpi

Endpoints:
  GET  /summary          → build_monthly_summary for one role × branch × year
  GET  /targets          → list raw target rows
  PUT  /targets/upsert   → create-or-update one target cell
  DELETE /targets/{id}   → clear a target cell
  GET  /roles            → static role metadata
"""
from __future__ import annotations

import logging
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.team_kpi import TeamKPITarget
from app.services.team_kpi_service import (
    KPI_DEFS,
    ROLE_META,
    build_monthly_summary,
)

log = logging.getLogger(__name__)
router = APIRouter()

VALID_ROLES = set(ROLE_META.keys())


# ── Roles metadata ────────────────────────────────────────────────────────────

@router.get("/roles")
def get_roles():
    roles = []
    for role_key, meta in ROLE_META.items():
        roles.append({
            "key": role_key,
            "label": meta["label"],
            "person": meta["person"],
            "emoji": meta["emoji"],
            "auto_actuals": meta["auto_actuals"],
            "kpi_defs": KPI_DEFS.get(role_key, []),
        })
    return {"success": True, "data": roles, "error": None}


# ── Summary ───────────────────────────────────────────────────────────────────

@router.get("/summary")
def get_summary(
    role: str = Query(...),
    year: int = Query(2026),
    branch_id: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    if role not in VALID_ROLES:
        raise HTTPException(400, f"role must be one of: {', '.join(VALID_ROLES)}")
    try:
        data = build_monthly_summary(db, role, year, branch_id)
    except Exception as exc:
        log.exception("team_kpi summary error role=%s year=%s", role, year)
        raise HTTPException(500, str(exc))
    return {"success": True, "data": data, "error": None}


# ── Targets CRUD ──────────────────────────────────────────────────────────────

@router.get("/targets")
def list_targets(
    role: Optional[str] = Query(None),
    year: Optional[int] = Query(None),
    branch_id: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    q = db.query(TeamKPITarget)
    if role:
        q = q.filter(TeamKPITarget.role_key == role)
    if year:
        q = q.filter(TeamKPITarget.year == year)
    if branch_id:
        q = q.filter(TeamKPITarget.branch_id == branch_id)
    rows = q.order_by(TeamKPITarget.month, TeamKPITarget.kpi_key).all()
    return {
        "success": True,
        "data": [_row_out(r) for r in rows],
        "error": None,
    }


class UpsertTargetBody(BaseModel):
    role_key: str
    branch_id: Optional[str] = None
    year: int
    month: int
    kpi_key: str
    target_value: Optional[float]


@router.put("/targets/upsert")
def upsert_target(body: UpsertTargetBody, db: Session = Depends(get_db)):
    if body.role_key not in VALID_ROLES:
        raise HTTPException(400, f"Invalid role_key: {body.role_key}")
    if not 1 <= body.month <= 12:
        raise HTTPException(400, "month must be 1–12")

    branch_uuid = UUID(body.branch_id) if body.branch_id else None

    stmt = (
        pg_insert(TeamKPITarget)
        .values(
            role_key=body.role_key,
            branch_id=branch_uuid,
            year=body.year,
            month=body.month,
            kpi_key=body.kpi_key,
            target_value=body.target_value,
        )
        .on_conflict_do_update(
            constraint="uq_team_kpi_targets",
            set_={"target_value": body.target_value},
        )
    )
    db.execute(stmt)
    db.commit()
    return {"success": True, "data": body.model_dump(), "error": None}


@router.delete("/targets/{target_id}")
def delete_target(target_id: UUID, db: Session = Depends(get_db)):
    row = db.query(TeamKPITarget).filter(TeamKPITarget.id == target_id).first()
    if not row:
        raise HTTPException(404, "Target not found")
    db.delete(row)
    db.commit()
    return {"success": True, "data": None, "error": None}


# ── Manual actual upsert (for Designer / CRM / PM) ───────────────────────────

class UpsertActualBody(BaseModel):
    role_key: str
    branch_id: Optional[str] = None
    year: int
    month: int
    kpi_key: str
    actual_value: Optional[float]


@router.put("/actuals/upsert")
def upsert_actual(body: UpsertActualBody, db: Session = Depends(get_db)):
    """Store a manually-entered actual. Uses a dedicated column on the same row
    as the target so we don't need a separate table for Phase 1."""
    if body.role_key not in VALID_ROLES:
        raise HTTPException(400, f"Invalid role_key: {body.role_key}")
    # For now we store manual actuals in a separate key: "{kpi_key}__actual"
    # This keeps the schema simple without adding an actual_value column.
    actual_kpi_key = f"{body.kpi_key}__actual"
    branch_uuid = UUID(body.branch_id) if body.branch_id else None

    stmt = (
        pg_insert(TeamKPITarget)
        .values(
            role_key=body.role_key,
            branch_id=branch_uuid,
            year=body.year,
            month=body.month,
            kpi_key=actual_kpi_key,
            target_value=body.actual_value,
        )
        .on_conflict_do_update(
            constraint="uq_team_kpi_targets",
            set_={"target_value": body.actual_value},
        )
    )
    db.execute(stmt)
    db.commit()
    return {"success": True, "data": None, "error": None}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _row_out(row: TeamKPITarget) -> dict:
    return {
        "id": str(row.id),
        "role_key": row.role_key,
        "branch_id": str(row.branch_id) if row.branch_id else None,
        "year": row.year,
        "month": row.month,
        "kpi_key": row.kpi_key,
        "target_value": float(row.target_value) if row.target_value is not None else None,
    }


