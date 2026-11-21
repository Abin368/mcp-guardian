"""Bearer check for the loopback gateway.

The gateway token is ``MCP_GUARDIAN_GATEWAY_TOKEN``. It is not the IPC token.
A missing or wrong bearer is HTTP 401 and never reaches the MCP server.

Authenticated HTTP requests also get a disconnect Event on the ASGI scope so
a pending tools/call can cancel when the client drops the connection. The
event is set when ``http.disconnect`` is observed; the message is still
forwarded so the SDK's SSE path can see it too.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping

from starlette.types import ASGIApp, Receive, Scope, Send

from mcp_guardian.application.client_disconnect import DISCONNECT_STATE_KEY
from mcp_guardian.ipc.protocol import TOKEN_RE, tokens_match

logger = logging.getLogger("mcp_guardian.gateway")

ENV_GATEWAY_TOKEN = "MCP_GUARDIAN_GATEWAY_TOKEN"
_BEARER_PREFIX = b"bearer "
_UNAUTHORIZED = b'{"error":"unauthorized"}'


class GatewayStartupError(Exception):
    """The process must not listen. The message never includes a token."""


def require_gateway_token(environ: Mapping[str, str]) -> str:
    """Return the gateway bearer, or raise if it is missing or unusable.

    Callers must not log the returned value. The IPC token is a different
    secret and is not read here.
    """
    if ENV_GATEWAY_TOKEN not in environ:
        raise GatewayStartupError("MCP_GUARDIAN_GATEWAY_TOKEN is required")
    token = environ[ENV_GATEWAY_TOKEN]
    if not isinstance(token, str) or TOKEN_RE.fullmatch(token) is None:
        raise GatewayStartupError("MCP_GUARDIAN_GATEWAY_TOKEN is invalid")
    return token


def _presented_bearer(scope: Scope) -> str | None:
    """Return the single Bearer token, or None when the header is unusable."""
    matches: list[bytes] = []
    for name, value in scope.get("headers", ()):
        if name.lower() == b"authorization":
            matches.append(value)
    if len(matches) != 1:
        return None
    raw = matches[0]
    if len(raw) <= len(_BEARER_PREFIX) or raw[: len(_BEARER_PREFIX)].lower() != _BEARER_PREFIX:
        return None
    token_bytes = raw[len(_BEARER_PREFIX) :]
    if not token_bytes or b" " in token_bytes:
        return None
    try:
        return token_bytes.decode("ascii")
    except UnicodeDecodeError:
        return None


async def _unauthorized(send: Send) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(_UNAUTHORIZED)).encode("ascii")),
                (b"www-authenticate", b"Bearer"),
                (b"cache-control", b"no-store"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": _UNAUTHORIZED})


class GatewayAuth:
    """Reject HTTP requests that do not present the gateway bearer.

    Lifespan scopes are forwarded so the Streamable HTTP session manager can
    start. The token is not part of ``repr`` and is not written to the log.
    """

    def __init__(self, app: ASGIApp, token: str) -> None:
        if TOKEN_RE.fullmatch(token) is None:
            raise GatewayStartupError("MCP_GUARDIAN_GATEWAY_TOKEN is invalid")
        self._app = app
        self._token = token

    def __repr__(self) -> str:
        return "GatewayAuth(token=redacted)"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Lifespan is not an HTTP request. It has to reach the session manager
        # or Streamable HTTP never starts. Every HTTP request is authenticated.
        if scope["type"] == "lifespan":
            await self._app(scope, receive, send)
            return
        if scope["type"] != "http":
            return
        presented = _presented_bearer(scope)
        if not tokens_match(self._token, presented):
            logger.warning("gateway_unauthorized")
            await _unauthorized(send)
            return
        disconnect = asyncio.Event()
        state = scope.setdefault("state", {})
        state[DISCONNECT_STATE_KEY] = disconnect

        async def receive_with_disconnect() -> object:
            message = await receive()
            if message.get("type") == "http.disconnect":
                disconnect.set()
            return message

        await self._app(scope, receive_with_disconnect, send)
