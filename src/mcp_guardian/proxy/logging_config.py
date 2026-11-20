"""Stderr-only diagnostics for the STDIO proxy.

Stdout is reserved exclusively for MCP JSON-RPC framed messages (Claude Desktop
reads the proxy's stdout as the MCP server stream).
"""

from __future__ import annotations

import logging
import sys


def configure_proxy_logging(level: int = logging.INFO) -> logging.Logger:
    """Attach a stderr StreamHandler to the ``mcp_guardian.proxy`` logger."""
    logger = logging.getLogger("mcp_guardian.proxy")
    logger.handlers.clear()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    logger.addHandler(handler)
    logger.setLevel(level)
    # Do not bubble into root (root may have been configured for stdout).
    logger.propagate = False
    return logger


def log_safe(
    logger: logging.Logger,
    level: int,
    message: str,
    *,
    server_name: str | None = None,
    tool_name: str | None = None,
    request_id: str | None = None,
    correlation_id: str | None = None,
    decision: str | None = None,
) -> None:
    """Log operational metadata without tool argument values."""
    parts = [message]
    if server_name is not None:
        parts.append(f"server={server_name}")
    if tool_name is not None:
        parts.append(f"tool={tool_name}")
    if request_id is not None:
        parts.append(f"approval_id={request_id}")
    if correlation_id is not None:
        parts.append(f"correlation={correlation_id}")
    if decision is not None:
        parts.append(f"decision={decision}")
    logger.log(level, " ".join(parts))
