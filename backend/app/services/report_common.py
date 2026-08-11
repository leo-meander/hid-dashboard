"""
Shared primitives for the report renderers (Weekly + Bi-Weekly).

These started life as private helpers inside `routers/report.py`. They were
lifted out when the Bi-Weekly Branch Manager report landed, because both
reports need identical behaviour in two places that actually matter:

  - `cell_attrs()` — the `data-metric-key` / `data-branch-id` contract the
    frontend comment drawer keys off. If the two reports emitted different
    attributes, clicking a cell on one page would silently open nothing.
  - `safe_section()` — the rollback-on-failure wrapper that keeps one slow
    query from killing an entire report.

`routers/report.py` keeps its old private names as thin aliases so the
existing 3k-line module didn't need a rewrite.
"""
import logging
from datetime import datetime, date, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

ICT_TZ = ZoneInfo("Asia/Ho_Chi_Minh")

MONTHS_EN = ["", "January", "February", "March", "April", "May", "June",
             "July", "August", "September", "October", "November", "December"]


def ict_today() -> date:
    """Today's date in Asia/Ho_Chi_Minh.

    The report crons fire in the evening UTC, which is already the next day
    in ICT. Resolving "today" with `date.today()` on a UTC server therefore
    lands on the wrong side of a week boundary and rolls the reporting
    window a full week back. Always resolve in ICT so a report tracks the
    team's calendar.
    """
    return datetime.now(ICT_TZ).date()


def attr_escape(value: str) -> str:
    """Escape HTML attribute special characters so values containing
    quotes/ampersands don't break the rendered tag.
    """
    return (
        str(value)
        .replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def cell_attrs(branch_id, metric_key: str, label: Optional[str] = None) -> str:
    """Render the data-* attributes the frontend uses to detect a
    clickable metric cell or row. Email clients ignore `class` and
    `data-*` attributes that don't have inline styles backing them, so
    this is safe to include in the same HTML the email send pipeline
    produces.

    `label`, when provided, is read by the frontend as a human-readable
    drawer title — useful for dynamic keys (per-country, per-source rows)
    where the static frontend label map can't enumerate every variation.
    """
    bid = f' data-branch-id="{branch_id}"' if branch_id else ''
    lbl = f' data-metric-label="{attr_escape(label)}"' if label else ''
    return f' class="hid-metric-cell" data-metric-key="{metric_key}"{bid}{lbl}'


def safe_section(db: Session, label: str, fn, default):
    """Run `fn()` and degrade to `default` on failure so one slow query
    can't kill the whole report. After a Postgres statement_timeout the
    session is in an aborted-transaction state and every subsequent query
    would fail — rollback() here clears it so the next section can keep
    querying.
    """
    try:
        return fn()
    except Exception as e:
        logger.warning("Report section '%s' failed: %s: %s", label, type(e).__name__, e)
        try:
            db.rollback()
        except Exception:
            pass
        return default


def envelope(data):
    """The project-wide API response envelope."""
    return {"success": True, "data": data, "error": None,
            "timestamp": datetime.now(timezone.utc).isoformat()}


# ── Number formatting (team display rules) ───────────────────────────────────


def fmt(val, currency=""):
    """Full number with currency symbol, no K/M/B abbreviation (per team rule)."""
    if val is None:
        return "—"
    sym = {"VND": "₫", "TWD": "NT$", "JPY": "¥"}.get(currency, currency + " ")
    return f"{sym}{round(val):,}"


def pct(val):
    """Percentage with 2 decimals (per team rule)."""
    if val is None:
        return "—"
    return f"{val:.2f}%"


def num(val):
    """Integer with thousands separator."""
    if val is None:
        return "—"
    return f"{int(val):,}"


def signed_pct(val):
    """Signed percentage with 2 decimals (e.g. +12.34% / -5.67%)."""
    if val is None:
        return "—"
    sign = "+" if val >= 0 else ""
    return f"{sign}{val:.2f}%"


def signed_pts(val):
    """Signed OCC delta, 1 decimal, shown with a '%' suffix like every other
    delta in the report (e.g. +2.4% / −5.7%).

    The number itself is still a percentage-POINT move (occupancy 80% to 84%
    is +4, not the +5% a relative change would read as) — only the label
    changed, per team feedback (2026-08-11): a mix of "%" and "pts" across
    the report read as inconsistent, and "%" is how the team already talks
    about occupancy moves informally. Bi-weekly report only.
    """
    if val is None:
        return "—"
    sign = "+" if val >= 0 else "−"
    return f"{sign}{abs(val):.1f}%"
