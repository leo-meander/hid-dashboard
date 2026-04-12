"""Alert management endpoints — view, acknowledge, resolve, and configure alerts."""
from datetime import date, datetime, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import case, func, and_
from sqlalchemy.orm import Session

from app.database import get_db, SessionLocal
from app.models.alert import AlertRule, AlertHistory
from app.models.branch import Branch

router = APIRouter()


# ── Schemas ──────────────────────────────────────────────────────────────────

class AlertOut(BaseModel):
    id: UUID
    branch_id: UUID
    branch_name: Optional[str] = None
    metric_key: str
    alert_date: date
    severity: str
    category: str
    current_value: Optional[float] = None
    threshold_value: Optional[float] = None
    baseline_value: Optional[float] = None
    deviation_pct: Optional[float] = None
    message: str
    recommendation: str
    status: str
    email_sent: bool
    created_at: datetime

    class Config:
        from_attributes = True


class AlertSummary(BaseModel):
    critical: int
    warning: int
    info: int
    total: int


class AlertRuleOut(BaseModel):
    id: UUID
    metric_key: str
    display_name: str
    category: str
    severity: str
    threshold_type: str
    threshold_value: float
    comparison_op: str
    lookback_days: int
    branch_id: Optional[UUID] = None
    is_active: bool
    notify_email: bool

    class Config:
        from_attributes = True


class AlertRuleUpdate(BaseModel):
    threshold_value: Optional[float] = None
    severity: Optional[str] = None
    is_active: Optional[bool] = None
    notify_email: Optional[bool] = None
    lookback_days: Optional[int] = None


def _envelope(data):
    return {
        "success": True,
        "data": data,
        "error": None,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _err(msg: str, code: int = 400):
    raise HTTPException(status_code=code, detail={
        "success": False,
        "data": None,
        "error": msg,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.get("/today")
def get_alerts_today(
    branch_id: Optional[UUID] = Query(None),
    severity: Optional[str] = Query(None),
    status: Optional[str] = Query(None, description="active, acknowledged, resolved"),
    db: Session = Depends(get_db),
):
    """Get today's (or most recent) alerts."""
    try:
        # Find most recent alert date
        latest_date = db.query(func.max(AlertHistory.alert_date)).scalar()
        if not latest_date:
            return _envelope([])

        q = db.query(AlertHistory).filter(AlertHistory.alert_date == latest_date)

        if branch_id:
            q = q.filter(AlertHistory.branch_id == branch_id)
        if severity:
            q = q.filter(AlertHistory.severity == severity.upper())
        if status:
            q = q.filter(AlertHistory.status == status)

        severity_order = case(
            (AlertHistory.severity == "CRITICAL", 0),
            (AlertHistory.severity == "WARNING", 1),
            (AlertHistory.severity == "INFO", 2),
            else_=3,
        )
        alerts = q.order_by(severity_order, AlertHistory.branch_id).all()

        # Enrich with branch name
        branch_map = {b.id: b.name for b in db.query(Branch).all()}
        result = []
        for a in alerts:
            out = AlertOut.model_validate(a)
            out.branch_name = branch_map.get(a.branch_id, "Unknown")
            result.append(out.model_dump())

        return _envelope(result)
    except Exception as e:
        _err(str(e), 500)


@router.get("/history")
def get_alerts_history(
    branch_id: Optional[UUID] = Query(None),
    date_from: Optional[date] = Query(None),
    date_to: Optional[date] = Query(None),
    severity: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    limit: int = Query(50, le=200),
    offset: int = Query(0),
    db: Session = Depends(get_db),
):
    """Get historical alerts with filters."""
    try:
        q = db.query(AlertHistory)

        if branch_id:
            q = q.filter(AlertHistory.branch_id == branch_id)
        if date_from:
            q = q.filter(AlertHistory.alert_date >= date_from)
        if date_to:
            q = q.filter(AlertHistory.alert_date <= date_to)
        if severity:
            q = q.filter(AlertHistory.severity == severity.upper())
        if category:
            q = q.filter(AlertHistory.category == category)
        if status:
            q = q.filter(AlertHistory.status == status)

        total = q.count()
        alerts = q.order_by(AlertHistory.alert_date.desc(), AlertHistory.severity).offset(offset).limit(limit).all()

        branch_map = {b.id: b.name for b in db.query(Branch).all()}
        result = []
        for a in alerts:
            out = AlertOut.model_validate(a)
            out.branch_name = branch_map.get(a.branch_id, "Unknown")
            result.append(out.model_dump())

        return _envelope({"items": result, "total": total, "limit": limit, "offset": offset})
    except Exception as e:
        _err(str(e), 500)


@router.get("/summary")
def get_alerts_summary(
    branch_id: Optional[UUID] = Query(None),
    db: Session = Depends(get_db),
):
    """Get alert counts by severity for sidebar badge."""
    try:
        latest_date = db.query(func.max(AlertHistory.alert_date)).scalar()
        if not latest_date:
            return _envelope(AlertSummary(critical=0, warning=0, info=0, total=0).model_dump())

        q = db.query(AlertHistory.severity, func.count(AlertHistory.id)).filter(
            AlertHistory.alert_date == latest_date,
            AlertHistory.status == "active",
        )
        if branch_id:
            q = q.filter(AlertHistory.branch_id == branch_id)

        counts = dict(q.group_by(AlertHistory.severity).all())
        summary = AlertSummary(
            critical=counts.get("CRITICAL", 0),
            warning=counts.get("WARNING", 0),
            info=counts.get("INFO", 0),
            total=sum(counts.values()),
        )
        return _envelope(summary.model_dump())
    except Exception as e:
        _err(str(e), 500)


@router.patch("/{alert_id}/acknowledge")
def acknowledge_alert(alert_id: UUID, db: Session = Depends(get_db)):
    """Mark an alert as acknowledged."""
    try:
        alert = db.query(AlertHistory).filter_by(id=alert_id).first()
        if not alert:
            _err("Alert not found", 404)

        alert.status = "acknowledged"
        alert.acknowledged_at = datetime.now(timezone.utc)
        db.commit()
        return _envelope({"id": str(alert.id), "status": "acknowledged"})
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        _err(str(e), 500)


@router.patch("/{alert_id}/resolve")
def resolve_alert(alert_id: UUID, db: Session = Depends(get_db)):
    """Mark an alert as resolved."""
    try:
        alert = db.query(AlertHistory).filter_by(id=alert_id).first()
        if not alert:
            _err("Alert not found", 404)

        alert.status = "resolved"
        db.commit()
        return _envelope({"id": str(alert.id), "status": "resolved"})
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        _err(str(e), 500)


@router.get("/rules")
def get_alert_rules(db: Session = Depends(get_db)):
    """List all alert rules (admin)."""
    try:
        rules = db.query(AlertRule).order_by(AlertRule.category, AlertRule.severity).all()
        result = [AlertRuleOut.model_validate(r).model_dump() for r in rules]
        return _envelope(result)
    except Exception as e:
        _err(str(e), 500)


@router.put("/rules/{rule_id}")
def update_alert_rule(rule_id: UUID, body: AlertRuleUpdate, db: Session = Depends(get_db)):
    """Update an alert rule's threshold/severity/active status (admin)."""
    try:
        rule = db.query(AlertRule).filter_by(id=rule_id).first()
        if not rule:
            _err("Alert rule not found", 404)

        if body.threshold_value is not None:
            rule.threshold_value = body.threshold_value
        if body.severity is not None:
            rule.severity = body.severity
        if body.is_active is not None:
            rule.is_active = body.is_active
        if body.notify_email is not None:
            rule.notify_email = body.notify_email
        if body.lookback_days is not None:
            rule.lookback_days = body.lookback_days

        db.commit()
        return _envelope(AlertRuleOut.model_validate(rule).model_dump())
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        _err(str(e), 500)


@router.post("/evaluate-now")
def evaluate_now(db: Session = Depends(get_db)):
    """Manually trigger alert evaluation for yesterday (admin)."""
    try:
        from app.services.alert_engine import run_daily_alerts
        run_daily_alerts(SessionLocal)
        return _envelope({"message": "Alert evaluation completed"})
    except Exception as e:
        _err(str(e), 500)
