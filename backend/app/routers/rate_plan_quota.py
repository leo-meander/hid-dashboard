"""Rate plan quota CRUD + live status endpoint.

Powers the /rate-plan-quotas dashboard page and the GitHub Actions cron
that fires `evaluate-now` every 30 min. Auth model mirrors alerts.py:
internal HiD users hit CRUD endpoints; cron uses X-Sync-Token on
evaluate-now.
"""
from datetime import date, datetime, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field, validator
from sqlalchemy.orm import Session

from app.database import SessionLocal, get_db
from app.models.rate_plan_quota import RatePlanQuota, RatePlanQuotaStatus
from app.routers.sync import verify_sync_token
from app.services.rate_plan_quota_engine import evaluate_quotas

router = APIRouter()


# ── Schemas ──────────────────────────────────────────────────────────────────

class QuotaIn(BaseModel):
    rate_plan_name: str = Field(..., min_length=1, max_length=200)
    display_name: Optional[str] = Field(None, max_length=200)
    limit_count: int = Field(..., ge=1)
    alert_threshold_pct: Optional[float] = Field(90, ge=0, le=100)
    branch_scope: Optional[str] = Field("all_excl_oani")
    branch_ids: Optional[list[UUID]] = None
    notify_email: Optional[bool] = True
    is_active: Optional[bool] = True

    @validator("branch_scope")
    def _scope_valid(cls, v):
        if v not in {"all_excl_oani", "specific"}:
            raise ValueError("branch_scope must be 'all_excl_oani' or 'specific'")
        return v


class QuotaUpdate(BaseModel):
    rate_plan_name: Optional[str] = Field(None, min_length=1, max_length=200)
    display_name: Optional[str] = Field(None, max_length=200)
    limit_count: Optional[int] = Field(None, ge=1)
    alert_threshold_pct: Optional[float] = Field(None, ge=0, le=100)
    branch_scope: Optional[str] = None
    branch_ids: Optional[list[UUID]] = None
    notify_email: Optional[bool] = None
    is_active: Optional[bool] = None


class QuotaStatusOut(BaseModel):
    active_count: int
    canceled_count: int
    consumed_pct: float
    by_branch: Optional[list] = None
    last_alert_bucket: int
    last_alerted_at: Optional[datetime] = None
    evaluated_at: Optional[datetime] = None


class QuotaOut(BaseModel):
    id: UUID
    rate_plan_name: str
    display_name: Optional[str] = None
    limit_count: int
    alert_threshold_pct: float
    branch_scope: str
    branch_ids: Optional[list] = None
    notify_email: bool
    is_active: bool
    created_at: datetime
    updated_at: datetime
    status: Optional[QuotaStatusOut] = None


def _envelope(data):
    return {
        "success": True,
        "data": data,
        "error": None,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _serialize(q: RatePlanQuota) -> dict:
    s = q.status
    return {
        "id": str(q.id),
        "rate_plan_name": q.rate_plan_name,
        "display_name": q.display_name,
        "limit_count": q.limit_count,
        "alert_threshold_pct": float(q.alert_threshold_pct or 0),
        "branch_scope": q.branch_scope,
        "branch_ids": q.branch_ids,
        "notify_email": q.notify_email,
        "is_active": q.is_active,
        "created_at": q.created_at,
        "updated_at": q.updated_at,
        "status": {
            "active_count": s.active_count,
            "canceled_count": s.canceled_count,
            "consumed_pct": float(s.consumed_pct or 0),
            "by_branch": s.by_branch,
            "last_alert_bucket": s.last_alert_bucket,
            "last_alerted_at": s.last_alerted_at,
            "evaluated_at": s.evaluated_at,
        } if s else None,
    }


# ── CRUD ─────────────────────────────────────────────────────────────────────

@router.get("/")
def list_quotas(db: Session = Depends(get_db)):
    quotas = db.query(RatePlanQuota).order_by(RatePlanQuota.created_at.desc()).all()
    return _envelope([_serialize(q) for q in quotas])


@router.post("/")
def create_quota(payload: QuotaIn, db: Session = Depends(get_db)):
    existing = (
        db.query(RatePlanQuota)
        .filter_by(rate_plan_name=payload.rate_plan_name)
        .first()
    )
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"Quota for '{payload.rate_plan_name}' already exists",
        )
    quota = RatePlanQuota(
        rate_plan_name=payload.rate_plan_name,
        display_name=payload.display_name,
        limit_count=payload.limit_count,
        alert_threshold_pct=payload.alert_threshold_pct,
        branch_scope=payload.branch_scope or "all_excl_oani",
        branch_ids=[str(b) for b in payload.branch_ids] if payload.branch_ids else None,
        notify_email=payload.notify_email if payload.notify_email is not None else True,
        is_active=payload.is_active if payload.is_active is not None else True,
    )
    db.add(quota)
    db.commit()
    db.refresh(quota)
    return _envelope(_serialize(quota))


@router.patch("/{quota_id}")
def update_quota(quota_id: UUID, payload: QuotaUpdate, db: Session = Depends(get_db)):
    quota = db.query(RatePlanQuota).filter_by(id=quota_id).first()
    if not quota:
        raise HTTPException(status_code=404, detail="Quota not found")

    data = payload.dict(exclude_unset=True)
    if "branch_ids" in data and data["branch_ids"] is not None:
        data["branch_ids"] = [str(b) for b in data["branch_ids"]]
    # Allow re-arming alerts: if the user raises the limit so that the cap
    # is no longer breached, reset last_alert_bucket so a future breach
    # fires fresh emails.
    if "limit_count" in data and quota.status:
        new_pct = (
            (quota.status.active_count / data["limit_count"]) * 100
            if data["limit_count"] > 0
            else 0
        )
        threshold = float(data.get("alert_threshold_pct") or quota.alert_threshold_pct)
        if new_pct < threshold:
            quota.status.last_alert_bucket = 0

    for k, v in data.items():
        setattr(quota, k, v)
    db.commit()
    db.refresh(quota)
    return _envelope(_serialize(quota))


@router.delete("/{quota_id}")
def delete_quota(quota_id: UUID, db: Session = Depends(get_db)):
    quota = db.query(RatePlanQuota).filter_by(id=quota_id).first()
    if not quota:
        raise HTTPException(status_code=404, detail="Quota not found")
    db.delete(quota)
    db.commit()
    return _envelope({"deleted": str(quota_id)})


# ── Live evaluate ────────────────────────────────────────────────────────────

@router.post("/evaluate-now", dependencies=[Depends(verify_sync_token)])
def evaluate_now(background_tasks: BackgroundTasks):
    """Trigger Cloudbeds incremental sync + recount + dispatch alerts.

    Returns 202 immediately because the Cloudbeds incremental pull can take
    30-90 seconds across 4 branches. GitHub Actions cron retries with curl
    timeout 300s; in-app callers (UI Refresh button) should use the
    /refresh endpoint below which skips the sync for a faster response.
    """
    background_tasks.add_task(evaluate_quotas, SessionLocal, refresh=True)
    return _envelope({"status": "started", "message": "Evaluation running in background"})


@router.post("/refresh")
def refresh_now():
    """Recount from DB only, no Cloudbeds API call. Synchronous, fast.

    The dashboard's "Refresh" button uses this when the user just wants the
    counts redrawn (e.g. right after editing the limit). For a true live
    pull they should wait for the next cron tick or hit /evaluate-now
    (which requires the sync token).
    """
    result = evaluate_quotas(SessionLocal, refresh=False)
    return _envelope(result)


@router.get("/{quota_id}")
def get_quota(quota_id: UUID, db: Session = Depends(get_db)):
    quota = db.query(RatePlanQuota).filter_by(id=quota_id).first()
    if not quota:
        raise HTTPException(status_code=404, detail="Quota not found")
    return _envelope(_serialize(quota))
