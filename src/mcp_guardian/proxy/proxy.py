"""Claude Desktop-facing MCP server that proxies one upstream MCP server.

Architecture (Phase 3):
- Toward Claude Desktop: low-level ``Server`` over STDIO (stdout = protocol only).
- Toward upstream: high-level ``Client`` (STDIO subprocess or in-process for tests).
- tools/call is intercepted; other supported ops are forwarded when possible.
"""

from __future__ import annotations

import logging
from typing import Any

import mcp_types as types
from mcp import Client
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel import Server
from mcp.shared.exceptions import MCPError

from mcp_guardian.application.client_disconnect import disconnect_event_from_request
from mcp_guardian.application.pending_coordinator import PendingRequestCoordinator
from mcp_guardian.proxy.approval_provider import (
    ApprovalProvider,
    blocked_tool_result,
)
from mcp_guardian.proxy.logging_config import log_safe

logger = logging.getLogger("mcp_guardian.proxy")


class GuardianProxy:
    """MCP server facade that gates tools/call through an ApprovalProvider."""

    def __init__(
        self,
        *,
        upstream: Client,
        approval: ApprovalProvider,
        server_name: str,
        proxy_name: str = "mcp-guardian",
        proxy_version: str = "0.1.0",
        approval_timeout: float | None = 120.0,
        coordinator: PendingRequestCoordinator | None = None,
    ) -> None:
        self._upstream = upstream
        self._approval = approval
        self._server_name = server_name
        self._proxy_name = proxy_name
        self._proxy_version = proxy_version
        self._approval_timeout = approval_timeout
        # Optional reference for shutdown cancellation of still-pending gates.
        self._coordinator = coordinator
        self._closed = False

    @property
    def server_name(self) -> str:
        return self._server_name

    def build_server(self) -> Server[Any]:
        """Construct the Claude-facing low-level Server with intercept handlers.

        Phase 3 advertises tools only (list + call). Ping and initialize are
        handled by the SDK. Resources/prompts are intentionally not advertised
        so Claude is not offered capabilities the configured upstream may lack.
        """
        return Server(
            self._proxy_name,
            version=self._proxy_version,
            instructions=(
                f"MCP Guardian proxy for upstream server {self._server_name!r}. "
                "Tool calls require human approval before execution."
            ),
            on_list_tools=self._on_list_tools,
            on_call_tool=self._on_call_tool,
        )

    async def shutdown(self) -> None:
        """Fail-closed shutdown: cancel pending approvals; mark closed."""
        self._closed = True
        if self._coordinator is None:
            return
        for request_id in await self._coordinator.pending_ids():
            try:
                await self._coordinator.cancel(request_id, reason="proxy_shutdown")
            except Exception:
                log_safe(
                    logger,
                    logging.WARNING,
                    "shutdown_cancel_failed",
                    request_id=request_id,
                )

    def _ensure_open(self) -> None:
        if self._closed:
            raise MCPError(code=-32000, message="MCP Guardian proxy is shut down")

    async def _on_list_tools(
        self,
        ctx: ServerRequestContext[Any],
        params: types.PaginatedRequestParams | None,
    ) -> types.ListToolsResult:
        self._ensure_open()
        log_safe(
            logger,
            logging.INFO,
            "passthrough tools/list",
            server_name=self._server_name,
            correlation_id=_correlation(ctx),
        )
        # Forward to the single configured upstream only.
        return await self._upstream.list_tools(
            cursor=params.cursor if params is not None else None,
        )

    async def _on_call_tool(
        self,
        ctx: ServerRequestContext[Any],
        params: types.CallToolRequestParams,
    ) -> types.CallToolResult:
        self._ensure_open()
        tool_name = params.name
        # Copy args for the approval record; never log their values.
        arguments: dict[str, Any] = dict(params.arguments or {})
        correlation_id = _correlation(ctx)

        log_safe(
            logger,
            logging.INFO,
            "intercept tools/call",
            server_name=self._server_name,
            tool_name=tool_name,
            correlation_id=correlation_id,
        )

        outcome = await self._approval.await_decision(
            server_name=self._server_name,
            tool_name=tool_name,
            arguments=arguments,
            correlation_id=correlation_id,
            timeout=self._approval_timeout,
            client_disconnect=disconnect_event_from_request(ctx.request),
        )

        if not outcome.allows_execution:
            # Denied / timed out / cancelled — never reach upstream.
            log_safe(
                logger,
                logging.INFO,
                "tools/call blocked; upstream not contacted",
                server_name=self._server_name,
                tool_name=tool_name,
                request_id=outcome.request.request_id,
                correlation_id=correlation_id,
                decision=outcome.decision.action.value,
            )
            return types.CallToolResult(
                content=[
                    types.TextContent(
                        type="text",
                        text=blocked_tool_result(outcome.decision),
                    )
                ],
                isError=True,
            )

        log_safe(
            logger,
            logging.INFO,
            "tools/call approved; forwarding upstream",
            server_name=self._server_name,
            tool_name=tool_name,
            request_id=outcome.request.request_id,
            correlation_id=correlation_id,
            decision=outcome.decision.action.value,
        )

        # Forward the tool payload only. Upstream ``_meta`` is session
        # metadata (including a newer protocol's serverInfo stamp) and must
        # not be written to the downstream client.
        try:
            upstream_result = await self._upstream.call_tool(tool_name, arguments)
        except MCPError:
            raise
        except Exception as exc:
            log_safe(
                logger,
                logging.ERROR,
                f"upstream_call_failed: {type(exc).__name__}",
                server_name=self._server_name,
                tool_name=tool_name,
                request_id=outcome.request.request_id,
                correlation_id=correlation_id,
            )
            return types.CallToolResult(
                content=[
                    types.TextContent(
                        type="text",
                        text=f"Upstream MCP server error: {type(exc).__name__}",
                    )
                ],
                isError=True,
            )
        return _tool_result_for_client(upstream_result)


def _tool_result_for_client(upstream: types.CallToolResult) -> types.CallToolResult:
    """Rebuild a tool result from the payload fields this SDK supports.

    ``content``, ``isError``, and ``structuredContent`` (when the model has
    it) are copied. Upstream ``_meta`` is left behind.
    """
    fields = types.CallToolResult.model_fields
    payload: dict[str, Any] = {"content": upstream.content}
    if "is_error" in fields:
        payload["isError"] = upstream.is_error
    if "structured_content" in fields:
        payload["structuredContent"] = upstream.structured_content
    return types.CallToolResult(**payload)


def _correlation(ctx: ServerRequestContext[Any]) -> str | None:
    if ctx.request_id is None:
        return None
    return str(ctx.request_id)
