"""Loopback Streamable HTTP gateway.

Claude Web reaches this process over HTTP. The STDIO proxy is a separate
entry point and is unchanged. This package does not open a public tunnel
and does not implement OAuth.
"""

from mcp_guardian.gateway.app import MCP_PATH, build_gateway_app
from mcp_guardian.gateway.auth import (
    ENV_GATEWAY_TOKEN,
    GatewayStartupError,
    require_gateway_token,
)
from mcp_guardian.gateway.hosts import (
    DEFAULT_ALLOWED_HOSTS,
    build_transport_security,
    normalize_allowed_host,
)
from mcp_guardian.gateway.listener import DEFAULT_GATEWAY_PORT, open_loopback_listener

__all__ = [
    "DEFAULT_ALLOWED_HOSTS",
    "DEFAULT_GATEWAY_PORT",
    "ENV_GATEWAY_TOKEN",
    "MCP_PATH",
    "GatewayStartupError",
    "build_gateway_app",
    "build_transport_security",
    "normalize_allowed_host",
    "open_loopback_listener",
    "require_gateway_token",
]
