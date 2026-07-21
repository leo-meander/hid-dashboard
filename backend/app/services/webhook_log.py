"""
In-memory ring buffer for webhook processing results.

Stores the last MAX_EVENTS reservation processing outcomes in RAM.
Cleared on process restart — intended for short-term admin monitoring only.
"""
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Optional

MAX_EVENTS = 500

_events: deque = deque(maxlen=MAX_EVENTS)


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
    """Append one webhook processing result to the ring buffer."""
    _events.appendleft({
        "id": str(uuid.uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "reservation_id": reservation_id,
        "branch": branch,
        "guest_email": guest_email,
        "source": source,
        "ghl": ghl,
        "meta": meta,
        "google_ads": google_ads,
        "tiktok": tiktok,
    })


def get_events(branch: Optional[str] = None, limit: int = 100) -> list:
    events = list(_events)
    if branch:
        events = [e for e in events if e["branch"] == branch]
    return events[:limit]


def clear() -> int:
    count = len(_events)
    _events.clear()
    return count
