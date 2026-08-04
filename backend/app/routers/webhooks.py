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

# The configured property credentials use the stable v1.2 API. v1.3 times out
# for several properties, so it left scheduled polls without usable results.
CLOUDBEDS_API_BASE = "https://hotels.cloudbeds.com/api/v1.2"
WEBSITE_SOURCES = {"website", "booking engine"}


def _poll_branches() -> list[tuple[str, str, str]]:
    """(branch, property_id, api_key) for every branch the poller covers."""
    return [
        ("saigon", settings.CB_PROPERTY_ID_SAIGON, settings.CB_API_KEY_SAIGON),
        ("taipei", settings.CB_PROPERTY_ID_TAIPEI, settings.CB_API_KEY_TAIPEI),
        ("1948", settings.CB_PROPERTY_ID_1948, settings.CB_API_KEY_1948),
        ("oani", settings.CB_PROPERTY_ID_OANI, settings.CB_API_KEY_OANI),
        ("osaka", settings.CB_PROPERTY_ID_OSAKA, settings.CB_API_KEY_OSAKA),
    ]

def _mark_seen(reservation_id: str) -> bool:
    """Return True if already seen (duplicate). Otherwise mark and return False.

    Backed by webhook_events rather than a process-local set, so a redeploy no
    longer makes the next poll re-fan-out everything inside its window.
    """
    if webhook_log.has_seen(reservation_id):
        return True
    webhook_log.mark_seen(reservation_id)
    return False


# ── Core fan-out (shared by polling + webhook paths) ─────────────────────────

def _meta_error_message(result: dict) -> str | None:
    """Pull Meta's human-readable error out of the Graph API error envelope."""
    err = (result.get("response") or {}).get("error") or {}
    return err.get("error_user_msg") or err.get("message")


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
            action = result["action"]
            if action == "skipped":
                # No guest email — nothing to upsert. A deliberate skip, not a
                # failure; counting it as one drowns the failure filter.
                ghl_log = {"success": None, "action": "skipped_no_email"}
            else:
                ghl_log = {
                    "success": action in ("created", "updated"),
                    "action": action,
                    "error": result.get("error"),
                }
        except Exception as e:
            logger.error("GHL upsert error branch=%s reservation=%s: %s", branch, reservation_id, e)
            ghl_log = {"success": False, "error": str(e)}
    else:
        ghl_log = {"success": None, "action": "skipped_no_config"}

    # ── Meta CAPI Purchase event ─────────────────────────────────────────────
    # Website bookings already fire Purchase from the browser pixel; sending the
    # offline event too would double-count them.
    if is_website_source:
        meta_log = {"success": None, "action": "skipped_website_source"}
    elif not (cfg["meta_pixel_id"] and cfg["meta_access_token"]):
        meta_log = {"success": None, "action": "skipped_no_config"}
    else:
        try:
            result = send_purchase_event(
                reservation=reservation,
                pixel_id=cfg["meta_pixel_id"],
                access_token=cfg["meta_access_token"],
                currency=cfg["currency"],
                tz_offset_hours=cfg["tz_offset_hours"],
                event_time_extra_offset=cfg["event_time_extra_offset"],
                phone_country_code=cfg["phone_country_code"],
            )
            if webhook_log.is_nothing_to_send(result):
                meta_log = {"success": None, "action": "skipped_no_user_data"}
            else:
                meta_log = {
                    "success": result["success"],
                    "action": "purchase" if result["success"] else None,
                    "error": None if result["success"] else (
                        result.get("error") or _meta_error_message(result)
                    ),
                }
        except Exception as e:
            logger.error("Meta CAPI error branch=%s reservation=%s: %s", branch, reservation_id, e)
            meta_log = {"success": False, "error": str(e)}

    # ── Google Ads offline conversion (Data Manager API) ─────────────────────
    # Website-sourced bookings already convert through the site tag, so only
    # non-Website sources are uploaded — same gate as the original Make flow.
    if is_website_source:
        gads_log = {"success": None, "action": "skipped_website_source"}
    elif not (cfg["google_ads_refresh_token"] and cfg["google_ads_customer_id"]):
        gads_log = {"success": None, "action": "skipped_no_config"}
    else:
        try:
            result = upload_offline_conversion(
                reservation=reservation,
                customer_id=cfg["google_ads_customer_id"],
                client_id=settings.datamanager_client_id,
                client_secret=settings.datamanager_client_secret,
                refresh_token=cfg["google_ads_refresh_token"],
                conversion_action_single=cfg["google_ads_conversion_single"],
                conversion_action_both=cfg["google_ads_conversion_both"],
                conversion_action_phone=cfg["google_ads_conversion_phone"],
                login_customer_id=settings.GOOGLE_LOGIN_CUSTOMER_ID,
                currency=cfg["currency"],
                tz_offset_hours=cfg["tz_offset_hours"],
                event_time_extra_offset=cfg["event_time_extra_offset"],
                phone_country_code=cfg["phone_country_code"],
            )
            if webhook_log.is_nothing_to_send(result):
                gads_log = {"success": None, "action": "skipped_no_identifiers"}
            else:
                gads_log = {
                    "success": result["success"],
                    "action": result.get("case"),
                    "status_code": result.get("status_code"),
                    "error": None if result["success"] else result.get("error"),
                }
        except Exception as e:
            logger.error("Google Ads upload error branch=%s reservation=%s: %s", branch, reservation_id, e)
            gads_log = {"success": False, "error": str(e)}

    # ── TikTok Events API — Saigon only ──────────────────────────────────────
    if branch == "saigon":
        if is_website_source:
            tiktok_log = {"success": None, "action": "skipped_website_source"}
        elif not (cfg["tiktok_access_token"] and cfg["tiktok_event_source_id"]):
            tiktok_log = {"success": None, "action": "skipped_no_config"}
        else:
            try:
                result = send_complete_payment_event(
                    reservation=reservation,
                    access_token=cfg["tiktok_access_token"],
                    event_source_id=cfg["tiktok_event_source_id"],
                )
                if webhook_log.is_nothing_to_send(result):
                    tiktok_log = {"success": None, "action": "skipped_no_user_data"}
                else:
                    tiktok_log = {
                        "success": result["success"],
                        "action": "complete_payment" if result["success"] else None,
                        # TikTok returns message="OK" on success — only a failure
                        # should put anything in the error column.
                        "error": None if result["success"] else (
                            result.get("error") or (result.get("response") or {}).get("message")
                        ),
                    }
            except Exception as e:
                logger.error("TikTok CAPI error reservation=%s: %s", reservation_id, e)
                tiktok_log = {"success": False, "error": str(e)}

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
    days: int | None = None,
    failures_only: bool = False,
    _admin=Depends(require_admin),
) -> dict:
    """Return recent webhook processing results. Admin only.

    History is kept for webhook_log.RETENTION_DAYS; `days` narrows within that.
    """
    events = webhook_log.get_events(
        branch=branch,
        limit=min(limit, 500),
        days=days,
        failures_only=failures_only,
    )
    # Mask guest email before returning — show j***@gmail.com
    for ev in events:
        ev["guest_email"] = _mask_email(ev.get("guest_email", ""))
    return {"success": True, "data": events, "total": len(events)}


def _mask_email(email: str) -> str:
    if not email or "@" not in email:
        return email
    local, domain = email.split("@", 1)
    return local[:2] + "***@" + domain


@router.get("/admin/poll-diagnostic")
async def poll_diagnostic(
    minutes: int = 60,
    _admin=Depends(require_admin),
) -> dict:
    """Report why the poller is or isn't producing events, per branch.

    `poll_new_reservations` swallows every failure into a log line and stays
    silent when a window is simply empty, so from the monitor a dead API key, a
    rejected API version and a quiet hour all look identical: no rows. This runs
    the same getReservations request per branch and returns what actually came
    back. It fans nothing out and writes nothing — safe to run any time.
    """
    from app.scheduler import scheduler

    now_utc = datetime.now(timezone.utc)
    from_dt = now_utc - timedelta(minutes=minutes)
    date_from = from_dt.strftime("%Y-%m-%d %H:%M:%S")
    date_to = now_utc.strftime("%Y-%m-%d %H:%M:%S")

    # Is APScheduler even alive? If the job is missing or has no next run, the
    # poll has not been firing at all and no per-branch result below matters.
    job = scheduler.get_job("cloudbeds_reservation_poll")
    scheduler_state = {
        "running": scheduler.running,
        "poll_job_registered": job is not None,
        "poll_job_next_run": job.next_run_time.isoformat() if job and job.next_run_time else None,
    }

    branches = []
    for branch, property_id, api_key in _poll_branches():
        entry = {
            "branch": branch,
            "property_id": property_id or None,
            # Never echo the key itself — presence and length are enough to tell
            # "missing" apart from "present but rejected".
            "api_key_present": bool(api_key),
            "api_key_length": len(api_key) if api_key else 0,
        }
        if not property_id or not api_key:
            # The poller's silent `continue` — the case that leaves no trace.
            entry["skipped"] = "missing property_id or api_key"
            branches.append(entry)
            continue

        # The poller talks v1.3; the rest of the app talks v1.2. If a property's
        # credentials are only authorized for one of them, the split shows here.
        entry["versions"] = {}
        for version in ("v1.3", "v1.2"):
            try:
                with httpx.Client(timeout=20) as client:
                    resp = client.get(
                        f"https://hotels.cloudbeds.com/api/{version}/getReservations",
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
                reservations = body.get("data") or []
                if isinstance(reservations, dict):
                    reservations = list(reservations.values())
                rids = [str(r.get("reservationID", "")) for r in reservations]
                rids = [r for r in rids if r]
                entry["versions"][version] = {
                    "status_code": resp.status_code,
                    "api_success": body.get("success"),
                    "message": body.get("message"),
                    "returned": len(rids),
                    # A window full of already-processed reservations produces no
                    # new rows either — same blank monitor, different cause.
                    "already_seen": sum(1 for r in rids if webhook_log.has_seen(r)),
                    "sample_ids": rids[:5],
                }
            except Exception as e:
                entry["versions"][version] = {"error": f"{type(e).__name__}: {e}"}

        branches.append(entry)

    return {
        "success": True,
        "data": {
            "window": {"from": date_from, "to": date_to, "minutes": minutes},
            "scheduler": scheduler_state,
            "branches": branches,
        },
    }


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
