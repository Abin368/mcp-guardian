"""Production approval provider: ask the Guardian UI over loopback IPC.

This is the Phase 4 implementation of the Phase 3 ``ApprovalProvider``
protocol. ``deny-all``, ``allow-all``, and the scripted provider stay
available for tests and for explicit local wiring.

Fail closed:
- Only ``action == "approve"`` for this request ID calls ``coordinator.approve``.
- Disconnect, timeout, malformed frames, a missing UI, and cancellation do not.
- The dialog's own timer is slightly shorter than this wait, so a late Allow
  arrives after the buttons are already disabled and is rejected as a duplicate.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from mcp_guardian.application.pending_coordinator import (
    DuplicateDecisionError,
    PendingRequestCoordinator,
    RequestNotFoundError,
    WaitOutcome,
)
from mcp_guardian.domain.models import ApprovalRequest, RequestState
from mcp_guardian.ipc.client import IpcApprovalClient
from mcp_guardian.ipc.endpoint import IpcEndpoint
from mcp_guardian.ipc.payload import PayloadError, arguments_for_transport
from mcp_guardian.ipc.protocol import (
    ACTION_APPROVE,
    ACTION_CANCEL,
    ACTION_DENY,
    ACTION_TIMEOUT,
    ApprovalNotice,
    IpcDisconnected,
    IpcError,
    action_to_apply,
)
from mcp_guardian.proxy.logging_config import log_safe

logger = logging.getLogger("mcp_guardian.proxy.approval")

MAX_APPROVAL_TIMEOUT = 600.0
_DEFAULT_TIMEOUT = 120.0


def ui_budget_seconds(proxy_timeout: float) -> float:
    """How long the dialog may stay open, which is less than the proxy wait.

    The gap is there so the window gives up first. Otherwise a click that
    lands in the same instant as the proxy timeout can paint "Allowed" after
    the proxy has already refused to run the tool.
    """
    grace = 2.0 if proxy_timeout >= 5 else proxy_timeout * 0.25
    return max(0.05, proxy_timeout - grace)


class IpcApprovalProvider:
    """Gate ``tools/call`` on a decision from the running Guardian UI."""

    def __init__(
        self,
        *,
        coordinator: PendingRequestCoordinator,
        endpoint: IpcEndpoint | None = None,
        endpoint_file: Path | None = None,
        server_name: str = "mcp-guardian",
        default_timeout: float | None = _DEFAULT_TIMEOUT,
        connect_timeout: float = 5.0,
        client: IpcApprovalClient | None = None,
    ) -> None:
        self.coordinator = coordinator
        self.default_timeout = _DEFAULT_TIMEOUT if default_timeout is None else default_timeout
        self._client = client or IpcApprovalClient(
            endpoint=endpoint,
            endpoint_file=endpoint_file,
            server_name=server_name,
            connect_timeout=connect_timeout,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def await_decision(
        self,
        *,
        server_name: str,
        tool_name: str,
        arguments: Mapping[str, Any],
        correlation_id: str | None = None,
        timeout: float | None = None,
    ) -> WaitOutcome:
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
        if (
            not isinstance(wait_timeout, (int, float))
            or isinstance(wait_timeout, bool)
            or wait_timeout <= 0
            or wait_timeout > MAX_APPROVAL_TIMEOUT
        ):
            await self._apply(request.request_id, ACTION_TIMEOUT, "invalid_timeout")
            outcome = await self.coordinator.wait_for_decision(request.request_id, timeout=1)
            self._log_resolved(request, correlation_id, outcome)
            return outcome

        hook = asyncio.create_task(
            self._ipc_hook(request, float(wait_timeout)),
            name=f"ipc-approval-{request.request_id}",
        )
        try:
            outcome = await self.coordinator.wait_for_decision(
                request.request_id,
                timeout=float(wait_timeout),
            )
        finally:
            if not hook.done():
                hook.cancel()
            try:
                await hook
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                # The hook is supposed to record a non-approval itself. If it
                # escaped, make sure a still-pending request cannot run.
                logger.error(
                    "approval_hook_failed request_id=%s error_type=%s",
                    request.request_id,
                    type(exc).__name__,
                )
                await self._apply(request.request_id, ACTION_CANCEL, "hook_failed")

        self._log_resolved(request, correlation_id, outcome)
        return outcome

    async def _ipc_hook(self, request: ApprovalRequest, timeout: float) -> None:
        try:
            safe_arguments = arguments_for_transport(request.snapshot_parameters())
        except PayloadError:
            logger.error(
                "approval_redaction_failed request_id=%s (fail closed)",
                request.request_id,
            )
            await self._apply(request.request_id, ACTION_CANCEL, "redaction_failed")
            return

        notice = ApprovalNotice(
            request_id=request.request_id,
            server_name=request.server_name,
            tool_name=request.tool_name,
            arguments=safe_arguments,
            timeout_seconds=ui_budget_seconds(timeout),
        )
        try:
            decision = await self._client.request_decision(notice)
        except asyncio.CancelledError:
            raise
        except IpcDisconnected:
            await self._apply(request.request_id, ACTION_CANCEL, "ui_disconnected")
            return
        except IpcError:
            await self._apply(request.request_id, ACTION_CANCEL, "ui_unavailable")
            return
        except Exception as exc:
            logger.error(
                "approval_ipc_failed request_id=%s error_type=%s",
                request.request_id,
                type(exc).__name__,
            )
            await self._apply(request.request_id, ACTION_CANCEL, "ipc_failed")
            return

        task = asyncio.current_task()
        if task is not None and task.cancelling():
            raise asyncio.CancelledError()

        action = action_to_apply(request.request_id, decision)
        await self._apply(request.request_id, action, decision.reason)

    async def _apply(self, request_id: str, action: str, reason: str) -> None:
        """Record one terminal action. Never upgrades a failure into approve."""
        if action == ACTION_APPROVE:
            operation = self.coordinator.approve
        elif action == ACTION_DENY:
            operation = self.coordinator.deny
        elif action == ACTION_TIMEOUT:
            operation = self.coordinator.mark_timed_out
        else:
            operation = self.coordinator.cancel
            action = ACTION_CANCEL
        try:
            await operation(request_id, reason=reason)
        except (DuplicateDecisionError, RequestNotFoundError):
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(
                "approval_apply_failed request_id=%s action=%s error_type=%s",
                request_id,
                action,
                type(exc).__name__,
            )
            try:
                current = await self.coordinator.get_request(request_id)
            except RequestNotFoundError:
                return
            if current.state is RequestState.PENDING:
                try:
                    await self.coordinator.cancel(request_id, reason="apply_failed")
                except (DuplicateDecisionError, RequestNotFoundError):
                    return

    def _log_resolved(
        self,
        request: ApprovalRequest,
        correlation_id: str | None,
        outcome: WaitOutcome,
    ) -> None:
        log_safe(
            logger,
            logging.INFO,
            "approval_resolved",
            server_name=request.server_name,
            tool_name=request.tool_name,
            request_id=request.request_id,
            correlation_id=correlation_id,
            decision=outcome.decision.action.value,
        )
