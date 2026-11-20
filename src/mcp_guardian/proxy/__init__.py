"""Phase 3: Claude Desktop-compatible MCP STDIO proxy."""

from mcp_guardian.proxy.approval_provider import (
    ApprovalProvider,
    CoordinatorApprovalProvider,
    make_allow_all_provider,
    make_deny_all_provider,
    make_scripted_provider,
)
from mcp_guardian.proxy.config import ProxyConfig, UpstreamStdioConfig
from mcp_guardian.proxy.ipc_approval import IpcApprovalProvider
from mcp_guardian.proxy.proxy import GuardianProxy

__all__ = [
    "ApprovalProvider",
    "CoordinatorApprovalProvider",
    "GuardianProxy",
    "IpcApprovalProvider",
    "ProxyConfig",
    "UpstreamStdioConfig",
    "make_allow_all_provider",
    "make_deny_all_provider",
    "make_scripted_provider",
]
