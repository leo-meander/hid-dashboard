"""HiD MCP server — exposes HiD data tools to external Claude clients.

Mounted at /mcp on the main FastAPI app. Auth via X-API-Key header or
Authorization: Bearer <key>, validated against the existing api_keys table.
Each key has per-tool and per-branch scopes (allowed_tools, allowed_branches);
default is DENY so a freshly created key can do nothing until an admin scopes it.

Every tool call (allowed, denied, or errored) writes one row to mcp_audit_log.
"""
from app.mcp_server.server import mcp_asgi_app

__all__ = ["mcp_asgi_app"]
