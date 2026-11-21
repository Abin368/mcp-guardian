"""ASGI app: bearer check in front of the SDK Streamable HTTP transport.

``GuardianProxy.build_server()`` is the MCP server. The SDK's
``Server.streamable_http_app`` owns the session manager and speaks MCP on
HTTP. This module does not attach stdio and does not add OAuth routes.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from mcp.server import Server
from starlette.types import ASGIApp

from mcp_guardian.gateway.auth import GatewayAuth
from mcp_guardian.gateway.hosts import build_transport_security
from mcp_guardian.ipc.protocol import LOOPBACK_HOST

MCP_PATH = "/mcp"


def build_gateway_app(
    server: Server[Any],
    *,
    token: str,
    allowed_hosts: Sequence[str] | None = None,
) -> ASGIApp:
    """Return the loopback gateway ASGI app for an already built MCP server.

    ``token`` is the gateway bearer. The server is stateful Streamable HTTP
    with SSE responses, which is what a remote client uses for ``initialize``
    followed by later calls on ``Mcp-Session-Id``.

    ``host`` stays ``127.0.0.1`` so the SDK never treats a public name as a
    reason to disable DNS-rebinding protection. Extra Host patterns (for a
    future tunnel) go through ``allowed_hosts`` into an explicit
    ``transport_security`` allowlist with protection left on. The listen
    socket itself is bound separately and only to loopback.
    """
    transport_security = build_transport_security(allowed_hosts)
    inner = server.streamable_http_app(
        streamable_http_path=MCP_PATH,
        json_response=False,
        stateless_http=False,
        host=LOOPBACK_HOST,
        transport_security=transport_security,
    )
    return GatewayAuth(inner, token)
