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

@router.get("/debug/ads-tasks-jul")
def debug_ads_tasks_jul():
    """Show raw PIC field for completed Ads tasks in Jun/Jul 2026."""
    from app.services.lark_service import _fetch_all_records, _parse_month_year, _resolve_project
    records = _fetch_all_records()
    samples = []
    for rec in records:
        project = _resolve_project(rec.get("Project"))
        if "ads" not in project.lower():
            continue
        status = str(rec.get("Status") or "").lower().strip()
        if status != "completed":
            continue
        ym = _parse_month_year(rec.get("Date Created"))
        if not ym or ym[0] != 2026 or ym[1] not in (6, 7):
            continue
        task_raw = rec.get("Task")
        task_text = ""
        if isinstance(task_raw, list) and task_raw:
            task_text = task_raw[0].get("text", "")[:40] if isinstance(task_raw[0], dict) else str(task_raw[0])[:40]
        pic_raw = rec.get("PIC")
        samples.append({
            "task": task_text,
            "project": project,
            "pic_raw": repr(pic_raw)[:120],
            "pic_type": type(pic_raw).__name__,
        })
        if len(samples) >= 15:
            break
    return {"success": True, "data": {"count": len(samples), "samples": samples}, "error": None}


@router.get("/debug/kol-revenue-by-branch")
def debug_kol_revenue_by_branch(year: int = Query(2026)):
    """Show KOL revenue (raw VND) per branch per month: cached (KOL Engine) vs Cloudbeds."""
    import calendar
    from datetime import date
    from app.services.team_kpi_service import get_kol_actuals_yearly_db
    from app.routers.marketing_activity import _fetch_kol_totals_cloudbeds, _month_range
    from app.models.branch import Branch
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        data = get_kol_actuals_yearly_db(db, year)
        branches = db.query(Branch).filter(Branch.is_active.is_(True)).all()
        BRANCH_KEYS = {"saigon", "taipei", "1948", "oani", "osaka"}

        def branch_key(name):
            nl = (name or "").lower()
            for k in BRANCH_KEYS:
                if k in nl:
                    return k
            return None

        today = date.today()
        cur_month = today.month if today.year == year else (12 if today.year > year else 0)

        cloudbeds = {}
        for m in range(1, cur_month + 1):
            d_from, d_to = _month_range(f"{year}-{m:02d}")
            cloudbeds[str(m)] = {}
            for b in branches:
                bk = branch_key(b.name)
                if not bk:
                    continue
                _, rev = _fetch_kol_totals_cloudbeds(db, b.id, d_from, d_to, use_native=False)
                cloudbeds[str(m)][bk] = round(rev, 0)
    finally:
        db.close()

    cached = {}
    for m, bmap in sorted(data.items()):
        cached[str(m)] = {bk: round(v.get("kol_revenue", 0), 0) for bk, v in bmap.items() if bk != "all"}

    return {"success": True, "data": {"kol_engine_cache": cached, "cloudbeds": cloudbeds}, "error": None}


@router.get("/debug/kol-revenue-raw")
def debug_kol_revenue_raw(year: int = Query(2026), month: int = Query(7)):
    """Show RAW KOL Engine revenue API response for one month including excluded bookings."""
    from app.services.kol_engine import fetch_kol_revenue
    from app.config import settings
    import time
    # Bust the in-process cache so we always get a fresh response
    from app.services import kol_engine as _ke
    _ke._kol_revenue_cache.clear()
    data = fetch_kol_revenue(
        base_url=settings.KOL_ENGINE_URL,
        org_slug=settings.KOL_TARGETS_ORG_SLUG,
        api_key=settings.KOL_REVENUE_API_SECRET,
        year=year,
        month=month,
    )
    if data is None:
        return {"success": False, "data": None, "error": "fetch returned None — check config/creds"}
    return {"success": True, "data": data, "error": None}


@router.get("/debug/kol-targets")
def debug_kol_targets(year: int = Query(2026), month: int = Query(7)):
    """Expose raw KOL Engine targets API response for one month."""
    from app.services.kol_engine import fetch_kol_targets
    from app.config import settings
    data = fetch_kol_targets(
        base_url=settings.KOL_ENGINE_URL,
        org_slug=settings.KOL_TARGETS_ORG_SLUG,
        api_key=settings.KOL_PUBLIC_API_KEY,
        year=year,
        month=month,
    )
    if data is None:
        return {"success": False, "data": None, "error": "fetch returned None — check config/creds"}
    branches = data.get("branches") or []
    sample = branches[0] if branches else {}
    return {"success": True, "data": {"branch_keys": list(sample.keys()), "first_branch": sample, "branch_count": len(branches)}, "error": None}


@router.get("/debug/nora-tasks")
def debug_nora_tasks():
    """Show ALL Nora completed tasks with parsed deadline month."""
    from app.services.lark_service import _fetch_all_records, _NORA_KEYS, _extract_pic_key, _parse_month_year
    import datetime as _dt
    records = _fetch_all_records()
    all_nora = []
    for rec in records:
        if _extract_pic_key(rec.get("PIC")) not in _NORA_KEYS:
            continue
        status = str(rec.get("Status") or "").lower().strip()
        deadline_raw = rec.get("Deadline")
        created_raw = rec.get("Date Created")
        ym_deadline = _parse_month_year(deadline_raw)
        ym_created = _parse_month_year(created_raw)
        task_raw = rec.get("Task")
        task_text = ""
        if isinstance(task_raw, list) and task_raw:
            task_text = task_raw[0].get("text", "")[:50] if isinstance(task_raw[0], dict) else str(task_raw[0])[:50]
        all_nora.append({
            "task": task_text,
            "status": status,
            "deadline_parsed": f"{ym_deadline[0]}-{ym_deadline[1]:02d}" if ym_deadline else None,
            "created_parsed": f"{ym_created[0]}-{ym_created[1]:02d}" if ym_created else None,
            "deadline_raw": repr(deadline_raw)[:60],
        })
    completed = [r for r in all_nora if r["status"] == "completed"]
    return {"success": True, "data": {
        "total_nora_tasks": len(all_nora),
        "completed_count": len(completed),
        "completed": completed,
    }, "error": None}


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

    # Collect all unique PIC record IDs with task samples to identify each person
    from collections import defaultdict
    pic_id_tasks: dict = defaultdict(list)
    for rec in records:
        pic_raw = rec.get("PIC") or {}
        ids = pic_raw.get("link_record_ids", []) if isinstance(pic_raw, dict) else []
        task_text = ""
        task_val = rec.get("Task")
        if isinstance(task_val, list) and task_val:
            task_text = task_val[0].get("text", "")[:50] if isinstance(task_val[0], dict) else str(task_val[0])[:50]
        for uid in ids:
            pic_id_tasks[uid].append(task_text)
    result["pic_ids"] = {
        uid: {"count": len(tasks), "samples": tasks[:3]}
        for uid, tasks in pic_id_tasks.items()
    }
    result["all_field_names"] = list(records[0].keys()) if records else []

    # Sample raw "On-time vs Original" values to debug on-time detection
    ot_samples = []
    for rec in records[:50]:
        v = rec.get("On-time vs Original")
        if v is not None:
            ot_samples.append(repr(v)[:60])
    result["on_time_field_samples"] = list(dict.fromkeys(ot_samples))[:10]

    return {"success": True, "data": result, "error": None}


# ── Roles metadata ────────────────────────────────────────────────────────────

@router.get("/task-overview")
def get_task_overview(year: int = Query(2026)):
    """Return per-PIC per-month task stats for the Task Overview tab."""
    from app.services.lark_service import get_task_overview_yearly, PIC_NAME_MAP
    try:
        data = get_task_overview_yearly(year)
    except Exception as exc:
        log.exception("task_overview error year=%s", year)
        raise HTTPException(500, str(exc))

    # Merge entries with the same display name (e.g. Nora has 2 record IDs)
    merged: dict[str, dict] = {}
    for pic_id, pic_data in data.items():
        open_workload = pic_data.get("open_workload", 0)
        no_deadline_count = pic_data.get("no_deadline_count", 0)
        name = PIC_NAME_MAP.get(pic_id, f"User {pic_id[-4:]}")
        if name not in merged:
            merged[name] = {"pic_id": pic_id, "name": name, "months": {}, "open_workload": 0, "no_deadline_count": 0}
        m = merged[name]
        m["open_workload"] += open_workload
        m["no_deadline_count"] += no_deadline_count
        for month_key, stats in pic_data.items():
            if not isinstance(month_key, int) and not (isinstance(month_key, str) and month_key.isdigit()):
                continue
            if month_key not in m["months"]:
                m["months"][month_key] = {k: 0 for k in stats}
            for k, v in stats.items():
                if v is None:
                    continue
                prev = m["months"][month_key].get(k)
                m["months"][month_key][k] = (prev or 0) + v

    result = [v for v in merged.values() if not v["name"].startswith("User ")]
    return {"success": True, "data": result, "error": None}


@router.get("/task-detail")
def get_task_detail_endpoint(
    pic_name: str = Query(...),
    year: int = Query(2026),
    month: int = Query(...),
    category: str = Query("total"),
):
    """Return individual task names for a person/month/category (drilldown)."""
    from app.services.lark_service import get_task_detail
    valid_categories = {"total", "done", "on_time", "late", "overdue"}
    if category not in valid_categories:
        raise HTTPException(400, f"category must be one of: {', '.join(sorted(valid_categories))}")
    if not 1 <= month <= 12:
        raise HTTPException(400, "month must be 1–12")
    try:
        tasks = get_task_detail(pic_name, year, month, category)
    except Exception as exc:
        log.exception("task_detail error pic=%s year=%s month=%s cat=%s", pic_name, year, month, category)
        raise HTTPException(500, str(exc))
    return {"success": True, "data": tasks, "error": None}


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
    _upsert_row(db, body.role_key, branch_uuid, body.year, body.month, body.kpi_key, body.target_value)
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
    actual_kpi_key = f"{body.kpi_key}__actual"
    branch_uuid = UUID(body.branch_id) if body.branch_id else None
    _upsert_row(db, body.role_key, branch_uuid, body.year, body.month, actual_kpi_key, body.actual_value)
    return {"success": True, "data": None, "error": None}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _upsert_row(db: Session, role_key, branch_uuid, year, month, kpi_key, value):
    """SELECT + UPDATE or INSERT — avoids partial-index ON CONFLICT dependency."""
    row = (
        db.query(TeamKPITarget)
        .filter(
            TeamKPITarget.role_key == role_key,
            TeamKPITarget.branch_id == branch_uuid,
            TeamKPITarget.year == year,
            TeamKPITarget.month == month,
            TeamKPITarget.kpi_key == kpi_key,
        )
        .first()
    )
    if row:
        row.target_value = value
    else:
        db.add(TeamKPITarget(
            role_key=role_key,
            branch_id=branch_uuid,
            year=year,
            month=month,
            kpi_key=kpi_key,
            target_value=value,
        ))
    db.commit()


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


