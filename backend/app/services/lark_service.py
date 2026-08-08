"""Lark Base API client — fetches task records for Designer KPI actuals.
Also provides get_task_overview_yearly() for the Task Overview tab.

Mirrors exactly what the Google Apps Script does:
  - Branch from 'Project' field: "[1948] Ads" -> "1948", "[Sai Gon] Ads" -> "saigon"
  - Month from 'Date Created' field (ms timestamp)
  - Status must be 'Completed' (case-insensitive)
  - No PIC filter — all tasks with a branch prefix count

KPIs derived:
  Designer (Nora):  design_assets    = Design-only_Number of images
                    videos_delivered = Design-only_Number of video
"""
from __future__ import annotations

import logging
import re
import time
from datetime import date, datetime
from typing import Optional
from zoneinfo import ZoneInfo

import requests

from app.config import settings

log = logging.getLogger(__name__)

_ICT_TZ = ZoneInfo("Asia/Ho_Chi_Minh")

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


def _parse_date(val) -> Optional[date]:
    """Parse date field (ms timestamp, ISO string, or Lark date dict) → date.

    Timestamps are read in ICT: Lark stores day-granularity dates at local
    midnight, so reading them in UTC would shift the day (and sometimes the
    month) one back.
    """
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
            return datetime.fromtimestamp(val / 1000, tz=_ICT_TZ).date()
        except Exception:
            return None
    if isinstance(val, str) and val.strip():
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(val.strip()[:19], fmt).date()
            except ValueError:
                continue
    return None


def _parse_month_year(val) -> Optional[tuple[int, int]]:
    """Parse date field (ms timestamp, ISO string, or Lark date dict) → (year, month)."""
    d = _parse_date(val)
    return (d.year, d.month) if d else None


def _today_ict() -> date:
    return datetime.now(tz=_ICT_TZ).date()


def _extract_number(raw) -> Optional[float]:
    """Read a Lark number / numeric-formula field into a float.

    Formula fields wrap the number in a list: {'type': 2, 'value': [8]}.
    Hand-entered number fields come back bare. Reading only the bare shape
    silently treated every formula field as empty.
    """
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, dict):
        return _extract_number(raw.get("value", raw.get("number")))
    if isinstance(raw, list):
        return _extract_number(raw[0]) if raw else None
    if isinstance(raw, str):
        try:
            return float(raw.strip())
        except ValueError:
            return None
    return None


# Estimated Days / Cycle Time are formulas in Lark and currently emit some
# nonsense (negative values, 46112). Anything outside this range is a broken
# formula result, not a real estimate — dropped from averages and counted in
# `bad_duration_count` so it stays visible rather than silently vanishing.
_MAX_SANE_DAYS = 365


def _sane_days(raw) -> tuple[Optional[float], bool]:
    """Return (value, was_rejected). Value is None unless 0 < v <= 365."""
    v = _extract_number(raw)
    if v is None:
        return None, False
    if v <= 0 or v > _MAX_SANE_DAYS:
        return None, True
    return v, False


def _extract_text(raw) -> str:
    """Read a Lark text / single-select field into a plain string.

    Lark returns these in three shapes depending on field type and API version:
      {'type': 1, 'value': [{'text': 'On-time', 'type': 'text'}]}
      [{'text': 'On-time'}]
      'On-time'
    """
    if not raw:
        return ""
    if isinstance(raw, dict):
        val_list = raw.get("value", [])
        if isinstance(val_list, list) and val_list:
            first = val_list[0]
            return (first.get("text", "") if isinstance(first, dict) else str(first)).strip()
        if isinstance(val_list, str):
            return val_list.strip()
        return ""
    if isinstance(raw, list):
        if not raw:
            return ""
        first = raw[0]
        return (first.get("text", "") if isinstance(first, dict) else str(first)).strip()
    return str(raw).strip()


# ── Late Reason ───────────────────────────────────────────────────────────────
# Single-select on the Lark task table. Every option it offers is a reason
# outside the assignee's control, so picking any of them excuses the miss —
# the gate is whether a reason was given at all, not which one. Keeping the
# judgement out of the option values means adding an option in Lark needs no
# code change here.

# Statuses that sit outside the KPI entirely: backlog, standing work, and work
# that cannot proceed. Dropped from Task Overview before anything is counted —
# no totals, no on-time rate, no overdue, no missing-deadline chase. Every
# other status without a deadline stays in no_deadline_count.
# Singular/plural variants are listed because the Lark options get renamed.
_EXCLUDED_STATUSES = {
    "upcoming tasks",
    "upcoming task",
    "regular task",
    "regular tasks",
    "blocked task",
    "blocked tasks",
}


def _norm_reason(raw) -> str:
    """Normalize a Late Reason value for comparison (case, spacing, slashes)."""
    s = _extract_text(raw).lower()
    s = re.sub(r"\s*/\s*", "/", s)
    return re.sub(r"\s+", " ", s).strip()


def _norm_status(raw) -> str:
    return re.sub(r"\s+", " ", _extract_text(raw).lower()).strip()


def _is_excluded_status(status_norm: str) -> bool:
    """True when this task sits outside the Task Overview entirely."""
    return status_norm in _EXCLUDED_STATUSES


def _created_in_scope(raw, year: int) -> bool:
    """True when a record was created inside the tracked window.

    Task data is only standardized from _LARK_START_MONTH onward, so anything
    created before that is legacy and not worth chasing. Used for tasks with no
    deadline, which have no month of their own to filter on.

    An unreadable creation date counts as in scope — surfacing a stale row is
    the cheaper mistake compared to hiding a real one.
    """
    created = _parse_date(raw)
    if created is None:
        return True
    return (created.year, created.month) >= (year, _LARK_START_MONTH)


def _is_excused(reason_norm: str) -> bool:
    """True when a miss should not count against the assignee.

    Any Late Reason excuses it; leaving the field blank does not. A miss costs
    by default, so silence is never forgiveness.
    """
    return bool(reason_norm)


def _lark_record_url(record_id: str) -> str:
    """Deep link to a single record in the Lark base. Empty if not configured."""
    if not (record_id and settings.LARK_BASE_APP_TOKEN and settings.LARK_TASKS_TABLE_ID):
        return ""
    return (
        f"https://{settings.LARK_WORKSPACE_DOMAIN}/base/{settings.LARK_BASE_APP_TOKEN}"
        f"?table={settings.LARK_TASKS_TABLE_ID}&record={record_id}"
    )


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


def list_field_definitions() -> list[dict]:
    """Field definitions for the tasks table, straight from Lark.

    Authoritative — unlike record keys, this lists fields that exist even when
    every record leaves them empty.
    """
    token = _get_token()
    if not token or not settings.LARK_BASE_APP_TOKEN or not settings.LARK_TASKS_TABLE_ID:
        return []
    try:
        resp = requests.get(
            _LARK_FIELDS_URL.format(
                app_token=settings.LARK_BASE_APP_TOKEN,
                table_id=settings.LARK_TASKS_TABLE_ID,
            ),
            headers={"Authorization": f"Bearer {token}"},
            params={"page_size": 200},
            timeout=15,
        )
        resp.raise_for_status()
        items = resp.json().get("data", {}).get("items", [])
        out = []
        for f in items:
            entry = {"name": f.get("field_name"), "type": f.get("type")}
            opts = ((f.get("property") or {}).get("options")) or []
            if opts:
                entry["options"] = [o.get("name") for o in opts]
            out.append(entry)
        return out
    except Exception as exc:
        log.warning("Lark field definitions fetch failed: %s", exc)
        return []


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
                fields = item.get("fields", {})
                fields["_record_id"] = item.get("record_id", "")
                records.append(fields)
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
                fields = item.get("fields", {})
                fields["_record_id"] = item.get("record_id", "")
                records.append(fields)
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
_LARK_START_MONTH = 7  # data clean from July 2026 onwards; ignore earlier months


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
        if month < _LARK_START_MONTH:
            continue

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
        if month < _LARK_START_MONTH:
            continue

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
    """Return ``{month: {'all': {delivery_rate}}}`` — one person's on-time rate.

    Reads the same aggregation the Task Overview tab renders, so the Team KPI
    row and the person's scorecard can never disagree: on-time ÷ scored, where
    scored = completed tasks marked On-time or Late, minus misses carrying a
    Late Reason (out of the assignee's hands — dropped from both sides).
    Tasks are grouped by Deadline month and start at _LARK_START_MONTH.

    Org-wide, with no branch split: Nora works across every branch and the
    Lark tasks carry no reliable branch attribution.

    A month the person had nothing scored in is omitted, not zeroed — the
    caller renders it blank.
    """
    rows = merge_pic_overview(get_task_overview_yearly(year))
    row = next((r for r in rows if r["name"] == pic_name), None)
    if row is None:
        log.warning("no Lark task rows for %s in %s; delivery_rate blank", pic_name, year)
        return {}

    out: dict[int, dict[str, dict]] = {}
    for month_key, stats in (row.get("months") or {}).items():
        rate = stats.get("on_time_rate")
        if rate is None:
            continue
        out[int(month_key)] = {"all": {"delivery_rate": float(rate)}}
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
        if month < _LARK_START_MONTH:
            continue

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


# ── Raw records cache (shared across overview + detail) ───────────────────────

_raw_records_cache: dict = {}  # "all" → (fetched_at, records)


def _get_cached_records() -> list[dict]:
    cached = _raw_records_cache.get("all")
    if cached and (time.time() - cached[0]) < _LARK_TTL:
        return cached[1]
    records = _fetch_all_records(cutoff_ms=None)
    _raw_records_cache["all"] = (time.time(), records)
    return records


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

    today = _today_ict()

    records = _get_cached_records()

    # agg[pic_id][month] → running totals
    from collections import defaultdict
    agg: dict[str, dict[int, dict]] = defaultdict(lambda: defaultdict(lambda: {
        "total_tasks": 0,
        "open_count": 0,      # not Completed, deadline inside this month
        "completed": 0,
        "on_time_count": 0,
        "late_count": 0,
        "on_time_filled": 0,  # tasks that have on-time field filled (denominator)
        "overdue_count": 0,
        "late_excused_count": 0,     # completed late, reason excuses it
        "overdue_excused_count": 0,  # still open past deadline, reason excuses it
        "reason_counts": defaultdict(int),  # every reason seen on a missed task
        "bad_duration_count": 0,  # Cycle Time / Estimated Days outside 0–365
        "reopen_count": 0,
        "reopen_filled": 0,   # records that actually carried a Reopen Count
        "cycle_times": [],
        "estimated_days": [],
    }))
    open_workload: dict[str, int] = defaultdict(int)
    no_deadline: dict[str, int] = defaultdict(int)
    excluded_status: dict[str, int] = defaultdict(int)

    for rec in records:
        pic_id: Optional[str] = _extract_pic_key(rec.get("PIC"))
        if not pic_id:
            continue

        status = _norm_status(rec.get("Status"))
        if _is_excluded_status(status):
            # Backlog / standing work — out of the KPI before anything counts.
            # Tallied only so the rows are accounted for, never scored.
            excluded_status[pic_id] += 1
            continue
        is_completed = status == "completed"

        # Deadline-based month grouping
        deadline = _parse_date(rec.get("Deadline"))
        if not deadline:
            # No deadline set — flag as missing, but only for tasks created
            # since the standardization cutoff. Older ones have no month to
            # sit in and nobody is going back to fix them.
            if not is_completed and _created_in_scope(rec.get("Date Created"), year):
                no_deadline[pic_id] += 1
            continue

        if deadline.year != year:
            continue
        month = deadline.month
        if month < _LARK_START_MONTH:
            continue

        # Open workload is scoped exactly like total_tasks — same year, same
        # start month. Counting it before these filters let a person show more
        # open tasks than tasks ("13 tasks · 15 open"), because the workload
        # swept in deadlines from other years and pre-July months.
        if not is_completed:
            open_workload[pic_id] += 1

        bucket = agg[pic_id][month]
        bucket["total_tasks"] += 1
        if not is_completed:
            bucket["open_count"] += 1

        reason = _norm_reason(rec.get("Late Reason"))
        excused = _is_excused(reason)

        if is_completed:
            bucket["completed"] += 1
            on_time_val = _extract_text(rec.get("On-time vs Original"))
            on_time_val = on_time_val.lower().replace("-", " ").replace("_", " ")
            if on_time_val in ("on time", "ontime", "yes", "true", "đúng hạn", "ok"):
                bucket["on_time_count"] += 1
                bucket["on_time_filled"] += 1
            elif on_time_val in ("late", "trễ", "no", "false", "over"):
                if reason:
                    bucket["reason_counts"][reason] += 1
                if excused:
                    # Out of the assignee's hands — drop from numerator AND
                    # denominator so it neither helps nor hurts the rate.
                    bucket["late_excused_count"] += 1
                else:
                    bucket["late_count"] += 1
                    bucket["on_time_filled"] += 1
            # empty / unfilled → not counted in on_time_filled
        else:
            # Overdue: deadline date already passed and still not completed.
            # Day-level, not month-level — a task due earlier this month counts.
            if deadline < today:
                if reason:
                    bucket["reason_counts"][reason] += 1
                if excused:
                    bucket["overdue_excused_count"] += 1
                else:
                    bucket["overdue_count"] += 1

        # Reopen Count is absent from most rows in the Lark base. Track how many
        # records actually carried it, so the score can tell "zero reopens" from
        # "nobody fills this field" instead of handing out a free 10.
        rc = _extract_number(rec.get("Reopen Count"))
        if rc is not None:
            bucket["reopen_filled"] += 1
            if rc > 0:
                bucket["reopen_count"] += int(rc)

        ct, ct_bad = _sane_days(rec.get("Cycle Time"))
        if ct is not None:
            bucket["cycle_times"].append(ct)
        ed, ed_bad = _sane_days(rec.get("Estimated Days"))
        if ed is not None:
            bucket["estimated_days"].append(ed)
        if ct_bad or ed_bad:
            bucket["bad_duration_count"] += 1

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
                "open_count": b["open_count"],
                "completed": comp,
                "on_time_count": on_time,
                "late_count": b["late_count"],
                "overdue_count": b["overdue_count"],
                "late_excused_count": b["late_excused_count"],
                "overdue_excused_count": b["overdue_excused_count"],
                "reason_counts": dict(b["reason_counts"]),
                "bad_duration_count": b["bad_duration_count"],
                "reopen_count": b["reopen_count"],
                "reopen_filled": b["reopen_filled"],
                "cycle_time_avg": ct_avg,
                "estimated_avg": ed_avg,
                "cycle_ratio": cycle_ratio,
                "completion_rate": round(comp / total * 100, 1) if total > 0 else None,
                    "on_time_filled": b["on_time_filled"],
                "on_time_rate": round(on_time / b["on_time_filled"] * 100, 1) if b["on_time_filled"] > 0 else None,
            }
        result[pic_id]["open_workload"] = open_workload.get(pic_id, 0)
        result[pic_id]["no_deadline_count"] = no_deadline.get(pic_id, 0)
        result[pic_id]["excluded_status_count"] = excluded_status.get(pic_id, 0)

    _task_overview_cache[year] = (time.time(), result)
    return result


# Per-PIC totals that sit alongside the month keys in the overview result.
# Listed in one place so a new one cannot be computed above and then silently
# dropped while merging.
_PIC_TOTAL_KEYS = ("open_workload", "no_deadline_count", "excluded_status_count")


def merge_pic_overview(data: dict) -> list[dict]:
    """Collapse per-record-ID stats into one row per person.

    get_task_overview_yearly keys by Lark record ID and one person can hold
    several (Nora has two), so their months, tallies and totals are summed.
    Rates are recomputed from the merged counts rather than summed — adding two
    percentages together produced a meaningless number.
    """
    merged: dict[str, dict] = {}
    for pic_id, pic_data in data.items():
        name = PIC_NAME_MAP.get(pic_id, f"User {pic_id[-4:]}")
        if name not in merged:
            merged[name] = {"pic_id": pic_id, "name": name, "months": {}}
        m = merged[name]
        for key in _PIC_TOTAL_KEYS:
            m[key] = m.get(key, 0) + pic_data.get(key, 0)

        for month_key, stats in pic_data.items():
            is_month = isinstance(month_key, int) or (
                isinstance(month_key, str) and month_key.isdigit()
            )
            if not is_month:
                continue
            if month_key not in m["months"]:
                m["months"][month_key] = {
                    k: ({} if isinstance(v, dict) else 0) for k, v in stats.items()
                }
            for k, v in stats.items():
                if v is None:
                    continue
                prev = m["months"][month_key].get(k)
                if isinstance(v, dict):
                    counts = dict(prev or {})
                    for rk, rv in v.items():
                        counts[rk] = counts.get(rk, 0) + rv
                    m["months"][month_key][k] = counts
                else:
                    m["months"][month_key][k] = (prev or 0) + v

    for m in merged.values():
        for stats in m["months"].values():
            filled = stats.get("on_time_filled") or 0
            total = stats.get("total_tasks") or 0
            stats["on_time_rate"] = (
                round(stats.get("on_time_count", 0) / filled * 100, 1) if filled else None
            )
            stats["completion_rate"] = (
                round(stats.get("completed", 0) / total * 100, 1) if total else None
            )

    return [v for v in merged.values() if not v["name"].startswith("User ")]


def get_task_detail(pic_name: str, year: int, month: Optional[int], category: str) -> list[dict]:
    """Return individual task names for a person/month/category for drilldown.

    month is ignored for the "no_deadline" category — those tasks have no
    deadline, so they belong to no month.

    month is optional everywhere else: passing None covers the year from
    _LARK_START_MONTH on, which is exactly the period the scorecards aggregate
    when the Month filter is All.
    """
    today = _today_ict()

    target_pic_ids = {pid for pid, name in PIC_NAME_MAP.items() if name == pic_name}
    if not target_pic_ids:
        return []

    records = _get_cached_records()
    tasks = []

    for rec in records:
        pic_id = _extract_pic_key(rec.get("PIC"))
        if not pic_id or pic_id not in target_pic_ids:
            continue

        status = _norm_status(rec.get("Status"))
        if _is_excluded_status(status):
            continue
        is_completed = status == "completed"

        deadline = _parse_date(rec.get("Deadline"))
        if category == "no_deadline":
            # Mirrors no_deadline_count: open tasks carrying no deadline at
            # all, created since the standardization cutoff.
            if deadline or is_completed:
                continue
            if not _created_in_scope(rec.get("Date Created"), year):
                continue
        elif not deadline or deadline.year != year:
            continue
        elif month is None:
            # No month asked for = the scorecard's "All" period: the whole year
            # from the standardization cutoff on, scoped exactly like
            # total_tasks and open_count so a drilldown can never list tasks the
            # card did not count.
            if deadline.month < _LARK_START_MONTH:
                continue
        elif deadline.month != month:
            continue

        task_name = _extract_text(rec.get("Task"))

        on_time_cat: Optional[str] = None
        if is_completed:
            ot_val = _extract_text(rec.get("On-time vs Original")).lower()
            ot_val = ot_val.replace("-", " ").replace("_", " ")
            if ot_val in ("on time", "ontime", "yes", "true", "đúng hạn", "ok"):
                on_time_cat = "on_time"
            elif ot_val in ("late", "trễ", "no", "false", "over"):
                on_time_cat = "late"

        reason = _norm_reason(rec.get("Late Reason"))
        excused = _is_excused(reason)
        missed = (
            (is_completed and on_time_cat == "late")
            or (not is_completed and deadline is not None and deadline < today)
        )

        include = False
        if category == "total":
            include = True
        elif category == "done":
            include = is_completed
        elif category == "on_time":
            include = is_completed and on_time_cat == "on_time"
        elif category == "late":
            include = is_completed and on_time_cat == "late" and not excused
        elif category == "overdue":
            include = not is_completed and deadline is not None and deadline < today and not excused
        elif category == "missed":
            # Overdue and Late reported as one: every blown deadline without a
            # Late Reason, whether the task got finished afterwards or not.
            include = missed and not excused
        elif category == "excused":
            include = missed and excused
        elif category == "open":
            include = not is_completed
        elif category == "no_deadline":
            include = True  # already filtered above

        if include:
            tasks.append({
                "name": task_name or "(no name)",
                "status": "completed" if is_completed else "open",
                "lark_status": _extract_text(rec.get("Status")),
                "on_time": on_time_cat,
                "deadline": deadline.isoformat() if deadline else None,
                "late_reason": _extract_text(rec.get("Late Reason")),
                "late_note": _extract_text(rec.get("Late Note")),
                "excused": excused if missed else False,
                "lark_url": _lark_record_url(rec.get("_record_id", "")),
            })

    if month is None:
        # Spans several months — oldest deadline first, so what is already
        # overdue sits at the top of the list.
        tasks.sort(key=lambda t: t["deadline"] or "")

    return tasks
