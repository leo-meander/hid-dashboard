"""
Persistent log of reservation fan-out results.

Was an in-memory ring buffer, which every Zeabur deploy wiped — repeatedly
losing the history right when it was needed to confirm a fix had landed. Rows
now live in `webhook_events` and a nightly job prunes anything older than
RETENTION_DAYS.

Writes must never break the fan-out: a failure to log is logged and swallowed,
because losing a monitor row is much cheaper than losing the conversion upload
that row was describing.
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.database import SessionLocal
from app.models.webhook_event import WebhookEvent

logger = logging.getLogger(__name__)

RETENTION_DAYS = 7

# Fast path for the dedup check. The DB is the source of truth (it survives
# restarts), but a repeat poll inside one process lifetime shouldn't spend a
# round-trip per reservation — the connection pool is only 8 wide.
_seen_cache: set = set()
_SEEN_CACHE_MAX = 5000

SERVICES = ("ghl", "meta", "google_ads", "tiktok")


def _is_failure(result: Optional[dict]) -> bool:
    """A service failed only if it reported success=False.

    success=None means skipped (no config, website source) — not a failure, and
    it must not light up the failure filter.
    """
    return bool(result) and result.get("success") is False


# Service outcomes that mean "there was nothing to send", not "the send
# failed". The upload services report these as success=False because from
# their side nothing was delivered, but a reservation carrying no contact
# details is routine — walk-ins and OTA bookings with masked guests do it
# constantly. Logged as failures they buried the real incidents: 51 rows
# behind the failure filter, almost none of them worth looking at.
_NOTHING_TO_SEND = {"no_user_data", "skipped_no_identifiers"}


def is_nothing_to_send(result: dict) -> bool:
    """True when a service had no data to send, as opposed to failing to send."""
    if not result:
        return False
    return (
        result.get("error") in _NOTHING_TO_SEND
        or str(result.get("case") or "").startswith("skipped")
    )


def record(
    reservation_id: str,
    branch: str,
    guest_email: str,
    source: str,
    ghl: Optional[dict] = None,
    meta: Optional[dict] = None,
    google_ads: Optional[dict] = None,
    tiktok: Optional[dict] = None,
) -> None:
    """Persist one reservation processing result."""
    results = {"ghl": ghl, "meta": meta, "google_ads": google_ads, "tiktok": tiktok}
    db = SessionLocal()
    try:
        db.add(
            WebhookEvent(
                reservation_id=str(reservation_id),
                branch=branch,
                guest_email=guest_email,
                source=source,
                has_failure=any(_is_failure(r) for r in results.values()),
                **results,
            )
        )
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error("webhook_log: failed to persist reservation=%s: %s", reservation_id, e)
    finally:
        db.close()


def has_seen(reservation_id: str) -> bool:
    """True if this reservation was already fanned out (survives restarts)."""
    rid = str(reservation_id)
    if rid in _seen_cache:
        return True
    db = SessionLocal()
    try:
        exists = (
            db.query(WebhookEvent.id)
            .filter(WebhookEvent.reservation_id == rid)
            .first()
            is not None
        )
    except Exception as e:
        # Fail open: re-processing is cheap (Meta dedupes on event_id, Google
        # on transactionId, TikTok on event_id), skipping a real booking is not.
        logger.error("webhook_log: dedup lookup failed reservation=%s: %s", rid, e)
        return False
    finally:
        db.close()

    if exists:
        _remember(rid)
    return exists


def _remember(reservation_id: str) -> None:
    if len(_seen_cache) >= _SEEN_CACHE_MAX:
        _seen_cache.clear()
    _seen_cache.add(str(reservation_id))


def mark_seen(reservation_id: str) -> None:
    """Add to the in-process cache so the same poll window doesn't re-check."""
    _remember(reservation_id)


def get_events(
    branch: Optional[str] = None,
    limit: int = 100,
    days: Optional[int] = None,
    failures_only: bool = False,
) -> list:
    """Most recent events first, optionally filtered by branch/age/outcome."""
    db = SessionLocal()
    try:
        q = db.query(WebhookEvent)
        if branch:
            q = q.filter(WebhookEvent.branch == branch)
        if days:
            cutoff = datetime.now(timezone.utc) - timedelta(days=days)
            q = q.filter(WebhookEvent.created_at >= cutoff)
        if failures_only:
            q = q.filter(WebhookEvent.has_failure.is_(True))
        rows = q.order_by(WebhookEvent.created_at.desc()).limit(limit).all()
        return [
            {
                "id": str(r.id),
                "timestamp": r.created_at.isoformat() if r.created_at else None,
                "reservation_id": r.reservation_id,
                "branch": r.branch,
                "guest_email": r.guest_email,
                "source": r.source,
                "ghl": r.ghl,
                "meta": r.meta,
                "google_ads": r.google_ads,
                "tiktok": r.tiktok,
            }
            for r in rows
        ]
    except Exception as e:
        logger.error("webhook_log: query failed: %s", e)
        return []
    finally:
        db.close()


def purge_old(retention_days: int = RETENTION_DAYS) -> int:
    """Delete events older than the retention window. Returns rows removed."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    db = SessionLocal()
    try:
        deleted = (
            db.query(WebhookEvent)
            .filter(WebhookEvent.created_at < cutoff)
            .delete(synchronize_session=False)
        )
        db.commit()
        if deleted:
            logger.info("webhook_log: purged %d events older than %dd", deleted, retention_days)
        return deleted
    except Exception as e:
        db.rollback()
        logger.error("webhook_log: purge failed: %s", e)
        return 0
    finally:
        db.close()


def clear() -> int:
    """Delete every event. Admin escape hatch."""
    db = SessionLocal()
    try:
        count = db.query(WebhookEvent).delete(synchronize_session=False)
        db.commit()
        _seen_cache.clear()
        return count
    except Exception as e:
        db.rollback()
        logger.error("webhook_log: clear failed: %s", e)
        return 0
    finally:
        db.close()
