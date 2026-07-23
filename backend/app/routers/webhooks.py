"""
Cloudbeds reservation fan-out router + polling job.

Fan-out targets (per reservation):
  1. GHL CRM — upsert contact
  2. Meta CAPI — Purchase event (non-Website sources)
  3. Google Ads — offline conversion upload (non-Website sources)
  4. TikTok Events API — Saigon only

Trigger modes:
  A. Polling — APScheduler job runs every 10 min, calls getReservations for
     each branch, deduplicates via in-memory seen-set, fans out new ones.
  B. Webhook (optional) — POST /api/webhooks/cloudbeds if Cloudbeds ever
     supports push webhooks for this property.
"""
import hashlib
import hmac
import logging
from collections import deque
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request

from app.config import settings
from app.routers.auth import require_admin
from app.services.ghl_crm_service import upsert_contact_from_reservation
from app.services.google_ads_service import upload_offline_conversion
from app.services.meta_capi_service import send_purchase_event
from app.services.tiktok_capi_service import send_complete_payment_event
from app.services import webhook_log

logger = logging.getLogger(__name__)
router = APIRouter()

CLOUDBEDS_API_BASE = "https://hotels.cloudbeds.com/api/v1.3"
WEBSITE_SOURCES = {"website", "booking engine"}

# Dedup: remember the last 2000 reservation IDs we processed so the 10-min
# polling window overlap never double-fires the same reservation.
_seen_reservation_ids: deque = deque(maxlen=2000)
_seen_set: set = set()


def _mark_seen(reservation_id: str) -> bool:
    """Return True if already seen (duplicate). Otherwise mark and return False."""
    if reservation_id in _seen_set:
        return True
    _seen_reservation_ids.append(reservation_id)
    _seen_set.add(reservation_id)
    # Keep set in sync with bounded deque
    if len(_seen_reservation_ids) == 2000:
        oldest = _seen_reservation_ids[0]
        _seen_set.discard(oldest)
    return False


# ── Core fan-out (shared by polling + webhook paths) ─────────────────────────

def _fan_out(property_id: str, reservation_id: str, reservation: dict) -> None:
    """Process one reservation: fan out to GHL, Meta, Google Ads, TikTok."""
    branch = settings.cloudbeds_property_to_branch.get(str(property_id))
    if not branch:
        logger.error("Unknown propertyID=%s — no branch mapping", property_id)
        return

    cfg = settings.get_webhook_config_for_branch(branch)
    source = (reservation.get("source") or "").lower()
    is_website_source = any(kw in source for kw in WEBSITE_SOURCES)
    guest_email = (reservation.get("guestEmail") or "").strip().lower()

    ghl_log = meta_log = gads_log = tiktok_log = None

    # ── GHL CRM upsert ───────────────────────────────────────────────────────
    if cfg["ghl_location_id"] and cfg["ghl_api_key"]:
        try:
            result = upsert_contact_from_reservation(
                reservation=reservation,
                location_id=cfg["ghl_location_id"],
                api_key=cfg["ghl_api_key"],
                branch=branch,
            )
            logger.info("GHL upsert branch=%s action=%s contact_id=%s", branch, result["action"], result["contact_id"])
            ghl_log = {"success": result["action"] in ("created", "updated"), "action": result["action"], "error": result.get("error")}
        except Exception as e:
            logger.error("GHL upsert error branch=%s reservation=%s: %s", branch, reservation_id, e)
            ghl_log = {"success": False, "error": str(e)}
    else:
        ghl_log = {"success": None, "action": "skipped_no_config"}

    # ── Meta CAPI + Google Ads — disabled pending rebuild ────────────────────
    meta_log = {"success": None, "action": "skipped_disabled"}
    gads_log = {"success": None, "action": "skipped_disabled"}

    # ── TikTok Events API — disabled pending rebuild ─────────────────────────
    if branch == "saigon":
        tiktok_log = {"success": None, "action": "skipped_disabled"}

    webhook_log.record(
        reservation_id=reservation_id,
        branch=branch,
        guest_email=guest_email,
        source=source,
        ghl=ghl_log,
        meta=meta_log,
        google_ads=gads_log,
        tiktok=tiktok_log,
    )


def _fetch_full_reservation(property_id: str, reservation_id: str) -> dict | None:
    """Call getReservation (singular) to get full data including guestEmail, guestList."""
    api_key = settings.get_api_key_for_property(str(property_id))
    if not api_key:
        logger.error("No Cloudbeds API key for propertyID=%s", property_id)
        return None
    try:
        with httpx.Client(timeout=20) as client:
            resp = client.get(
                f"{CLOUDBEDS_API_BASE}/getReservation",
                headers={"Authorization": f"Bearer {api_key}"},
                params={"propertyID": str(property_id), "reservationID": str(reservation_id)},
            )
            resp.raise_for_status()
            body = resp.json()
            if not body.get("success"):
                logger.error("getReservation failed: %s", body.get("message"))
                return None
            return body.get("data")
    except Exception as e:
        logger.error("Error fetching reservation %s: %s", reservation_id, e)
        return None


def _process_reservation(property_id: str, reservation_id: str) -> None:
    """Fetch a single reservation from Cloudbeds then fan out."""
    reservation = _fetch_full_reservation(property_id, reservation_id)
    if reservation:
        _fan_out(property_id, reservation_id, reservation)


# ── Polling job (called by APScheduler every 10 min) ─────────────────────────

def poll_new_reservations() -> None:
    """
    Poll all branches for reservations created in the last 15 minutes.
    Skips any reservation already in the dedup set.
    """
    now_utc = datetime.now(timezone.utc)
    from_dt = now_utc - timedelta(minutes=15)
    date_from = from_dt.strftime("%Y-%m-%d %H:%M:%S")
    date_to = now_utc.strftime("%Y-%m-%d %H:%M:%S")

    branches = [
        (settings.CB_PROPERTY_ID_SAIGON, settings.CB_API_KEY_SAIGON),
        (settings.CB_PROPERTY_ID_TAIPEI, settings.CB_API_KEY_TAIPEI),
        (settings.CB_PROPERTY_ID_1948, settings.CB_API_KEY_1948),
        (settings.CB_PROPERTY_ID_OANI, settings.CB_API_KEY_OANI),
        (settings.CB_PROPERTY_ID_OSAKA, settings.CB_API_KEY_OSAKA),
    ]

    for property_id, api_key in branches:
        if not property_id or not api_key:
            continue
        try:
            with httpx.Client(timeout=20) as client:
                resp = client.get(
                    f"{CLOUDBEDS_API_BASE}/getReservations",
                    headers={"Authorization": f"Bearer {api_key}"},
                    params={
                        "propertyID": property_id,
                        "dateCreatedFrom": date_from,
                        "dateCreatedTo": date_to,
                        "includeGuestList": "true",
                        "pageSize": 50,
                    },
                )
                body = resp.json()
                if not body.get("success"):
                    logger.warning("getReservations failed property=%s: %s", property_id, body.get("message"))
                    continue

                reservations = body.get("data") or []
                if isinstance(reservations, dict):
                    reservations = list(reservations.values())

                new_count = 0
                for res in reservations:
                    rid = str(res.get("reservationID", ""))
                    if not rid or _mark_seen(rid):
                        continue
                    full = _fetch_full_reservation(property_id, rid)
                    if not full:
                        continue
                    new_count += 1
                    logger.info("Poll: new reservation=%s property=%s", rid, property_id)
                    _fan_out(property_id, rid, full)

                if new_count:
                    logger.info("Poll property=%s: processed %d new reservations", property_id, new_count)

        except Exception as e:
            logger.error("Poll error property=%s: %s", property_id, e)


# ── Routes ────────────────────────────────────────────────────────────────────

def _verify_signature(raw_body: bytes, signature: str | None) -> bool:
    secret = settings.CLOUDBEDS_WEBHOOK_SECRET
    if not secret:
        return True
    if not signature:
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", signature)


@router.post("/webhooks/cloudbeds")
async def cloudbeds_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_cloudbeds_signature: str | None = Header(default=None),
) -> dict:
    """Optional push webhook endpoint — used if Cloudbeds supports it."""
    raw_body = await request.body()
    if not _verify_signature(raw_body, x_cloudbeds_signature):
        raise HTTPException(status_code=401, detail="Invalid signature")
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    property_id = str(payload.get("propertyID") or payload.get("property_id") or "")
    reservation_id = str(payload.get("reservationID") or payload.get("reservation_id") or "")
    if not property_id or not reservation_id:
        return {"success": True, "message": "skipped — missing IDs"}
    if _mark_seen(reservation_id):
        return {"success": True, "message": "already processed"}

    background_tasks.add_task(_process_reservation, property_id, reservation_id)
    return {"success": True, "message": "queued"}


@router.get("/admin/webhook-events")
async def get_webhook_events(
    branch: str | None = None,
    limit: int = 100,
    _admin=Depends(require_admin),
) -> dict:
    """Return recent webhook processing results. Admin only."""
    events = webhook_log.get_events(branch=branch, limit=min(limit, 500))
    # Mask guest email before returning — show j***@gmail.com
    for ev in events:
        ev["guest_email"] = _mask_email(ev.get("guest_email", ""))
    return {"success": True, "data": events, "total": len(events)}


def _mask_email(email: str) -> str:
    if not email or "@" not in email:
        return email
    local, domain = email.split("@", 1)
    return local[:2] + "***@" + domain


@router.post("/admin/poll-now")
async def poll_now(
    background_tasks: BackgroundTasks,
    minutes: int = 60,
    _admin=Depends(require_admin),
) -> dict:
    """Manually trigger a Cloudbeds poll for the last N minutes. Admin only."""
    background_tasks.add_task(_poll_with_window, minutes)
    return {"success": True, "message": f"Polling last {minutes} minutes in background"}


def _poll_with_window(minutes: int) -> None:
    """Like poll_new_reservations but with a custom lookback window and no dedup."""
    from datetime import datetime, timedelta, timezone
    now_utc = datetime.now(timezone.utc)
    from_dt = now_utc - timedelta(minutes=minutes)
    date_from = from_dt.strftime("%Y-%m-%d %H:%M:%S")
    date_to = now_utc.strftime("%Y-%m-%d %H:%M:%S")

    branches = [
        (settings.CB_PROPERTY_ID_SAIGON, settings.CB_API_KEY_SAIGON),
        (settings.CB_PROPERTY_ID_TAIPEI, settings.CB_API_KEY_TAIPEI),
        (settings.CB_PROPERTY_ID_1948, settings.CB_API_KEY_1948),
        (settings.CB_PROPERTY_ID_OANI, settings.CB_API_KEY_OANI),
        (settings.CB_PROPERTY_ID_OSAKA, settings.CB_API_KEY_OSAKA),
    ]

    for property_id, api_key in branches:
        if not property_id or not api_key:
            continue
        try:
            with httpx.Client(timeout=20) as client:
                resp = client.get(
                    f"{CLOUDBEDS_API_BASE}/getReservations",
                    headers={"Authorization": f"Bearer {api_key}"},
                    params={
                        "propertyID": property_id,
                        "dateCreatedFrom": date_from,
                        "dateCreatedTo": date_to,
                        "includeGuestList": "true",
                        "pageSize": 50,
                    },
                )
                body = resp.json()
                if not body.get("success"):
                    logger.warning("poll-now getReservations failed property=%s: %s", property_id, body.get("message"))
                    continue
                reservations = body.get("data") or []
                if isinstance(reservations, dict):
                    reservations = list(reservations.values())
                for res in reservations:
                    rid = str(res.get("reservationID", ""))
                    if not rid:
                        continue
                    full = _fetch_full_reservation(property_id, rid)
                    if not full:
                        continue
                    logger.info("poll-now: processing reservation=%s property=%s", rid, property_id)
                    _fan_out(property_id, rid, full)
        except Exception as e:
            logger.error("poll-now error property=%s: %s", property_id, e)
