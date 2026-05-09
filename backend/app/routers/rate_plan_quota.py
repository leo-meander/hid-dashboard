"""Rate plan quota CRUD + live status endpoint.

Powers the /rate-plan-quotas dashboard page and the GitHub Actions cron
that fires `evaluate-now` every 30 min. Auth model mirrors alerts.py:
internal HiD users hit CRUD endpoints; cron uses X-Sync-Token on
evaluate-now.
"""
from datetime import datetime, timezone
from typing import Dict, Optional
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

def _validate_branch_limits(v: Dict) -> Dict[str, int]:
    if not v:
        raise ValueError("branch_limits must include at least one branch")
    out: Dict[str, int] = {}
    for k, val in v.items():
        # Accept UUID instances OR string keys (Pydantic JSON keys come in as
        # str regardless of the annotated type).
        try:
            key = str(UUID(str(k)))
        except (ValueError, AttributeError, TypeError):
            raise ValueError(f"branch_limits key '{k}' is not a valid UUID")
        try:
            cap = int(val)
        except (TypeError, ValueError):
            raise ValueError(f"branch_limits[{k}] must be an integer")
        if cap < 1:
            raise ValueError(f"branch_limits[{k}] must be >= 1")
        out[key] = cap
    return out


class QuotaIn(BaseModel):
    rate_plan_name: str = Field(..., min_length=1, max_length=200)
    display_name: Optional[str] = Field(None, max_length=200)
    branch_limits: Dict[str, int] = Field(...)
    alert_threshold_pct: Optional[float] = Field(90, ge=0, le=100)
    notify_email: Optional[bool] = True
    is_active: Optional[bool] = True

    @validator("branch_limits")
    def _check_limits(cls, v):
        return _validate_branch_limits(v)


class QuotaUpdate(BaseModel):
    rate_plan_name: Optional[str] = Field(None, min_length=1, max_length=200)
    display_name: Optional[str] = Field(None, max_length=200)
    branch_limits: Optional[Dict[str, int]] = None
    alert_threshold_pct: Optional[float] = Field(None, ge=0, le=100)
    notify_email: Optional[bool] = None
    is_active: Optional[bool] = None

    @validator("branch_limits")
    def _check_limits(cls, v):
        if v is None:
            return v
        return _validate_branch_limits(v)


def _envelope(data):
    return {
        "success": True,
        "data": data,
        "error": None,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _serialize(q: RatePlanQuota) -> dict:
    s = q.status
    limits = q.branch_limits or {}
    total_cap = sum(int(v or 0) for v in limits.values())
    return {
        "id": str(q.id),
        "rate_plan_name": q.rate_plan_name,
        "display_name": q.display_name,
        "branch_limits": limits,
        "total_cap": total_cap,
        "alert_threshold_pct": float(q.alert_threshold_pct or 0),
        "notify_email": q.notify_email,
        "is_active": q.is_active,
        "created_at": q.created_at,
        "updated_at": q.updated_at,
        "status": {
            "active_count": int(s.active_count or 0),
            "canceled_count": int(s.canceled_count or 0),
            "consumed_pct": float(s.consumed_pct or 0),
            "by_branch": s.by_branch,
            "last_alert_buckets": s.last_alert_buckets or {},
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
        branch_limits=payload.branch_limits,
        alert_threshold_pct=payload.alert_threshold_pct,
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

    # Re-arm alerts: if a branch's cap is raised so its count is no longer
    # breached, reset that branch's bucket so a future breach fires fresh.
    if "branch_limits" in data and data["branch_limits"] is not None and quota.status:
        new_limits = data["branch_limits"]
        threshold = float(
            data.get("alert_threshold_pct") or quota.alert_threshold_pct
        )
        breakdown = quota.status.by_branch or []
        active_by_branch = {
            r["branch_id"]: int(r.get("active") or 0) for r in breakdown
        }
        buckets = dict(quota.status.last_alert_buckets or {})
        # Drop bucket history for branches that were removed entirely.
        for bid in list(buckets.keys()):
            if bid not in new_limits:
                buckets.pop(bid, None)
        # Reset buckets for branches whose new cap puts them back below
        # threshold, so the next eval can re-fire if they climb again.
        for bid, cap in new_limits.items():
            active = active_by_branch.get(bid, 0)
            new_pct = (active / cap * 100) if cap > 0 else 0
            if new_pct < threshold:
                buckets[bid] = 0
        quota.status.last_alert_buckets = buckets

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
