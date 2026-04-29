"""Marketing Budget Planner — monthly allocation per branch & channel.

Allocation is stored once per (branch, year, month, channel) in VND. Native
amounts are computed at read time from current FX rates so a TWD branch sees
its plan in NT$ even though the canonical figure stays in VND.

Channels supported today: ``paid_ads``, ``kol``, ``crm``. The schema accepts
arbitrary channel strings, so adding a new channel later is purely a frontend
change — no migration needed.

Actual-spend sources (per channel):
    paid_ads → SUM(ads_performance.cost_*) where grain='daily' and date in month
    kol      → SUM(kol_records.cost_*) where COALESCE(invitation_date,
               published_date, created_at::date) is in month
    crm      → 0 (no spend tracking yet)
"""
from __future__ import annotations

import asyncio
import calendar
import logging
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import Date, func
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.ads import AdsPerformance
from app.models.branch import Branch
from app.models.kol import KOLRecord
from app.models.marketing_budget import MarketingBudget
from app.services.currency import fetch_rate

router = APIRouter()
log = logging.getLogger(__name__)


CHANNELS = ("paid_ads", "kol", "crm")
CHANNEL_LABELS = {"paid_ads": "Paid Ads", "kol": "KOL", "crm": "CRM"}


def _envelope(data):
    return {
        "success": True, "data": data, "error": None,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _month_range(year: int, month: int) -> tuple[date, date]:
    first = date(year, month, 1)
    last = date(year, month, calendar.monthrange(year, month)[1])
    return first, last


def _vnd_to_native(amount_vnd: float, currency: str, rate_vnd_per_unit: float) -> float:
    """Convert VND → branch currency. ``rate_vnd_per_unit`` = VND per 1 unit
    of branch currency (e.g. TWD→VND ~785). VND→VND returns the input."""
    if currency.upper() == "VND" or not rate_vnd_per_unit:
        return amount_vnd
    return round(amount_vnd / rate_vnd_per_unit, 2)


def _get_rate_to_vnd(currency: str) -> float:
    """Sync wrapper around ``fetch_rate`` — returns VND per 1 unit of currency.
    Falls back to 1.0 when rate unavailable so the UI doesn't crash."""
    if currency.upper() == "VND":
        return 1.0
    try:
        rate = asyncio.run(fetch_rate(currency, "VND"))
    except RuntimeError:
        # Already inside an event loop (uvicorn worker) — use blocking call.
        loop = asyncio.new_event_loop()
        try:
            rate = loop.run_until_complete(fetch_rate(currency, "VND"))
        finally:
            loop.close()
    return float(rate) if rate else 1.0


def _resolve_branches(db: Session, branch_id: Optional[UUID]) -> list[Branch]:
    q = db.query(Branch).filter(Branch.is_active.is_(True))
    if branch_id is not None:
        q = q.filter(Branch.id == branch_id)
    return q.order_by(Branch.name).all()


# ── Actuals ──────────────────────────────────────────────────────────────────

def _paid_ads_actual_vnd(db: Session, branch_id: UUID, d_from: date, d_to: date) -> float:
    val = db.query(func.coalesce(func.sum(AdsPerformance.cost_vnd), 0)).filter(
        AdsPerformance.branch_id == branch_id,
        AdsPerformance.grain == "daily",
        AdsPerformance.date_from >= d_from,
        AdsPerformance.date_from <= d_to,
    ).scalar()
    return float(val or 0)


def _kol_actual_vnd(db: Session, branch_id: UUID, d_from: date, d_to: date) -> float:
    """KOL spend attributed by COALESCE(invitation_date, published_date,
    created_at::date) — first non-null is the cost-incurred date."""
    eff_date = func.coalesce(
        KOLRecord.invitation_date,
        KOLRecord.published_date,
        func.cast(KOLRecord.created_at, Date),
    )
    val = db.query(func.coalesce(func.sum(KOLRecord.cost_vnd), 0)).filter(
        KOLRecord.branch_id == branch_id,
        eff_date >= d_from,
        eff_date <= d_to,
    ).scalar()
    return float(val or 0)


def _channel_actual_vnd(db: Session, branch_id: UUID, channel: str,
                        d_from: date, d_to: date) -> float:
    if channel == "paid_ads":
        return _paid_ads_actual_vnd(db, branch_id, d_from, d_to)
    if channel == "kol":
        return _kol_actual_vnd(db, branch_id, d_from, d_to)
    return 0.0


# ── Allocation lookup ────────────────────────────────────────────────────────

def _allocations_for(db: Session, branch_id: UUID, year: int) -> dict:
    """Return {(month, channel): {allocated_vnd, note}} for one branch+year."""
    rows = db.query(MarketingBudget).filter(
        MarketingBudget.branch_id == branch_id,
        MarketingBudget.year == year,
    ).all()
    out: dict = {}
    for r in rows:
        out[(r.month, r.channel)] = {
            "allocated_vnd": float(r.allocated_vnd or 0),
            "note": r.note or "",
        }
    return out


# ── Pydantic ─────────────────────────────────────────────────────────────────

class BudgetUpsertItem(BaseModel):
    branch_id: UUID
    year: int = Field(ge=2000, le=2100)
    month: int = Field(ge=1, le=12)
    channel: str
    allocated_vnd: Decimal = Field(ge=0)
    note: Optional[str] = None


class BulkUpsertBody(BaseModel):
    items: list[BudgetUpsertItem]


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.get("/yearly")
def get_yearly(
    branch_id: UUID = Query(...),
    year: int = Query(...),
    db: Session = Depends(get_db),
):
    """Per-month summary for one branch & year, with per-channel breakdown."""
    branch = db.query(Branch).filter(Branch.id == branch_id).first()
    if not branch:
        raise HTTPException(404, "Branch not found")

    cur = (branch.currency or "VND").upper()
    rate = _get_rate_to_vnd(cur)
    allocs = _allocations_for(db, branch_id, year)

    months_data = []
    year_alloc_vnd = 0.0
    year_actual_vnd = 0.0

    for m in range(1, 13):
        d_from, d_to = _month_range(year, m)

        channels = []
        m_alloc = 0.0
        m_actual = 0.0
        for ch in CHANNELS:
            a = allocs.get((m, ch), {})
            alloc_vnd = float(a.get("allocated_vnd", 0))
            actual_vnd = _channel_actual_vnd(db, branch_id, ch, d_from, d_to)
            m_alloc += alloc_vnd
            m_actual += actual_vnd
            channels.append({
                "channel": ch,
                "label": CHANNEL_LABELS[ch],
                "allocated_vnd": alloc_vnd,
                "allocated_native": _vnd_to_native(alloc_vnd, cur, rate),
                "actual_vnd": actual_vnd,
                "actual_native": _vnd_to_native(actual_vnd, cur, rate),
                "note": a.get("note", ""),
            })

        year_alloc_vnd += m_alloc
        year_actual_vnd += m_actual
        months_data.append({
            "month": m,
            "allocated_vnd": m_alloc,
            "allocated_native": _vnd_to_native(m_alloc, cur, rate),
            "actual_vnd": m_actual,
            "actual_native": _vnd_to_native(m_actual, cur, rate),
            "remaining_vnd": m_alloc - m_actual,
            "remaining_native": _vnd_to_native(m_alloc - m_actual, cur, rate),
            "pct": round((m_actual / m_alloc * 100) if m_alloc > 0 else 0, 1),
            "channels": channels,
        })

    return _envelope({
        "branch_id": str(branch_id),
        "branch_name": branch.name,
        "currency": cur,
        "rate_to_vnd": rate,
        "year": year,
        "total_allocated_vnd": year_alloc_vnd,
        "total_allocated_native": _vnd_to_native(year_alloc_vnd, cur, rate),
        "total_actual_vnd": year_actual_vnd,
        "total_actual_native": _vnd_to_native(year_actual_vnd, cur, rate),
        "total_remaining_vnd": year_alloc_vnd - year_actual_vnd,
        "total_remaining_native": _vnd_to_native(
            year_alloc_vnd - year_actual_vnd, cur, rate),
        "pct": round((year_actual_vnd / year_alloc_vnd * 100)
                     if year_alloc_vnd > 0 else 0, 1),
        "months": months_data,
    })


@router.get("/monthly")
def get_monthly(
    branch_id: UUID = Query(...),
    year: int = Query(...),
    month: int = Query(..., ge=1, le=12),
    db: Session = Depends(get_db),
):
    """Single-month detail for one branch with per-channel allocation vs spend.

    Adds a simple linear-projection that scales current-month-to-date spend
    across all days in the month (same shape as the screenshot's
    'Projected: 42.638' line).
    """
    branch = db.query(Branch).filter(Branch.id == branch_id).first()
    if not branch:
        raise HTTPException(404, "Branch not found")

    cur = (branch.currency or "VND").upper()
    rate = _get_rate_to_vnd(cur)
    d_from, d_to = _month_range(year, month)
    today = date.today()

    # Days elapsed for projection (clamp into month range).
    days_in_month = (d_to - d_from).days + 1
    if today < d_from:
        days_elapsed = 0
        days_remaining = days_in_month
    elif today > d_to:
        days_elapsed = days_in_month
        days_remaining = 0
    else:
        days_elapsed = (today - d_from).days + 1
        days_remaining = days_in_month - days_elapsed

    allocs = _allocations_for(db, branch_id, year)

    channels = []
    total_alloc = 0.0
    total_actual = 0.0

    for ch in CHANNELS:
        a = allocs.get((month, ch), {})
        alloc_vnd = float(a.get("allocated_vnd", 0))
        actual_vnd = _channel_actual_vnd(db, branch_id, ch, d_from, d_to)
        total_alloc += alloc_vnd
        total_actual += actual_vnd
        proj = (actual_vnd / days_elapsed * days_in_month) if days_elapsed else actual_vnd
        channels.append({
            "channel": ch,
            "label": CHANNEL_LABELS[ch],
            "allocated_vnd": alloc_vnd,
            "allocated_native": _vnd_to_native(alloc_vnd, cur, rate),
            "actual_vnd": actual_vnd,
            "actual_native": _vnd_to_native(actual_vnd, cur, rate),
            "projected_vnd": proj,
            "projected_native": _vnd_to_native(proj, cur, rate),
            "pct": round((actual_vnd / alloc_vnd * 100) if alloc_vnd > 0 else 0, 1),
            "status": _status_label(actual_vnd, alloc_vnd, proj),
            "note": a.get("note", ""),
        })

    total_proj = (total_actual / days_elapsed * days_in_month) if days_elapsed else total_actual

    return _envelope({
        "branch_id": str(branch_id),
        "branch_name": branch.name,
        "currency": cur,
        "rate_to_vnd": rate,
        "year": year,
        "month": month,
        "days_in_month": days_in_month,
        "days_elapsed": days_elapsed,
        "days_remaining": days_remaining,
        "total": {
            "allocated_vnd": total_alloc,
            "allocated_native": _vnd_to_native(total_alloc, cur, rate),
            "actual_vnd": total_actual,
            "actual_native": _vnd_to_native(total_actual, cur, rate),
            "projected_vnd": total_proj,
            "projected_native": _vnd_to_native(total_proj, cur, rate),
            "pct": round((total_actual / total_alloc * 100) if total_alloc > 0 else 0, 1),
            "status": _status_label(total_actual, total_alloc, total_proj),
        },
        "channels": channels,
    })


def _status_label(actual: float, allocated: float, projected: float) -> str:
    """3-state status badge — Under / On Track / Over.

    Compares projected end-of-month spend to allocation:
      proj > 110% alloc → Over
      proj < 90%  alloc → Under
      else              → On Track
    """
    if allocated <= 0:
        return "—"
    ratio = projected / allocated
    if ratio > 1.10:
        return "Over"
    if ratio < 0.90:
        return "Under"
    return "On Track"


@router.get("/channel-splits")
def get_channel_splits(
    branch_id: UUID = Query(...),
    year: int = Query(...),
    db: Session = Depends(get_db),
):
    """Per-month total + channel %. Mirror of the 'Channel Splits' screenshot,
    rebased on Paid Ads / KOL / CRM."""
    branch = db.query(Branch).filter(Branch.id == branch_id).first()
    if not branch:
        raise HTTPException(404, "Branch not found")

    cur = (branch.currency or "VND").upper()
    rate = _get_rate_to_vnd(cur)
    allocs = _allocations_for(db, branch_id, year)

    rows = []
    for m in range(1, 13):
        per_channel = {ch: float(allocs.get((m, ch), {}).get("allocated_vnd", 0))
                       for ch in CHANNELS}
        total = sum(per_channel.values())
        pcts = {}
        for ch, v in per_channel.items():
            pcts[ch + "_pct"] = round((v / total * 100), 1) if total > 0 else 0
        rows.append({
            "month": m,
            "total_vnd": total,
            "total_native": _vnd_to_native(total, cur, rate),
            **{ch + "_vnd": v for ch, v in per_channel.items()},
            "paid_ads_native": _vnd_to_native(per_channel["paid_ads"], cur, rate),
            "kol_native": _vnd_to_native(per_channel["kol"], cur, rate),
            "crm_native": _vnd_to_native(per_channel["crm"], cur, rate),
            **pcts,
        })

    return _envelope({
        "branch_id": str(branch_id),
        "branch_name": branch.name,
        "currency": cur,
        "rate_to_vnd": rate,
        "year": year,
        "channels": [{"key": k, "label": v} for k, v in CHANNEL_LABELS.items()],
        "months": rows,
    })


@router.put("/")
def upsert_budget(
    body: BudgetUpsertItem,
    db: Session = Depends(get_db),
):
    """Upsert a single allocation row. Channel must be in the supported set."""
    if body.channel not in CHANNELS:
        raise HTTPException(400, f"channel must be one of {CHANNELS}")
    branch = db.query(Branch).filter(Branch.id == body.branch_id).first()
    if not branch:
        raise HTTPException(404, "Branch not found")

    row = db.query(MarketingBudget).filter(
        MarketingBudget.branch_id == body.branch_id,
        MarketingBudget.year == body.year,
        MarketingBudget.month == body.month,
        MarketingBudget.channel == body.channel,
    ).first()

    if row is None:
        row = MarketingBudget(
            branch_id=body.branch_id,
            year=body.year,
            month=body.month,
            channel=body.channel,
            allocated_vnd=body.allocated_vnd,
            note=body.note,
        )
        db.add(row)
    else:
        row.allocated_vnd = body.allocated_vnd
        row.note = body.note

    db.commit()
    db.refresh(row)
    return _envelope({
        "id": str(row.id),
        "branch_id": str(row.branch_id),
        "year": row.year,
        "month": row.month,
        "channel": row.channel,
        "allocated_vnd": float(row.allocated_vnd or 0),
        "note": row.note or "",
    })


@router.put("/bulk")
def upsert_bulk(
    body: BulkUpsertBody,
    db: Session = Depends(get_db),
):
    """Bulk upsert — used by 'Apply to all months' and the channel-split UIs."""
    out = []
    for it in body.items:
        if it.channel not in CHANNELS:
            raise HTTPException(400, f"channel must be one of {CHANNELS}")
        row = db.query(MarketingBudget).filter(
            MarketingBudget.branch_id == it.branch_id,
            MarketingBudget.year == it.year,
            MarketingBudget.month == it.month,
            MarketingBudget.channel == it.channel,
        ).first()
        if row is None:
            row = MarketingBudget(
                branch_id=it.branch_id, year=it.year, month=it.month,
                channel=it.channel, allocated_vnd=it.allocated_vnd,
                note=it.note,
            )
            db.add(row)
        else:
            row.allocated_vnd = it.allocated_vnd
            if it.note is not None:
                row.note = it.note
        out.append({
            "branch_id": str(it.branch_id), "year": it.year,
            "month": it.month, "channel": it.channel,
            "allocated_vnd": float(it.allocated_vnd),
        })
    db.commit()
    return _envelope({"updated": len(out), "items": out})


@router.get("/setup")
def get_setup(
    year: int = Query(...),
    db: Session = Depends(get_db),
):
    """Top-of-screenshot 'Budget Setup' grid — all branches × 12 months totals."""
    branches = _resolve_branches(db, None)

    # Pull all allocations once — group by (branch, month).
    rows = db.query(MarketingBudget).filter(MarketingBudget.year == year).all()
    by_branch_month: dict = {}
    for r in rows:
        key = (str(r.branch_id), r.month)
        by_branch_month[key] = by_branch_month.get(key, 0.0) + float(r.allocated_vnd or 0)

    grand_total_vnd = 0.0
    branch_rows = []
    for b in branches:
        cur = (b.currency or "VND").upper()
        rate = _get_rate_to_vnd(cur)
        bid = str(b.id)
        months = []
        total = 0.0
        for m in range(1, 13):
            v = by_branch_month.get((bid, m), 0.0)
            total += v
            months.append({
                "month": m,
                "allocated_vnd": v,
                "allocated_native": _vnd_to_native(v, cur, rate),
            })
        grand_total_vnd += total
        branch_rows.append({
            "branch_id": bid,
            "branch_name": b.name,
            "currency": cur,
            "rate_to_vnd": rate,
            "total_vnd": total,
            "total_native": _vnd_to_native(total, cur, rate),
            "months": months,
        })

    return _envelope({
        "year": year,
        "grand_total_vnd": grand_total_vnd,
        "branches": branch_rows,
    })
