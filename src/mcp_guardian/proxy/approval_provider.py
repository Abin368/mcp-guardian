"""Approval providers for the STDIO proxy.

``deny-all``, ``allow-all``, and the scripted hook stay available for tests
and for explicit local wiring. The production path is ``IpcApprovalProvider``
in ``ipc_approval.py`` (``--approval ipc``).

Fail-closed rules:
- Only an explicit APPROVE from the Phase 1 coordinator allows execution.
- Default temporary modes never silently approve (deny-all is the safe default).
- ``allow-all`` exists only for local/dev wiring and must be chosen explicitly.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from mcp_guardian.application.pending_coordinator import (
    DuplicateDecisionError,
    PendingRequestCoordinator,
    RequestNotFoundError,
    WaitOutcome,
)
from mcp_guardian.domain.models import ApprovalRequest, Decision, DecisionAction
from mcp_guardian.proxy.logging_config import log_safe

logger = logging.getLogger("mcp_guardian.proxy.approval")


@runtime_checkable
class ApprovalProvider(Protocol):
    """Gate every tools/call before the proxy may talk to upstream."""

    async def await_decision(
        self,
        *,
        server_name: str,
        tool_name: str,
        arguments: Mapping[str, Any],
        correlation_id: str | None = None,
        timeout: float | None = None,
        client_disconnect: asyncio.Event | None = None,
    ) -> WaitOutcome:
        """Return a WaitOutcome. Execution is allowed only if allows_execution."""


DecisionHook = Callable[[ApprovalRequest], Awaitable[None]]
"""Called after a PENDING request is created; may approve/deny/cancel it."""


@dataclass(slots=True)
class CoordinatorApprovalProvider:
    """Uses PendingRequestCoordinator plus an injectable decision hook.

    The hook is how tests (and later the UI companion) resolve approvals without
    the proxy knowing about Qt or IPC. If the hook never decides, wait times out
    and fails closed.
    """

    coordinator: PendingRequestCoordinator
    decision_hook: DecisionHook
    default_timeout: float | None = 120.0

    async def await_decision(
        self,
        *,
        server_name: str,
        tool_name: str,
        arguments: Mapping[str, Any],
        correlation_id: str | None = None,
        timeout: float | None = None,
        client_disconnect: asyncio.Event | None = None,
    ) -> WaitOutcome:
        # Arguments are stored on the request for the decision-maker; never logged.
        request = await self.coordinator.create_request(
            server_name=server_name,
            tool_name=tool_name,
            parameters=dict(arguments),
        )
        log_safe(
            logger,
            logging.INFO,
            "approval_pending",
            server_name=server_name,
            tool_name=tool_name,
            request_id=request.request_id,
            correlation_id=correlation_id,
        )

        wait_timeout = self.default_timeout if timeout is None else timeout
        hook_task = asyncio.create_task(
            self._run_hook(request),
            name=f"approval-hook-{request.request_id}",
        )
        watcher: asyncio.Task[None] | None = None
        if client_disconnect is not None:
            watcher = asyncio.create_task(
                self._cancel_on_disconnect(client_disconnect, request.request_id),
                name=f"approval-disconnect-{request.request_id}",
            )
        try:
            outcome = await self.coordinator.wait_for_decision(
                request.request_id,
                timeout=wait_timeout,
            )
        except asyncio.CancelledError:
            await self._shielded_cancel(request.request_id, "client_disconnect")
            raise
        finally:
            if watcher is not None and not watcher.done():
                watcher.cancel()
                try:
                    await watcher
                except asyncio.CancelledError:
                    pass
            if not hook_task.done():
                hook_task.cancel()
                try:
                    await hook_task
                except asyncio.CancelledError:
                    pass

        log_safe(
            logger,
            logging.INFO,
            "approval_resolved",
            server_name=server_name,
            tool_name=tool_name,
            request_id=request.request_id,
            correlation_id=correlation_id,
            decision=outcome.decision.action.value,
        )
        return outcome

    async def _cancel_on_disconnect(
        self,
        disconnect: asyncio.Event,
        request_id: str,
    ) -> None:
        await disconnect.wait()
        await self._shielded_cancel(request_id, "client_disconnect")

    async def _shielded_cancel(self, request_id: str, reason: str) -> None:
        async def _do() -> None:
            try:
                await self.coordinator.cancel(request_id, reason=reason)
            except (DuplicateDecisionError, RequestNotFoundError):
                return

        cleanup = asyncio.create_task(_do(), name=f"approval-cancel-{request_id}")
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            await cleanup
            raise

    async def _run_hook(self, request: ApprovalRequest) -> None:
        try:
            await self.decision_hook(request)
        except asyncio.CancelledError:
            await self._shielded_cancel(request.request_id, "client_disconnect")
            raise
        except Exception:
            logger.exception(
                "decision_hook_failed approval_id=%s (fail closed; not approving)",
                request.request_id,
            )


async def _deny_hook(
    coordinator: PendingRequestCoordinator,
    request: ApprovalRequest,
    reason: str,
) -> None:
    await coordinator.deny(request.request_id, reason=reason)


async def _approve_hook(
    coordinator: PendingRequestCoordinator,
    request: ApprovalRequest,
    reason: str,
) -> None:
    await coordinator.approve(request.request_id, reason=reason)


def make_deny_all_provider(
    coordinator: PendingRequestCoordinator | None = None,
    *,
    timeout: float | None = 120.0,
) -> CoordinatorApprovalProvider:
    """Temporary fail-closed provider: every call is denied (never silent approve)."""
    coord = coordinator or PendingRequestCoordinator()

    async def hook(request: ApprovalRequest) -> None:
        await _deny_hook(coord, request, "temporary_deny_all")

    return CoordinatorApprovalProvider(
        coordinator=coord,
        decision_hook=hook,
        default_timeout=timeout,
    )


def make_allow_all_provider(
    coordinator: PendingRequestCoordinator | None = None,
    *,
    timeout: float | None = 120.0,
) -> CoordinatorApprovalProvider:
    """Explicit auto-approve for local wiring only — never the default."""
    coord = coordinator or PendingRequestCoordinator()

    async def hook(request: ApprovalRequest) -> None:
        await _approve_hook(coord, request, "temporary_allow_all")

    return CoordinatorApprovalProvider(
        coordinator=coord,
        decision_hook=hook,
        default_timeout=timeout,
    )


def make_scripted_provider(
    script: DecisionHook,
    coordinator: PendingRequestCoordinator | None = None,
    *,
    timeout: float | None = 120.0,
) -> CoordinatorApprovalProvider:
    """Test helper: caller supplies per-request approve/deny/hang behavior."""
    return CoordinatorApprovalProvider(
        coordinator=coordinator or PendingRequestCoordinator(),
        decision_hook=script,
        default_timeout=timeout,
    )


def blocked_tool_result(decision: Decision) -> str:
    """Human-readable denial text for CallToolResult (no secrets)."""
    action = decision.action
    if action is DecisionAction.DENY:
        return "Blocked by MCP Guardian: tool call denied."
    if action is DecisionAction.TIMEOUT:
        return "Blocked by MCP Guardian: approval timed out (fail closed)."
    if action is DecisionAction.CANCEL:
        return "Blocked by MCP Guardian: approval cancelled (fail closed)."
    return "Blocked by MCP Guardian: tool call not approved."
