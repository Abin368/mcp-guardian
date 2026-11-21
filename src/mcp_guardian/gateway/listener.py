"""Bind the gateway to 127.0.0.1 and refuse anything else.

The host is checked before a socket is created. After ``bind``, ``getsockname``
is checked again. A socket that is not IPv4 loopback is closed.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn
from starlette.types import ASGIApp

from mcp_guardian.ipc.protocol import LOOPBACK_HOST

logger = logging.getLogger("mcp_guardian.gateway")

DEFAULT_GATEWAY_PORT = 8080


class GatewayBindError(RuntimeError):
    """The listener is not 127.0.0.1. It must not stay open."""


def open_loopback_listener(port: int, *, host: str = LOOPBACK_HOST) -> socket.socket:
    """Listen on ``127.0.0.1`` only.

    ``port`` may be ``0`` so the OS picks a free port (tests). Any host other
    than ``127.0.0.1`` raises before ``socket.socket`` is called.
    """
    if host != LOOPBACK_HOST:
        raise ValueError("gateway listen host must be 127.0.0.1")
    if isinstance(port, bool) or not isinstance(port, int):
        raise ValueError("gateway port must be an integer")
    if port != 0 and not 1 <= port <= 65535:
        raise ValueError("gateway port is out of range")

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        # Bind the constant, not a caller-supplied address that merely
        # compared equal before the call.
        sock.bind((LOOPBACK_HOST, port))
        sock.listen(128)
        bound_host = sock.getsockname()[0]
        if bound_host != LOOPBACK_HOST or sock.family != socket.AF_INET:
            raise GatewayBindError(
                "gateway refused a listener that is not bound to 127.0.0.1"
            )
        return sock
    except Exception:
        sock.close()
        raise


def assert_only_loopback(server: uvicorn.Server) -> None:
    """Close nothing here; raise if uvicorn kept a non-loopback socket."""
    sockets: list[socket.socket] = []
    for listener in getattr(server, "servers", ()):
        sockets.extend(listener.sockets or [])
    if len(sockets) != 1:
        raise GatewayBindError("gateway must keep exactly one listening socket")
    bound = sockets[0].getsockname()
    if bound[0] != LOOPBACK_HOST or sockets[0].family != socket.AF_INET:
        raise GatewayBindError(
            "gateway refused a listener that is not bound to 127.0.0.1"
        )


def _uvicorn_config(app: ASGIApp, port: int) -> uvicorn.Config:
    # host is 127.0.0.1 even though we pass a pre-bound socket. proxy_headers
    # is off so a client cannot rewrite the peer via X-Forwarded-For.
    # access_log is off so request headers (the bearer) are not written out.
    return uvicorn.Config(
        app,
        host=LOOPBACK_HOST,
        port=port,
        access_log=False,
        log_level="warning",
        lifespan="on",
        proxy_headers=False,
        forwarded_allow_ips=[],
        server_header=False,
        workers=1,
    )


@asynccontextmanager
async def running_loopback(app: ASGIApp, port: int) -> AsyncIterator[int]:
    """Start the ASGI app on one 127.0.0.1 socket and stop it on exit.

    ``Server.serve`` is the SDK-facing entry that installs the lifespan.
    Calling ``startup`` on its own never does, so the session manager would
    not run.
    """
    sock = open_loopback_listener(port)
    bound_port = int(sock.getsockname()[1])
    config = _uvicorn_config(app, bound_port)
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve(sockets=[sock]), name="mcp-guardian-gateway")
    try:
        deadline = asyncio.get_running_loop().time() + 5
        while not server.started:
            if task.done():
                await task
                raise GatewayBindError("gateway listener exited during startup")
            if asyncio.get_running_loop().time() > deadline:
                raise GatewayBindError("gateway listener did not start")
            await asyncio.sleep(0.01)
        assert_only_loopback(server)
        yield bound_port
    finally:
        server.should_exit = True
        if not task.done():
            await asyncio.shield(task)


async def serve_on_loopback(app: ASGIApp, port: int) -> None:
    """Listen until the task is cancelled. MCP bytes stay on this socket."""
    async with running_loopback(app, port) as bound_port:
        logger.info(
            "gateway_listening host=%s port=%s",
            LOOPBACK_HOST,
            bound_port,
        )
        # Park until cancellation (Ctrl+C via asyncio.run).
        await asyncio.Event().wait()
