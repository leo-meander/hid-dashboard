"""FastMCP server instance + ASGI app for mounting on /mcp.

Usage (in app/main.py):
    from app.mcp_server import mcp_asgi_app
    app.mount("/mcp", mcp_asgi_app)

Client config (Claude Desktop / Claude Code):
    {
      "mcpServers": {
        "hid": {
          "url": "https://<zeabur-host>/mcp/mcp/",
          "headers": {"Authorization": "Bearer hid_..."}
        }
      }
    }

NOTE on the URL path: streamable_http_app() returns a Starlette app whose
internal endpoint is `/mcp`. Mounting it under /mcp on the parent app makes
the externally-visible URL `/mcp/mcp/`. We accept that for now to keep this
file tiny; if it bites users we can configure FastMCP's internal path later.
"""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from app.mcp_server.auth import McpAuthMiddleware
from app.mcp_server.tools import register_tools


_mcp = FastMCP(
    name="hid-mcp",
    instructions=(
        "HiD — Hotel Intelligence Dashboard. Tools here return read-only "
        "marketing + operations data for the MEANDER hotel group (5 branches: "
        "Saigon, Taipei, 1948, Osaka, Oani). Every tool call is scoped by the "
        "API key the user configured; tools may return empty results when the "
        "key isn't permitted to see a given branch."
    ),
    stateless_http=True,
)

register_tools(_mcp)


# streamable_http_app() returns a Starlette app — wrap with our auth middleware
# BEFORE mounting so unauthenticated requests are rejected with 401 without
# ever reaching the MCP protocol handler.
_inner_app = _mcp.streamable_http_app()


class _MountedApp:
    """Tiny ASGI wrapper that runs auth middleware around the FastMCP app.
    We don't use _inner_app.add_middleware() because Starlette freezes the
    middleware stack once the app has been built."""

    def __init__(self, inner) -> None:
        self._authed = McpAuthMiddleware(inner)

    async def __call__(self, scope, receive, send):
        await self._authed(scope, receive, send)


mcp_asgi_app = _MountedApp(_inner_app)
