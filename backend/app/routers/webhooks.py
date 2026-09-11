"""
Cloudbeds reservation fan-out router + polling job.

Fan-out targets (per reservation):
  1. GHL CRM — upsert contact
  2. Meta CAPI — Purchase event (non-Website sources)
  3. Google Ads — offline conversion upload (non-Website sources)
  4. TikTok Events API — branches in config.TIKTOK_BRANCHES (Saigon, Osaka)

Trigger modes (per branch, chosen by settings.WEBHOOK_REALTIME_BRANCHES):
  A. Polling — APScheduler job runs every 10 min, calls getReservations for
     each branch still on this path and fans out whatever it has not seen.
  B. Realtime — Cloudbeds POSTs to /api/webhooks/cloudbeds, which queues the
     fan-out WEBHOOK_SETTLE_SECONDS later. Those branches also get a slow,
     wide safety-net poll, because push delivery is best-effort: a redeploy
     drops queued jobs and Cloudbeds stops retrying eventually.

Either way a reservation is marked seen only after its fan-out has run, so a
Cloudbeds fetch that fails is retried by the next pass rather than lost.

The poll window is enforced on our side, not Cloudbeds'. Verified 2026-09-11:
`dateCreatedFrom`/`dateCreatedTo` are not honoured — a 1-minute window and a
3-hour window return byte-identical pages for every property. The params are
still sent (correctly, in property-local time, so honouring them later is an
improvement rather than a break), but what actually bounds the work is the
client-side age check plus pagination that stops at the window's edge.
"""
import hashlib
import hmac
import logging
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request

from app.config import TIKTOK_BRANCHES, settings
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
RESERVATION_LIST_TIMEOUT = httpx.Timeout(30.0, connect=5.0)
RESERVATION_LIST_RETRY_DELAYS = (2, 5)
# Cloudbeds' list page size. A full page means "there is more behind this one".
PAGE_SIZE = 50
CLOUDBEDS_DT_FORMAT = "%Y-%m-%d %H:%M:%S"


def _poll_branches() -> list[tuple[str, str, str]]:
    """(branch, property_id, api_key) for every branch the poller covers."""
    return [
        ("saigon", settings.CB_PROPERTY_ID_SAIGON, settings.CB_API_KEY_SAIGON),
        ("taipei", settings.CB_PROPERTY_ID_TAIPEI, settings.CB_API_KEY_TAIPEI),
        ("1948", settings.CB_PROPERTY_ID_1948, settings.CB_API_KEY_1948),
        ("oani", settings.CB_PROPERTY_ID_OANI, settings.CB_API_KEY_OANI),
        ("osaka", settings.CB_PROPERTY_ID_OSAKA, settings.CB_API_KEY_OSAKA),
    ]


# ── Cloudbeds timestamps ─────────────────────────────────────────────────────
# Every date Cloudbeds hands back — `dateCreated` included — is in the
# property's local time, with no offset attached. The branch offset lives in
# config (`tz_offset_hours`), which is also what the Meta/Google/TikTok
# services add to build their event timestamps.

def _branch_tz_offset(branch: str) -> int:
    return int(settings.get_webhook_config_for_branch(branch)["tz_offset_hours"])


def _parse_local_dt(value) -> datetime | None:
    """Parse a Cloudbeds timestamp into a naive datetime in property-local time."""
    if not value:
        return None
    text = str(value)
    for fmt, length in ((CLOUDBEDS_DT_FORMAT, 19), ("%Y-%m-%d %H:%M", 16)):
        try:
            return datetime.strptime(text[:length], fmt)
        except (ValueError, TypeError):
            continue
    return None


def _reservation_created_utc(reservation: dict, branch: str) -> datetime | None:
    """When Cloudbeds created this reservation, as a UTC instant.

    Straight tz conversion — deliberately without the `event_time_extra_offset`
    the ad-platform services apply. That extra hour is a Make-era fudge for
    attribution windows; putting it in here would bend the measured lag.
    """
    local = _parse_local_dt(reservation.get("dateCreated"))
    if local is None:
        return None
    try:
        offset = _branch_tz_offset(branch)
    except Exception:
        return None
    return (local - timedelta(hours=offset)).replace(tzinfo=timezone.utc)


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
    # Captured before the four uploads run, so the logged lag measures Cloudbeds
    # → fan-out and not how long Meta happened to take on this row.
    reservation_created_at = _reservation_created_utc(reservation, branch)
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
                    # How many custom fields the write actually carried. A
                    # contact upsert reports success on names and phone alone,
                    # so without this a run that silently wrote no reservation
                    # data at all still showed up green.
                    "custom_fields": result.get("custom_fields"),
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

    # ── TikTok Events API — branches advertising on TikTok ───────────────────
    if branch in TIKTOK_BRANCHES:
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
                    currency=cfg["currency"],
                    tz_offset_hours=cfg["tz_offset_hours"],
                    event_time_extra_offset=cfg["event_time_extra_offset"],
                    phone_country_code=cfg["phone_country_code"],
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
        reservation_created_at=reservation_created_at,
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
    """Fetch a single reservation from Cloudbeds then fan out.

    The seen-check is repeated here because a poll can beat a queued push event
    to the same reservation during the settle delay. Marking happens on the way
    out: marking on the way in, as this used to, meant a failed Cloudbeds fetch
    took the reservation out of the running for the rest of the process's life
    — the poller skipped it ever after and the conversion never went up.
    """
    if webhook_log.has_seen(reservation_id):
        logger.info("Fan-out skipped reservation=%s — already processed", reservation_id)
        return
    reservation = _fetch_full_reservation(property_id, reservation_id)
    if not reservation:
        logger.warning(
            "Could not fetch reservation=%s property=%s — left for the next poll",
            reservation_id,
            property_id,
        )
        return
    _fan_out(property_id, reservation_id, reservation)
    webhook_log.mark_seen(reservation_id)


def _get_reservation_list(
    property_id: str,
    api_key: str,
    date_from: str,
    date_to: str,
    page_number: int = 1,
) -> tuple[httpx.Response, dict, int]:
    """Fetch a small reservation list, retrying transient Cloudbeds failures.

    The caller only needs reservation IDs, then fetches each new reservation in
    sequence.  Deliberately omit ``includeGuestList`` here: it makes the list
    response much larger without being used by the poller.
    """
    for attempt, delay in enumerate((*RESERVATION_LIST_RETRY_DELAYS, None), start=1):
        try:
            with httpx.Client(timeout=RESERVATION_LIST_TIMEOUT) as client:
                response = client.get(
                    f"{CLOUDBEDS_API_BASE}/getReservations",
                    headers={"Authorization": f"Bearer {api_key}"},
                    params={
                        "propertyID": property_id,
                        "dateCreatedFrom": date_from,
                        "dateCreatedTo": date_to,
                        "pageNumber": page_number,
                        "pageSize": PAGE_SIZE,
                    },
                )
            response.raise_for_status()
            return response, response.json(), attempt
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            if delay is None:
                raise
            logger.warning(
                "Cloudbeds list attempt %d failed property=%s; retrying in %ss: %s",
                attempt,
                property_id,
                delay,
                exc,
            )
            time.sleep(delay)

    raise RuntimeError("unreachable")


def _iter_reservation_pages(property_id: str, api_key: str, date_from: str, date_to: str):
    """Yield every Cloudbeds list page for a historical date-created window."""
    page_number = 1
    while True:
        _, body, _ = _get_reservation_list(
            property_id, api_key, date_from, date_to, page_number=page_number
        )
        if not body.get("success"):
            raise RuntimeError(body.get("message") or "Cloudbeds getReservations failed")
        reservations = body.get("data") or []
        if isinstance(reservations, dict):
            reservations = list(reservations.values())
        yield reservations
        if len(reservations) < PAGE_SIZE:
            return
        page_number += 1


# ── Polling jobs (called by APScheduler) ────────────────────────────────────

POLL_WINDOW_MINUTES = 60
SAFETY_NET_WINDOW_MINUTES = 90
# Pages walked per branch per pass before giving up. At PAGE_SIZE=50 that is
# 500 reservations inside one window — orders of magnitude more than any branch
# takes in an hour, so hitting the cap means something is wrong and says so.
MAX_POLL_PAGES = 10


def _branches_by_mode(realtime: bool) -> list[tuple[str, str, str]]:
    """Split the branch list by how its reservations arrive.

    A branch named in WEBHOOK_REALTIME_BRANCHES is driven by push events. The
    poller still visits it, but only on the slow, wide safety-net pass.
    """
    live = settings.webhook_realtime_branches
    return [row for row in _poll_branches() if (row[0] in live) == realtime]


def _poll_branch(
    branch: str,
    property_id: str,
    api_key: str,
    now_utc: datetime,
    minutes: int,
) -> int:
    """Fan out this branch's unseen reservations created in the last `minutes`.

    Walks pages until the window runs out. Before pagination this read page 1
    and stopped, which was silently lossy in exactly the case that matters: a
    burst of more than PAGE_SIZE reservations between two ticks pushed the
    oldest of them off the page, and since the next tick's page is newer still
    they were never fanned out at all — not delayed, dropped.

    Returns the number of reservations fanned out.
    """
    offset = _branch_tz_offset(branch)
    # Naive on purpose: these are compared against Cloudbeds' dateCreated, which
    # carries no offset and is already the property's local clock.
    now_local = now_utc.replace(tzinfo=None) + timedelta(hours=offset)
    cutoff_local = now_local - timedelta(minutes=minutes)
    date_from = cutoff_local.strftime(CLOUDBEDS_DT_FORMAT)
    # A few minutes of headroom on the upper bound: if Cloudbeds ever starts
    # honouring these params, a clock a little ahead of ours must not clip the
    # newest reservation — the one this whole job exists to catch.
    date_to = (now_local + timedelta(minutes=5)).strftime(CLOUDBEDS_DT_FORMAT)

    new_count = 0
    for page_number in range(1, MAX_POLL_PAGES + 1):
        _, body, _ = _get_reservation_list(
            property_id, api_key, date_from, date_to, page_number=page_number
        )
        if not body.get("success"):
            logger.warning(
                "getReservations failed property=%s page=%d: %s",
                property_id, page_number, body.get("message"),
            )
            return new_count

        reservations = body.get("data") or []
        if isinstance(reservations, dict):
            reservations = list(reservations.values())

        in_window = 0
        for res in reservations:
            created_local = _parse_local_dt(res.get("dateCreated"))
            # Age first, dedup second: an out-of-window row must not cost a
            # `has_seen` round-trip, and there are a lot of them behind a full
            # page. A row with an unreadable dateCreated is treated as in
            # window — dropping a real booking is the expensive mistake.
            if created_local is not None and created_local < cutoff_local:
                continue
            in_window += 1

            rid = str(res.get("reservationID", ""))
            if not rid or webhook_log.has_seen(rid):
                continue
            full = _fetch_full_reservation(property_id, rid)
            if not full:
                # Deliberately left unmarked — the next pass tries again.
                logger.warning("Poll: could not fetch reservation=%s branch=%s", rid, branch)
                continue
            new_count += 1
            logger.info("Poll: new reservation=%s branch=%s", rid, branch)
            _fan_out(property_id, rid, full)
            webhook_log.mark_seen(rid)

        # The list comes back newest-created first, so once a whole page falls
        # outside the window every page behind it does too.
        if in_window == 0 or len(reservations) < PAGE_SIZE:
            break
    else:
        logger.warning(
            "Poll branch=%s: hit the %d-page cap with the window still open — "
            "reservations older than that were not reached this pass",
            branch, MAX_POLL_PAGES,
        )

    return new_count


def poll_new_reservations(
    minutes: int = POLL_WINDOW_MINUTES,
    realtime_branches: bool = False,
) -> None:
    """Fan out reservations created in the last `minutes` that aren't seen yet.

    The default arguments are the every-10-minutes job over the polled
    branches; `realtime_branches=True` runs it over the push-driven ones.

    The window is six ticks wide on purpose. A reservation whose Cloudbeds
    fetch fails is left unmarked for a later pass, and a window only as wide as
    the tick gave that retry a single chance before the reservation aged out of
    it for good.
    """
    now_utc = datetime.now(timezone.utc)

    for branch, property_id, api_key in _branches_by_mode(realtime_branches):
        if not property_id or not api_key:
            continue
        try:
            new_count = _poll_branch(branch, property_id, api_key, now_utc, minutes)
            if new_count:
                logger.info("Poll branch=%s: processed %d new reservations", branch, new_count)
        except Exception as e:
            logger.error("Poll error branch=%s: %s", branch, e)


def poll_realtime_safety_net() -> None:
    """Hourly wide sweep over the push-driven branches.

    Push delivery is best-effort: a redeploy drops whatever settle-delay jobs
    were still pending, and Cloudbeds gives up retrying eventually. Re-walking
    a 90-minute window makes a missed event cost a delay, not a conversion.
    """
    poll_new_reservations(minutes=SAFETY_NET_WINDOW_MINUTES, realtime_branches=True)


# ── Routes ────────────────────────────────────────────────────────────────────

def _verify_signature(raw_body: bytes, signature: str | None) -> bool:
    """Fail closed.

    An unset secret used to mean "accept anything", which left an endpoint that
    fans out to four ad platforms open to whoever found the URL. A missing
    secret now rejects every push, so enabling realtime without configuring the
    secret fails loudly instead of quietly running unauthenticated.
    """
    secret = settings.CLOUDBEDS_WEBHOOK_SECRET
    if not secret or not signature:
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    # Cloudbeds' header format isn't pinned down, so accept the prefixed digest
    # and the bare hex rather than failing on a cosmetic difference.
    return any(
        hmac.compare_digest(candidate, signature)
        for candidate in (f"sha256={expected}", expected)
    )


def _payload_id(payload: dict, *keys: str) -> str:
    """Read an id from the payload root or from a nested `data` object."""
    nested = payload.get("data")
    for source in (payload, nested if isinstance(nested, dict) else {}):
        for key in keys:
            value = source.get(key)
            if value:
                return str(value)
    return ""


def _queue_fan_out(property_id: str, reservation_id: str):
    """Schedule the fan-out once the reservation has had time to settle.

    Returns the scheduled time, or None when there is no running scheduler to
    hand the job to (tests, or a startup where the scheduler failed to come up)
    — the caller then falls back to processing it immediately.
    """
    from apscheduler.triggers.date import DateTrigger

    from app.scheduler import scheduler

    if not scheduler.running:
        return None
    run_at = datetime.now(timezone.utc) + timedelta(
        seconds=max(0, settings.WEBHOOK_SETTLE_SECONDS)
    )
    scheduler.add_job(
        _process_reservation,
        trigger=DateTrigger(run_date=run_at),
        args=[str(property_id), str(reservation_id)],
        id=f"webhook_fanout_{reservation_id}",
        replace_existing=True,
        executor="default",
        misfire_grace_time=600,
    )
    return run_at


@router.post("/webhooks/cloudbeds")
async def cloudbeds_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_cloudbeds_signature: str | None = Header(default=None),
) -> dict:
    """Cloudbeds push endpoint — the realtime alternative to the 10-min poll.

    Cloudbeds gets its answer immediately; the fan-out itself waits out
    WEBHOOK_SETTLE_SECONDS so an OTA booking has its guest details attached
    before Meta, Google and TikTok are asked to match on them.
    """
    raw_body = await request.body()
    if not _verify_signature(raw_body, x_cloudbeds_signature):
        raise HTTPException(status_code=401, detail="Invalid signature")
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Expected a JSON object")

    property_id = _payload_id(payload, "propertyID", "property_id", "propertyId")
    reservation_id = _payload_id(payload, "reservationID", "reservation_id", "reservationId")
    if not property_id or not reservation_id:
        return {"success": True, "message": "skipped — missing IDs"}
    if webhook_log.has_seen(reservation_id):
        return {"success": True, "message": "already processed"}

    run_at = _queue_fan_out(property_id, reservation_id)
    if run_at is None:
        background_tasks.add_task(_process_reservation, property_id, reservation_id)
        return {"success": True, "message": "queued"}
    return {"success": True, "message": f"queued for {run_at.isoformat()}"}


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

    # Is APScheduler even alive? If the job is missing or has no next run, the
    # poll has not been firing at all and no per-branch result below matters.
    job = scheduler.get_job("cloudbeds_reservation_poll")
    safety_job = scheduler.get_job("cloudbeds_realtime_safety_net")
    realtime = sorted(settings.webhook_realtime_branches)
    scheduler_state = {
        "running": scheduler.running,
        "poll_job_registered": job is not None,
        "poll_job_next_run": job.next_run_time.isoformat() if job and job.next_run_time else None,
        "safety_net_next_run": (
            safety_job.next_run_time.isoformat()
            if safety_job and safety_job.next_run_time
            else None
        ),
        # Realtime canary state. Without these three the monitor cannot tell a
        # branch that is quiet from one whose pushes are being rejected.
        "realtime_branches": realtime,
        "realtime_secret_set": bool(settings.CLOUDBEDS_WEBHOOK_SECRET),
        "settle_seconds": settings.WEBHOOK_SETTLE_SECONDS,
        "pending_fan_outs": sum(
            1 for j in scheduler.get_jobs() if str(j.id).startswith("webhook_fanout_")
        ),
    }

    branches = []
    for branch, property_id, api_key in _poll_branches():
        entry = {
            "branch": branch,
            "mode": "realtime" if branch in realtime else "poll",
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

        # Diagnose exactly the same Cloudbeds v1.2 request the poller uses —
        # including its per-branch, property-local window.
        offset = _branch_tz_offset(branch)
        now_local = now_utc.replace(tzinfo=None) + timedelta(hours=offset)
        cutoff_local = now_local - timedelta(minutes=minutes)
        date_from = cutoff_local.strftime(CLOUDBEDS_DT_FORMAT)
        date_to = (now_local + timedelta(minutes=5)).strftime(CLOUDBEDS_DT_FORMAT)
        entry["tz_offset_hours"] = offset
        entry["window_local"] = {"from": date_from, "to": date_to}

        entry["versions"] = {}
        for version in ("v1.2",):
            try:
                resp, body, attempts = _get_reservation_list(property_id, api_key, date_from, date_to)
                reservations = body.get("data") or []
                if isinstance(reservations, dict):
                    reservations = list(reservations.values())
                rids = [str(r.get("reservationID", "")) for r in reservations]
                rids = [r for r in rids if r]
                created = [_parse_local_dt(r.get("dateCreated")) for r in reservations]
                in_window = sum(1 for c in created if c is None or c >= cutoff_local)
                entry["versions"][version] = {
                    "status_code": resp.status_code,
                    "api_success": body.get("success"),
                    "message": body.get("message"),
                    "attempts": attempts,
                    "returned": len(rids),
                    # Cloudbeds does not honour dateCreatedFrom/To (verified
                    # 2026-09-11: a 1-minute and a 3-hour window come back
                    # identical). `returned` is therefore whatever page 1 holds;
                    # `in_window` is what the poller would actually act on, and
                    # the gap between the two is that bug, visible.
                    "in_window": in_window,
                    "server_side_filter_honoured": len(rids) < PAGE_SIZE and in_window == len(rids),
                    "page_full": len(rids) >= PAGE_SIZE,
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
            # Per-branch windows differ by tz offset — each branch carries its
            # own under `window_local`.
            "window": {"minutes": minutes, "now_utc": now_utc.strftime(CLOUDBEDS_DT_FORMAT)},
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


@router.post("/admin/reservation-backfill")
async def reservation_backfill(
    background_tasks: BackgroundTasks,
    date_from: str,
    date_to: str,
    _admin=Depends(require_admin),
) -> dict:
    """Re-send all reservations created within an inclusive historical window.

    This deliberately bypasses the normal seen check. It is for recovery after
    an outage; downstream event IDs and CRM upserts make repeated delivery safe.
    """
    try:
        vietnam_tz = ZoneInfo("Asia/Ho_Chi_Minh")
        start = datetime.strptime(date_from, "%Y-%m-%d").replace(tzinfo=vietnam_tz)
        end = datetime.strptime(date_to, "%Y-%m-%d").replace(
            hour=23, minute=59, second=59, tzinfo=vietnam_tz
        )
    except ValueError:
        raise HTTPException(status_code=422, detail="date_from/date_to must use YYYY-MM-DD")
    if end < start or end - start > timedelta(days=7):
        raise HTTPException(status_code=422, detail="Choose an inclusive window from 1 to 7 days")

    background_tasks.add_task(
        _backfill_reservations,
        start.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        end.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
    )
    return {"success": True, "message": f"Backfill queued for {date_from} through {date_to}"}


def _backfill_reservations(date_from: str, date_to: str) -> None:
    """Fetch every page and fan out one reservation at a time."""
    for branch, property_id, api_key in _poll_branches():
        if not property_id or not api_key:
            logger.warning("Backfill skipped branch=%s: missing Cloudbeds config", branch)
            continue

        processed = 0
        try:
            for reservations in _iter_reservation_pages(property_id, api_key, date_from, date_to):
                for reservation in reservations:
                    reservation_id = str(reservation.get("reservationID") or "")
                    if not reservation_id:
                        continue
                    # Do not use _mark_seen: this is an explicit recovery run.
                    full = _fetch_full_reservation(property_id, reservation_id)
                    if not full:
                        continue
                    _fan_out(property_id, reservation_id, full)
                    processed += 1
            logger.info("Backfill complete branch=%s reservations=%d", branch, processed)
        except Exception:
            logger.exception("Backfill failed branch=%s after %d reservations", branch, processed)


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
            _, body, _ = _get_reservation_list(property_id, api_key, date_from, date_to)
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
