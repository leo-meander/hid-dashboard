"""Lark Base API client — fetches task records for Designer & Paid Ads KPI actuals.

Mirrors exactly what the Google Apps Script does:
  - Branch from 'Project' field: "[1948] Ads" -> "1948", "[Sai Gon] Ads" -> "saigon"
  - Month from 'Date Created' field (ms timestamp)
  - Status must be 'Completed' (case-insensitive)
  - No PIC filter — all tasks with a branch prefix count

KPIs derived:
  Designer (Nora):  design_assets    = Ads-only_Number of images
                    videos_delivered = Ads-only_Number of video
  Paid Ads (Mason): ads_material     = images + videos
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from typing import Optional

import requests

from app.config import settings

log = logging.getLogger(__name__)

_LARK_AUTH_URL = "https://open.larksuite.com/open-apis/auth/v3/tenant_access_token/internal"
_LARK_RECORDS_URL = "https://open.larksuite.com/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records"

_token_cache: dict = {}

# Branch prefix patterns from Project field → branch_key
# Matches [1948], [Sai Gon], [Taipei], [Oani], [Osaka], [Saigon], [SGN]
_BRANCH_RE = re.compile(r"\[([^\]]+)\]")

def _norm_branch(raw: str) -> Optional[str]:
    """Normalize branch name: lowercase + strip spaces, then map to branch key."""
    s = raw.lower().replace(" ", "").strip()
    if s == "1948":    return "1948"
    if s == "taipei":  return "taipei"
    if s == "oani":    return "oani"
    if s == "osaka":   return "osaka"
    if s in ("saigon", "sgn", "saigòn"): return "saigon"
    return None


def _parse_branch_from_project(project_val) -> Optional[str]:
    """Extract branch key from Project field value like '[1948] Ads'."""
    if not project_val:
        return None
    s = str(project_val)
    m = _BRANCH_RE.search(s)
    if not m:
        return None
    return _norm_branch(m.group(1))


def _parse_month_year(val) -> Optional[tuple[int, int]]:
    """Parse 'Date Created' field (ms timestamp or ISO string) → (year, month)."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        try:
            dt = datetime.fromtimestamp(val / 1000, tz=timezone.utc)
            return dt.year, dt.month
        except Exception:
            return None
    if isinstance(val, str) and val.strip():
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(val.strip()[:19], fmt)
                return dt.year, dt.month
            except ValueError:
                continue
    return None


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
        _token_cache["token"] = token
        _token_cache["expires_at"] = now + body.get("expire", 7200)
        return token
    except Exception as exc:
        log.error("Lark auth failed: %s", exc)
        return None


def _fetch_all_records() -> list[dict]:
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
            resp = requests.get(url, headers=headers, params=params, timeout=30)
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


# ── In-memory cache (10 min TTL) ─────────────────────────────────────────────

_lark_cache: dict = {}  # year → (fetched_at, data)
_LARK_TTL = 600


def _get_yearly_agg(year: int) -> dict:
    """
    Return aggregated counts per (year, month, branch_key):
    {branch_key: {month: {images, videos}}}

    Only counts tasks where:
      - Project field contains a [Branch] prefix
      - Status == 'completed' (case-insensitive)
      - Date Created falls in `year`
      - images > 0 or videos > 0 (matches script: skip if both 0)
    """
    cached = _lark_cache.get(year)
    if cached and (time.time() - cached[0]) < _LARK_TTL:
        return cached[1]

    records = _fetch_all_records()
    agg: dict[str, dict[int, dict]] = {}

    for rec in records:
        project = rec.get("Project") or ""
        branch_key = _parse_branch_from_project(project)
        if not branch_key:
            continue

        status = str(rec.get("Status") or "").lower().strip()
        if status != "completed":
            continue

        ym = _parse_month_year(rec.get("Date Created"))
        if not ym or ym[0] != year:
            continue

        _, month = ym
        images = float(rec.get("Ads-only_Number of images") or 0)
        videos = float(rec.get("Ads-only_Number of video") or 0)
        if images == 0 and videos == 0:
            continue  # skip tasks with no asset counts (matches script behaviour)

        if branch_key not in agg:
            agg[branch_key] = {}
        if month not in agg[branch_key]:
            agg[branch_key][month] = {"images": 0.0, "videos": 0.0}
        agg[branch_key][month]["images"] += images
        agg[branch_key][month]["videos"] += videos

    _lark_cache[year] = (time.time(), agg)
    return agg


def get_designer_actuals_yearly(year: int) -> dict[int, dict[str, dict]]:
    """
    Return {month: {branch_key: {design_assets, videos_delivered}}}
    design_assets    = images
    videos_delivered = videos
    """
    agg = _get_yearly_agg(year)
    out: dict[int, dict[str, dict]] = {}
    for branch_key, months in agg.items():
        for month, counts in months.items():
            out.setdefault(month, {})[branch_key] = {
                "design_assets":    round(counts["images"]),
                "videos_delivered": round(counts["videos"]),
            }
    return out


def get_ads_material_yearly(year: int) -> dict[int, dict[str, dict]]:
    """
    Return {month: {branch_key: {ads_material}}}
    ads_material = images + videos  (matches Paid Ads script: source='both')
    """
    agg = _get_yearly_agg(year)
    out: dict[int, dict[str, dict]] = {}
    for branch_key, months in agg.items():
        for month, counts in months.items():
            out.setdefault(month, {})[branch_key] = {
                "ads_material": round(counts["images"] + counts["videos"]),
            }
    return out
