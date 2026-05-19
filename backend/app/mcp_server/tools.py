"""MCP tool wrappers — thin shims over chat_tools.execute_tool().

chat_tools is the single source of truth for what data Claude can read.
The in-app HiD Assistant uses it; this MCP module reuses it without
duplicating SQL or schemas. Each wrapper:
  1. resolves the current User from ContextVar (set by McpAuthMiddleware)
  2. calls chat_tools.execute_tool() to do the work
  3. writes one mcp_audit_log row (ok / error)

v1 access model: every active HiD user gets full access (all tools, all
branches). Per-user scoping can be added later by storing allowlist columns
on the users table and filtering response rows here."""
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


def register_tools(mcp: FastMCP) -> None:
    """Attach all v1 tools to the given FastMCP instance."""

    @mcp.tool()
    def get_branches() -> dict:
        """List the 5 hotel branches in the MEANDER group (id, name, city, country, currency, total_rooms)."""
        started = time.perf_counter()
        try:
            user = _require_user()
            db = SessionLocal()
            try:
                result = execute_tool("get_branches", {}, db, None)
            finally:
                db.close()
            dur = int((time.perf_counter() - started) * 1000)
            audit.record(user, "get_branches", {}, "ok", dur, response=result)
            return result
        except Exception as e:
            dur = int((time.perf_counter() - started) * 1000)
            audit.record(get_current_user(), "get_branches", {}, "error", dur, error_message=str(e))
            logger.exception("MCP get_branches failed")
            raise

    @mcp.tool()
    def get_performance(
        branch_id: Optional[str] = None,
        period: str = "monthly",
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
    ) -> dict:
        """Performance metrics (OCC, ADR, RevPAR, Revenue, bookings, cancellations)
        aggregated daily / weekly / monthly. Pass branch_id='<uuid>' to scope to one
        branch, or omit / 'all' for every branch. Defaults: period='monthly',
        last ~6 months.

        Revenue follows HiD canonical rules: accommodation revenue only,
        excluding Blogger / House Use / KOL / Special Case / Work Exchange."""
        args = {
            "branch_id": branch_id,
            "period": period,
            "date_from": date_from,
            "date_to": date_to,
        }
        started = time.perf_counter()
        try:
            user = _require_user()
            db = SessionLocal()
            try:
                result = execute_tool("get_performance", args, db, None)
            finally:
                db.close()
            dur = int((time.perf_counter() - started) * 1000)
            audit.record(user, "get_performance", args, "ok", dur, response=result)
            return result
        except Exception as e:
            dur = int((time.perf_counter() - started) * 1000)
            audit.record(get_current_user(), "get_performance", args, "error", dur, error_message=str(e))
            logger.exception("MCP get_performance failed")
            raise
