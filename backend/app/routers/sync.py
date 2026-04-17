from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.database import get_db
from app.models.branch import Branch
from app.models.reservation import Reservation
from pathlib import Path

from app.services.cloudbeds import sync_branch, sync_all_branches, sync_branch_revenue, sync_daily_revenue, fetch_total_rooms, backfill_accommodation_total, backfill_room_type_and_rate_plan, map_country_code
from app.services.ingest_csv import import_all_csvs, import_csv_file, CSV_CONFIGS
from app.services import meta_ads as meta_service
from app.services import angle_classifier
from app.services.metrics_engine import recompute_branch_range
from app.models.ads import AdsPerformance
from app.models.angle import AdAngle
from app.config import settings

CSV_DIR = Path(r"C:\Users\duyth\Downloads")

router = APIRouter()


class SyncRequest(BaseModel):
    branch_id: Optional[UUID] = None  # if omitted, sync all active branches


def _envelope(data):
    return {
        "success": True,
        "data": data,
        "error": None,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }



def _run_backfill_bg(branch_configs: list, df, dt, do_recompute: bool):
    """Background worker: runs backfill + optional recompute for each branch config."""
    import logging
    from app.database import SessionLocal
    log = logging.getLogger(__name__)
    db = SessionLocal()
    try:
        for cfg in branch_configs:
            try:
                result = backfill_accommodation_total(
                    cfg["branch_id"], cfg["property_id"], cfg["currency"],
                    api_key=cfg["api_key"], checkin_from=df, checkin_to=dt, limit=cfg.get("limit")
                )
                log.info("Backfill %s: fetched=%s updated=%s skipped=%s",
                         cfg["name"], result.get("fetched"), result.get("updated"), result.get("skipped"))
                if do_recompute and result.get("updated", 0) > 0:
                    branch = db.query(Branch).filter_by(id=cfg["branch_id"]).first()
                    if branch:
                        days = recompute_branch_range(db, branch, df, dt)
                        log.info("Recompute %s: %d days", cfg["name"], days)
            except Exception as exc:
                log.error("Backfill failed for %s: %s", cfg["name"], exc)
    finally:
        db.close()


@router.post("/backfill")
def trigger_backfill(
    background_tasks: BackgroundTasks,
    branch_id: Optional[UUID] = Query(None),
    date_from: Optional[str] = Query(None, description="YYYY-MM-DD check-in from (default: 2 years ago)"),
    date_to: Optional[str] = Query(None, description="YYYY-MM-DD check-in to (default: today)"),
    limit: Optional[int] = Query(None, description="Max reservations per branch (for testing)"),
    recompute: bool = Query(True, description="Recompute daily_metrics after backfill"),
    db: Session = Depends(get_db),
):
    """
    Backfill grand_total_native for reservations where it is NULL.
    Calls Cloudbeds getReservation (singular) for each NULL-revenue reservation
    and extracts balanceDetailed.subTotal - additionalItems.
    This fixes OTA bookings that don't appear in Room Revenue transactions.
    Returns immediately — backfill runs in background. Check Railway logs for progress.
    """
    from datetime import date, timedelta

    today = date.today()
    df = date.fromisoformat(date_from) if date_from else today - timedelta(days=365 * 2)
    dt = date.fromisoformat(date_to) if date_to else today

    branches_q = db.query(Branch).filter_by(is_active=True)
    if branch_id:
        branches_q = branches_q.filter(Branch.id == branch_id)
    branches = branches_q.all()

    branch_configs = []
    skipped = []
    for branch in branches:
        pid = branch.cloudbeds_property_id
        if not pid:
            skipped.append({"branch": branch.name, "reason": "no property_id"})
            continue
        api_key = settings.get_api_key_for_property(str(pid))
        if not api_key:
            skipped.append({"branch": branch.name, "reason": f"no api_key for property {pid}"})
            continue
        branch_configs.append({
            "branch_id": str(branch.id),
            "property_id": str(pid),
            "currency": branch.currency or "VND",
            "api_key": api_key,
            "name": branch.name,
            "limit": limit,
        })

    background_tasks.add_task(_run_backfill_bg, branch_configs, df, dt, recompute)

    return _envelope({
        "status": "started",
        "message": "Backfill running in background. Check Railway logs for progress.",
        "window": {"from": df.isoformat(), "to": dt.isoformat()},
        "branches_queued": [c["name"] for c in branch_configs],
        "skipped": skipped,
    })


def _run_rate_plan_backfill_bg(branch_configs: list, df, dt):
    """Background worker: runs rate plan backfill for each branch config."""
    import logging
    log = logging.getLogger(__name__)
    for cfg in branch_configs:
        try:
            result = backfill_room_type_and_rate_plan(
                cfg["branch_id"], cfg["property_id"],
                api_key=cfg["api_key"], checkin_from=df, checkin_to=dt, limit=cfg.get("limit")
            )
            log.info("Rate plan backfill %s: fetched=%s rate_plans=%s room_types=%s",
                     cfg["name"], result.get("fetched"), result.get("rate_plans_filled"), result.get("room_types_filled"))
        except Exception as exc:
            log.error("Rate plan backfill failed for %s: %s", cfg["name"], exc)


@router.post("/backfill-rate-plan")
def trigger_rate_plan_backfill(
    background_tasks: BackgroundTasks,
    branch_id: Optional[UUID] = Query(None),
    date_from: Optional[str] = Query(None, description="YYYY-MM-DD check-in from (default: 90 days ago)"),
    date_to: Optional[str] = Query(None, description="YYYY-MM-DD check-in to (default: 180 days forward)"),
    limit: Optional[int] = Query(None, description="Max reservations per branch (for testing)"),
    db: Session = Depends(get_db),
):
    """
    Backfill rate_plan_name for reservations where it is NULL.
    Calls Cloudbeds getReservation (singular) for each reservation and extracts
    ratePlanNamePublic from room data. Also backfills room_type if still NULL.
    Returns immediately — backfill runs in background. Check Railway logs for progress.
    """
    from datetime import date as date_cls, timedelta

    today = date_cls.today()
    df = date_cls.fromisoformat(date_from) if date_from else today - timedelta(days=90)
    dt = date_cls.fromisoformat(date_to) if date_to else today + timedelta(days=180)

    branches_q = db.query(Branch).filter_by(is_active=True)
    if branch_id:
        branches_q = branches_q.filter(Branch.id == branch_id)
    branches = branches_q.all()

    branch_configs = []
    skipped = []
    for branch in branches:
        pid = branch.cloudbeds_property_id
        if not pid:
            skipped.append({"branch": branch.name, "reason": "no property_id"})
            continue
        api_key = settings.get_api_key_for_property(str(pid))
        if not api_key:
            skipped.append({"branch": branch.name, "reason": f"no api_key for property {pid}"})
            continue
        branch_configs.append({
            "branch_id": str(branch.id),
            "property_id": str(pid),
            "api_key": api_key,
            "name": branch.name,
            "limit": limit,
        })

    background_tasks.add_task(_run_rate_plan_backfill_bg, branch_configs, df, dt)

    return _envelope({
        "status": "started",
        "message": "Rate plan backfill running in background. Check Railway logs for progress.",
        "window": {"from": df.isoformat(), "to": dt.isoformat()},
        "branches_queued": [c["name"] for c in branch_configs],
        "skipped": skipped,
    })


@router.post("/daily-revenue")
def trigger_daily_revenue_sync(
    branch_id: Optional[UUID] = Query(None),
    date_from: Optional[str] = Query(None, description="YYYY-MM-DD, defaults to start of current month"),
    date_to: Optional[str] = Query(None, description="YYYY-MM-DD, defaults to today"),
    db: Session = Depends(get_db),
):
    """
    Sync revenue directly from Cloudbeds transaction dates into daily_metrics.
    Uses getTransactions filtered by TRANSACTION DATE (= the night the charge was posted).
    Each stay-night already has its own Room Revenue transaction — no proration needed.
    Matches Cloudbeds OCC report exactly. Much simpler than reservation-based approach.
    """
    from datetime import date, timedelta
    from app.config import settings

    today = date.today()
    df = date.fromisoformat(date_from) if date_from else today.replace(day=1)
    dt = date.fromisoformat(date_to) if date_to else today

    branches_q = db.query(Branch).filter_by(is_active=True)
    if branch_id:
        branches_q = branches_q.filter(Branch.id == branch_id)
    branches = branches_q.all()

    results = []
    for branch in branches:
        pid = branch.cloudbeds_property_id
        if not pid:
            results.append({"branch": branch.name, "error": "no property_id"})
            continue
        api_key = settings.get_api_key_for_property(str(pid))
        if not api_key:
            results.append({"branch": branch.name, "error": f"no api_key for property {pid}"})
            continue
        try:
            result = sync_daily_revenue(str(branch.id), str(pid), branch.currency or "VND",
                                        api_key=api_key, date_from=df, date_to=dt)
            result["branch"] = branch.name
            results.append(result)
        except Exception as exc:
            results.append({"branch": branch.name, "error": str(exc)})

    return _envelope({"synced_branches": results})


@router.post("/recompute")
def trigger_recompute(
    branch_id: Optional[UUID] = Query(None),
    date_from: Optional[str] = Query(None, description="YYYY-MM-DD, defaults to 2 years ago"),
    date_to: Optional[str] = Query(None, description="YYYY-MM-DD, defaults to today"),
    db: Session = Depends(get_db),
):
    """
    Recompute daily_metrics for all active branches (or a single branch).
    Must be run after CSV import to populate revenue, OCC%, ADR, RevPAR.
    """
    from datetime import date, timedelta

    today = date.today()
    df = date.fromisoformat(date_from) if date_from else today - timedelta(days=365 * 2)
    dt = date.fromisoformat(date_to) if date_to else today

    branches_q = db.query(Branch).filter_by(is_active=True)
    if branch_id:
        branches_q = branches_q.filter(Branch.id == branch_id)
    branches = branches_q.all()

    results = []
    for branch in branches:
        try:
            days = recompute_branch_range(db, branch, df, dt)
            results.append({"branch": branch.name, "days_recomputed": days})
        except Exception as exc:
            results.append({"branch": branch.name, "error": str(exc)})

    return _envelope({"date_from": df.isoformat(), "date_to": dt.isoformat(), "branches": results})


class CsvSyncRequest(BaseModel):
    csv_dir: Optional[str] = None   # override default Downloads dir
    filename: Optional[str] = None  # import a single file only


@router.post("/csv")
def trigger_csv_import(
    payload: CsvSyncRequest = CsvSyncRequest(),
    recompute: bool = Query(True, description="Auto-recompute daily_metrics after import"),
    db: Session = Depends(get_db),
):
    """
    Import reservation data from exported Cloudbeds CSV files.
    Reads from C:\\Users\\duyth\\Downloads\\ by default.
    Upserts all reservations including grand_total_native (revenue).
    Automatically recomputes daily_metrics for all affected branches after import.
    """
    from datetime import date, timedelta

    csv_dir = Path(payload.csv_dir) if payload.csv_dir else CSV_DIR

    if payload.filename:
        config = CSV_CONFIGS.get(payload.filename)
        if not config:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown filename. Known: {list(CSV_CONFIGS.keys())}",
            )
        path = csv_dir / payload.filename
        if not path.exists():
            raise HTTPException(status_code=404, detail=f"File not found: {path}")
        result = import_csv_file(
            path,
            branch_id=config["branch_id"],
            currency=config["currency"],
            european_numbers=config["european_numbers"],
        )
        if recompute:
            _recompute_after_import(db, [config["branch_id"]])
        return _envelope({payload.filename: result})

    results = import_all_csvs(csv_dir)
    total = {"created": 0, "updated": 0, "skipped": 0}
    for v in results.values():
        if "error" not in v:
            for k in total:
                total[k] += v.get(k, 0)
    if recompute:
        branch_ids = [c["branch_id"] for c in CSV_CONFIGS.values()]
        _recompute_after_import(db, branch_ids)
    return _envelope({"files": results, "total": total})


def _recompute_after_import(db: Session, branch_ids: list):
    """Recompute daily_metrics for given branch UUIDs over last 2 years."""
    from datetime import date, timedelta
    today = date.today()
    df = today - timedelta(days=365 * 2)
    for bid in branch_ids:
        branch = db.query(Branch).filter_by(id=bid).first()
        if branch:
            try:
                recompute_branch_range(db, branch, df, today)
            except Exception as exc:
                import logging
                logging.getLogger(__name__).error("Recompute failed branch %s: %s", bid, exc)


@router.post("/fix-revenue")
def fix_revenue_from_csv(
    db: Session = Depends(get_db),
):
    """
    Fast targeted revenue fix: reads each CSV, re-parses Grand Total with the
    corrected auto-detect parser, and bulk-updates only reservations whose
    stored grand_total_native differs by >5% from the correctly-parsed value.
    Much faster than full CSV re-import — only touches revenue fields.
    """
    import csv as _csv
    from app.services.ingest_csv import _parse_amount, CSV_CONFIGS
    from app.services.cloudbeds import get_cached_rate
    from sqlalchemy import text

    results = {}
    for filename, config in CSV_CONFIGS.items():
        path = CSV_DIR / filename
        if not path.exists():
            results[filename] = {"error": "file not found"}
            continue

        branch_id = config["branch_id"]
        currency = config["currency"]
        rate = get_cached_rate(currency, "VND")

        try:
            with open(path, newline="", encoding="utf-8-sig") as f:
                rows = list(_csv.DictReader(f))

            # Build map: res_num → (correct_native, correct_vnd)
            correct: dict[str, tuple] = {}
            for row in rows:
                res_num = (row.get("Reservation Number") or "").strip()
                if not res_num:
                    continue
                gt = _parse_amount(row.get("Grand Total", ""))
                if gt is None or gt <= 0:
                    continue
                gt_vnd = round(float(gt) * rate, 2) if rate is not None else None
                correct[res_num] = (float(gt), gt_vnd)

            if not correct:
                results[filename] = {"updated": 0, "skipped": 0}
                continue

            # Fetch existing revenue for this branch (only ID + revenue fields)
            existing = db.execute(
                text("""
                    SELECT cloudbeds_reservation_id, grand_total_native
                    FROM reservations
                    WHERE branch_id = :bid
                """),
                {"bid": branch_id},
            ).fetchall()

            updates = []
            for row_db in existing:
                res_num = row_db[0]
                stored = row_db[1]
                if res_num not in correct:
                    continue
                correct_val, correct_vnd = correct[res_num]
                # Skip if stored value is within 5% of correct
                if stored is not None and abs(float(stored) - correct_val) / max(correct_val, 1) < 0.05:
                    continue
                updates.append({
                    "res_num": res_num,
                    "branch_id": branch_id,
                    "gtn": correct_val,
                    "gtv": correct_vnd,
                })

            updated = 0
            for chunk_start in range(0, len(updates), 500):
                chunk = updates[chunk_start:chunk_start + 500]
                for u in chunk:
                    db.execute(
                        text("""
                            UPDATE reservations
                            SET grand_total_native = :gtn, grand_total_vnd = :gtv
                            WHERE cloudbeds_reservation_id = :res_num
                              AND branch_id = :branch_id
                        """),
                        u,
                    )
                db.commit()
                updated += len(chunk)

            results[filename] = {
                "total_in_csv": len(correct),
                "updated": updated,
                "already_correct": len(existing) - updated,
            }
        except Exception as exc:
            db.rollback()
            results[filename] = {"error": str(exc)}

    return _envelope({"results": results})


@router.get("/debug/raw-sample")
def debug_raw_sample(
    branch_id: UUID = Query(...),
    has_zero_revenue: bool = Query(True),
    db: Session = Depends(get_db),
):
    """Return raw_data keys + revenue fields for a sample reservation (debugging only)."""
    q = db.query(Reservation).filter(Reservation.branch_id == branch_id)
    if has_zero_revenue:
        q = q.filter(
            (Reservation.grand_total_native == None) | (Reservation.grand_total_native == 0)
        )
    else:
        q = q.filter(Reservation.grand_total_native > 0)
    r = q.first()
    if not r:
        return {"success": False, "data": None}
    raw = r.raw_data or {}
    # Return revenue-related fields from raw_data
    revenue_keys = {k: v for k, v in raw.items() if any(
        kw in k.lower() for kw in ["total", "amount", "revenue", "price", "balance", "paid", "rate", "fee"]
    )}
    return {"success": True, "data": {
        "cloudbeds_id": r.cloudbeds_reservation_id,
        "status": r.status,
        "source": r.source,
        "grand_total_native": r.grand_total_native,
        "check_in": str(r.check_in_date),
        "nights": r.nights,
        "all_raw_keys": list(raw.keys()),
        "revenue_fields_in_raw": revenue_keys,
    }}


@router.get("/debug/cloudbeds-reservation")
def debug_cloudbeds_reservation(
    property_id: str = Query(...),
    reservation_id: str = Query(...),
):
    """Fetch a single reservation from Cloudbeds API to inspect available fields."""
    import httpx
    api_key = settings.get_api_key_for_property(property_id)
    if not api_key:
        raise HTTPException(status_code=400, detail="No API key for property")
    r = httpx.get(
        "https://hotels.cloudbeds.com/api/v1.2/getReservation",
        params={"propertyID": property_id, "reservationID": reservation_id},
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=30,
    )
    data = r.json().get("data", {})
    revenue_keys = {k: v for k, v in data.items() if any(
        kw in k.lower() for kw in ["total", "amount", "revenue", "price", "balance", "paid", "rate", "accommodation", "fee"]
    )}
    return {"success": True, "data": {
        "all_keys": list(data.keys()),
        "revenue_fields": revenue_keys,
        "status": data.get("status"),
    }}


@router.get("/debug/cloudbeds-reservation-full")
def debug_cloudbeds_reservation_full(
    property_id: str = Query(...),
    reservation_id: str = Query(...),
):
    """Fetch a single reservation from Cloudbeds API — return assigned/unassigned rooms with rate plan info."""
    import httpx
    api_key = settings.get_api_key_for_property(property_id)
    if not api_key:
        raise HTTPException(status_code=400, detail="No API key for property")
    r = httpx.get(
        "https://hotels.cloudbeds.com/api/v1.2/getReservation",
        params={"propertyID": property_id, "reservationID": reservation_id},
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=30,
    )
    data = r.json().get("data", {})
    assigned = data.get("assigned", {})
    unassigned = data.get("unassigned", {})
    rooms_info = []
    for key in ("assigned", "unassigned"):
        rooms = data.get(key) or {}
        if isinstance(rooms, dict):
            rooms = list(rooms.values())
        for room in rooms:
            rooms_info.append({
                "source": key,
                "roomTypeName": room.get("roomTypeName"),
                "roomTypeID": room.get("roomTypeID"),
                "ratePlanID": room.get("ratePlanID"),
                "ratePlanNamePublic": room.get("ratePlanNamePublic"),
                "ratePlanNamePrivate": room.get("ratePlanNamePrivate"),
                "roomRate": room.get("roomRate"),
                "all_keys": list(room.keys()) if isinstance(room, dict) else [],
            })
    return {"success": True, "data": {
        "reservation_id": reservation_id,
        "status": data.get("status"),
        "rooms": rooms_info,
    }}


@router.get("/debug/compute-day")
def debug_compute_day(
    branch_id: UUID = Query(...),
    target_date: str = Query("2026-03-14"),
    db: Session = Depends(get_db),
):
    """Debug: run compute_day for one branch/date and return full traceback."""
    import traceback
    from datetime import date
    from app.services.metrics_engine import compute_day
    branch = db.query(Branch).filter_by(id=branch_id).first()
    if not branch:
        return {"error": "Branch not found"}
    try:
        dm = compute_day(db, branch, date.fromisoformat(target_date))
        return {"success": True, "revenue_native": float(dm.revenue_native or 0), "rooms_sold": dm.rooms_sold}
    except Exception as exc:
        return {"success": False, "error": str(exc), "traceback": traceback.format_exc()}


@router.get("/debug/spanning-reservations")
def debug_spanning_reservations(
    branch_id: UUID = Query(...),
    target_date: str = Query("2026-03-14"),
    db: Session = Depends(get_db),
):
    """Debug: list all reservations spanning target_date with their grand_total_native and nightly split."""
    from datetime import date
    from app.models.reservation import Reservation
    d = date.fromisoformat(target_date)
    rows = db.query(Reservation).filter(
        Reservation.branch_id == branch_id,
        Reservation.check_in_date <= d,
        Reservation.check_out_date > d,
    ).all()
    result = []
    total_nightly = 0.0
    for r in rows:
        nights = max(int(r.nights or 1), 1)
        gt = float(r.grand_total_native or 0)
        nightly = gt / nights
        total_nightly += nightly
        result.append({
            "res_num": r.cloudbeds_reservation_id,
            "check_in": str(r.check_in_date),
            "check_out": str(r.check_out_date),
            "nights": nights,
            "grand_total_native": gt,
            "nightly_contribution": round(nightly, 2),
            "status": r.status,
        })
    return _envelope({
        "date": target_date,
        "count": len(result),
        "total_nightly_revenue": round(total_nightly, 2),
        "reservations": result,
    })


# ── Meta Ads sync ──────────────────────────────────────────────────────────

META_CONFIG = {
    "saigon": ("META_ACCESS_TOKEN_SAIGON", "META_AD_ACCOUNT_SAIGON"),
    "1948":   ("META_ACCESS_TOKEN_1948",   "META_AD_ACCOUNT_1948"),
    "taipei": ("META_ACCESS_TOKEN_TAIPEI", "META_AD_ACCOUNT_TAIPEI"),
    "osaka":  ("META_ACCESS_TOKEN_OSAKA",  "META_AD_ACCOUNT_OSAKA"),
    "oani":   ("META_ACCESS_TOKEN_OANI",   "META_AD_ACCOUNT_OANI"),
}


def _get_meta_creds(branch: Branch) -> tuple[str, str]:
    """Return (token, account_id) from settings based on branch name."""
    key = branch.name.lower().replace("meander ", "").strip()
    for suffix, (tok_field, acc_field) in META_CONFIG.items():
        if suffix in key:
            token = getattr(settings, tok_field, "")
            account = getattr(settings, acc_field, "")
            return token, account
    return "", ""


@router.post("/meta")
def trigger_meta_sync(
    branch_id: UUID = Query(...),
    date_preset: str = Query("last_30d", description="Meta date preset (ignored if date_from/date_to set)"),
    date_from: Optional[str] = Query(None, description="YYYY-MM-DD custom range start"),
    date_to: Optional[str] = Query(None, description="YYYY-MM-DD custom range end (default: today)"),
    classify_angles: bool = Query(True),
    db: Session = Depends(get_db),
):
    """Pull Meta Ads data for one branch. Upserts ads_performance rows.

    Use date_from/date_to for a custom range (e.g. from March 1 onwards).
    If omitted, falls back to date_preset (default: last_30d).
    If classify_angles=True and ANTHROPIC_API_KEY is set, Claude Haiku will
    classify hook_type + keypoints for each ad with a creative body.
    """
    branch = db.query(Branch).filter_by(id=branch_id, is_active=True).first()
    if not branch:
        raise HTTPException(status_code=404, detail="Branch not found")

    token, account_id = _get_meta_creds(branch)
    if not token or not account_id:
        raise HTTPException(
            status_code=400,
            detail=f"No Meta credentials configured for branch '{branch.name}'. "
                   "Set META_ACCESS_TOKEN_* and META_AD_ACCOUNT_* in .env",
        )

    # Pull from Meta API
    try:
        ads = meta_service.sync_ads(token, account_id, date_preset,
                                    date_from=date_from, date_to=date_to)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Meta API error: {exc}")

    created = updated = 0
    classified = 0

    for ad in ads:
        # ── Upsert ads_performance ─────────────────────────────────────
        existing = (
            db.query(AdsPerformance)
            .filter_by(meta_ad_id=ad["meta_ad_id"])
            .first()
        )
        if existing:
            existing.campaign_name = ad["campaign_name"]
            existing.adset_name = ad["adset_name"]
            existing.ad_name = ad["ad_name"]
            existing.target_country = ad["target_country"]
            existing.target_audience = ad["target_audience"]
            existing.funnel_stage = ad["funnel_stage"]
            existing.pic = ad["pic"]
            existing.ad_body = ad["ad_body"]
            existing.channel = "Meta"
            existing.cost_native = ad["spend_vnd"]
            existing.cost_vnd = ad["spend_vnd"]
            existing.impressions = ad["impressions"]
            existing.clicks = ad["clicks"]
            existing.leads = ad["leads"]
            existing.bookings = ad.get("bookings", 0) or 0
            existing.lp_views = ad.get("lp_views", 0) or 0
            existing.add_to_cart = ad.get("add_to_cart", 0) or 0
            existing.initiate_checkout = ad.get("initiate_checkout", 0) or 0
            existing.revenue_native = ad.get("revenue", 0.0) or 0.0
            if ad["date_start"]:
                from datetime import date
                existing.date_from = date.fromisoformat(ad["date_start"])
            if ad["date_stop"]:
                from datetime import date
                existing.date_to = date.fromisoformat(ad["date_stop"])
            row = existing
            updated += 1
        else:
            from datetime import date as _date
            row = AdsPerformance(
                branch_id=branch.id,
                meta_ad_id=ad["meta_ad_id"],
                meta_campaign_id=ad["meta_campaign_id"],
                campaign_name=ad["campaign_name"],
                adset_name=ad["adset_name"],
                ad_name=ad["ad_name"],
                channel="Meta",
                target_country=ad["target_country"],
                target_audience=ad["target_audience"],
                funnel_stage=ad["funnel_stage"],
                pic=ad["pic"],
                ad_body=ad["ad_body"],
                cost_native=ad["spend_vnd"],
                cost_vnd=ad["spend_vnd"],
                impressions=ad["impressions"],
                clicks=ad["clicks"],
                leads=ad["leads"],
                bookings=ad.get("bookings", 0) or 0,
                lp_views=ad.get("lp_views", 0) or 0,
                add_to_cart=ad.get("add_to_cart", 0) or 0,
                initiate_checkout=ad.get("initiate_checkout", 0) or 0,
                revenue_native=ad.get("revenue", 0.0) or 0.0,
                date_from=_date.fromisoformat(ad["date_start"]) if ad["date_start"] else None,
                date_to=_date.fromisoformat(ad["date_stop"]) if ad["date_stop"] else None,
            )
            db.add(row)
            db.flush()
            created += 1

        # ── Classify angle if body text exists and no angle assigned yet ──
        if classify_angles and ad["ad_body"] and not row.ad_angle_id:
            api_key = settings.ANTHROPIC_API_KEY
            if api_key:
                result = angle_classifier.classify(ad["ad_body"], api_key)
                if result["hook_type"]:
                    # Find or create matching AdAngle for this branch+hook
                    angle_name = f"{result['hook_type']} — {branch.name}"
                    angle = (
                        db.query(AdAngle)
                        .filter_by(branch_id=branch.id, hook_type=result["hook_type"])
                        .first()
                    )
                    if not angle:
                        kps = result["keypoints"]
                        angle = AdAngle(
                            branch_id=branch.id,
                            name=angle_name,
                            hook_type=result["hook_type"],
                            keypoint_1=kps[0] if len(kps) > 0 else None,
                            keypoint_2=kps[1] if len(kps) > 1 else None,
                            keypoint_3=kps[2] if len(kps) > 2 else None,
                            keypoint_4=kps[3] if len(kps) > 3 else None,
                            keypoint_5=kps[4] if len(kps) > 4 else None,
                        )
                        db.add(angle)
                        db.flush()
                    row.ad_angle_id = angle.id
                    classified += 1

    db.commit()

    return _envelope({
        "branch": branch.name,
        "ads_fetched": len(ads),
        "created": created,
        "updated": updated,
        "classified": classified,
    })


@router.post("/cloudbeds")
def trigger_cloudbeds_sync(
    background_tasks: BackgroundTasks,
    payload: SyncRequest = SyncRequest(),
    db: Session = Depends(get_db),
):
    """
    Trigger on-demand Cloudbeds sync (runs in background to avoid HTTP timeout).
    If branch_id is provided, sync that branch only. Otherwise sync all active branches.
    Returns immediately with 202 Accepted — sync runs asynchronously.
    """
    import logging
    _logger = logging.getLogger(__name__)

    def _run_sync(branch_id=None):
        from app.database import SessionLocal
        sdb = SessionLocal()
        try:
            if branch_id:
                branch = sdb.query(Branch).filter_by(id=branch_id, is_active=True).first()
                if not branch:
                    _logger.error("Branch %s not found", branch_id)
                    return
                pid = branch.cloudbeds_property_id
                api_key = settings.get_api_key_for_property(str(pid)) if pid else None
                if not pid or not api_key:
                    _logger.error("No property_id/api_key for branch %s", branch.name)
                    return
                result = sync_branch(str(branch.id), pid, branch.currency, api_key=api_key)
                _logger.info("Cloudbeds sync done branch=%s: %s", branch.name, result)
            else:
                branches = sdb.query(Branch).filter_by(is_active=True).all()
                for branch in branches:
                    pid = branch.cloudbeds_property_id
                    if not pid:
                        continue
                    api_key = settings.get_api_key_for_property(str(pid))
                    if not api_key:
                        continue
                    try:
                        result = sync_branch(str(branch.id), pid, branch.currency, api_key=api_key)
                        _logger.info("Cloudbeds sync done branch=%s: %s", branch.name, result)
                    except Exception as exc:
                        _logger.error("Cloudbeds sync failed branch=%s: %s", branch.name, exc)
        except Exception as exc:
            _logger.exception("Cloudbeds background sync error: %s", exc)
        finally:
            sdb.close()

    if payload.branch_id:
        branch = db.query(Branch).filter_by(id=payload.branch_id, is_active=True).first()
        if not branch:
            raise HTTPException(status_code=404, detail="Branch not found or inactive")
        background_tasks.add_task(_run_sync, branch_id=payload.branch_id)
        return _envelope({"status": "sync_started", "branch": branch.name, "message": "Full sync running in background"})

    branches = db.query(Branch).filter_by(is_active=True).all()
    background_tasks.add_task(_run_sync)
    return _envelope({
        "status": "sync_started",
        "branches": [b.name for b in branches],
        "message": "Full sync for all branches running in background",
    })


@router.post("/daily")
def trigger_daily_sync(db: Session = Depends(get_db)):
    """
    Daily sync: reservations + revenue for current month + next month only.
    Skips past check-in dates to keep sync fast and focused.
    Run daily at 08:00 via scheduler.
    """
    import calendar
    from datetime import date, timedelta

    today = date.today()
    # Window: first day of current month → last day of next month
    date_from = date(today.year, today.month, 1)
    next_month = today.month % 12 + 1
    next_year = today.year if today.month < 12 else today.year + 1
    last_day_next = calendar.monthrange(next_year, next_month)[1]
    date_to = date(next_year, next_month, last_day_next)

    branches = db.query(Branch).filter_by(is_active=True).all()
    reservation_results = []
    revenue_results = []
    recompute_results = []

    for branch in branches:
        pid = branch.cloudbeds_property_id
        if not pid:
            reservation_results.append({"branch": branch.name, "error": "no property_id"})
            continue
        api_key = settings.get_api_key_for_property(str(pid))
        if not api_key:
            reservation_results.append({"branch": branch.name, "error": "no api_key"})
            continue

        # Step 1: incremental reservation sync (only modified in last 2 days — fast)
        # Catches new bookings, cancellations, status changes without pulling all history
        try:
            res = sync_branch(str(branch.id), pid, branch.currency, api_key=api_key,
                              incremental=True)
            res["branch"] = branch.name
            reservation_results.append(res)
        except Exception as exc:
            reservation_results.append({"branch": branch.name, "error": str(exc)})
            continue

        # Step 2: sync revenue for same window
        try:
            rev = sync_branch_revenue(str(branch.id), str(pid), branch.currency or "VND",
                                      api_key=api_key, date_from=date_from, date_to=date_to)
            rev["branch"] = branch.name
            revenue_results.append(rev)
        except Exception as exc:
            revenue_results.append({"branch": branch.name, "error": str(exc)})

        # Step 3: recompute daily_metrics for the same window
        try:
            days = recompute_branch_range(db, branch, date_from, date_to)
            recompute_results.append({"branch": branch.name, "days_recomputed": days})
        except Exception as exc:
            recompute_results.append({"branch": branch.name, "error": str(exc)})

    return _envelope({
        "window": {"from": date_from.isoformat(), "to": date_to.isoformat()},
        "reservations": reservation_results,
        "revenue": revenue_results,
        "recompute": recompute_results,
    })


@router.post("/rooms")
def sync_room_counts(db: Session = Depends(get_db)):
    """
    Fetch total_rooms from Cloudbeds getRooms API for every active branch
    and update the branches table.
    """
    branches = db.query(Branch).filter_by(is_active=True).all()
    results = []

    for branch in branches:
        pid = branch.cloudbeds_property_id
        if not pid:
            results.append({"branch": branch.name, "error": "no property_id"})
            continue

        api_key = settings.get_api_key_for_property(str(pid))
        if not api_key:
            results.append({"branch": branch.name, "error": "no api_key"})
            continue

        try:
            count = fetch_total_rooms(str(pid), api_key=api_key)
            branch.total_rooms = count
            db.add(branch)
            results.append({"branch": branch.name, "total_rooms": count})
        except Exception as exc:
            results.append({"branch": branch.name, "error": str(exc)})

    db.commit()
    return _envelope({"results": results})


# ── Google Ads (via Google Sheets) sync ────────────────────────────────────

GOOGLE_SHEET_MAP = {
    "11111111-1111-1111-1111-111111111101": "GOOGLE_SHEET_TAIPEI",
    "11111111-1111-1111-1111-111111111102": "GOOGLE_SHEET_SAIGON",
    "11111111-1111-1111-1111-111111111103": "GOOGLE_SHEET_1948",
    "11111111-1111-1111-1111-111111111104": "GOOGLE_SHEET_OANI",
    "11111111-1111-1111-1111-111111111105": "GOOGLE_SHEET_OSAKA",
}


@router.post("/google-ads")
def trigger_google_ads_sync(
    branch_id: Optional[UUID] = Query(None, description="Sync one branch; omit for all"),
    date_from: Optional[str] = Query(None, description="YYYY-MM-DD"),
    date_to: Optional[str] = Query(None, description="YYYY-MM-DD"),
    db: Session = Depends(get_db),
):
    """
    Pull Google Ads data from each branch's Google Sheet and upsert into ads_performance.
    Upsert key: branch_id + channel='Google' + campaign_name + date_from.
    """
    from datetime import date as _date
    from app.services.google_sheets_ads import sync_google_ads_sheet
    from app.models.ads import AdsPerformance

    client_id     = settings.GOOGLE_CLIENT_ID
    client_secret = settings.GOOGLE_CLIENT_SECRET
    refresh_token = settings.GOOGLE_REFRESH_TOKEN

    if not client_id or not refresh_token:
        raise HTTPException(status_code=400, detail="GOOGLE_CLIENT_ID / GOOGLE_REFRESH_TOKEN not configured")

    df = _date.fromisoformat(date_from) if date_from else None
    dt = _date.fromisoformat(date_to) if date_to else None

    branches_q = db.query(Branch).filter_by(is_active=True)
    if branch_id:
        branches_q = branches_q.filter(Branch.id == branch_id)
    branches = branches_q.all()

    results = []
    for branch in branches:
        sheet_attr = GOOGLE_SHEET_MAP.get(str(branch.id))
        if not sheet_attr:
            results.append({"branch": branch.name, "skipped": "no sheet configured"})
            continue
        spreadsheet_id = getattr(settings, sheet_attr, "")
        if not spreadsheet_id:
            results.append({"branch": branch.name, "skipped": "sheet ID empty"})
            continue

        try:
            data = sync_google_ads_sheet(
                branch_id=str(branch.id),
                branch_name=branch.name,
                spreadsheet_id=spreadsheet_id,
                currency=branch.currency or "VND",
                client_id=client_id,
                client_secret=client_secret,
                refresh_token=refresh_token,
                date_from=df,
                date_to=dt,
            )

            created = updated = 0
            for row in data["rows"]:
                # Upsert key: branch + channel + campaign_name + date
                existing = (
                    db.query(AdsPerformance)
                    .filter_by(
                        branch_id=branch.id,
                        channel="Google",
                        campaign_name=row["campaign_name"],
                        date_from=row["date_from"],
                    )
                    .first()
                )
                if existing:
                    existing.funnel_stage      = row["funnel_stage"]
                    existing.campaign_category = row["campaign_category"]
                    existing.target_country    = row["target_country"]
                    existing.cost_native       = row["cost_native"]
                    existing.impressions       = row["impressions"]
                    existing.clicks            = row["clicks"]
                    existing.leads             = row["leads"]
                    existing.bookings          = row["bookings"]
                    existing.revenue_native    = row["revenue_native"]
                    existing.date_to           = row["date_to"]
                    updated += 1
                else:
                    from datetime import date as _d
                    ap = AdsPerformance(
                        branch_id=branch.id,
                        channel="Google",
                        campaign_name=row["campaign_name"],
                        campaign_category=row["campaign_category"],
                        funnel_stage=row["funnel_stage"],
                        target_country=row["target_country"],
                        cost_native=row["cost_native"],
                        cost_vnd=row["cost_vnd"],
                        impressions=row["impressions"],
                        clicks=row["clicks"],
                        leads=row["leads"],
                        bookings=row["bookings"],
                        revenue_native=row["revenue_native"],
                        date_from=row["date_from"],
                        date_to=row["date_to"],
                    )
                    db.add(ap)
                    created += 1

            db.commit()
            results.append({
                "branch":   branch.name,
                "fetched":  data["fetched"],
                "filtered": data["filtered"],
                "created":  created,
                "updated":  updated,
            })
        except Exception as exc:
            db.rollback()
            results.append({"branch": branch.name, "error": str(exc)})

    return _envelope({"synced": results})



# ---------------------------------------------------------------------------
# KOL sheet sync
# ---------------------------------------------------------------------------

@router.post("/sheets-kol")
def trigger_sheets_kol(
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """
    Sync KOL bookings from the combined 'KOL Combine All Branch (VND)' Google Sheet.
    For each row: upsert KOLRecord by (branch, kol_name), then upsert KOLBooking
    linking the KOL to the Cloudbeds reservation with attributed_revenue_vnd.
    Runs in background.
    """
    client_id     = settings.GOOGLE_CLIENT_ID
    client_secret = settings.GOOGLE_CLIENT_SECRET
    refresh_token = settings.GOOGLE_REFRESH_TOKEN

    if not client_id or not refresh_token:
        raise HTTPException(status_code=400, detail="Google credentials not configured")

    def _run():
        import logging
        from sqlalchemy import text
        from app.database import SessionLocal
        from app.models.kol import KOLRecord, KOLBooking
        from app.services.sheets_kol import read_kol_bookings
        log = logging.getLogger(__name__)
        db2 = SessionLocal()
        try:
            # Build branch lookup: last word of branch name (lower) → Branch object
            # e.g. "MEANDER Saigon" → "saigon", "MEANDER 1948" → "1948"
            branch_by_key: dict = {}
            for b in db2.query(Branch).filter_by(is_active=True).all():
                for word in b.name.lower().split():
                    branch_by_key[word] = b

            rows = read_kol_bookings(client_id, client_secret, refresh_token)

            kol_created = 0
            booking_upserted = 0
            skipped = 0

            for row in rows:
                # Match branch by any word in sheet branch name
                branch = None
                for word in row["branch_name"].lower().split():
                    branch = branch_by_key.get(word)
                    if branch:
                        break
                if not branch:
                    log.debug("No branch match for: %s", row["branch_name"])
                    skipped += 1
                    continue

                # Upsert KOLRecord
                kol = db2.query(KOLRecord).filter_by(
                    branch_id=branch.id, kol_name=row["kol_name"]
                ).first()
                if not kol:
                    kol = KOLRecord(
                        branch_id=branch.id,
                        kol_name=row["kol_name"],
                        kol_nationality=row["kol_nationality"],
                        published_date=row["published_date"],
                        invitation_date=row["invitation_date"],
                    )
                    db2.add(kol)
                    db2.flush()
                    kol_created += 1
                else:
                    # Fill in missing fields
                    if not kol.kol_nationality and row["kol_nationality"]:
                        kol.kol_nationality = row["kol_nationality"]
                    if not kol.published_date and row["published_date"]:
                        kol.published_date = row["published_date"]
                    if not kol.invitation_date and row["invitation_date"]:
                        kol.invitation_date = row["invitation_date"]

                # Find reservation
                res = db2.execute(text("""
                    SELECT id FROM reservations
                    WHERE branch_id = :bid
                      AND cloudbeds_reservation_id = :res_num
                    LIMIT 1
                """), {"bid": str(branch.id), "res_num": row["res_num"]}).fetchone()

                if not res:
                    skipped += 1
                    continue

                # Upsert KOLBooking
                booking = db2.query(KOLBooking).filter_by(
                    kol_id=kol.id, reservation_id=res[0]
                ).first()
                if not booking:
                    booking = KOLBooking(
                        kol_id=kol.id,
                        reservation_id=res[0],
                        attributed_revenue_vnd=row["revenue_vnd"],
                    )
                    db2.add(booking)
                else:
                    booking.attributed_revenue_vnd = row["revenue_vnd"]
                booking_upserted += 1

            db2.commit()
            log.info(
                "KOL sync done: %d KOLs created, %d bookings upserted, %d skipped",
                kol_created, booking_upserted, skipped,
            )
        except Exception as exc:
            db2.rollback()
            log.error("KOL sheet sync failed: %s", exc, exc_info=True)
        finally:
            db2.close()

    background_tasks.add_task(_run)
    return _envelope({
        "status": "started",
        "message": "KOL sync running in background. Check Railway logs.",
        "sheet": "KOL Combine All Branch (VND)",
    })


# ---------------------------------------------------------------------------
# Backfill grand_total_vnd for reservations where it's NULL
# ---------------------------------------------------------------------------

@router.post("/backfill-vnd")
def backfill_grand_total_vnd(
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """
    Fill grand_total_vnd for reservations where it's NULL but grand_total_native
    is available. Uses fallback exchange rates. Runs in background.
    """
    FALLBACK_RATES = {"VND": 1.0, "TWD": 795.0, "JPY": 170.0}

    def _run():
        import logging
        from sqlalchemy import text
        from app.database import SessionLocal
        log = logging.getLogger(__name__)
        db2 = SessionLocal()
        try:
            branches = db2.query(Branch).filter_by(is_active=True).all()
            total = 0
            for b in branches:
                rate = FALLBACK_RATES.get(b.currency)
                if not rate:
                    log.warning("No rate for %s (%s)", b.name, b.currency)
                    continue
                if b.currency == "VND":
                    result = db2.execute(text("""
                        UPDATE reservations
                        SET grand_total_vnd = grand_total_native, updated_at = NOW()
                        WHERE grand_total_vnd IS NULL
                          AND grand_total_native IS NOT NULL
                          AND branch_id = :bid
                    """), {"bid": str(b.id)})
                else:
                    result = db2.execute(text("""
                        UPDATE reservations
                        SET grand_total_vnd = ROUND(CAST(grand_total_native AS NUMERIC) * :rate, 2),
                            updated_at = NOW()
                        WHERE grand_total_vnd IS NULL
                          AND grand_total_native IS NOT NULL
                          AND branch_id = :bid
                    """), {"rate": rate, "bid": str(b.id)})
                db2.commit()
                log.info("Backfill VND %s: %d rows (rate=%s)", b.name, result.rowcount, rate)
                total += result.rowcount
            log.info("Backfill VND done: %d total rows updated", total)
        except Exception as exc:
            db2.rollback()
            log.error("Backfill VND failed: %s", exc, exc_info=True)
        finally:
            db2.close()

    background_tasks.add_task(_run)
    return _envelope({
        "status": "started",
        "message": "VND backfill running in background. Check logs for results.",
        "rates": FALLBACK_RATES,
    })


# ---------------------------------------------------------------------------
# KOL Engine sync (replaces Google Sheets source)
# ---------------------------------------------------------------------------

@router.post("/kol-engine")
def trigger_kol_engine_sync(
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """
    Sync KOL data from the KOL Media Engine API.
    Upserts KOLRecord by (branch, kol_name) with enriched metadata.
    Runs in background.
    """
    if not settings.KOL_SYNC_API_KEY:
        raise HTTPException(status_code=400, detail="KOL_SYNC_API_KEY not configured")

    def _run():
        import logging
        from app.database import SessionLocal
        from app.models.kol import KOLRecord
        from app.services.kol_engine import fetch_kol_data
        log = logging.getLogger(__name__)
        db2 = SessionLocal()
        try:
            # Build branch lookup: keyword → Branch object
            branch_by_key: dict = {}
            for b in db2.query(Branch).filter_by(is_active=True).all():
                for word in b.name.lower().split():
                    branch_by_key[word] = b

            records = fetch_kol_data(
                settings.KOL_ENGINE_URL,
                settings.KOL_ENGINE_ORG_ID,
                settings.KOL_SYNC_API_KEY,
            )

            kol_created = 0
            kol_updated = 0
            skipped = 0

            for rec in records:
                branch = branch_by_key.get(rec["branch_key"])
                if not branch:
                    log.debug("No branch match for key: %s", rec["branch_key"])
                    skipped += 1
                    continue

                # Upsert KOLRecord by (branch_id, kol_name)
                kol = db2.query(KOLRecord).filter_by(
                    branch_id=branch.id, kol_name=rec["kol_name"]
                ).first()

                # Extract social links from platforms
                link_ig = None
                link_tiktok = None
                link_youtube = None
                for p in rec.get("platforms") or []:
                    platform = (p.get("platform") or "").lower()
                    url = p.get("profile_url") or ""
                    if "instagram" in platform and url:
                        link_ig = url
                    elif "tiktok" in platform and url:
                        link_tiktok = url
                    elif "youtube" in platform and url:
                        link_youtube = url

                # Determine is_gifted_stay
                is_gifted = rec.get("collab_type") == "hosted_stay"

                if not kol:
                    kol = KOLRecord(
                        branch_id=branch.id,
                        kol_name=rec["kol_name"],
                        kol_nationality=rec.get("kol_nationality"),
                        language=rec.get("language"),
                        target_audience=rec.get("target_audience"),
                        is_gifted_stay=is_gifted,
                        link_ig=link_ig,
                        link_tiktok=link_tiktok,
                        link_youtube=link_youtube,
                        notes=rec.get("case_id"),
                    )
                    db2.add(kol)
                    db2.flush()
                    kol_created += 1
                else:
                    # Update fields if they have new data
                    if rec.get("kol_nationality"):
                        kol.kol_nationality = rec["kol_nationality"]
                    if rec.get("language"):
                        kol.language = rec["language"]
                    if rec.get("target_audience"):
                        kol.target_audience = rec["target_audience"]
                    if link_ig:
                        kol.link_ig = link_ig
                    if link_tiktok:
                        kol.link_tiktok = link_tiktok
                    if link_youtube:
                        kol.link_youtube = link_youtube
                    kol.is_gifted_stay = is_gifted
                    if rec.get("case_id"):
                        kol.notes = rec["case_id"]
                    kol_updated += 1

            db2.commit()
            log.info(
                "KOL Engine sync done: %d created, %d updated, %d skipped",
                kol_created, kol_updated, skipped,
            )
        except Exception as exc:
            db2.rollback()
            log.error("KOL Engine sync failed: %s", exc, exc_info=True)
        finally:
            db2.close()

    background_tasks.add_task(_run)
    return _envelope({
        "status": "started",
        "message": "KOL Engine sync running in background.",
        "source": "kol-media-engine",
    })


# ---------------------------------------------------------------------------
# Cloudbeds Insights sync (manual trigger)
# ---------------------------------------------------------------------------

@router.post("/insights")
def trigger_insights_sync(
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """
    Manually trigger a full revenue + insights refresh.

    Per-branch pipeline:
      1. backfill_accommodation_total — getReservation per booking where
                                     grand_total_native is NULL or 0 (catches
                                     future "confirmed" bookings that have no
                                     Accommodation transactions yet)
      2. sync_branch_revenue       — refresh Reservation.grand_total_native
                                     from Cloudbeds Accommodation transactions
      3. populate_reservation_daily — rebuild per-night rows in reservation_daily
      4. sync_cloudbeds_occupancy  — OCC/ADR/RevPAR from Cloudbeds Insights
      5. sync_cloudbeds_filtered   — recompute daily_metrics.revenue from
                                     reservation_daily with source-exclusion filter

    Range: last 14 days through end of next month.
    Runs in background.
    """
    import calendar
    from datetime import date, timedelta
    from app.services.cloudbeds import (
        backfill_accommodation_total,
        sync_branch_revenue,
        populate_reservation_daily,
        sync_cloudbeds_occupancy,
        sync_cloudbeds_filtered,
    )

    today = date.today()
    sync_start = today - timedelta(days=14)
    if today.month == 12:
        next_month_year, next_month = today.year + 1, 1
    else:
        next_month_year, next_month = today.year, today.month + 1
    sync_end = date(next_month_year, next_month,
                    calendar.monthrange(next_month_year, next_month)[1])

    def _run():
        import logging
        from app.database import SessionLocal
        log = logging.getLogger(__name__)
        db2 = SessionLocal()
        try:
            branches = db2.query(Branch).filter_by(is_active=True).all()
            for branch in branches:
                pid = branch.cloudbeds_property_id
                api_key = settings.get_api_key_for_property(str(pid)) if pid else None
                if not pid or not api_key:
                    continue
                try:
                    backfill_accommodation_total(
                        str(branch.id), pid, branch.currency, api_key,
                        checkin_from=sync_start, checkin_to=sync_end,
                    )
                    sync_branch_revenue(
                        str(branch.id), pid, branch.currency, api_key,
                        date_from=sync_start, date_to=sync_end,
                    )
                    populate_reservation_daily(
                        db2, str(branch.id),
                        date_from=sync_start, date_to=sync_end,
                        property_id=pid, currency=branch.currency, api_key=api_key,
                    )
                    sync_cloudbeds_occupancy(
                        db2, str(branch.id), pid, branch.currency, api_key,
                        date_from=sync_start, date_to=sync_end,
                    )
                    sync_cloudbeds_filtered(
                        db2, str(branch.id), pid, branch.currency, api_key,
                        total_rooms=branch.total_rooms,
                        total_room_count=branch.total_room_count or 0,
                        total_dorm_count=branch.total_dorm_count or 0,
                        date_from=sync_start, date_to=sync_end,
                    )
                    log.info("Manual insights sync OK branch=%s [%s..%s]", branch.name, sync_start, sync_end)
                except Exception as e:
                    log.warning("Manual insights sync FAIL branch=%s: %s", branch.name, e)
        except Exception:
            log.exception("Manual insights sync job failed")
        finally:
            db2.close()

    background_tasks.add_task(_run)
    return _envelope({
        "status": "started",
        "message": f"Insights sync running in background [{sync_start}..{sync_end}]",
        "date_from": str(sync_start),
        "date_to": str(sync_end),
    })


# ── Revenue diagnostic ───────────────────────────────────────────────────────

@router.get("/diagnostic/revenue")
def diagnostic_revenue(
    year: int = Query(...),
    month: int = Query(...),
    branch_id: Optional[UUID] = Query(None),
    db: Session = Depends(get_db),
):
    """
    Compare revenue across the 3 storage layers for a given branch/month:
      - daily_metrics.revenue_native        (what the UI reads)
      - reservation_daily.nightly_rate      (with source/status filter)
      - reservations.grand_total_native     (stay-level, for cross-check)

    Also returns counts grouped by source & status for outlier detection.
    Use this when the Home / All Branches revenue looks wrong.
    """
    import calendar
    from datetime import date as date_type
    from app.models.reservation_daily import ReservationDaily
    from app.models.daily_metrics import DailyMetrics
    from app.services.metrics_engine import (
        EXCLUDED_SOURCES_REVENUE, EXCLUDED_STATUSES,
    )

    first_day = date_type(year, month, 1)
    last_day = date_type(year, month, calendar.monthrange(year, month)[1])

    branches_q = db.query(Branch).filter_by(is_active=True)
    if branch_id:
        branches_q = branches_q.filter(Branch.id == branch_id)
    branches = branches_q.all()

    out = []
    for b in branches:
        dm_sum = (db.query(func.coalesce(func.sum(DailyMetrics.revenue_native), 0))
                  .filter(DailyMetrics.branch_id == b.id,
                          DailyMetrics.date >= first_day,
                          DailyMetrics.date <= last_day)
                  .scalar()) or 0

        rd_rows = (db.query(ReservationDaily)
                   .filter(ReservationDaily.branch_id == b.id,
                           ReservationDaily.date >= first_day,
                           ReservationDaily.date <= last_day)
                   .all())
        rd_filtered = 0.0
        rd_zero_rate = 0
        rd_total = len(rd_rows)
        rd_src_breakdown: dict = {}
        rd_status_breakdown: dict = {}
        for rd in rd_rows:
            status = (rd.status or "").lower().strip()
            status_norm = status.replace("-", "_").replace(" ", "_")
            src = (rd.source or "").lower().strip()
            rd_src_breakdown[src or "(empty)"] = rd_src_breakdown.get(src or "(empty)", 0) + 1
            rd_status_breakdown[status or "(empty)"] = rd_status_breakdown.get(status or "(empty)", 0) + 1
            if status in EXCLUDED_STATUSES or status_norm in EXCLUDED_STATUSES:
                continue
            if src in EXCLUDED_SOURCES_REVENUE:
                continue
            night = float(rd.nightly_rate or 0)
            rd_filtered += night
            if night == 0:
                rd_zero_rate += 1

        # Reservations checking in this month — used to cross-check grand_total
        res_rows = (db.query(Reservation)
                    .filter(Reservation.branch_id == b.id,
                            Reservation.check_in_date >= first_day,
                            Reservation.check_in_date <= last_day)
                    .all())
        res_total = len(res_rows)
        res_zero_grand = 0
        res_grand_sum = 0.0
        for r in res_rows:
            status = (r.status or "").lower().strip()
            status_norm = status.replace("-", "_").replace(" ", "_")
            src = (r.source or "").lower().strip()
            if status in EXCLUDED_STATUSES or status_norm in EXCLUDED_STATUSES:
                continue
            if src in EXCLUDED_SOURCES_REVENUE:
                continue
            g = float(r.grand_total_native or 0)
            res_grand_sum += g
            if g == 0:
                res_zero_grand += 1

        out.append({
            "branch": b.name,
            "currency": b.currency,
            "daily_metrics_revenue": round(float(dm_sum), 2),
            "reservation_daily_revenue_filtered": round(rd_filtered, 2),
            "reservation_daily_rows": rd_total,
            "reservation_daily_zero_rate_rows_after_filter": rd_zero_rate,
            "reservation_daily_source_counts": rd_src_breakdown,
            "reservation_daily_status_counts": rd_status_breakdown,
            "reservations_checkin_in_month": res_total,
            "reservations_grand_total_filtered_sum": round(res_grand_sum, 2),
            "reservations_with_grand_total_zero_after_filter": res_zero_grand,
        })

    return _envelope({
        "year": year, "month": month,
        "branches": out,
    })


# ── Country normalization ────────────────────────────────────────────────────

@router.post("/normalize-countries")
def normalize_countries(
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """
    One-time normalization: re-map guest_country_code for all reservations
    using the expanded COUNTRY_MAP. Also normalizes guest_country.
    """
    import logging
    log = logging.getLogger(__name__)

    def _run():
        db2 = next(get_db())
        try:
            rows = db2.query(Reservation).filter(
                Reservation.guest_country.isnot(None),
            ).all()

            updated = 0
            for r in rows:
                new_code = map_country_code(r.guest_country)
                # Also normalize guest_country itself if it's a short code
                new_country = new_code  # use canonical name for display too

                if r.guest_country_code != new_code or r.guest_country != new_country:
                    r.guest_country_code = new_code
                    r.guest_country = new_country
                    updated += 1

            db2.commit()
            log.info("Country normalization complete: %d/%d updated", updated, len(rows))
        except Exception:
            db2.rollback()
            log.exception("Country normalization failed")
        finally:
            db2.close()

    background_tasks.add_task(_run)
    return _envelope({"status": "started", "message": "Country normalization running in background"})
