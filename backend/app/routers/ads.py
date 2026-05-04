"""Ads Performance router"""
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.database import get_db
from app.models.ads import AdsPerformance

router = APIRouter()


def _envelope(data):
    return {"success": True, "data": data, "error": None,
            "timestamp": datetime.now(timezone.utc).isoformat()}


def _last_ads_synced_at(db: Session, branch_id: Optional[UUID]) -> Optional[str]:
    """Latest updated_at across the AdsPerformance rows that feed this view."""
    q = db.query(func.max(AdsPerformance.updated_at))
    if branch_id:
        q = q.filter(AdsPerformance.branch_id == branch_id)
    ts = q.scalar()
    return ts.isoformat() if ts else None


def _clean(data: dict) -> dict:
    for k in ("date_from", "date_to"):
        if data.get(k) == "":
            data[k] = None
    return data


class AdsIn(BaseModel):
    branch_id: UUID
    campaign_name: Optional[str] = None
    adset_name: Optional[str] = None
    ad_name: Optional[str] = None
    channel: Optional[str] = None           # Meta, Google, TikTok
    target_country: Optional[str] = None
    target_audience: Optional[str] = None
    campaign_category: Optional[str] = None
    funnel_stage: Optional[str] = None      # TOF, MOF, BOF
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    cost_native: Optional[float] = None
    cost_vnd: Optional[float] = None
    impressions: Optional[int] = None
    clicks: Optional[int] = None
    leads: Optional[int] = None
    bookings: Optional[int] = None
    revenue_native: Optional[float] = None
    revenue_vnd: Optional[float] = None


@router.get("")
def list_ads(
    branch_id: Optional[UUID] = Query(None),
    channel: Optional[str] = Query(None),
    limit: int = Query(200, ge=1, le=1000),
    db: Session = Depends(get_db),
):
    q = db.query(AdsPerformance)
    if branch_id:
        q = q.filter(AdsPerformance.branch_id == branch_id)
    if channel:
        q = q.filter(AdsPerformance.channel == channel)
    rows = q.order_by(AdsPerformance.date_from.desc().nullslast()).limit(limit).all()
    last_synced_iso = _last_ads_synced_at(db, branch_id)
    out = []
    for r in rows:
        d = _row(r)
        d["data_synced_at"] = last_synced_iso
        out.append(d)
    return _envelope(out)


@router.get("/summary")
def ads_summary(
    branch_id: Optional[UUID] = Query(None),
    channel: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    """Aggregate spend, impressions, clicks, bookings, revenue and ROAS by channel."""
    q = db.query(
        AdsPerformance.channel,
        func.count(AdsPerformance.id).label("campaigns"),
        func.coalesce(func.sum(AdsPerformance.cost_native), 0).label("cost_native"),
        func.coalesce(func.sum(AdsPerformance.cost_vnd), 0).label("cost_vnd"),
        func.coalesce(func.sum(AdsPerformance.impressions), 0).label("impressions"),
        func.coalesce(func.sum(AdsPerformance.clicks), 0).label("clicks"),
        func.coalesce(func.sum(AdsPerformance.leads), 0).label("leads"),
        func.coalesce(func.sum(AdsPerformance.bookings), 0).label("bookings"),
        func.coalesce(func.sum(AdsPerformance.revenue_native), 0).label("revenue_native"),
        func.coalesce(func.sum(AdsPerformance.revenue_vnd), 0).label("revenue_vnd"),
    )
    if branch_id:
        q = q.filter(AdsPerformance.branch_id == branch_id)
    if channel:
        q = q.filter(AdsPerformance.channel == channel)
    rows = q.group_by(AdsPerformance.channel).all()
    last_synced_iso = _last_ads_synced_at(db, branch_id)

    result = []
    for r in rows:
        cost = float(r.cost_native or 0)
        rev = float(r.revenue_native or 0)
        clicks = int(r.clicks or 0)
        impr = int(r.impressions or 0)
        result.append({
            "channel": r.channel or "Unknown",
            "campaigns": r.campaigns,
            "cost_native": cost,
            "cost_vnd": float(r.cost_vnd or 0),
            "impressions": impr,
            "clicks": clicks,
            "leads": int(r.leads or 0),
            "bookings": int(r.bookings or 0),
            "revenue_native": rev,
            "revenue_vnd": float(r.revenue_vnd or 0),
            "roas": round(rev / cost, 2) if cost > 0 else None,
            "ctr_pct": round(clicks / impr * 100, 2) if impr > 0 else None,
            "cpc_native": round(cost / clicks, 2) if clicks > 0 else None,
            "data_synced_at": last_synced_iso,
        })
    return _envelope(result)


@router.post("")
def create_ad(body: AdsIn, db: Session = Depends(get_db)):
    obj = AdsPerformance(**_clean(body.model_dump()))
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return _envelope(_row(obj))


@router.put("/{ad_id}")
def update_ad(ad_id: UUID, body: AdsIn, db: Session = Depends(get_db)):
    obj = db.query(AdsPerformance).filter(AdsPerformance.id == ad_id).first()
    if not obj:
        raise HTTPException(404, "Ad not found")
    for k, v in _clean(body.model_dump(exclude_unset=True)).items():
        setattr(obj, k, v)
    obj.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(obj)
    return _envelope(_row(obj))


@router.delete("/{ad_id}")
def delete_ad(ad_id: UUID, db: Session = Depends(get_db)):
    obj = db.query(AdsPerformance).filter(AdsPerformance.id == ad_id).first()
    if not obj:
        raise HTTPException(404, "Ad not found")
    db.delete(obj)
    db.commit()
    return _envelope({"deleted": str(ad_id)})


def _row(r: AdsPerformance):
    cost = float(r.cost_native or 0)
    rev = float(r.revenue_native or 0)
    clicks = int(r.clicks or 0)
    impr = int(r.impressions or 0)
    return {
        "id": str(r.id),
        "branch_id": str(r.branch_id),
        "campaign_name": r.campaign_name,
        "adset_name": r.adset_name,
        "ad_name": r.ad_name,
        "channel": r.channel,
        "target_country": r.target_country,
        "target_audience": r.target_audience,
        "campaign_category": r.campaign_category,
        "funnel_stage": r.funnel_stage,
        "date_from": r.date_from.isoformat() if r.date_from else None,
        "date_to": r.date_to.isoformat() if r.date_to else None,
        "cost_native": cost,
        "cost_vnd": float(r.cost_vnd or 0),
        "impressions": impr,
        "clicks": clicks,
        "leads": int(r.leads or 0),
        "bookings": int(r.bookings or 0),
        "revenue_native": rev,
        "revenue_vnd": float(r.revenue_vnd or 0),
        "roas": round(rev / cost, 2) if cost > 0 else None,
        "ctr_pct": round(clicks / impr * 100, 2) if impr > 0 else None,
        "cpc_native": round(cost / clicks, 2) if clicks > 0 else None,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }
