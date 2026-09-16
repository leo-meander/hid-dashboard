"""Will the year's revenue target be hit — months that happened, plus pace.

The KPI grid can only report what a month has already earned. Ask it in
September whether 2026 clears its target and it answers 79%, because four of
the twelve months have not happened yet and are counted as zero. The question
people actually ask is the other one: **on the pace we are running, does the
year land on target or short of it, and by how much.**

    projection = revenue of the months that have finished
               + the pace forecast for every month still open

The first half is not re-derived here. It is the same number the KPI grid
shows, through the same function — `kpi_engine.month_actual_and_target` — so a
manual accounting override stays exactly as it was typed, and the deduction and
other-revenue adjustments are applied to the Cloudbeds sum and to nothing else.
Two pages disagreeing about what a settled month earned would make every
comparison below worthless.

The second half is `pace_forecast`'s run-rate reading: what is on the books
now, plus the room-nights a booking day is currently adding, carried flat to
the end of each month and priced at the rate those nights are selling at. The
month underway is projected rather than read — a month three days old has most
of its revenue still ahead of it. It arrives already carrying the branch's
deduction and other revenue, applied where the money is made rather than here,
so both halves of the year sit on the basis the target was set against.

That half is a floor, not a prediction: bookings crowd towards check-in rather
than arriving evenly, so months still far off project low here by
construction. It answers "if the rest of the year sells at the rate it is
selling now, where does the year finish" — which is the question, and it is
the only reading this returns.

Currencies are summed in VND, because the group runs TWD, JPY and VND and plans
in VND. The rate is whatever `currency.get_cached_rate` holds, which in
practice is the hardcoded floor — the FX key has never been set — so treat the
group total as the planning figure it is rather than a settlement.
"""
from __future__ import annotations

import calendar
import logging
from datetime import date
from typing import Optional
from uuid import UUID

from sqlalchemy import extract, func
from sqlalchemy.orm import Session

from app.models.branch import Branch
from app.models.daily_metrics import DailyMetrics
from app.models.kpi import KPITarget
from app.services import fill_pace
from app.services.currency import get_cached_rate
from app.services.kpi_engine import month_actual_and_target

logger = logging.getLogger(__name__)

# Quarters are asked about by name, so the one that is still open is named.
Q4 = (10, 11, 12)


def _month_end(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def _to_vnd(amount: Optional[float], currency: Optional[str]) -> Optional[float]:
    if amount is None:
        return None
    return amount * (get_cached_rate(currency or "VND", "VND") or 1.0)


def _settled_revenue(db: Session, branch_ids: list, year: int) -> dict:
    """Cloudbeds revenue per (branch, month) — the KPI grid's raw input."""
    rows = db.query(
        DailyMetrics.branch_id,
        extract("month", DailyMetrics.date).label("m"),
        func.coalesce(func.sum(DailyMetrics.revenue_native), 0).label("revenue"),
    ).filter(
        DailyMetrics.branch_id.in_(branch_ids),
        extract("year", DailyMetrics.date) == year,
    ).group_by(DailyMetrics.branch_id, "m").all()
    return {(str(r.branch_id), int(r.m)): float(r.revenue or 0) for r in rows}


def _targets(db: Session, branch_ids: list, year: int) -> dict:
    rows = db.query(KPITarget).filter(
        KPITarget.branch_id.in_(branch_ids), KPITarget.year == year,
    ).all()
    return {
        (str(r.branch_id), r.month): {
            "target": float(r.target_revenue_native or 0),
            "override": (float(r.actual_revenue_override)
                         if r.actual_revenue_override is not None else None),
        }
        for r in rows
    }


def forecast_year(
    db: Session,
    branch_id: Optional[UUID] = None,
    year: Optional[int] = None,
    as_of: Optional[date] = None,
    days: int = 60,
) -> dict:
    """Where the year lands against its revenue target, branch by branch.

    Months that have finished are counted as they happened. Every month still
    open — the one underway included — is projected from booking pace. The
    two halves are labelled separately throughout, because "we are at 79% of
    target" and "we are on course for 99% of it" are different claims and only
    one of them answers the question.
    """
    as_of = as_of or date.today()
    year = year or as_of.year

    q = db.query(Branch).filter_by(is_active=True)
    if branch_id:
        q = q.filter(Branch.id == branch_id)
    branches = q.order_by(Branch.name).all()
    if not branches:
        return {"available": False, "reason": "no_branches"}

    branch_ids = [b.id for b in branches]
    keys = [str(b.id) for b in branches]
    targets = _targets(db, branch_ids, year)
    cloudbeds = _settled_revenue(db, keys and branch_ids, year)

    settled = [m for m in range(1, 13) if _month_end(year, m) < as_of]
    open_months = [m for m in range(1, 13) if m not in settled]

    # One pass over the open months, all branches at once. Scoped to a branch
    # only when the caller was: the forecast is per branch either way.
    cells: list[dict] = []
    if open_months:
        pace = fill_pace.get_fill_pace(
            db,
            branch_id=branch_id,
            months=[(year, m) for m in open_months],
            days=days,
            as_of=as_of,
            compare_last_year=True,
            include_forecast=True,
        )
        cells = (pace.get("forecast") or {}).get("cells") or []

    by_cell = {(c["branch_id"], c["month"]): c for c in cells}
    rows = []
    for branch in branches:
        bid = str(branch.id)
        actual = 0.0
        for m in settled:
            t = targets.get((bid, m), {})
            actual += month_actual_and_target(
                branch, t.get("target", 0.0), t.get("override"),
                cloudbeds.get((bid, m), 0.0),
            )["actual_revenue"]

        projected = low = high = ceiling = 0.0
        rooms_to_sell = 0.0
        extra_per_day = 0.0
        missing = []
        # One line per projected month, carrying what it was built from. The
        # page shows its working from this rather than asserting a total.
        detail = []
        for m in open_months:
            c = by_cell.get((bid, m))
            if not c or (c.get("run_rate") or {}).get("revenue_native") is None:
                missing.append(m)
                continue
            # Already adjusted where it was made — see pace_forecast._run_rate.
            adjusted = c["run_rate"]["revenue_native"]
            if adjusted is None:
                missing.append(m)
                continue
            projected += adjusted
            low += adjusted
            high += adjusted
            # The ceiling: every room still unsold, sold, at today's rate.
            ceiling += c["run_rate"].get("revenue_max_native") or adjusted
            rooms_to_sell += c["run_rate"].get("room_nights_to_sell") or 0
            extra_per_day += c["run_rate"].get("needed_extra_per_day") or 0
            detail.append({
                "month": m,
                "revenue_native": round(adjusted, 2),
                "target_native": targets.get((bid, m), {}).get("target", 0.0),
                "basis": "run_rate",
                "room_nights": c["run_rate"]["room_nights"],
                "otb_room_nights": c["run_rate"]["otb_room_nights"],
                "room_nights_per_day": c["run_rate"]["room_nights_per_day"],
                "days_left": c["run_rate"]["days_left"],
                "booked_revenue_native": c["booked_revenue_native"],
                "adr_remaining": c["run_rate"]["adr"],
            })

        covered = [m for m in range(1, 13) if m not in missing]
        target_covered = sum(targets.get((bid, m), {}).get("target", 0.0) for m in covered)
        target_full = sum(targets.get((bid, m), {}).get("target", 0.0) for m in range(1, 13))
        q4_cells = [by_cell.get((bid, m)) for m in Q4 if m in open_months]
        q4_settled = [m for m in Q4 if m in settled]
        # The same reading as the year above it — mixing the two would make
        # the quarter and the year it sits inside disagree.
        q4_projection = sum(
            c["run_rate"]["revenue_native"] or 0
            for c in q4_cells if c and c["run_rate"]["revenue_native"] is not None
        ) + sum(
            month_actual_and_target(
                branch, targets.get((bid, m), {}).get("target", 0.0),
                targets.get((bid, m), {}).get("override"),
                cloudbeds.get((bid, m), 0.0),
            )["actual_revenue"] for m in q4_settled
        )
        q4_target = sum(targets.get((bid, m), {}).get("target", 0.0)
                        for m in Q4 if m not in missing)
        q4_complete = not [m for m in Q4 if m in missing]

        projection = actual + projected
        # What it would take. Under the ceiling the target is a question of
        # filling faster; above it, no amount of filling reaches it and the
        # only lever left is the rate.
        ceiling_total = actual + ceiling
        reachable = ceiling_total >= target_covered if target_covered else None
        adr_for_target = None
        if target_covered and not reachable and rooms_to_sell:
            booked = sum((by_cell.get((bid, m)) or {}).get("booked_revenue_native") or 0
                         for m in open_months if m not in missing)
            mult = 1 - float(branch.deduction_pct or 0) / 100
            other = float(branch.other_revenue_native or 0) * len(
                [m for m in open_months if m not in missing])
            raw_needed = ((target_covered - actual) - other) / mult if mult else None
            if raw_needed and raw_needed > booked:
                adr_for_target = (raw_needed - booked) / rooms_to_sell
        rows.append({
            "branch_id": bid,
            "branch_name": branch.name,
            "currency": branch.currency,
            "actual_to_date_native": round(actual, 2),
            "forecast_remaining_native": round(projected, 2),
            "projection_native": round(projection, 2),
            "projection_low_native": round(actual + low, 2),
            "projection_high_native": round(actual + high, 2),
            "target_native": round(target_covered, 2),
            "target_full_year_native": round(target_full, 2),
            "gap_native": round(projection - target_covered, 2),
            "achievement_pct": (round(projection / target_covered * 100, 1)
                                if target_covered else None),
            "achievement_low_pct": (round((actual + low) / target_covered * 100, 1)
                                    if target_covered else None),
            "achievement_high_pct": (round((actual + high) / target_covered * 100, 1)
                                     if target_covered else None),
            "ceiling_native": round(ceiling_total, 2),
            "ceiling_achievement_pct": (round(ceiling_total / target_covered * 100, 1)
                                        if target_covered else None),
            "reachable_on_rooms": reachable,
            "room_nights_to_sell": round(rooms_to_sell, 1),
            "extra_per_day": round(extra_per_day, 2),
            "adr_for_target_native": round(adr_for_target, 2) if adr_for_target else None,
            "months_not_projected": missing,
            "projected_detail": detail,
            "settled_count": len(settled),
            "q4_projection_native": round(q4_projection, 2) if q4_complete else None,
            "q4_target_native": round(q4_target, 2),
            "q4_achievement_pct": (round(q4_projection / q4_target * 100, 1)
                                   if q4_complete and q4_target else None),
        })

    rows.sort(key=lambda r: -(r["projection_native"] or 0))
    return {
        "available": True,
        "year": year,
        "as_of": as_of.isoformat(),
        "settled_months": settled,
        "projected_months": open_months,
        "branches": rows,
        "total": _group_total(rows),
    }


def _group_total(rows: list[dict]) -> dict:
    """Everything in VND, which is the only currency the group can be added in.

    A branch whose year could not be fully projected is still counted — its
    projected months are real — but the target it is measured against is the
    target of those months alone, and `months_not_projected` says which ones
    were left out on both sides.
    """
    def vnd(key):
        total = 0.0
        for r in rows:
            v = _to_vnd(r.get(key), r.get("currency"))
            if v is not None:
                total += v
        return round(total, 2)

    projection = vnd("projection_native")
    ceiling = vnd("ceiling_native")
    target = vnd("target_native")
    q4_rows = [r for r in rows if r["q4_projection_native"] is not None]
    q4_projection = round(sum(_to_vnd(r["q4_projection_native"], r["currency"]) or 0
                              for r in q4_rows), 2)
    q4_target = round(sum(_to_vnd(r["q4_target_native"], r["currency"]) or 0
                          for r in q4_rows), 2)

    return {
        "currency": "VND",
        "actual_to_date_vnd": vnd("actual_to_date_native"),
        "forecast_remaining_vnd": vnd("forecast_remaining_native"),
        "projection_vnd": projection,
        "projection_low_vnd": vnd("projection_low_native"),
        "projection_high_vnd": vnd("projection_high_native"),
        "target_vnd": target,
        "target_full_year_vnd": vnd("target_full_year_native"),
        "gap_vnd": round(projection - target, 2),
        "achievement_pct": round(projection / target * 100, 1) if target else None,
        "ceiling_vnd": ceiling,
        "ceiling_achievement_pct": (round(ceiling / target * 100, 1) if target else None),
        "reachable_on_rooms": (ceiling >= target) if target else None,
        "extra_per_day": round(sum(r.get("extra_per_day") or 0 for r in rows), 2),
        # Branches whose target cannot be reached by filling, whatever the pace.
        "not_reachable": [
            {"branch_id": r["branch_id"], "branch_name": r["branch_name"],
             "currency": r["currency"], "ceiling_pct": r["ceiling_achievement_pct"],
             "adr_for_target": r["adr_for_target_native"]}
            for r in rows if r["reachable_on_rooms"] is False
        ],
        "achievement_low_pct": (round(vnd("projection_low_native") / target * 100, 1)
                                if target else None),
        "achievement_high_pct": (round(vnd("projection_high_native") / target * 100, 1)
                                 if target else None),
        "q4_projection_vnd": q4_projection,
        "q4_target_vnd": q4_target,
        "q4_achievement_pct": (round(q4_projection / q4_target * 100, 1)
                               if q4_target else None),
        "branches_fully_projected": len(q4_rows),
        "branches_in_scope": len(rows),
    }
