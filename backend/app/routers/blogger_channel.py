"""
GET /api/blogger-channel
Auth: Authorization: Bearer <HID_API_SECRET>
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db

router = APIRouter()
_bearer = HTTPBearer()


def _verify_bearer(credentials: HTTPAuthorizationCredentials = Depends(_bearer)) -> None:
    secret = settings.HID_API_SECRET
    if not secret or credentials.credentials != secret:
        raise HTTPException(status_code=401, detail="Invalid or missing Bearer token")


@router.get("/blogger-channel")
def get_blogger_channel(
    date_from: Optional[date] = Query(None, description="Start date (reservation_date). Defaults to 12 months ago."),
    date_to: Optional[date] = Query(None, description="End date (reservation_date). Defaults to today."),
    branch_id: Optional[str] = Query(None, description="Branch UUID to filter. Omit for all branches."),
    _auth: None = Depends(_verify_bearer),
    db: Session = Depends(get_db),
):
    """
    Blogger channel spend: reservations where source = 'Blogger' (KOL/influencer stays).
    Aggregated by month, by branch×month, and by branch.
    Auth: X-API-Key header (same keys as /api/public/*).
    """
    today = date.today()
    d_to = date_to or today
    d_from = date_from or (today.replace(day=1) - timedelta(days=365)).replace(day=1)

    excluded_clause = "('canceled','cancelled','no_show','no-show','cancelled_by_guest')"

    branch_clause = ""
    params: dict = {"d_from": d_from, "d_to": d_to}
    if branch_id:
        branch_clause = "AND r.branch_id = :branch_id::uuid"
        params["branch_id"] = branch_id

    rows = db.execute(text(f"""
        SELECT
            TO_CHAR(r.reservation_date, 'YYYY-MM')  AS month,
            b.name                                  AS branch_name,
            b.id::text                              AS branch_id,
            COUNT(*)                                AS bookings,
            COALESCE(SUM(r.grand_total_vnd), 0)     AS revenue_vnd
        FROM reservations r
        JOIN branches b ON b.id = r.branch_id
        WHERE r.reservation_date >= :d_from
          AND r.reservation_date <= :d_to
          AND LOWER(r.status) NOT IN {excluded_clause}
          AND LOWER(r.source) = 'blogger'
          {branch_clause}
        GROUP BY TO_CHAR(r.reservation_date, 'YYYY-MM'), b.id, b.code, b.name
        ORDER BY month, b.name
    """), params).fetchall()

    # ── by_month ──────────────────────────────────────────────────────────────
    month_map: dict[str, dict] = {}
    for r in rows:
        m = r.month
        if m not in month_map:
            month_map[m] = {"month": m, "revenue_vnd": 0.0, "bookings": 0}
        month_map[m]["revenue_vnd"] += float(r.revenue_vnd)
        month_map[m]["bookings"] += int(r.bookings)

    by_month = sorted(month_map.values(), key=lambda x: x["month"])

    # ── by_branch_month ───────────────────────────────────────────────────────
    by_branch_month = [
        {
            "branch_name": r.branch_name,
            "branch_id": r.branch_id,
            "month": r.month,
            "revenue_vnd": float(r.revenue_vnd),
            "bookings": int(r.bookings),
        }
        for r in rows
    ]

    # ── by_branch ─────────────────────────────────────────────────────────────
    branch_map: dict[str, dict] = {}
    for r in rows:
        bid = r.branch_id
        if bid not in branch_map:
            branch_map[bid] = {
                "branch_name": r.branch_name,
                "branch_id": bid,
                "revenue_vnd": 0.0,
                "bookings": 0,
            }
        branch_map[bid]["revenue_vnd"] += float(r.revenue_vnd)
        branch_map[bid]["bookings"] += int(r.bookings)

    by_branch = sorted(branch_map.values(), key=lambda x: -x["revenue_vnd"])

    total_revenue_vnd = sum(x["revenue_vnd"] for x in by_branch)
    total_bookings = sum(x["bookings"] for x in by_branch)

    return {
        "success": True,
        "data": {
            "date_from": d_from.isoformat(),
            "date_to": d_to.isoformat(),
            "filter_basis": "reservation_date",
            "by_month": by_month,
            "by_branch_month": by_branch_month,
            "by_branch": by_branch,
            "total_revenue_vnd": total_revenue_vnd,
            "total_bookings": total_bookings,
        },
        "error": None,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
