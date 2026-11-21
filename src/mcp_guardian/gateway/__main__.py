"""CLI entry: ``python -m mcp_guardian.gateway``.

Speaks MCP over Streamable HTTP on 127.0.0.1. Diagnostics stay on stderr.
The gateway bearer is ``MCP_GUARDIAN_GATEWAY_TOKEN`` and is never logged.
The IPC token is a different secret and is not an HTTP header.

``--approval ipc`` (the default) asks the Guardian UI. ``deny-all`` blocks
every tool call. ``allow-all`` is explicit local wiring only.

This does not change ``python -m mcp_guardian.proxy`` or its deny-all default.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from collections.abc import Mapping, Sequence

from mcp import Client, StdioServerParameters

from mcp_guardian.application.pending_coordinator import PendingRequestCoordinator
from mcp_guardian.gateway.app import build_gateway_app
from mcp_guardian.gateway.auth import GatewayStartupError, require_gateway_token
from mcp_guardian.gateway.hosts import normalize_allowed_host
from mcp_guardian.gateway.listener import DEFAULT_GATEWAY_PORT, serve_on_loopback
from mcp_guardian.ipc.protocol import LOOPBACK_HOST
from mcp_guardian.proxy.__main__ import (
    _build_approval,
    build_arg_parser,
    config_from_namespace,
)
from mcp_guardian.proxy.config import ProxyConfig
from mcp_guardian.proxy.logging_config import configure_proxy_logging
from mcp_guardian.proxy.proxy import GuardianProxy

logger = logging.getLogger("mcp_guardian.gateway")

# Claude.ai drops a hosted tool call after about 240 seconds. 180 leaves
# room for the dialog to finish first. The STDIO proxy keeps its own cap.
MAX_GATEWAY_APPROVAL_TIMEOUT = 180.0


def build_gateway_parser() -> argparse.ArgumentParser:
    """Proxy flags, with gateway defaults. Does not change the STDIO parser."""
    parser = build_arg_parser()
    parser.prog = "mcp-guardian-gateway"
    parser.description = (
        "MCP Guardian loopback gateway. Clients speak Streamable HTTP to "
        "127.0.0.1. Tool calls are gated before one upstream MCP server. "
        "Requires MCP_GUARDIAN_GATEWAY_TOKEN. Does not open a public tunnel."
    )
    for action in parser._actions:
        if action.dest == "approval":
            action.default = "ipc"
            action.help = (
                "ipc asks the Guardian UI on 127.0.0.1 (default). "
                "deny-all blocks every call. "
                "allow-all approves every call and is only for local wiring."
            )
    parser.add_argument(
        "--listen-host",
        default=LOOPBACK_HOST,
        help="Must be 127.0.0.1. Any other host is refused before a socket is opened.",
    )
    parser.add_argument(
        "--listen-port",
        type=int,
        default=DEFAULT_GATEWAY_PORT,
        help=f"Loopback TCP port (default {DEFAULT_GATEWAY_PORT}).",
    )
    parser.add_argument(
        "--allowed-host",
        action="append",
        default=[],
        dest="allowed_hosts",
        metavar="HOST",
        help=(
            "Extra Host header pattern to allow (repeatable). "
            "Loopback hosts stay allowed. Does not bind a public socket or "
            "start a tunnel; only prepares DNS-rebinding allowlisting."
        ),
    )
    return parser


def gateway_settings_from_args(
    args: argparse.Namespace,
) -> tuple[ProxyConfig, int, tuple[str, ...]]:
    """Validate the listen address, then reuse the STDIO proxy config."""
    if args.listen_host != LOOPBACK_HOST:
        raise ValueError("gateway listen host must be 127.0.0.1")
    if isinstance(args.listen_port, bool) or not isinstance(args.listen_port, int):
        raise ValueError("gateway port must be an integer")
    if args.listen_port != 0 and not 1 <= args.listen_port <= 65535:
        raise ValueError("gateway port is out of range")
    if (
        args.approval_timeout <= 0
        or args.approval_timeout > MAX_GATEWAY_APPROVAL_TIMEOUT
    ):
        raise ValueError(
            "approval timeout must be greater than 0 and at most 180 seconds"
        )
    extra_hosts = tuple(normalize_allowed_host(h) for h in args.allowed_hosts)
    return config_from_namespace(args), args.listen_port, extra_hosts


def configure_gateway_logging(level: int) -> None:
    """Stderr only. The bearer and the IPC token are not log fields."""
    configure_proxy_logging(level)
    logger.handlers.clear()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False


async def run_gateway(
    config: ProxyConfig,
    *,
    token: str,
    port: int,
    allowed_hosts: Sequence[str] = (),
) -> None:
    """Serve one upstream behind GuardianProxy on the loopback HTTP listener."""
    coordinator = PendingRequestCoordinator()
    approval = _build_approval(config, coordinator)
    upstream_params = StdioServerParameters(
        command=config.upstream.command,
        args=list(config.upstream.args),
        cwd=config.upstream.cwd,
        env=dict(config.upstream.env) if config.upstream.env else None,
    )
    logger.info(
        "starting gateway server_name=%s upstream_command=%s approval=%s",
        config.server_name,
        config.upstream.command,
        config.approval_mode,
    )
    proxy: GuardianProxy | None = None
    try:
        async with Client(upstream_params) as upstream:
            proxy = GuardianProxy(
                upstream=upstream,
                approval=approval,
                server_name=config.server_name,
                proxy_name=config.proxy_name,
                proxy_version=config.proxy_version,
                approval_timeout=config.approval_timeout_seconds,
                coordinator=coordinator,
            )
            app = build_gateway_app(
                proxy.build_server(),
                token=token,
                allowed_hosts=allowed_hosts,
            )
            await serve_on_loopback(app, port)
    except Exception:
        logger.exception("gateway_terminated")
        raise
    finally:
        if proxy is not None:
            await proxy.shutdown()
        aclose = getattr(approval, "aclose", None)
        if aclose is not None:
            await aclose()
        logger.info("gateway_shutdown_complete")


def run_main(
    argv: Sequence[str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    parser = build_gateway_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code
        return int(code) if isinstance(code, int) else 1

    source = environ if environ is not None else os.environ
    try:
        token = require_gateway_token(source)
        config, port, allowed_hosts = gateway_settings_from_args(args)
    except (GatewayStartupError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    configure_gateway_logging(getattr(logging, args.log_level))
    try:
        asyncio.run(
            run_gateway(
                config,
                token=token,
                port=port,
                allowed_hosts=allowed_hosts,
            )
        )
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("interrupted")
        return 0
    except Exception:
        return 1
    return 0


def main() -> None:
    raise SystemExit(run_main())


if __name__ == "__main__":
    main()
