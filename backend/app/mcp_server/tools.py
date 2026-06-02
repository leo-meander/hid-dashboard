"""MCP tool wrappers — thin shims over chat_tools.execute_tool().

chat_tools is the single source of truth for what data Claude can read.
The in-app HiD Assistant uses it; this MCP module reuses it so any tool
improvement automatically applies to both surfaces. Each `@mcp.tool()`
function:
  1. validates auth via ContextVar (set by McpAuthMiddleware)
  2. forwards to chat_tools.execute_tool() with the inputs
  3. writes one mcp_audit_log row (ok / error)

v1 access model: every active HiD user gets full access (all tools, all
branches). Per-user scoping can be added later by reading allowlist columns
off the User row and filtering response rows here."""
from __future__ import annotations

import logging
import time
from typing import Optional

from mcp.server.fastmcp import FastMCP

from app.database import SessionLocal
from app.mcp_server import audit
from app.mcp_server.auth import get_current_user
from app.services.chat_tools import execute_tool

logger = logging.getLogger(__name__)


def _require_user():
    """Return the User authenticated for this request. Should be unreachable
    when None because the middleware enforces 401 before tools run."""
    user = get_current_user()
    if user is None:
        raise RuntimeError("Unauthenticated request reached tool handler")
    return user


def _run(name: str, args: dict) -> dict:
    """Common scaffolding for every MCP tool: auth, dispatch, audit."""
    started = time.perf_counter()
    try:
        user = _require_user()
        db = SessionLocal()
        try:
            result = execute_tool(name, args, db, None)
        finally:
            db.close()
        dur = int((time.perf_counter() - started) * 1000)
        audit.record(user, name, args, "ok", dur, response=result)
        return result
    except Exception as e:
        dur = int((time.perf_counter() - started) * 1000)
        audit.record(get_current_user(), name, args, "error", dur, error_message=str(e))
        logger.exception("MCP %s failed", name)
        raise


def register_tools(mcp: FastMCP) -> None:
    """Attach all tools to the given FastMCP instance.

    Tool descriptions are imported verbatim from chat_tools.TOOL_DEFS so the
    in-app HiD Assistant and MCP share their guidance to Claude."""

    @mcp.tool()
    def get_branches() -> dict:
        """List the 5 hotel branches in the MEANDER group (id, name, city, country, currency, total_rooms).
        Use to resolve branch names to IDs before calling other tools."""
        return _run("get_branches", {})

    @mcp.tool()
    def get_performance(
        branch_id: Optional[str] = None,
        period: str = "monthly",
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
    ) -> dict:
        """Historical performance metrics (OCC, ADR, RevPAR, Revenue, bookings,
        cancellations) aggregated daily / weekly / monthly. Pass branch_id='<uuid>'
        to scope to one branch, or omit / 'all' for every branch. Defaults:
        period='monthly', last ~6 months.

        ADR (avg_adr_native) is blended across private rooms AND dorm beds; the
        per-segment split is also returned as avg_room_adr_native (private rooms)
        and avg_dorm_adr_native (dorm beds), so ADR CAN be broken out by dorm vs
        room. Dorm-heavy branches (Taipei, 1948, Oani) have a much lower dorm ADR
        than the blended figure.

        IMPORTANT: this returns ONLY what already happened (no forecast). For
        end-of-month projection, target achievement, or "are we on track"
        questions about an in-progress month, use get_kpi_status instead.

        Revenue follows HiD canonical rules: accommodation revenue only,
        excluding Blogger / House Use / KOL / Special Case / Work Exchange."""
        return _run("get_performance", {
            "branch_id": branch_id, "period": period,
            "date_from": date_from, "date_to": date_to,
        })

    @mcp.tool()
    def get_kpi_status(
        branch_id: Optional[str] = None,
        year: Optional[int] = None,
        month: Optional[int] = None,
    ) -> dict:
        """Revenue KPI achievement vs target for a given month. Returns target,
        actual revenue so far, achievement %, projected end-of-month revenue,
        and gap to target. Use when the user asks about KPI, target, achievement,
        'are we on track', or 'full-month revenue' for an in-progress month.

        For a future or in-progress month, projected_eom extrapolates the
        current pace across the remaining days — this is the right number for
        "full May" / "full June" forecast questions."""
        return _run("get_kpi_status", {
            "branch_id": branch_id, "year": year, "month": month,
        })

    @mcp.tool()
    def get_ota_mix(
        branch_id: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
    ) -> dict:
        """Channel mix breakdown — bookings + revenue per channel (Booking.com,
        Agoda, Direct, etc.) over a period. Use for 'channel mix', 'OTA share',
        'Direct vs OTA' questions."""
        return _run("get_ota_mix", {
            "branch_id": branch_id, "date_from": date_from, "date_to": date_to,
        })

    @mcp.tool()
    def get_country_breakdown(
        branch_id: Optional[str] = None,
        days: int = 30,
        limit: int = 10,
    ) -> dict:
        """Top guest source countries by booking volume + revenue over the last
        N days, with growth comparison vs prior period. Use for 'top markets',
        'where are guests from', 'growing markets'."""
        return _run("get_country_breakdown", {
            "branch_id": branch_id, "days": days, "limit": limit,
        })

    @mcp.tool()
    def get_source_by_country(
        branch_id: Optional[str] = None,
        source: Optional[str] = None,
        source_category: Optional[str] = None,
        country: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        days: int = 7,
        date_basis: str = "reservation",
        limit: int = 15,
    ) -> dict:
        """Bookings + revenue broken down by source AND country together — the
        source × country cross-tab. Each row is one (source, country) pair with
        current-period bookings, revenue, prior-period bookings, and growth
        (delta + %).

        Use when the user wants both dimensions at once: 'which country grew
        Website/Booking Engine bookings last week', 'which markets drove Agoda',
        'Direct bookings by country', 'where did OTA growth come from'. Filter
        with `source` (substring match on raw source name, e.g. 'website',
        'booking engine', 'agoda', 'booking.com') and/or `source_category`
        ('OTA' | 'Direct' | 'Local travel agency'); pass `country` to pin one
        market. date_basis='reservation' (when booked, default) or 'checkin'.
        Defaults to the last 7 days vs the prior 7 days. growth_pct is null for
        a market that was new this period (no prior-period bookings).

        For top markets WITHOUT a source split use get_country_breakdown; for
        channel mix WITHOUT a country split use get_ota_mix."""
        return _run("get_source_by_country", {
            "branch_id": branch_id, "source": source,
            "source_category": source_category, "country": country,
            "date_from": date_from, "date_to": date_to, "days": days,
            "date_basis": date_basis, "limit": limit,
        })

    @mcp.tool()
    def get_alerts(
        branch_id: Optional[str] = None,
        severity: str = "all",
    ) -> dict:
        """Active alerts — anomalies / issues the system flagged today (OCC drops,
        cancellation spikes, ROAS slipping, etc.). severity = 'all' | 'critical'
        | 'warning' | 'info'. Use when the user asks 'what's wrong', 'any alerts',
        or wants to triage issues."""
        return _run("get_alerts", {"branch_id": branch_id, "severity": severity})

    @mcp.tool()
    def get_upcoming_holidays(days: int = 60) -> dict:
        """Upcoming holiday windows across source markets in the next N days,
        with travel propensity and recommended action notes. Use for 'upcoming
        holidays', 'what to plan for', seasonal pushes."""
        return _run("get_upcoming_holidays", {"days": days})

    @mcp.tool()
    def get_ads_performance(
        branch_id: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
    ) -> dict:
        """Paid ads aggregates: spend, revenue, ROAS, impressions, clicks,
        bookings — grouped by channel and target country. Includes top
        performers and worst performers. Use for ad performance questions
        ('how are our Meta ads doing', 'which campaigns are losing money')."""
        return _run("get_ads_performance", {
            "branch_id": branch_id, "date_from": date_from, "date_to": date_to,
        })

    @mcp.tool()
    def get_kol_performance(branch_id: Optional[str] = None) -> dict:
        """KOL summary: invited, collaborated, posted, organic bookings, and
        rights expiring soon. Use for KOL / influencer questions."""
        return _run("get_kol_performance", {"branch_id": branch_id})

    @mcp.tool()
    def get_country_profile(
        branch_id: Optional[str] = None,
        country: Optional[str] = None,
        days: int = 90,
        limit: int = 10,
    ) -> dict:
        """Detailed booking profile for one or many source countries: lead time
        (avg + 0-7 / 8-30 / 31-60 / 60+ buckets), length of stay, pax distribution
        (solo=1 adult, couple=2, friends=3-4, family=5+), room type split
        (Dorm vs Room), and revenue.

        Use when the user asks about lead time, pax/segment composition, room
        type by country, 'who books from X', 'what target should we run for X',
        or any booking-behavior question. Pass `country` to drill into one
        country (also returns its top 5 room_type names); omit to get top N
        countries. Excludes cancellations and non-paying sources (KOL, Blogger,
        House Use, Special Case, Work Exchange, Maintenance) so figures reflect
        real paying guests."""
        return _run("get_country_profile", {
            "branch_id": branch_id, "country": country, "days": days, "limit": limit,
        })

    @mcp.tool()
    def get_marketing_activity(
        branch_id: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
    ) -> dict:
        """Consolidated marketing activity for a date range: CRM bookings, KOL
        bookings, paid ads bookings + revenue. Filtered by reservation_date
        (when booked), not check_in_date. Use for 'how's marketing performing'."""
        return _run("get_marketing_activity", {
            "branch_id": branch_id, "date_from": date_from, "date_to": date_to,
        })

    @mcp.tool()
    def get_cancellation_leadtime(
        branch_id: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
    ) -> dict:
        """How long before check-in the CANCELLED / no-show cohort cancelled:
        days between cancel date and check_in_date, bucketed (after/same-day,
        1-7, 8-30, 31-60, 60+ days) with avg + median. Use for 'how far in
        advance were the cancellations' and cancellation-timing questions.

        NOTE: the cancel date is APPROXIMATE — derived from the reservation's
        last-modified timestamp (Cloudbeds exposes no exact cancellationDate in
        HiD's data; for a cancelled booking the final modification is effectively
        the cancellation). Filtered by check_in_date; defaults to last 90 days."""
        return _run("get_cancellation_leadtime", {
            "branch_id": branch_id, "date_from": date_from, "date_to": date_to,
        })
