"""MCP tool wrappers — thin shims over chat_tools.execute_tool().

Why a shim: chat_tools is the single source of truth for what data Claude can
read. The in-app HiD Assistant uses it; this MCP module reuses it without
duplicating SQL or schemas. Each wrapper:
  1. resolves the current ApiKey from ContextVar
  2. checks the key is scoped for this tool
  3. validates branch_id input against allowed_branches (if specified)
  4. calls chat_tools.execute_tool() to do the work
  5. filters the response down to allowed_branches when needed
  6. writes one mcp_audit_log row (ok / denied / error)

We register only get_branches and get_performance in v1. Adding more is a
3-line copy-paste — see the TOOL_HANDLERS dict in chat_tools.py for the full
list of tools already implemented."""
from __future__ import annotations

import logging
import time
from typing import Optional

from mcp.server.fastmcp import FastMCP

from app.database import SessionLocal
from app.mcp_server import audit
from app.mcp_server.auth import (
    allowed_branches,
    branch_allowed,
    get_current_api_key,
    tool_allowed,
)
from app.services.chat_tools import execute_tool

logger = logging.getLogger(__name__)


class McpAccessDenied(Exception):
    """Raised when the current API key lacks scope for the requested tool/branch.
    FastMCP serializes this into a MCP error response that the client Claude
    will see and surface to the user."""


def _require_key_and_scope(tool_name: str):
    """Common pre-flight: returns the ApiKey or raises McpAccessDenied."""
    api_key = get_current_api_key()
    if api_key is None:
        # Should be unreachable — middleware enforces 401 before we get here.
        raise McpAccessDenied("Unauthenticated request reached tool handler")
    if not tool_allowed(api_key, tool_name):
        raise McpAccessDenied(
            f"API key '{api_key.name}' is not scoped for tool '{tool_name}'. "
            "Ask an admin to add it to allowed_tools."
        )
    return api_key


def _filter_branches_in_list(items: list, allow: Optional[set[str]]) -> list:
    if allow is None:
        return items
    out = []
    for it in items:
        bid = it.get("id") or it.get("branch_id")
        if bid is None or str(bid) in allow:
            out.append(it)
    return out


def register_tools(mcp: FastMCP) -> None:
    """Attach all v1 tools to the given FastMCP instance."""

    @mcp.tool()
    def get_branches() -> dict:
        """List hotel branches this key may access (id, name, city, country, currency, total_rooms)."""
        started = time.perf_counter()
        try:
            api_key = _require_key_and_scope("get_branches")
            allow = allowed_branches(api_key)
            db = SessionLocal()
            try:
                result = execute_tool("get_branches", {}, db, None)
                result["branches"] = _filter_branches_in_list(result.get("branches", []), allow)
            finally:
                db.close()
            dur = int((time.perf_counter() - started) * 1000)
            audit.record(api_key, "get_branches", {}, "ok", dur, response=result)
            return result
        except McpAccessDenied as e:
            dur = int((time.perf_counter() - started) * 1000)
            audit.record(get_current_api_key(), "get_branches", {}, "denied", dur, error_message=str(e))
            raise
        except Exception as e:
            dur = int((time.perf_counter() - started) * 1000)
            audit.record(get_current_api_key(), "get_branches", {}, "error", dur, error_message=str(e))
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
        aggregated daily / weekly / monthly. Pass branch_id='<uuid>' to scope
        to one branch, or omit / 'all' for every branch this key may access.
        Defaults: period='monthly', last ~6 months.

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
            api_key = _require_key_and_scope("get_performance")

            # Reject up-front if caller requested a specific branch they can't see.
            if branch_id and str(branch_id).lower() != "all" and not branch_allowed(api_key, branch_id):
                raise McpAccessDenied(
                    f"API key '{api_key.name}' is not scoped for branch_id={branch_id}"
                )

            allow = allowed_branches(api_key)
            db = SessionLocal()
            try:
                result = execute_tool("get_performance", args, db, None)
                if allow is not None:
                    result["rows"] = [r for r in result.get("rows", []) if str(r.get("branch_id")) in allow]
            finally:
                db.close()
            dur = int((time.perf_counter() - started) * 1000)
            audit.record(api_key, "get_performance", args, "ok", dur, response=result)
            return result
        except McpAccessDenied as e:
            dur = int((time.perf_counter() - started) * 1000)
            audit.record(get_current_api_key(), "get_performance", args, "denied", dur, error_message=str(e))
            raise
        except Exception as e:
            dur = int((time.perf_counter() - started) * 1000)
            audit.record(get_current_api_key(), "get_performance", args, "error", dur, error_message=str(e))
            logger.exception("MCP get_performance failed")
            raise
