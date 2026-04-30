"""Marketing Budget Planner — monthly allocation per branch & channel.

Allocation is stored once per (branch, year, month, channel) in VND. Native
amounts are computed at read time from current FX rates so a TWD branch sees
its plan in NT$ even though the canonical figure stays in VND.

Channels supported today: ``paid_ads``, ``kol``, ``crm``. The schema accepts
arbitrary channel strings, so adding a new channel later is purely a frontend
change — no migration needed.

Actual-spend sources (per channel):
    paid_ads → Ads Platform /api/export/budget/yearly-plan (per-budget-plan
               actual matching their UI; not every-ad daily aggregate)
    kol      → KOL Engine  /api/sync/budgets (per-hotel monthly_breakdown.actual,
               attributed by published_date and converted to VND upstream)
    crm      → manual_actual_vnd column on marketing_budgets (no upstream
               source; user enters monthly cost via Budget Planner UI)
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
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.branch import Branch
from app.models.marketing_budget import MarketingBudget
from app.models.yearly_plan import YearlyPlan
from app.services.currency import fetch_rate
from app.services.ads_platform import branch_slug_for
from app.services.upstream_actuals import (
    fetch_kol_yearly,
    fetch_paid_ads_yearly,
    kol_hotel_id_for,
)

router = APIRouter()
log = logging.getLogger(__name__)


CHANNELS = ("paid_ads", "kol", "crm")
CHANNEL_LABELS = {"paid_ads": "Paid Ads", "kol": "KOL", "crm": "CRM"}

# Hardcoded FX fallbacks — keep in sync with ghl_email_sync and
# ads_platform_sync.FX_FALLBACK_TO_VND.
FX_FALLBACK_TO_VND = {"VND": 1.0, "TWD": 830.0, "JPY": 165.0}


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

    Falls back to ``FX_FALLBACK_TO_VND`` when the live API is unavailable, so
    TWD/JPY branches show real native amounts instead of 1:1 with VND.
    """
    cur = currency.upper()
    if cur == "VND":
        return 1.0
    try:
        rate = asyncio.run(fetch_rate(cur, "VND"))
    except RuntimeError:
        # Already inside an event loop (uvicorn worker) — use blocking call.
        loop = asyncio.new_event_loop()
        try:
            rate = loop.run_until_complete(fetch_rate(cur, "VND"))
        finally:
            loop.close()
    if rate:
        return float(rate)
    return FX_FALLBACK_TO_VND.get(cur, 1.0)


def _resolve_branches(db: Session, branch_id: Optional[UUID]) -> list[Branch]:
    q = db.query(Branch).filter(Branch.is_active.is_(True))
    if branch_id is not None:
        q = q.filter(Branch.id == branch_id)
    return q.order_by(Branch.name).all()


# ── Actuals ──────────────────────────────────────────────────────────────────
#
# All actuals come from the upstream platforms — Ads Platform for paid_ads,
# KOL Engine for kol — fetched once per (branch, year) and cached in an
# ``ActualsCache`` instance so a single yearly/monthly response makes at most
# two upstream HTTP calls per branch instead of one per (channel × month).


class ActualsCache:
    """Per-request cache of upstream actuals + manual CRM values.

    Build once at the top of an endpoint, then call ``get(branch, year, month,
    channel)`` for each cell. Lazy-loads on first miss.
    """

    def __init__(self, db: Session):
        self.db = db
        # (branch_id, year) -> {month -> vnd}
        self._paid_ads: dict[tuple[str, int], dict[int, float]] = {}
        self._kol: dict[tuple[str, int], dict[int, float]] = {}
        self._manual: dict[tuple[str, int], dict[tuple[int, str], float]] = {}

    def _load_paid_ads(self, branch: Branch, year: int) -> dict[int, float]:
        key = (str(branch.id), year)
        if key in self._paid_ads:
            return self._paid_ads[key]
        slug = branch_slug_for(branch)
        rows = fetch_paid_ads_yearly(slug, year)
        self._paid_ads[key] = {m: v.get("spent_vnd", 0.0) for m, v in rows.items()}
        return self._paid_ads[key]

    def _load_kol(self, branch: Branch, year: int) -> dict[int, float]:
        key = (str(branch.id), year)
        if key in self._kol:
            return self._kol[key]
        hotel_id = kol_hotel_id_for(branch.id)
        if hotel_id is None:
            self._kol[key] = {}
        else:
            self._kol[key] = fetch_kol_yearly(hotel_id, year)
        return self._kol[key]

    def _load_manual(self, branch: Branch, year: int) -> dict[tuple[int, str], float]:
        """All ``manual_actual_vnd`` rows for this branch+year in one query."""
        key = (str(branch.id), year)
        if key in self._manual:
            return self._manual[key]
        rows = self.db.query(MarketingBudget).filter(
            MarketingBudget.branch_id == branch.id,
            MarketingBudget.year == year,
            MarketingBudget.manual_actual_vnd.isnot(None),
        ).all()
        self._manual[key] = {
            (r.month, r.channel): float(r.manual_actual_vnd or 0) for r in rows
        }
        return self._manual[key]

    def get(self, branch: Branch, year: int, month: int, channel: str) -> float:
        # Manual override always wins (lets ops correct an upstream miss).
        manual = self._load_manual(branch, year).get((month, channel))
        if manual is not None:
            return manual
        if channel == "paid_ads":
            return self._load_paid_ads(branch, year).get(month, 0.0)
        if channel == "kol":
            return self._load_kol(branch, year).get(month, 0.0)
        # crm and any future channel without an upstream feed: 0 unless manual
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


class ManualActualItem(BaseModel):
    """Per (branch, year, month, channel) manual actual-cost override.

    Currently used for the CRM channel (no upstream feed). ``None`` clears
    the override and lets the upstream / 0 default win again.
    """
    branch_id: UUID
    year: int = Field(ge=2000, le=2100)
    month: int = Field(ge=1, le=12)
    channel: str
    manual_actual_vnd: Optional[Decimal] = Field(default=None, ge=0)


class ManualActualBulk(BaseModel):
    items: list[ManualActualItem]


class YearlyPlanIn(BaseModel):
    """Input for ``PUT /yearly-plan`` — yearly total + 12 monthly %.

    Months default to flat 8.33% if any are missing. ``cascade_to_channels``
    when True (default) recomputes ``marketing_budgets`` rows for each
    (year, month) by preserving the existing channel ratio (or splitting
    evenly across channels if no prior rows exist).
    """
    branch_id: UUID
    year: int = Field(ge=2000, le=2100)
    total_vnd: Decimal = Field(ge=0)
    monthly_pcts: dict[str, float]   # {"1": 8.33, ..., "12": 8.37}
    cascade_to_channels: bool = True


def _normalised_monthly_pcts(raw: dict) -> dict[int, float]:
    """Coerce arbitrary input into ``{1..12: float}`` filling gaps with 8.33."""
    out: dict[int, float] = {}
    for k, v in (raw or {}).items():
        try:
            mi = int(k)
        except (TypeError, ValueError):
            continue
        if 1 <= mi <= 12:
            out[mi] = float(v or 0)
    for m in range(1, 13):
        out.setdefault(m, 8.33)
    return out


def _cascade_yearly_plan(
    db: Session, branch_id: UUID, year: int,
    total_vnd: float, monthly_pcts: dict[int, float],
):
    """Replace ``marketing_budgets`` allocations to match the new yearly plan.

    Preserves each month's existing channel ratio when present; otherwise
    splits evenly across ``CHANNELS``.
    """
    existing = db.query(MarketingBudget).filter(
        MarketingBudget.branch_id == branch_id,
        MarketingBudget.year == year,
    ).all()
    by_month: dict[int, list[MarketingBudget]] = {}
    for r in existing:
        by_month.setdefault(r.month, []).append(r)

    for m in range(1, 13):
        monthly_total = round(total_vnd * monthly_pcts.get(m, 0) / 100, 2)
        rows = by_month.get(m, [])
        # Build channel -> ratio (preserve), default to even split.
        if rows:
            old_total = sum(float(r.allocated_vnd or 0) for r in rows)
            ratios = (
                {r.channel: float(r.allocated_vnd or 0) / old_total for r in rows}
                if old_total > 0
                else {ch: 1.0 / len(CHANNELS) for ch in CHANNELS}
            )
        else:
            ratios = {ch: 1.0 / len(CHANNELS) for ch in CHANNELS}

        for ch in CHANNELS:
            ratio = ratios.get(ch, 0.0)
            new_alloc = round(monthly_total * ratio, 2)
            row = next((r for r in rows if r.channel == ch), None)
            if row is None:
                row = MarketingBudget(
                    branch_id=branch_id, year=year, month=m, channel=ch,
                    allocated_vnd=new_alloc,
                )
                db.add(row)
            else:
                row.allocated_vnd = new_alloc


# ── Endpoints ────────────────────────────────────────────────────────────────


@router.get("/yearly-plan")
def get_yearly_plan(
    branch_id: UUID = Query(...),
    year: int = Query(...),
    db: Session = Depends(get_db),
):
    """Yearly Plan tab data — yearly total, monthly %, and derived monthly
    budget per branch. Falls back to the sum of marketing_budgets if no
    yearly_plans row exists yet (so the UI can show something useful before
    ops sets a plan)."""
    branch = db.query(Branch).filter(Branch.id == branch_id).first()
    if not branch:
        raise HTTPException(404, "Branch not found")

    cur = (branch.currency or "VND").upper()
    rate = _get_rate_to_vnd(cur)

    plan = db.query(YearlyPlan).filter(
        YearlyPlan.branch_id == branch_id, YearlyPlan.year == year,
    ).first()

    if plan:
        total_vnd = float(plan.total_vnd or 0)
        pcts = _normalised_monthly_pcts(plan.monthly_pcts or {})
        exists = True
    else:
        # Derive from marketing_budgets: monthly total = sum(channels)
        rows = db.query(MarketingBudget).filter(
            MarketingBudget.branch_id == branch_id,
            MarketingBudget.year == year,
        ).all()
        monthly_totals: dict[int, float] = {}
        for r in rows:
            monthly_totals[r.month] = monthly_totals.get(r.month, 0.0) + float(r.allocated_vnd or 0)
        total_vnd = sum(monthly_totals.values())
        if total_vnd > 0:
            pcts = {m: round(v / total_vnd * 100, 4)
                    for m, v in monthly_totals.items()}
        else:
            pcts = {}
        pcts = _normalised_monthly_pcts(pcts)
        exists = False

    months = []
    for m in range(1, 13):
        pct = pcts.get(m, 0.0)
        budget_vnd = round(total_vnd * pct / 100, 2)
        months.append({
            "month": m,
            "pct": pct,
            "budget_vnd": budget_vnd,
            "budget_native": _vnd_to_native(budget_vnd, cur, rate),
        })

    sum_pct = round(sum(pcts.values()), 2)
    return _envelope({
        "branch_id": str(branch_id),
        "branch_name": branch.name,
        "currency": cur,
        "rate_to_vnd": rate,
        "year": year,
        "exists": exists,
        "total_vnd": total_vnd,
        "total_native": _vnd_to_native(total_vnd, cur, rate),
        "sum_pct": sum_pct,
        "months": months,
    })


@router.put("/yearly-plan")
def upsert_yearly_plan(
    body: YearlyPlanIn,
    db: Session = Depends(get_db),
):
    """Upsert yearly plan for one (branch, year). Cascades to
    marketing_budgets so per-channel allocations stay in sync."""
    branch = db.query(Branch).filter(Branch.id == body.branch_id).first()
    if not branch:
        raise HTTPException(404, "Branch not found")

    pcts_norm = _normalised_monthly_pcts(body.monthly_pcts)

    plan = db.query(YearlyPlan).filter(
        YearlyPlan.branch_id == body.branch_id,
        YearlyPlan.year == body.year,
    ).first()
    # JSONB stores string keys — store back as {"1": ..., "12": ...}.
    pcts_json = {str(m): pcts_norm[m] for m in range(1, 13)}

    if plan is None:
        plan = YearlyPlan(
            branch_id=body.branch_id, year=body.year,
            total_vnd=body.total_vnd, monthly_pcts=pcts_json,
        )
        db.add(plan)
    else:
        plan.total_vnd = body.total_vnd
        plan.monthly_pcts = pcts_json

    if body.cascade_to_channels:
        _cascade_yearly_plan(
            db, body.branch_id, body.year,
            float(body.total_vnd), pcts_norm,
        )

    db.commit()
    db.refresh(plan)
    return _envelope({
        "id": str(plan.id),
        "branch_id": str(plan.branch_id),
        "year": plan.year,
        "total_vnd": float(plan.total_vnd or 0),
        "monthly_pcts": dict(plan.monthly_pcts or {}),
        "cascaded": body.cascade_to_channels,
    })


# ── Endpoints (existing) ─────────────────────────────────────────────────────

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
    actuals = ActualsCache(db)

    months_data = []
    year_alloc_vnd = 0.0
    year_actual_vnd = 0.0

    for m in range(1, 13):
        channels = []
        m_alloc = 0.0
        m_actual = 0.0
        for ch in CHANNELS:
            a = allocs.get((m, ch), {})
            alloc_vnd = float(a.get("allocated_vnd", 0))
            actual_vnd = actuals.get(branch, year, m, ch)
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
    actuals = ActualsCache(db)

    channels = []
    total_alloc = 0.0
    total_actual = 0.0

    for ch in CHANNELS:
        a = allocs.get((month, ch), {})
        alloc_vnd = float(a.get("allocated_vnd", 0))
        actual_vnd = actuals.get(branch, year, month, ch)
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


@router.put("/manual-actual")
def upsert_manual_actual(
    body: ManualActualItem,
    db: Session = Depends(get_db),
):
    """Set/clear ``manual_actual_vnd`` for one (branch, year, month, channel).

    Used today by the CRM channel input on Budget Planner. Manual override
    wins over upstream actuals so it also doubles as a correction tool for
    Paid Ads / KOL when an upstream miss needs to be patched.
    """
    if body.channel not in CHANNELS:
        raise HTTPException(400, f"channel must be one of {CHANNELS}")
    if not db.query(Branch).filter(Branch.id == body.branch_id).first():
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
            allocated_vnd=0,
            manual_actual_vnd=body.manual_actual_vnd,
        )
        db.add(row)
    else:
        row.manual_actual_vnd = body.manual_actual_vnd

    db.commit()
    db.refresh(row)
    return _envelope({
        "id": str(row.id),
        "branch_id": str(row.branch_id),
        "year": row.year,
        "month": row.month,
        "channel": row.channel,
        "manual_actual_vnd": float(row.manual_actual_vnd) if row.manual_actual_vnd is not None else None,
    })


@router.put("/manual-actual/bulk")
def upsert_manual_actual_bulk(
    body: ManualActualBulk,
    db: Session = Depends(get_db),
):
    """Bulk variant of /manual-actual — used by Budget Planner's CRM grid."""
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
                channel=it.channel, allocated_vnd=0,
                manual_actual_vnd=it.manual_actual_vnd,
            )
            db.add(row)
        else:
            row.manual_actual_vnd = it.manual_actual_vnd
        out.append({
            "branch_id": str(it.branch_id), "year": it.year,
            "month": it.month, "channel": it.channel,
            "manual_actual_vnd": (
                float(it.manual_actual_vnd) if it.manual_actual_vnd is not None else None
            ),
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
