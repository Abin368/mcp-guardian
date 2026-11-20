"""Configuration for the Claude Desktop STDIO proxy.

Upstream command/args come only from local process configuration (CLI / env),
never from MCP request parameters — that prevents untrusted tool args from
becoming shell commands.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Mapping


ApprovalMode = Literal["deny-all", "allow-all", "ipc"]


@dataclass(frozen=True, slots=True)
class UpstreamStdioConfig:
    """How to launch the single configured upstream MCP server over STDIO."""

    command: str
    args: tuple[str, ...] = ()
    env: Mapping[str, str] | None = None
    cwd: str | None = None


@dataclass(frozen=True, slots=True)
class ProxyConfig:
    """Runtime settings for one Guardian proxy process."""

    server_name: str
    upstream: UpstreamStdioConfig
    approval_mode: ApprovalMode = "deny-all"
    approval_timeout_seconds: float = 120.0
    proxy_name: str = "mcp-guardian"
    proxy_version: str = "0.1.0"
    # Extra env keys merged onto the upstream subprocess (never from tool args).
    upstream_env_extra: dict[str, str] = field(default_factory=dict)
    # Loopback approval UI. Ignored unless approval_mode is "ipc".
    # None means "read the endpoint file the UI wrote".
    ipc_endpoint_file: str | None = None
    ipc_host: str | None = None
    ipc_port: int | None = None
    ipc_token: str | None = None
    ipc_connect_timeout_seconds: float = 5.0
