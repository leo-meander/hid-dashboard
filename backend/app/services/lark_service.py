"""Lark Base API client — fetches task records for Designer KPI auto-actuals.

KPIs derived from tasks where PIC contains "Nora":
  design_assets    → sum of Ads-only_Number of images (Completed tasks, branch-filtered)
  videos_delivered → sum of Ads-only_Number of video  (Completed tasks, branch-filtered)
  delivery_rate    → % Completed tasks with On-time vs Original = "On-time"

Branch detected from task name prefix: [1948], [Taipei], [Oani], [Osaka], [Saigon]/[SGN]/[Sai Gon]
Month detected from Complete date (fallback: Deadline).
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime
from typing import Optional

import requests

from app.config import settings

log = logging.getLogger(__name__)

_LARK_AUTH_URL = "https://open.larksuite.com/open-apis/auth/v3/tenant_access_token/internal"
_LARK_RECORDS_URL = "https://open.larksuite.com/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records"

_token_cache: dict = {}  # {token, expires_at}

# Branch prefix patterns → branch_key
_BRANCH_PATTERNS = [
    (re.compile(r"\[1948\]", re.IGNORECASE), "1948"),
    (re.compile(r"\[taipei\]", re.IGNORECASE), "taipei"),
    (re.compile(r"\[oani\]", re.IGNORECASE), "oani"),
    (re.compile(r"\[osaka\]", re.IGNORECASE), "osaka"),
    (re.compile(r"\[saigon\]|\[sgn\]|\[sai\s*gon\]", re.IGNORECASE), "saigon"),
]


def _get_token() -> Optional[str]:
    if not (settings.LARK_APP_ID and settings.LARK_APP_SECRET):
        log.warning("LARK_APP_ID / LARK_APP_SECRET not configured")
        return None

    now = time.time()
    if _token_cache.get("token") and _token_cache.get("expires_at", 0) > now + 60:
        return _token_cache["token"]

    try:
        resp = requests.post(
            _LARK_AUTH_URL,
            json={"app_id": settings.LARK_APP_ID, "app_secret": settings.LARK_APP_SECRET},
            timeout=10,
        )
        resp.raise_for_status()
        body = resp.json()
        token = body.get("tenant_access_token")
        expires_in = body.get("expire", 7200)
        _token_cache["token"] = token
        _token_cache["expires_at"] = now + expires_in
        return token
    except Exception as exc:
        log.error("Lark auth failed: %s", exc)
        return None


def _fetch_all_records() -> list[dict]:
    """Fetch all records from LARK_TASKS_TABLE_ID with pagination."""
    token = _get_token()
    if not token or not settings.LARK_BASE_APP_TOKEN or not settings.LARK_TASKS_TABLE_ID:
        return []

    url = _LARK_RECORDS_URL.format(
        app_token=settings.LARK_BASE_APP_TOKEN,
        table_id=settings.LARK_TASKS_TABLE_ID,
    )
    headers = {"Authorization": f"Bearer {token}"}
    records = []
    page_token = None

    while True:
        params: dict = {"page_size": 500}
        if page_token:
            params["page_token"] = page_token
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=15)
            resp.raise_for_status()
            body = resp.json()
            data = body.get("data", {})
            for item in data.get("items", []):
                records.append(item.get("fields", {}))
            if not data.get("has_more"):
                break
            page_token = data.get("page_token")
        except Exception as exc:
            log.error("Lark records fetch failed: %s", exc)
            break

    return records


def _parse_date(val) -> Optional[datetime]:
    """Parse Lark date field (ms timestamp or ISO string)."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        try:
            return datetime.utcfromtimestamp(val / 1000)
        except Exception:
            return None
    if isinstance(val, str):
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(val[:19], fmt)
            except ValueError:
                continue
    return None


def _detect_branch(task_name: str) -> Optional[str]:
    for pattern, key in _BRANCH_PATTERNS:
        if pattern.search(task_name):
            return key
    return None


def _pic_is_nora(pic_val) -> bool:
    """Return True if Nora is in the PIC field (string or list)."""
    if not pic_val:
        return False
    if isinstance(pic_val, list):
        return any("nora" in str(p).lower() for p in pic_val)
    return "nora" in str(pic_val).lower()


# ── In-memory cache (10 min TTL) ─────────────────────────────────────────────

_designer_cache: dict = {}  # year → (fetched_at, data)
_DESIGNER_TTL = 600


def get_designer_actuals_yearly(year: int) -> dict[int, dict[str, dict]]:
    """Return {month: {branch_key: {design_assets, videos_delivered, delivery_rate}}}."""
    cached = _designer_cache.get(year)
    if cached and (time.time() - cached[0]) < _DESIGNER_TTL:
        return cached[1]

    records = _fetch_all_records()
    if not records:
        return {}

    # Accumulator: (month, branch_key) → {images, videos, on_time, total}
    agg: dict[tuple, dict] = {}

    for rec in records:
        pic = rec.get("PIC") or rec.get("pIC") or ""
        if not _pic_is_nora(pic):
            continue

        status = str(rec.get("Status") or "").strip()
        if status != "Completed":
            continue

        task_name = str(rec.get("Task") or "")
        branch_key = _detect_branch(task_name)
        if not branch_key:
            continue

        # Month from Complete date, fallback to Deadline
        dt = _parse_date(rec.get("Complete date")) or _parse_date(rec.get("Deadline"))
        if not dt or dt.year != year:
            continue
        month = dt.month

        n_images = float(rec.get("Ads-only_Number of images") or 0)
        n_videos = float(rec.get("Ads-only_Number of video") or 0)
        on_time = str(rec.get("On-time vs Original") or "").strip() == "On-time"

        k = (month, branch_key)
        if k not in agg:
            agg[k] = {"images": 0.0, "videos": 0.0, "on_time": 0, "total": 0}
        agg[k]["images"] += n_images
        agg[k]["videos"] += n_videos
        agg[k]["total"] += 1
        if on_time:
            agg[k]["on_time"] += 1

    out: dict[int, dict[str, dict]] = {}
    for (month, branch_key), vals in agg.items():
        delivery = round(vals["on_time"] / vals["total"] * 100, 1) if vals["total"] else None
        out.setdefault(month, {})[branch_key] = {
            "design_assets":    round(vals["images"]),
            "videos_delivered": round(vals["videos"]),
            "delivery_rate":    delivery,
        }

    _designer_cache[year] = (time.time(), out)
    return out
