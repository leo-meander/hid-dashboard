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


# ── Lark debug ────────────────────────────────────────────────────────────────

@router.get("/debug/lark")
def debug_lark():
    """Test Lark connectivity: auth → record fetch → field parse."""
    from app.services.lark_service import _get_token, _fetch_all_records, _get_yearly_agg
    from app.config import settings
    result: dict = {}

    result["config"] = {
        "app_id_set":    bool(settings.LARK_APP_ID),
        "secret_set":    bool(settings.LARK_APP_SECRET),
        "app_token_set": bool(settings.LARK_BASE_APP_TOKEN),
        "table_id_set":  bool(settings.LARK_TASKS_TABLE_ID),
    }

    token = _get_token()
    result["auth"] = "ok" if token else "FAILED — check LARK_APP_ID / LARK_APP_SECRET"
    if not token:
        return {"success": False, "data": result, "error": "auth failed"}

    try:
        records = _fetch_all_records()
        result["records_fetched"] = len(records)
        if records:
            result["sample_fields"] = list(records[0].keys())[:15]
    except Exception as exc:
        result["fetch_error"] = str(exc)
        return {"success": False, "data": result, "error": str(exc)}

    # Show resolved Project names from first 20 records
    from app.services.lark_service import _resolve_project
    project_samples = []
    for rec in records[:20]:
        pv = rec.get("Project")
        resolved = _resolve_project(pv) if pv else ""
        project_samples.append({"raw": repr(pv)[:80], "resolved": resolved})
    result["project_field_samples"] = project_samples

    try:
        import datetime as _dt
        agg = _get_yearly_agg(_dt.datetime.utcnow().year)
        result["branches_found"] = [k for k in agg.keys()]
        result["sample_agg"] = {
            branch: {str(m): counts for m, counts in months.items()}
            for branch, months in agg.items()
        }
    except Exception as exc:
        result["agg_error"] = str(exc)

    # Delivery rate debug: show sample fields for Nora's records
    nora_samples = []
    for rec in records:
        pic = rec.get("PIC") or rec.get("Assignee") or rec.get("pic") or ""
        pic_str = str(pic)
        if "nora" in pic_str.lower():
            nora_samples.append({
                "PIC_raw":    pic_str[:80],
                "Status":     str(rec.get("Status") or "")[:40],
                "Deadline":   str(rec.get("Deadline") or "")[:40],
                "Complete date": str(rec.get("Complete date") or rec.get("Completion date") or "")[:40],
                "OnTime":     str(rec.get("On-time vs Original") or "")[:40],
            })
        if len(nora_samples) >= 5:
            break
    result["nora_sample_records"] = nora_samples
    result["all_field_names"] = list(records[0].keys()) if records else []

    return {"success": True, "data": result, "error": None}


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

    index_name = "uq_team_kpi_org_wide" if branch_uuid is None else "uq_team_kpi_branch"
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
            index_elements=_conflict_cols(branch_uuid),
            index_where=_conflict_where(branch_uuid),
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
            index_elements=_conflict_cols(branch_uuid),
            index_where=_conflict_where(branch_uuid),
            set_={"target_value": body.actual_value},
        )
    )
    db.execute(stmt)
    db.commit()
    return {"success": True, "data": None, "error": None}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _conflict_cols(branch_uuid):
    if branch_uuid is None:
        return ["role_key", "year", "month", "kpi_key"]
    return ["role_key", "branch_id", "year", "month", "kpi_key"]

def _conflict_where(branch_uuid):
    from sqlalchemy import text
    return text("branch_id IS NULL") if branch_uuid is None else text("branch_id IS NOT NULL")


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


