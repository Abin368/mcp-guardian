"""CLI entry: ``python -m mcp_guardian.proxy``.

Speaks MCP over STDIO toward Claude Desktop; launches one configured upstream
MCP server as a subprocess. Diagnostics stay on stderr.

``--approval deny-all`` (the default) blocks every tool call.
``--approval allow-all`` is explicit local wiring only.
``--approval ipc`` asks the Guardian UI over 127.0.0.1. Start the UI first.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sys
from collections.abc import Sequence
from pathlib import Path

from mcp import Client, StdioServerParameters
from mcp.server.stdio import stdio_server

from mcp_guardian.application.pending_coordinator import PendingRequestCoordinator
from mcp_guardian.ipc.endpoint import IpcEndpoint, default_endpoint_path
from mcp_guardian.ipc.protocol import LOOPBACK_HOST, validate_token
from mcp_guardian.proxy.approval_provider import (
    ApprovalProvider,
    make_allow_all_provider,
    make_deny_all_provider,
)
from mcp_guardian.proxy.config import ProxyConfig, UpstreamStdioConfig
from mcp_guardian.proxy.ipc_approval import IpcApprovalProvider
from mcp_guardian.proxy.logging_config import configure_proxy_logging
from mcp_guardian.proxy.proxy import GuardianProxy

_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

logger = logging.getLogger("mcp_guardian.proxy")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mcp-guardian-proxy",
        description=(
            "MCP Guardian STDIO proxy: Claude Desktop talks to this process; "
            "tool calls are gated before reaching one upstream MCP server."
        ),
    )
    parser.add_argument(
        "--server-name",
        required=True,
        help="Logical upstream server name shown in approval metadata",
    )
    parser.add_argument(
        "--upstream-command",
        required=True,
        help="Executable that launches the upstream MCP server (local config only)",
    )
    parser.add_argument(
        "--upstream-arg",
        action="append",
        default=[],
        dest="upstream_args",
        help="Argument for the upstream command (repeatable; never from tool args)",
    )
    parser.add_argument(
        "--upstream-cwd",
        default=None,
        help="Working directory for the upstream subprocess",
    )
    parser.add_argument(
        "--upstream-env",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help=(
            "Environment variable for the upstream process only "
            "(repeatable; never taken from tool arguments)"
        ),
    )
    parser.add_argument(
        "--approval",
        choices=("deny-all", "allow-all", "ipc"),
        default="deny-all",
        help=(
            "deny-all blocks every call (default). "
            "allow-all approves every call and is only for local wiring. "
            "ipc asks the Guardian UI on 127.0.0.1."
        ),
    )
    parser.add_argument(
        "--approval-timeout",
        type=float,
        default=120.0,
        help="Seconds to wait for approval before failing closed",
    )
    parser.add_argument(
        "--ipc-endpoint-file",
        default=None,
        help=(
            "JSON file written by the UI (host, port, token). "
            "Default: the file under %%LOCALAPPDATA%%\\mcp-guardian. "
            "Ignored when --ipc-port and --ipc-token are both set."
        ),
    )
    parser.add_argument(
        "--ipc-host",
        default=None,
        help="Must be 127.0.0.1 when set. Prefer the endpoint file.",
    )
    parser.add_argument(
        "--ipc-port",
        type=int,
        default=None,
        help="UI port. Requires --ipc-token. Prefer the endpoint file.",
    )
    parser.add_argument(
        "--ipc-token",
        default=None,
        help="Shared secret from the UI. Not logged. Requires --ipc-port.",
    )
    parser.add_argument(
        "--ipc-connect-timeout",
        type=float,
        default=5.0,
        help="Seconds to retry connecting to the UI before failing closed",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
    )
    return parser


def _parse_upstream_env(items: list[str]) -> dict[str, str]:
    env: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            raise ValueError("upstream env must be KEY=VALUE")
        key, value = item.split("=", 1)
        if _ENV_NAME.fullmatch(key) is None:
            raise ValueError("upstream env name is invalid")
        env[key] = value
    return env


def config_from_namespace(args: argparse.Namespace) -> ProxyConfig:
    if args.approval_timeout <= 0 or args.approval_timeout > 600:
        raise ValueError("approval timeout must be greater than 0 and at most 600 seconds")
    if args.ipc_connect_timeout <= 0 or args.ipc_connect_timeout > 30:
        raise ValueError("IPC connect timeout must be greater than 0 and at most 30 seconds")

    explicit = [args.ipc_host, args.ipc_port, args.ipc_token]
    if args.approval == "ipc" and any(item is not None for item in explicit):
        if args.ipc_host is None or args.ipc_port is None or args.ipc_token is None:
            raise ValueError(
                "set --ipc-host, --ipc-port, and --ipc-token together, "
                "or omit all three to use the endpoint file"
            )
        if args.ipc_host != LOOPBACK_HOST:
            raise ValueError("IPC host must be 127.0.0.1")
        if not 1 <= args.ipc_port <= 65535:
            raise ValueError("IPC port is out of range")
        validate_token(args.ipc_token)

    return ProxyConfig(
        server_name=args.server_name,
        upstream=UpstreamStdioConfig(
            command=args.upstream_command,
            args=tuple(args.upstream_args),
            cwd=args.upstream_cwd,
            env=_parse_upstream_env(args.upstream_env) or None,
        ),
        approval_mode=args.approval,
        approval_timeout_seconds=args.approval_timeout,
        ipc_endpoint_file=args.ipc_endpoint_file,
        ipc_host=args.ipc_host,
        ipc_port=args.ipc_port,
        ipc_token=args.ipc_token,
        ipc_connect_timeout_seconds=args.ipc_connect_timeout,
    )


def _build_approval(
    config: ProxyConfig,
    coordinator: PendingRequestCoordinator,
) -> ApprovalProvider:
    if config.approval_mode == "allow-all":
        logger.warning(
            "approval_mode=allow-all — every tools/call is auto-approved "
            "(explicit local wiring only; not for production)"
        )
        return make_allow_all_provider(
            coordinator,
            timeout=config.approval_timeout_seconds,
        )
    if config.approval_mode == "ipc":
        logger.info(
            "approval_mode=ipc — tool calls wait for the Guardian UI on 127.0.0.1"
        )
        if config.ipc_port is not None and config.ipc_token is not None:
            endpoint = IpcEndpoint(
                host=LOOPBACK_HOST,
                port=config.ipc_port,
                token=config.ipc_token,
            )
            return IpcApprovalProvider(
                coordinator=coordinator,
                endpoint=endpoint,
                server_name=config.server_name,
                default_timeout=config.approval_timeout_seconds,
                connect_timeout=config.ipc_connect_timeout_seconds,
            )
        endpoint_file = Path(config.ipc_endpoint_file) if config.ipc_endpoint_file else default_endpoint_path()
        return IpcApprovalProvider(
            coordinator=coordinator,
            endpoint_file=endpoint_file,
            server_name=config.server_name,
            default_timeout=config.approval_timeout_seconds,
            connect_timeout=config.ipc_connect_timeout_seconds,
        )
    logger.info("approval_mode=deny-all — every tools/call is blocked")
    return make_deny_all_provider(
        coordinator,
        timeout=config.approval_timeout_seconds,
    )


async def run_proxy(config: ProxyConfig) -> None:
    coordinator = PendingRequestCoordinator()
    approval = _build_approval(config, coordinator)

    # Upstream command comes only from local config, never from MCP tool args.
    upstream_params = StdioServerParameters(
        command=config.upstream.command,
        args=list(config.upstream.args),
        cwd=config.upstream.cwd,
        env=dict(config.upstream.env) if config.upstream.env else None,
    )

    logger.info(
        "starting proxy server_name=%s upstream_command=%s",
        config.server_name,
        config.upstream.command,
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
            server = proxy.build_server()
            init_opts = server.create_initialization_options()
            # stdout is exclusively the MCP protocol stream (SDK stdio_server).
            async with stdio_server() as (read_stream, write_stream):
                await server.run(read_stream, write_stream, init_opts)
    except Exception:
        logger.exception("proxy_terminated_with_error")
        raise
    finally:
        if proxy is not None:
            await proxy.shutdown()
        aclose = getattr(approval, "aclose", None)
        if aclose is not None:
            await aclose()
        logger.info("proxy_shutdown_complete")


def run_main(argv: Sequence[str] | None = None) -> int:
    parser = build_arg_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code
        return int(code) if isinstance(code, int) else 1

    configure_proxy_logging(getattr(logging, args.log_level))
    try:
        config = config_from_namespace(args)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        asyncio.run(run_proxy(config))
    except KeyboardInterrupt:
        logger.info("interrupted")
        return 0
    except Exception:
        return 1
    return 0


def main() -> None:
    raise SystemExit(run_main())


if __name__ == "__main__":
    main()
