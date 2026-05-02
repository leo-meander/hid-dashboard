"""KOL router"""
import re
from datetime import datetime, date, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.branch import Branch
from app.models.kol import KOLRecord
from app.services.cloudbeds import get_cached_rate
from app.services.csv_kol_sync import sync_kol_csv

router = APIRouter()

_KOL_RE = re.compile(r"\(KOL_([^)]+)\)")


def _extract_kol_name(room_type: str) -> str | None:
    """Extract 'KOL_Kennababe' from 'Superior Double (KOL_Kennababe)'."""
    m = _KOL_RE.search(room_type or "")
    return ("KOL_" + m.group(1).strip()) if m else None


@router.get("/summary")
def get_kol_summary(
    branch_id: Optional[UUID] = Query(None),
    db: Session = Depends(get_db),
):
    """
    Aggregate KOL bookings from reservations.
    Room types like 'Superior Double (KOL_Kennababe)' → KOL_Rate_Plan = 'KOL_Kennababe'.
    Enriches each KOL with management fields from kol_records and angle info from ad_angles.
    """
    # ── 1. Pull all KOL reservations ────────────────────────────────────
    branch_filter = "AND r.branch_id = :bid" if branch_id else ""
    res_rows = db.execute(text(f"""
        SELECT r.room_type,
               r.grand_total_native,
               r.status,
               b.id        AS branch_id,
               b.name      AS branch_name,
               b.currency
        FROM   reservations r
        JOIN   branches b ON r.branch_id = b.id
        WHERE  r.room_type ILIKE '%KOL_%'
          {branch_filter}
    """), {"bid": str(branch_id)} if branch_id else {}).fetchall()

    # ── 2. Aggregate per (kol_name, branch) ─────────────────────────────
    # key = (kol_name, branch_id)
    agg: dict[tuple, dict] = {}
    for r in res_rows:
        kol_name = _extract_kol_name(r[0])
        if not kol_name:
            continue
        status = (r[2] or "").lower()
        cancelled = status in ("canceled", "cancelled", "no_show", "no-show", "cancelled_by_guest")
        key = (kol_name, str(r[3]))
        if key not in agg:
            agg[key] = {
                "kol_rate_plan_name": kol_name,
                "branch_id": str(r[3]),
                "branch": r[4],
                "currency": r[5],
                "organic_booking": 0,
                "organic_revenue": 0.0,
            }
        if not cancelled:
            agg[key]["organic_booking"] += 1
            agg[key]["organic_revenue"] += float(r[1] or 0)

    if not agg:
        return _envelope([])

    # ── 3. Pull kol_records for management fields ────────────────────────
    # Only fetch records for the kol_names that appear in the reservations
    # we just aggregated — and scope to branch_id when provided.
    kol_map: dict[str, dict] = {}
    kol_names_seen = {key[0] for key in agg.keys()}
    kol_branch_filter = "AND k.branch_id = :bid" if branch_id else ""
    kol_rows = db.execute(text(f"""
        SELECT k.id, k.kol_name, k.kol_nationality, k.language,
               k.target_audience, k.cost_vnd, k.cost_native,
               k.link_ig, k.link_tiktok, k.link_youtube,
               k.paid_ads_usage_fee_vnd, k.paid_ads_channel,
               k.usage_rights_expiry_date, k.contract_status,
               k.deliverable_status, k.ad_angle_id, k.notes
        FROM   kol_records k
        WHERE  k.kol_name = ANY(:names)
          {kol_branch_filter}
    """), {
        "names": list(kol_names_seen),
        **({"bid": str(branch_id)} if branch_id else {}),
    }).fetchall()

    for kr in kol_rows:
        kol_map[kr[1]] = {
            "kol_record_id": str(kr[0]),
            "kol_nationality": kr[2],
            "language": kr[3],
            "target_audience": kr[4],
            "cost_vnd": float(kr[5]) if kr[5] else None,
            "cost_native": float(kr[6]) if kr[6] else None,
            "link_ig": kr[7],
            "link_tiktok": kr[8],
            "link_youtube": kr[9],
            "ads_usage_fee_vnd": float(kr[10]) if kr[10] else None,
            "channel": kr[11],
            "usage_rights_until": kr[12].isoformat() if kr[12] else None,
            "status": kr[13] or kr[14],   # contract_status or deliverable_status
            "ad_angle_id": str(kr[15]) if kr[15] else None,
            "notes": kr[16],
        }

    # ── 4. Pull angle info ───────────────────────────────────────────────
    angle_map: dict[str, dict] = {}
    angle_rows = db.execute(text("""
        SELECT id, angle_code, name FROM ad_angles
    """)).fetchall()
    for ar in angle_rows:
        angle_map[str(ar[0])] = {"angle_id": ar[1], "angle_info": ar[2]}

    # ── 5. Merge ─────────────────────────────────────────────────────────
    today = date.today()
    result = []
    for item in agg.values():
        kol_name = item["kol_rate_plan_name"]
        mgmt = kol_map.get(kol_name, {})
        item.update(mgmt)

        # Angle info
        aid = item.get("ad_angle_id")
        angle = angle_map.get(aid, {}) if aid else {}
        item["angle_id"] = angle.get("angle_id", "")
        item["angle_info"] = angle.get("angle_info", "")

        # Expiry countdown
        exp = item.get("usage_rights_until")
        item["expiry_days"] = None
        if exp:
            try:
                item["expiry_days"] = (date.fromisoformat(exp) - today).days
            except ValueError:
                pass

        result.append(item)

    result.sort(key=lambda x: (x["kol_rate_plan_name"], x["branch"]))
    return _envelope(result)


def _envelope(data):
    return {"success": True, "data": data, "error": None,
            "timestamp": datetime.now(timezone.utc).isoformat()}


def _blank_none(v):
    """Convert empty string to None for optional fields."""
    return v if v else None


class KOLIn(BaseModel):
    branch_id: UUID
    kol_name: str
    kol_nationality: Optional[str] = None
    language: Optional[str] = None
    target_audience: Optional[str] = None
    cost_native: Optional[float] = None
    cost_vnd: Optional[float] = None
    is_gifted_stay: bool = False
    invitation_date: Optional[str] = None
    published_date: Optional[str] = None
    link_ig: Optional[str] = None
    link_tiktok: Optional[str] = None
    link_youtube: Optional[str] = None
    deliverable_status: Optional[str] = None  # Not Started, In Progress, Editing, Done
    paid_ads_eligible: bool = False
    paid_ads_usage_fee_vnd: Optional[float] = None
    paid_ads_channel: Optional[str] = None
    usage_rights_expiry_date: Optional[str] = None
    ads_usage_status: Optional[str] = None  # Available / In Use / Expired / Not Allowed
    contract_status: Optional[str] = None     # Draft, Negotiating, Signed, Cancelled
    notes: Optional[str] = None


@router.get("")
def list_kol(
    branch_id: Optional[UUID] = Query(None),
    contract_status: Optional[str] = Query(None),
    paid_ads_eligible: Optional[bool] = Query(None),
    ads_usage_status: Optional[str] = Query(None),
    expiry_within_days: Optional[int] = Query(None, description="Filter KOLs with usage rights expiring within N days"),
    db: Session = Depends(get_db),
):
    q = db.query(KOLRecord)
    if branch_id:
        q = q.filter(KOLRecord.branch_id == branch_id)
    if contract_status:
        q = q.filter(KOLRecord.contract_status == contract_status)
    if paid_ads_eligible is not None:
        q = q.filter(KOLRecord.paid_ads_eligible == paid_ads_eligible)
    if ads_usage_status:
        q = q.filter(KOLRecord.ads_usage_status == ads_usage_status)
    rows = q.order_by(KOLRecord.created_at.desc()).all()

    today = date.today()
    result = []
    for r in rows:
        row = _row(r)
        # Expiry alert
        if r.usage_rights_expiry_date:
            days_left = (r.usage_rights_expiry_date - today).days
            row["expiry_days_left"] = days_left
            row["expiry_alert"] = days_left <= 30
        else:
            row["expiry_days_left"] = None
            row["expiry_alert"] = False

        if expiry_within_days is not None:
            if row["expiry_days_left"] is None or row["expiry_days_left"] > expiry_within_days:
                continue
        result.append(row)
    return _envelope(result)


def _clean(data: dict) -> dict:
    """Convert empty strings to None for date/optional string fields."""
    date_keys = {"invitation_date", "published_date", "usage_rights_expiry_date"}
    for k in date_keys:
        if k in data and data[k] == "":
            data[k] = None
    return data


def _fill_vnd(data: dict, branch_id, db: Session) -> dict:
    """Auto-compute cost_vnd from cost_native if cost_vnd not provided."""
    if data.get("cost_native") and not data.get("cost_vnd"):
        branch = db.query(Branch).filter_by(id=branch_id).first()
        if branch and branch.currency:
            rate = get_cached_rate(branch.currency, "VND")
            if rate:
                data["cost_vnd"] = round(float(data["cost_native"]) * rate, 2)
    return data


@router.post("")
def create_kol(body: KOLIn, db: Session = Depends(get_db)):
    data = _fill_vnd(_clean(body.model_dump()), body.branch_id, db)
    obj = KOLRecord(**data)
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return _envelope(_row(obj))


@router.put("/{kol_id}")
def update_kol(kol_id: UUID, body: KOLIn, db: Session = Depends(get_db)):
    obj = db.query(KOLRecord).filter(KOLRecord.id == kol_id).first()
    if not obj:
        raise HTTPException(404, "KOL not found")
    data = _fill_vnd(_clean(body.model_dump(exclude_unset=True)), body.branch_id, db)
    for k, v in data.items():
        setattr(obj, k, v)
    obj.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(obj)
    return _envelope(_row(obj))


@router.delete("/{kol_id}")
def delete_kol(kol_id: UUID, db: Session = Depends(get_db)):
    obj = db.query(KOLRecord).filter(KOLRecord.id == kol_id).first()
    if not obj:
        raise HTTPException(404, "KOL not found")
    db.delete(obj)
    db.commit()
    return _envelope({"deleted": str(kol_id)})


@router.post("/sync-csv")
async def sync_csv(file: UploadFile = File(...), db: Session = Depends(get_db)):
    """Upload a Lark CSV to upsert KOL records. Key: kol_name."""
    if not file.filename.endswith(".csv"):
        raise HTTPException(400, "Only .csv files are accepted")
    content = (await file.read()).decode("utf-8-sig")
    stats = sync_kol_csv(db, content)
    return _envelope(stats)


def _row(r: KOLRecord):
    return {
        "id": str(r.id),
        "branch_id": str(r.branch_id),
        "kol_name": r.kol_name,
        "kol_nationality": r.kol_nationality,
        "language": r.language,
        "target_audience": r.target_audience,
        "cost_native": float(r.cost_native) if r.cost_native else None,
        "cost_vnd": float(r.cost_vnd) if r.cost_vnd else None,
        "is_gifted_stay": r.is_gifted_stay,
        "invitation_date": r.invitation_date.isoformat() if r.invitation_date else None,
        "published_date": r.published_date.isoformat() if r.published_date else None,
        "link_ig": r.link_ig,
        "link_tiktok": r.link_tiktok,
        "link_youtube": r.link_youtube,
        "deliverable_status": r.deliverable_status,
        "paid_ads_eligible": r.paid_ads_eligible,
        "paid_ads_usage_fee_vnd": float(r.paid_ads_usage_fee_vnd) if r.paid_ads_usage_fee_vnd else None,
        "paid_ads_channel": r.paid_ads_channel,
        "usage_rights_expiry_date": r.usage_rights_expiry_date.isoformat() if r.usage_rights_expiry_date else None,
        "ads_usage_status": r.ads_usage_status,
        "contract_status": r.contract_status,
        "notes": r.notes,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }
