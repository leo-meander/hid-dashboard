"""
Cloudbeds inbound webhook router.

Receives reservation events from Cloudbeds and fans out to:
  1. GHL CRM — upsert contact (Flow 1: CB New Customer → GHL)
  2. Meta CAPI — Purchase event (Flow 2, non-Website sources)
  3. Google Ads — offline conversion upload (Flow 2, non-Website sources)

Endpoint: POST /api/webhooks/cloudbeds

Cloudbeds sends a webhook body with at minimum:
  { "propertyID": "...", "reservationID": "...", "type": "reservation/new", ... }

To register: go to Cloudbeds → Settings → Webhooks → New Webhook,
set URL to https://<your-hid>/api/webhooks/cloudbeds, select
"New Reservation" event, copy the secret into CLOUDBEDS_WEBHOOK_SECRET.
"""
import hashlib
import hmac
import logging

import httpx
from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request

from app.config import settings
from app.services.ghl_crm_service import upsert_contact_from_reservation
from app.services.google_ads_service import upload_offline_conversion
from app.services.meta_capi_service import send_purchase_event
from app.services.tiktok_capi_service import send_complete_payment_event
from app.services import webhook_log

logger = logging.getLogger(__name__)
router = APIRouter()

CLOUDBEDS_API_BASE = "https://hotels.cloudbeds.com/api/v1.3"

# Sources that should NOT be forwarded to Meta/Google Ads (booking engine / direct website)
WEBSITE_SOURCES = {"website", "booking engine"}


def _verify_signature(raw_body: bytes, signature: str | None) -> bool:
    """Verify Cloudbeds HMAC-SHA256 webhook signature. Skip if no secret configured."""
    secret = settings.CLOUDBEDS_WEBHOOK_SECRET
    if not secret:
        return True  # secret not configured → accept all (dev mode)
    if not signature:
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", signature)


def _fetch_reservation(property_id: str, reservation_id: str) -> dict | None:
    """Call Cloudbeds /v1.3/getReservation and return the data dict."""
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
                logger.error("Cloudbeds getReservation failed: %s", body.get("message"))
                return None
            return body.get("data")
    except Exception as e:
        logger.error("Error fetching reservation %s from Cloudbeds: %s", reservation_id, e)
        return None


def _process_reservation(property_id: str, reservation_id: str) -> None:
    """Background task: fetch reservation details and fan out to GHL + Meta + Google Ads."""
    logger.info("Processing Cloudbeds webhook property=%s reservation=%s", property_id, reservation_id)

    reservation = _fetch_reservation(property_id, reservation_id)
    if not reservation:
        logger.error("Could not fetch reservation %s — aborting", reservation_id)
        return

    branch = settings.cloudbeds_property_to_branch.get(str(property_id))
    if not branch:
        logger.error("Unknown propertyID=%s — no branch mapping", property_id)
        return

    cfg = settings.get_webhook_config_for_branch(branch)
    source = (reservation.get("source") or "").lower()
    is_website_source = any(kw in source for kw in WEBSITE_SOURCES)
    guest_email = (reservation.get("guestEmail") or "").strip().lower()

    ghl_log = meta_log = gads_log = tiktok_log = None

    # ── Flow 1: GHL CRM upsert ────────────────────────────────────────────────
    if cfg["ghl_location_id"] and cfg["ghl_api_key"]:
        try:
            result = upsert_contact_from_reservation(
                reservation=reservation,
                location_id=cfg["ghl_location_id"],
                api_key=cfg["ghl_api_key"],
                branch=branch,
            )
            logger.info("GHL upsert branch=%s action=%s contact_id=%s", branch, result["action"], result["contact_id"])
            ghl_log = {"success": result["action"] in ("created", "updated"), "action": result["action"]}
        except Exception as e:
            logger.error("GHL upsert error branch=%s reservation=%s: %s", branch, reservation_id, e)
            ghl_log = {"success": False, "error": str(e)}
    else:
        ghl_log = {"success": None, "action": "skipped_no_config"}

    # ── Flow 2: Meta CAPI + Google Ads (non-website sources only) ────────────
    if is_website_source:
        logger.info("Meta/Google Ads skipped — source is Website (%s)", source)
        meta_log = {"success": None, "action": "skipped_website_source"}
        gads_log = {"success": None, "action": "skipped_website_source"}
    else:
        if cfg["meta_pixel_id"] and cfg["meta_access_token"]:
            try:
                meta_result = send_purchase_event(
                    reservation=reservation,
                    pixel_id=cfg["meta_pixel_id"],
                    access_token=cfg["meta_access_token"],
                    currency=cfg["currency"],
                    tz_offset_hours=cfg["tz_offset_hours"],
                    event_time_extra_offset=cfg["event_time_extra_offset"],
                )
                ok = meta_result.get("success", False)
                logger.info("Meta CAPI branch=%s success=%s", branch, ok)
                meta_log = {"success": ok, "error": meta_result.get("error")}
            except Exception as e:
                logger.error("Meta CAPI error branch=%s reservation=%s: %s", branch, reservation_id, e)
                meta_log = {"success": False, "error": str(e)}
        else:
            meta_log = {"success": None, "action": "skipped_no_config"}

        if (
            cfg["google_ads_customer_id"]
            and cfg["google_ads_conversion_single"]
            and settings.GOOGLE_DEVELOPER_TOKEN
            and settings.GOOGLE_REFRESH_TOKEN
        ):
            try:
                gads_result = upload_offline_conversion(
                    reservation=reservation,
                    customer_id=cfg["google_ads_customer_id"],
                    developer_token=settings.GOOGLE_DEVELOPER_TOKEN,
                    client_id=settings.GOOGLE_CLIENT_ID,
                    client_secret=settings.GOOGLE_CLIENT_SECRET,
                    refresh_token=settings.GOOGLE_REFRESH_TOKEN,
                    conversion_action_single=cfg["google_ads_conversion_single"],
                    conversion_action_both=cfg["google_ads_conversion_both"],
                    login_customer_id=settings.GOOGLE_LOGIN_CUSTOMER_ID,
                    currency=cfg["currency"],
                    tz_offset_hours=cfg["tz_offset_hours"],
                    event_time_extra_offset=cfg["event_time_extra_offset"],
                    conversion_action_phone=cfg.get("google_ads_conversion_phone", ""),
                )
                ok = gads_result.get("success", False)
                logger.info("Google Ads branch=%s case=%s success=%s", branch, gads_result.get("case"), ok)
                gads_log = {
                    "success": ok,
                    "case": gads_result.get("case"),
                    "error": gads_result.get("error") or gads_result.get("partial_failure_error"),
                }
            except Exception as e:
                logger.error("Google Ads error branch=%s reservation=%s: %s", branch, reservation_id, e)
                gads_log = {"success": False, "error": str(e)}
        else:
            gads_log = {"success": None, "action": "skipped_no_config"}

    # ── TikTok Events API — Saigon only ──────────────────────────────────────
    if branch == "saigon" and cfg.get("tiktok_access_token") and cfg.get("tiktok_event_source_id"):
        try:
            tt_result = send_complete_payment_event(
                reservation=reservation,
                access_token=cfg["tiktok_access_token"],
                event_source_id=cfg["tiktok_event_source_id"],
            )
            ok = tt_result.get("success", False)
            logger.info("TikTok CAPI branch=%s success=%s", branch, ok)
            tiktok_log = {"success": ok, "error": tt_result.get("error")}
        except Exception as e:
            logger.error("TikTok CAPI error branch=%s reservation=%s: %s", branch, reservation_id, e)
            tiktok_log = {"success": False, "error": str(e)}
    elif branch == "saigon":
        logger.info("TikTok CAPI skipped — no TIKTOK_ACCESS_TOKEN_SAIGON or TIKTOK_EVENT_SOURCE_ID_SAIGON")
        tiktok_log = {"success": None, "action": "skipped_no_config"}

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


@router.post("/webhooks/cloudbeds")
async def cloudbeds_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_cloudbeds_signature: str | None = Header(default=None),
) -> dict:
    """
    Receive a Cloudbeds reservation webhook and fan out to GHL/Meta/Google Ads.
    Always returns 200 immediately; processing happens in the background.
    """
    raw_body = await request.body()

    if not _verify_signature(raw_body, x_cloudbeds_signature):
        logger.warning("Cloudbeds webhook signature mismatch — rejected")
        raise HTTPException(status_code=401, detail="Invalid signature")

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    property_id = str(payload.get("propertyID") or payload.get("property_id") or "")
    reservation_id = str(payload.get("reservationID") or payload.get("reservation_id") or "")

    if not property_id or not reservation_id:
        logger.warning("Cloudbeds webhook missing propertyID or reservationID: %s", payload)
        return {"success": True, "message": "skipped — missing IDs"}

    event_type = payload.get("type") or payload.get("event") or ""
    logger.info("Cloudbeds webhook received type=%s property=%s reservation=%s", event_type, property_id, reservation_id)

    background_tasks.add_task(_process_reservation, property_id, reservation_id)

    return {"success": True, "message": "queued"}


@router.get("/admin/webhook-events")
async def get_webhook_events(branch: str | None = None, limit: int = 100) -> dict:
    """Return recent webhook processing results from the in-memory ring buffer."""
    events = webhook_log.get_events(branch=branch, limit=min(limit, 500))
    return {"success": True, "data": events, "total": len(events)}
