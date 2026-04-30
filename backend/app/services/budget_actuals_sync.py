"""Daily sync: pull paid_ads + kol actuals from upstream and cache them
into ``marketing_budgets.cached_actual_vnd`` so Budget Planner reads
serve from local DB instead of hitting upstream on every request.

Triggered:
  - automatically by APScheduler at 04:00 Asia/Ho_Chi_Minh
  - manually by ``POST /api/sync/marketing-budget-actuals``
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.models.branch import Branch
from app.models.marketing_budget import MarketingBudget
from app.services.ads_platform import branch_slug_for
from app.services.upstream_actuals import (
    fetch_kol_yearly,
    fetch_paid_ads_yearly,
    kol_hotel_id_for,
)

log = logging.getLogger(__name__)


def _upsert(db: Session, branch_id, year: int, month: int, channel: str,
            cached: float, now: datetime):
    row = db.query(MarketingBudget).filter_by(
        branch_id=branch_id, year=year, month=month, channel=channel,
    ).first()
    if row is None:
        row = MarketingBudget(
            branch_id=branch_id, year=year, month=month, channel=channel,
            allocated_vnd=0,
        )
        db.add(row)
    row.cached_actual_vnd = round(cached, 2)
    row.actuals_synced_at = now


def sync_marketing_actuals(db: Session, year: Optional[int] = None) -> dict:
    """Pull paid_ads + kol actuals for every active branch into the cache.

    ``year`` defaults to today's year. Returns a small report so the manual
    trigger endpoint can show the user what happened.
    """
    target_year = year or date.today().year
    now = datetime.now(timezone.utc)
    branches = db.query(Branch).filter_by(is_active=True).all()

    paid_rows = 0
    kol_rows = 0
    skipped: list[str] = []

    for b in branches:
        slug = branch_slug_for(b)
        if not slug:
            skipped.append(f"{b.name}: no ads_platform_slug")
            continue
        # Paid Ads
        try:
            paid = fetch_paid_ads_yearly(slug, target_year)
        except Exception as exc:
            log.exception("paid-ads fetch failed for %s: %s", b.name, exc)
            paid = {}
        for m, vals in paid.items():
            _upsert(db, b.id, target_year, m, "paid_ads",
                    float(vals.get("spent_vnd") or 0), now)
            paid_rows += 1

        # KOL
        hotel_id = kol_hotel_id_for(b.id)
        if hotel_id is None:
            skipped.append(f"{b.name}: no KOL hotel_id mapping")
        else:
            try:
                kol = fetch_kol_yearly(hotel_id, target_year)
            except Exception as exc:
                log.exception("kol fetch failed for %s: %s", b.name, exc)
                kol = {}
            for m, val in kol.items():
                _upsert(db, b.id, target_year, m, "kol", float(val or 0), now)
                kol_rows += 1

    db.commit()
    return {
        "year": target_year,
        "branches": len(branches),
        "paid_ads_rows": paid_rows,
        "kol_rows": kol_rows,
        "skipped": skipped,
        "synced_at": now.isoformat(),
    }


def run_daily_marketing_actuals_job():
    """Entry point for the APScheduler daily job. Owns its own DB session."""
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        result = sync_marketing_actuals(db)
        log.info("marketing-actuals daily sync done: %s", result)
    except Exception:
        log.exception("marketing-actuals daily sync failed")
    finally:
        db.close()
