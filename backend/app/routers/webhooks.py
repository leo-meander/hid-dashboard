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

    # Resolve branch from property ID
    branch = settings.cloudbeds_property_to_branch.get(str(property_id))
    if not branch:
        logger.error("Unknown propertyID=%s — no branch mapping", property_id)
        return

    cfg = settings.get_webhook_config_for_branch(branch)
    source = (reservation.get("source") or "").lower()
    is_website_source = any(kw in source for kw in WEBSITE_SOURCES)

    # ── Flow 1: GHL CRM upsert ────────────────────────────────────────────────
    if cfg["ghl_location_id"] and cfg["ghl_api_key"]:
        try:
            result = upsert_contact_from_reservation(
                reservation=reservation,
                location_id=cfg["ghl_location_id"],
                api_key=cfg["ghl_api_key"],
            )
            logger.info("GHL upsert branch=%s action=%s contact_id=%s", branch, result["action"], result["contact_id"])
        except Exception as e:
            logger.error("GHL upsert error branch=%s reservation=%s: %s", branch, reservation_id, e)
    else:
        logger.info("GHL skipped branch=%s — no GHL config", branch)

    # ── Flow 2: Meta CAPI + Google Ads (non-website sources only) ────────────
    if is_website_source:
        logger.info("Meta/Google Ads skipped — source is Website (%s)", source)
        return

    if cfg["meta_pixel_id"] and cfg["meta_access_token"]:
        try:
            meta_result = send_purchase_event(
                reservation=reservation,
                pixel_id=cfg["meta_pixel_id"],
                access_token=cfg["meta_access_token"],
            )
            logger.info("Meta CAPI branch=%s success=%s", branch, meta_result.get("success"))
        except Exception as e:
            logger.error("Meta CAPI error branch=%s reservation=%s: %s", branch, reservation_id, e)
    else:
        logger.info("Meta CAPI skipped branch=%s — no pixel config", branch)

    if (
        cfg["google_ads_customer_id"]
        and cfg["google_ads_conversion_single"]
        and settings.GOOGLE_ADS_DEVELOPER_TOKEN
        and settings.GOOGLE_REFRESH_TOKEN
    ):
        try:
            gads_result = upload_offline_conversion(
                reservation=reservation,
                customer_id=cfg["google_ads_customer_id"],
                developer_token=settings.GOOGLE_ADS_DEVELOPER_TOKEN,
                client_id=settings.GOOGLE_CLIENT_ID,
                client_secret=settings.GOOGLE_CLIENT_SECRET,
                refresh_token=settings.GOOGLE_REFRESH_TOKEN,
                conversion_action_single=cfg["google_ads_conversion_single"],
                conversion_action_both=cfg["google_ads_conversion_both"],
            )
            logger.info("Google Ads branch=%s case=%s success=%s", branch, gads_result.get("case"), gads_result.get("success"))
        except Exception as e:
            logger.error("Google Ads error branch=%s reservation=%s: %s", branch, reservation_id, e)
    else:
        logger.info("Google Ads skipped branch=%s — no config", branch)


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
