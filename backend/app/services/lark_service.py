"""Lark Base API client — fetches task records for Designer & Paid Ads KPI actuals.
Also provides get_task_overview_yearly() for the Task Overview tab.

Mirrors exactly what the Google Apps Script does:
  - Branch from 'Project' field: "[1948] Ads" -> "1948", "[Sai Gon] Ads" -> "saigon"
  - Month from 'Date Created' field (ms timestamp)
  - Status must be 'Completed' (case-insensitive)
  - No PIC filter — all tasks with a branch prefix count

KPIs derived:
  Designer (Nora):  design_assets    = Design-only_Number of images
                    videos_delivered = Design-only_Number of video
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

# ── PIC name mapping (record_id OR email → display name) ─────────────────────
# Lark PIC field can be either a linked-record type (returns link_record_ids)
# or a Person/User type (returns a list of {id, name, email} objects).
# Both formats are handled by _extract_pic_key(); always normalize to one key.
PIC_NAME_MAP: dict[str, str] = {
    # linked-record IDs
    "recuOULUU1hNZe": "Mason",
    "recuOUM6YA5NP7": "Nora",   # non-Ads tasks
    "recv6JxUlC2N9p": "Nora",   # Ads design tasks (same person, second record)
    "recvfBvofwVG5z":  "Mel",
    "recuOUECycRmpy": "Nuha",
    "recuGw12iUnRNJ":  "Kin",
    # email fallback (Person-type field)
    "mason@staymeander.com":  "Mason",
    "nora@staymeander.com":   "Nora",
    "mel@staymeander.com":    "Mel",
    "nuha@staymeander.com":   "Nuha",
    "kin@staymeander.com":    "Kin",
}

_NORA_KEYS = {"recuOUM6YA5NP7", "recv6JxUlC2N9p", "nora@staymeander.com"}

# Optional project keyword filter per PIC (case-insensitive substring match on resolved project name).
# If a PIC is not listed here, all projects are counted.
PIC_PROJECT_FILTER: dict[str, str] = {
    "recuOULUU1hNZe": "ads",   # Mason: Ads projects only
}

_LARK_AUTH_URL    = "https://open.larksuite.com/open-apis/auth/v3/tenant_access_token/internal"
_LARK_RECORDS_URL = "https://open.larksuite.com/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records"
_LARK_FIELDS_URL  = "https://open.larksuite.com/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/fields"

_token_cache: dict = {}
_link_map_cache: dict = {}   # record_id → display name for linked tables
_LINK_MAP_TTL = 3600         # 1 hour — project names rarely change

# Branch prefix patterns from Project field → branch_key
# Matches [1948], [Sai Gon], [Taipei], [Oani], [Osaka], [Saigon], [SGN]
_BRANCH_RE = re.compile(r"\[([^\]]+)\]")

def _extract_pic_key(pic_raw) -> Optional[str]:
    """Return a stable PIC key from either a linked-record or Person-type field.

    Linked-record: {"link_record_ids": ["recXXX"]} → returns first record ID.
    Person type:   [{"email": "nora@staymeander.com", ...}]  → returns email.
    Returns None if the field is empty or unrecognised.
    """
    if not pic_raw:
        return None
    # Linked-record dict
    if isinstance(pic_raw, dict):
        ids = pic_raw.get("link_record_ids") or []
        return ids[0] if ids else None
    # Person-type list
    if isinstance(pic_raw, list):
        for item in pic_raw:
            if isinstance(item, dict):
                # linked-record item inside list
                rec_ids = item.get("link_record_ids") or []
                if rec_ids:
                    return rec_ids[0]
                # Person item
                email = (item.get("email") or "").strip().lower()
                if email:
                    return email
    return None


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
    """Extract branch key from Project field value.

    Handles two formats from the linked table:
      - '[1948] Ads', '[Sai Gon] KOL' → extract text inside []
      - 'Osaka', 'Saigon' → bare name, match directly
    """
    if not project_val:
        return None
    s = str(project_val)
    # Try bracket format first: [Branch] ...
    m = _BRANCH_RE.search(s)
    if m:
        return _norm_branch(m.group(1))
    # Fallback: bare branch name — only accept known keys
    _KNOWN = {"saigon", "taipei", "1948", "oani", "osaka"}
    first = s.split(",")[0].strip()
    key = _norm_branch(first)
    return key if key in _KNOWN else None


def _parse_month_year(val) -> Optional[tuple[int, int]]:
    """Parse date field (ms timestamp, ISO string, or Lark date dict) → (year, month)."""
    if val is None:
        return None
    # Lark date fields return {"date": "2026-07-15"} or {"timestamp": <ms>}
    if isinstance(val, dict):
        if val.get("date"):
            val = val["date"]
        elif val.get("timestamp"):
            val = val["timestamp"]
        else:
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


_LARK_SEARCH_URL = "https://open.larksuite.com/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/search"


def _get_link_map() -> dict:
    """Build map of record_id → display name for linked tables (e.g. Project field).

    Mirrors Apps Script buildLinkMap_():
    1. Fetch field definitions for the tasks table
    2. For each linked-record field, fetch all records from the linked table
    3. Map record_id → first field value (primary = display name like "[1948] Ads")

    Result is cached 1 hour — project names don't change often.
    """
    global _link_map_cache
    cached = _link_map_cache.get("data")
    if cached and (time.time() - _link_map_cache.get("ts", 0)) < _LINK_MAP_TTL:
        return cached

    token = _get_token()
    if not token or not settings.LARK_BASE_APP_TOKEN or not settings.LARK_TASKS_TABLE_ID:
        return {}

    auth_h = {"Authorization": f"Bearer {token}"}
    result: dict[str, str] = {}

    try:
        # Step 1: get field definitions to find linked-record fields
        fields_url = _LARK_FIELDS_URL.format(
            app_token=settings.LARK_BASE_APP_TOKEN,
            table_id=settings.LARK_TASKS_TABLE_ID,
        )
        resp = requests.get(fields_url, headers=auth_h, params={"page_size": 100}, timeout=15)
        resp.raise_for_status()
        fields_data = resp.json().get("data", {})
        linked_table_ids: set[str] = set()
        for fld in fields_data.get("items", []):
            prop = fld.get("property") or {}
            linked_tid = prop.get("table_id")
            if linked_tid:
                linked_table_ids.add(linked_tid)

        # Step 2: for each linked table, fetch records and map id → primary name
        for ltid in linked_table_ids:
            try:
                page_token = None
                while True:
                    params: dict = {"page_size": 500}
                    if page_token:
                        params["page_token"] = page_token
                    recs_url = _LARK_RECORDS_URL.format(
                        app_token=settings.LARK_BASE_APP_TOKEN,
                        table_id=ltid,
                    )
                    r = requests.get(recs_url, headers=auth_h, params=params, timeout=15)
                    r.raise_for_status()
                    d = r.json().get("data", {})
                    for item in d.get("items", []):
                        rid = item.get("record_id", "")
                        fields = item.get("fields", {})
                        # Primary field = first key with a non-empty string value
                        for v in fields.values():
                            if isinstance(v, str) and v.strip():
                                result[rid] = v.strip()
                                break
                    if not d.get("has_more"):
                        break
                    page_token = d.get("page_token")
            except Exception as exc:
                log.warning("Lark link map: failed to fetch linked table %s: %s", ltid, exc)

        log.info("Lark link map: resolved %d linked records", len(result))
    except Exception as exc:
        log.warning("Lark link map build failed: %s", exc)

    _link_map_cache["data"] = result
    _link_map_cache["ts"] = time.time()
    return result


def _resolve_project(raw_val) -> str:
    """Resolve a Project field value to its display name.

    Lark linked-record fields return {'link_record_ids': ['recXXX', ...]} or
    a list of such objects. Resolve IDs via _get_link_map() to get the text
    name like '[1948] Ads'.
    """
    if not raw_val:
        return ""
    if isinstance(raw_val, str):
        return raw_val  # already plain text (e.g. in older API versions)
    link_map = _get_link_map()
    ids: list[str] = []
    if isinstance(raw_val, dict):
        ids = raw_val.get("link_record_ids") or []
    elif isinstance(raw_val, list):
        for item in raw_val:
            if isinstance(item, dict):
                ids += item.get("link_record_ids") or []
                if item.get("record_id"):
                    ids.append(item["record_id"])
    names = [link_map.get(rid, "") for rid in ids if rid]
    return ", ".join(n for n in names if n)


def _fetch_all_records(cutoff_ms: Optional[int] = None) -> list[dict]:
    """Fetch Lark Base records, optionally filtered by Date Created >= cutoff_ms.

    Uses the /records/search endpoint with a server-side date filter to avoid
    pulling the full table (which times out on large datasets). Falls back to
    the plain GET /records endpoint if search fails.
    """
    token = _get_token()
    if not token or not settings.LARK_BASE_APP_TOKEN or not settings.LARK_TASKS_TABLE_ID:
        return []

    # Default cutoff: Jan 1 of current year (ms timestamp)
    if cutoff_ms is None:
        import datetime as _dt
        now = _dt.datetime.utcnow()
        cutoff_ms = int(_dt.datetime(now.year, 1, 1).timestamp() * 1000)

    search_url = _LARK_SEARCH_URL.format(
        app_token=settings.LARK_BASE_APP_TOKEN,
        table_id=settings.LARK_TASKS_TABLE_ID,
    )
    list_url = _LARK_RECORDS_URL.format(
        app_token=settings.LARK_BASE_APP_TOKEN,
        table_id=settings.LARK_TASKS_TABLE_ID,
    )
    auth_headers = {"Authorization": f"Bearer {token}"}

    def _search_pages() -> list[dict]:
        records: list[dict] = []
        page_token = None
        # -1 day buffer same as Apps Script
        server_cutoff = cutoff_ms - 86_400_000
        while True:
            params = {"page_size": 500}
            if page_token:
                params["page_token"] = page_token
            body = {
                "filter": {
                    "conjunction": "and",
                    "conditions": [{
                        "field_name": "Date Created",
                        "operator": "isGreater",
                        "value": ["ExactDate", server_cutoff],
                    }],
                },
                "automatic_fields": True,
            }
            resp = requests.post(
                search_url, headers=auth_headers, params=params,
                json=body, timeout=30,
            )
            resp.raise_for_status()
            data = resp.json().get("data", {})
            if resp.json().get("code", 0) != 0:
                raise RuntimeError(f"Lark search error: {resp.json()}")
            for item in data.get("items", []):
                records.append(item.get("fields", {}))
            if not data.get("has_more"):
                break
            page_token = data.get("page_token")
        return records

    def _list_pages() -> list[dict]:
        records: list[dict] = []
        page_token = None
        while True:
            params: dict = {"page_size": 500}
            if page_token:
                params["page_token"] = page_token
            resp = requests.get(list_url, headers=auth_headers, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json().get("data", {})
            for item in data.get("items", []):
                records.append(item.get("fields", {}))
            if not data.get("has_more"):
                break
            page_token = data.get("page_token")
        return records

    try:
        records = _search_pages()
        log.info("Lark: fetched %d records via search (cutoff %d)", len(records), cutoff_ms)
        return records
    except Exception as exc:
        log.warning("Lark search failed, falling back to list all: %s", exc)

    try:
        records = _list_pages()
        log.info("Lark: fetched %d records via list-all (fallback)", len(records))
        return records
    except Exception as exc:
        log.error("Lark list-all fallback also failed: %s", exc)
        return []


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
        project = _resolve_project(rec.get("Project"))
        branch_key = _parse_branch_from_project(project)
        if not branch_key:
            continue

        status = str(rec.get("Status") or "").lower().strip()
        if status != "completed":
            continue

        ym = _parse_month_year(rec.get("Deadline"))
        if not ym or ym[0] != year:
            continue

        _, month = ym
        images = float(rec.get("Design-only_Number of images") or 0)
        videos = float(rec.get("Design-only_Number of video") or 0)
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


_NORA_PIC_ID = "recuOUM6YA5NP7"

def get_designer_actuals_yearly(year: int, nora_name: str = "Nora") -> dict[int, dict[str, dict]]:
    """
    Return {month: {branch_key: {design_assets, videos_delivered}}}
    Counts ALL completed tasks assigned to Nora (all projects, incl. uncategorized),
    grouped by Date Created month (consistent with _get_yearly_agg). Each completed task = 1 design asset.
    Branch derived from Project name if available; falls back to 'all'.
    """
    records = _fetch_all_records()
    # {month: {branch_key: {images, videos}}}
    from collections import defaultdict
    agg: dict = defaultdict(lambda: defaultdict(lambda: {"images": 0, "videos": 0}))

    for rec in records:
        pic_key = _extract_pic_key(rec.get("PIC"))
        if pic_key not in _NORA_KEYS:
            continue

        status = str(rec.get("Status") or "").lower().strip()
        if status != "completed":
            continue

        ym = _parse_month_year(rec.get("Deadline"))
        if not ym or ym[0] != year:
            continue
        _, month = ym

        # Use branch from project if available, else 'all'
        project = _resolve_project(rec.get("Project"))
        branch_key = _parse_branch_from_project(project) or "all"

        images = float(rec.get("Design-only_Number of images") or 0)
        videos = float(rec.get("Design-only_Number of video") or 0)
        # For non-Ads tasks these fields are 0 — count the task itself as 1 asset
        if images == 0 and videos == 0:
            images = 1

        agg[month][branch_key]["images"] += images
        agg[month][branch_key]["videos"] += videos

    out: dict[int, dict[str, dict]] = {}
    for month, branches in agg.items():
        for branch_key, counts in branches.items():
            out.setdefault(month, {})[branch_key] = {
                "design_assets":    round(counts["images"]),
                "videos_delivered": round(counts["videos"]),
            }

    return out


def get_delivery_rate_yearly(year: int, pic_name: str = "Nora") -> dict[int, dict[str, dict]]:
    """
    Return {month: {'all': {delivery_rate}}}
    delivery_rate = % of pic_name's completed tasks (with Deadline in that month)
    where 'On-time vs Original' == 'On-time'. Reported as org-wide (no branch split)
    since Nora works across all branches.
    """
    records = _fetch_all_records()
    counts: dict[int, dict] = {}  # month → {on_time, total}

    for rec in records:
        pic_val = rec.get("PIC") or ""
        pic_str = pic_val if isinstance(pic_val, str) else str(pic_val)
        if pic_name.lower() not in pic_str.lower():
            continue

        status = str(rec.get("Status") or "").lower().strip()
        if status != "completed":
            continue

        ym = _parse_month_year(rec.get("Deadline"))
        if not ym or ym[0] != year:
            continue
        _, month = ym

        if month not in counts:
            counts[month] = {"on_time": 0, "total": 0}
        counts[month]["total"] += 1
        on_time_val = str(rec.get("On-time vs Original") or "").strip().lower()
        if on_time_val == "on-time":
            counts[month]["on_time"] += 1

    out: dict[int, dict[str, dict]] = {}
    for month, c in counts.items():
        rate = round(c["on_time"] / c["total"] * 100, 1) if c["total"] > 0 else None
        out[month] = {"all": {"delivery_rate": rate}}
    return out


def get_task_completion_rate_yearly(year: int) -> dict[int, dict[str, dict]]:
    """
    Return {month: {'all': {task_completion_rate}}}
    task_completion_rate = % of tasks with Deadline in that month that are Completed.
    """
    records = _fetch_all_records()
    counts: dict[int, dict] = {}  # month → {total, completed}

    for rec in records:
        ym = _parse_month_year(rec.get("Deadline"))
        if not ym or ym[0] != year:
            continue
        _, month = ym

        status = str(rec.get("Status") or "").lower().strip()
        if month not in counts:
            counts[month] = {"total": 0, "completed": 0}
        counts[month]["total"] += 1
        if status == "completed":
            counts[month]["completed"] += 1

    out: dict[int, dict[str, dict]] = {}
    for month, c in counts.items():
        rate = round(c["completed"] / c["total"] * 100, 1) if c["total"] > 0 else None
        out[month] = {"all": {"task_completion_rate": rate}}
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


# ── Task Overview cache ───────────────────────────────────────────────────────

_task_overview_cache: dict = {}  # year → (fetched_at, data)


def get_task_overview_yearly(year: int) -> dict:
    """
    Return per-PIC per-month task stats for the Task Overview tab.

    Structure:
      {pic_id: {month: {total_tasks, completed, on_time_count, late_count,
                        overdue_count, cycle_time_avg, estimated_avg,
                        cycle_ratio, completion_rate, on_time_rate},
                "open_workload": int}}

    Months are int (1–12).  open_workload is a special key (not a month dict).
    """
    cached = _task_overview_cache.get(year)
    if cached and (time.time() - cached[0]) < _LARK_TTL:
        return cached[1]

    import datetime as _dt
    now_utc = _dt.datetime.now(tz=timezone.utc)
    current_year_month = (now_utc.year, now_utc.month)

    records = _fetch_all_records(cutoff_ms=None)  # fetch all, we'll filter by deadline year

    # agg[pic_id][month] → running totals
    from collections import defaultdict
    agg: dict[str, dict[int, dict]] = defaultdict(lambda: defaultdict(lambda: {
        "total_tasks": 0,
        "completed": 0,
        "on_time_count": 0,
        "late_count": 0,
        "on_time_filled": 0,  # tasks that have on-time field filled (denominator)
        "overdue_count": 0,
        "cycle_times": [],
        "estimated_days": [],
    }))
    open_workload: dict[str, int] = defaultdict(int)
    no_deadline: dict[str, int] = defaultdict(int)

    for rec in records:
        pic_id: Optional[str] = _extract_pic_key(rec.get("PIC"))
        if not pic_id:
            continue

        status = str(rec.get("Status") or "").lower().strip()
        is_completed = status == "completed"

        # Deadline-based month grouping
        ym = _parse_month_year(rec.get("Deadline"))
        if not ym:
            # No deadline set — count as missing, not in open workload
            if not is_completed:
                no_deadline[pic_id] += 1
            continue

        # Open workload: only incomplete tasks that have a deadline
        if not is_completed:
            open_workload[pic_id] += 1
        if ym[0] != year:
            continue
        _, month = ym

        bucket = agg[pic_id][month]
        bucket["total_tasks"] += 1

        if is_completed:
            bucket["completed"] += 1
            raw_ot = rec.get("On-time vs Original") or ""
            # Field format: {'type': 1, 'value': [{'text': 'On-time', 'type': 'text'}]}
            if isinstance(raw_ot, dict):
                val_list = raw_ot.get("value", [])
                if isinstance(val_list, list) and val_list:
                    first = val_list[0]
                    on_time_val = first.get("text", "") if isinstance(first, dict) else str(first)
                else:
                    on_time_val = ""
            elif isinstance(raw_ot, list):
                on_time_val = raw_ot[0].get("text", "") if raw_ot and isinstance(raw_ot[0], dict) else str(raw_ot[0]) if raw_ot else ""
            else:
                on_time_val = str(raw_ot)
            on_time_val = on_time_val.strip().lower().replace("-", " ").replace("_", " ")
            if on_time_val in ("on time", "ontime", "yes", "true", "đúng hạn", "ok"):
                bucket["on_time_count"] += 1
                bucket["on_time_filled"] += 1
            elif on_time_val in ("late", "trễ", "no", "false", "over"):
                bucket["late_count"] += 1
                bucket["on_time_filled"] += 1
            # empty / unfilled → not counted in on_time_filled
        else:
            # Overdue: deadline in a past month and still not completed
            if (year, month) < current_year_month:
                bucket["overdue_count"] += 1

        # Cycle time
        ct_raw = rec.get("Cycle Time")
        ct: Optional[float] = None
        if isinstance(ct_raw, (int, float)) and ct_raw > 0:
            ct = float(ct_raw)
        elif isinstance(ct_raw, dict):
            v = ct_raw.get("value") or ct_raw.get("number")
            if isinstance(v, (int, float)) and v > 0:
                ct = float(v)
        if ct is not None:
            bucket["cycle_times"].append(ct)

        # Estimated days
        ed_raw = rec.get("Estimated Days")
        ed: Optional[float] = None
        if isinstance(ed_raw, (int, float)) and ed_raw > 0:
            ed = float(ed_raw)
        elif isinstance(ed_raw, dict):
            v = ed_raw.get("value") or ed_raw.get("number")
            if isinstance(v, (int, float)) and v > 0:
                ed = float(v)
        if ed is not None:
            bucket["estimated_days"].append(ed)

    # Build final output
    result: dict = {}
    for pic_id, months in agg.items():
        result[pic_id] = {}
        for month, b in months.items():
            ct_vals = b["cycle_times"]
            ed_vals = b["estimated_days"]
            ct_avg = round(sum(ct_vals) / len(ct_vals), 2) if ct_vals else None
            ed_avg = round(sum(ed_vals) / len(ed_vals), 2) if ed_vals else None
            cycle_ratio = round(ct_avg / ed_avg, 3) if (ct_avg and ed_avg) else None
            total = b["total_tasks"]
            comp = b["completed"]
            on_time = b["on_time_count"]
            result[pic_id][month] = {
                "total_tasks": total,
                "completed": comp,
                "on_time_count": on_time,
                "late_count": b["late_count"],
                "overdue_count": b["overdue_count"],
                "cycle_time_avg": ct_avg,
                "estimated_avg": ed_avg,
                "cycle_ratio": cycle_ratio,
                "completion_rate": round(comp / total * 100, 1) if total > 0 else None,
                    "on_time_filled": b["on_time_filled"],
                "on_time_rate": round(on_time / b["on_time_filled"] * 100, 1) if b["on_time_filled"] > 0 else None,
            }
        result[pic_id]["open_workload"] = open_workload.get(pic_id, 0)
        result[pic_id]["no_deadline_count"] = no_deadline.get(pic_id, 0)

    _task_overview_cache[year] = (time.time(), result)
    return result
