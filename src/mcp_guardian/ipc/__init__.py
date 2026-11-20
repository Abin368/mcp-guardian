"""Loopback IPC between the proxy process and the approval UI.

See ``docs/architecture.md`` for why this is a ``127.0.0.1`` TCP socket and
what the security assumptions are. This package does not import Qt.
"""

from mcp_guardian.ipc.protocol import (
    LOOPBACK_HOST,
    ApprovalNotice,
    DecisionNotice,
    IpcDisconnected,
    IpcError,
    IpcProtocolError,
    IpcUnavailable,
)

__all__ = [
    "LOOPBACK_HOST",
    "ApprovalNotice",
    "DecisionNotice",
    "IpcDisconnected",
    "IpcError",
    "IpcProtocolError",
    "IpcUnavailable",
]
