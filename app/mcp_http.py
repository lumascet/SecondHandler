"""The MCP server inside the app: Streamable HTTP at /mcp (docs/agent-api.md, README "MCP").

A remote agent needs no local process and no repo:

  claude mcp add --transport http secondhandler https://<host>/mcp --header "Authorization: Bearer <token>"

It serves the very tools of app/mcp_server.py. Instead of an HTTP client of `/api/*` they call the app in-process
(httpx.ASGITransport, no network, no port) and forward the credentials the /mcp request came with, so `/api/*`
checks them exactly as for any other client. /mcp itself is guarded by DashboardAuth like the JSON API (bearer token,
Basic auth, login cookie; without a password the home-network rule of the API).

Stateless + JSON responses: every POST stands alone (no session to keep sticky behind a reverse proxy) and the
credentials of each request reach the tool that runs for it.
"""
from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

log = logging.getLogger("secondhandler")

# What of the /mcp request the tools pass on to /api/*: the credentials, and the host so that login_needed() sees the
# same address (a request over the public web address needs the login there as well).
FORWARD = ("authorization", "cookie", "host", "x-forwarded-host", "x-forwarded-proto")


@dataclass
class InProcess:
    """The app to call and the headers of the /mcp request being served."""
    app: ASGIApp
    headers: dict[str, str] = field(default_factory=dict)


# Set by Endpoint for the duration of one /mcp request; the tools of mcp_server.py read it. None: stdio mode (HTTP client).
current: ContextVar[InProcess | None] = ContextVar("mcp_in_process", default=None)

_app: ASGIApp | None = None
_manager: Any = None      # the running StreamableHTTPSessionManager, only while the app runs


class Endpoint:
    """ASGI app behind /mcp: hands the request to the session manager with the caller's credentials in `current`."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return
        if _manager is None or _app is None:
            await JSONResponse({"detail": "MCP server is not running"}, status_code=503)(scope, receive, send)
            return
        if scope["method"] != "POST":     # stateless: no server-to-client stream (GET) and no session to end (DELETE)
            await JSONResponse({"detail": "POST only"}, status_code=405, headers={"allow": "POST"})(scope, receive, send)
            return
        h = Headers(scope=scope)
        token = current.set(InProcess(_app, {k: h[k] for k in FORWARD if k in h}))
        try:
            await _manager.handle_request(scope, receive, send)
        finally:
            current.reset(token)


def install(app: Any) -> None:
    """Serve /mcp (with and without the slash, so a client is never redirected away from its POST)."""
    global _app
    _app = app
    endpoint = Endpoint()
    for path in ("/mcp", "/mcp/"):
        app.router.add_route(path, endpoint, include_in_schema=False)


@contextlib.asynccontextmanager
async def running() -> AsyncIterator[None]:
    """Run the MCP session manager while the app runs (around the lifespan's yield). A manager can run only once,
    so a fresh one is built per app start (the tests enter the lifespan many times). If it cannot start, the app
    still comes up without /mcp (503)."""
    global _manager
    try:
        from mcp.server.transport_security import TransportSecuritySettings

        from app import mcp_server
        # The library's DNS-rebinding guard only knows localhost, and would turn every other Host away (the public
        # address, the LAN name). Off on purpose: DashboardAuth already guards /mcp like /api/ (it demands the bearer
        # token, the password or the login cookie, or applies the home-network rule), and that is where it is decided.
        mcp_server.mcp.streamable_http_app(
            streamable_http_path="/mcp", json_response=True, stateless_http=True,
            transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))
        manager = mcp_server.mcp.session_manager
    except Exception as e:  # noqa: BLE001 - the dashboard must not depend on the MCP library
        log.warning("MCP server (/mcp) not started: %s", e)
        yield
        return
    async with manager.run():
        _manager = manager
        try:
            yield
        finally:
            _manager = None
