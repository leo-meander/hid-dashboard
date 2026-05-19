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
from mcp.server.transport_security import TransportSecuritySettings

from app.mcp_server.auth import McpAuthMiddleware
from app.mcp_server.tools import register_tools


# DNS rebinding protection: FastMCP's streamable HTTP transport rejects any
# Host header outside this allowlist with 421 Misdirected Request. Behind
# Zeabur the Host arrives as the public hostname, so we add it here. The
# ":*" suffix wildcards the port (Zeabur terminates TLS at the proxy and
# forwards on whatever port — we don't want to hardcode 443).
_TRANSPORT_SECURITY = TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=[
        "localhost:*",
        "127.0.0.1:*",
        "meander-hid-dashboard.zeabur.app",
        "meander-hid-dashboard.zeabur.app:*",
    ],
)

mcp_instance = FastMCP(
    name="hid-mcp",
    instructions=(
        "HiD — Hotel Intelligence Dashboard. Tools here return read-only "
        "marketing + operations data for the MEANDER hotel group (5 branches: "
        "Saigon, Taipei, 1948, Osaka, Oani). Identity is the HiD user who "
        "completed OAuth via /oauth/authorize; every active HiD user gets "
        "full access to all tools and all branches."
    ),
    stateless_http=True,
    transport_security=_TRANSPORT_SECURITY,
)

register_tools(mcp_instance)


# streamable_http_app() returns a Starlette app — wrap with our auth middleware
# BEFORE mounting so unauthenticated requests are rejected with 401 without
# ever reaching the MCP protocol handler.
#
# IMPORTANT: the FastMCP session manager needs to be started inside an async
# context for the streamable HTTP transport to work — otherwise the first
# request raises "Task group is not initialized. Make sure to use run()."
# main.py wires `mcp_instance.session_manager.run()` into the FastAPI lifespan
# so it's active for the lifetime of the process.
_inner_app = mcp_instance.streamable_http_app()


class _MountedApp:
    """Tiny ASGI wrapper that runs auth middleware around the FastMCP app.
    We don't use _inner_app.add_middleware() because Starlette freezes the
    middleware stack once the app has been built."""

    def __init__(self, inner) -> None:
        self._authed = McpAuthMiddleware(inner)

    async def __call__(self, scope, receive, send):
        await self._authed(scope, receive, send)


mcp_asgi_app = _MountedApp(_inner_app)
