"""Connect approval dialogs to PendingRequestCoordinator.

Qt widgets stay in the dialog. This module is the only presentation code that
calls approve, deny, or cancel. It never executes an MCP tool.

Dialog signals are synchronous. Slots only schedule coroutines on the running
asyncio loop (qasync's loop when the app is running). The slot itself does not
await, so the window stays responsive.
"""

from __future__ import annotations

import asyncio
import logging

from mcp_guardian.application.pending_coordinator import (
    DuplicateDecisionError,
    PendingRequestCoordinator,
    RequestNotFoundError,
)
from mcp_guardian.domain.models import ApprovalRequest, RequestState
from mcp_guardian.presentation.approval_dialog import ApprovalDialog
from mcp_guardian.presentation.messages import (
    ERROR_MESSAGE,
    PHASE2_COPY,
    UNAVAILABLE_MESSAGE,
    ApprovalCopy,
)
from mcp_guardian.presentation.redaction import format_arguments_for_display

logger = logging.getLogger(__name__)

# Long enough for a person to read the dialog. Tests pass a shorter timeout.
DEFAULT_APPROVAL_TIMEOUT = 120.0

_ACTIONS = frozenset({"approve", "deny", "cancel"})


def _arguments_text(request: ApprovalRequest) -> str:
    """Build display text. On failure, do not fall back to the raw arguments."""
    try:
        return format_arguments_for_display(request.snapshot_parameters())
    except Exception:
        logger.error("argument display failed request_id=%s", request.request_id)
        return "(arguments could not be displayed safely)"


class ApprovalController:
    """Owns one dialog and one watcher per pending request ID."""

    def __init__(
        self,
        coordinator: PendingRequestCoordinator,
        *,
        timeout: float | None = DEFAULT_APPROVAL_TIMEOUT,
        copy: ApprovalCopy | None = None,
    ) -> None:
        self._coordinator = coordinator
        self._timeout = timeout
        self._copy = copy or PHASE2_COPY
        self._dialogs: dict[str, ApprovalDialog] = {}
        self._watch_tasks: dict[str, asyncio.Task[None]] = {}
        self._decision_tasks: set[asyncio.Task[None]] = set()
        # Request IDs that already received a UI decision. Blocks duplicate clicks.
        self._handled: set[str] = set()

    def open_request(
        self,
        request: ApprovalRequest,
        *,
        timeout: float | None = None,
    ) -> ApprovalDialog:
        """Show-ready dialog for ``request``. A second call returns the same dialog.

        ``timeout`` is seconds for this request only. ``None`` uses the
        controller default. The coordinator marks the request timed out if no
        decision arrives; timeout never approves.
        """
        existing = self._dialogs.get(request.request_id)
        if existing is not None:
            return existing

        dialog = ApprovalDialog(
            request_id=request.request_id,
            server_name=request.server_name,
            tool_name=request.tool_name,
            arguments_text=_arguments_text(request),
            status_text=request.state.value,
            close_hint=self._copy.close_hint,
            waiting_message=self._copy.waiting,
        )
        dialog.decision_requested.connect(self._on_decision)
        self._dialogs[request.request_id] = dialog

        effective_timeout = self._timeout if timeout is None else timeout
        watch = asyncio.get_running_loop().create_task(
            self._watch(request.request_id, effective_timeout)
        )
        self._watch_tasks[request.request_id] = watch
        watch.add_done_callback(self._log_task_error)

        # Server and tool identify the request. Argument values are not logged.
        logger.info(
            "opened approval dialog request_id=%s server=%s tool=%s",
            request.request_id,
            request.server_name,
            request.tool_name,
        )
        return dialog

    def dialog_for(self, request_id: str) -> ApprovalDialog | None:
        return self._dialogs.get(request_id)

    def open_request_ids(self) -> tuple[str, ...]:
        return tuple(self._dialogs)

    async def wait_until_settled(self, request_id: str) -> None:
        """Wait until this request's watcher, and any click tasks, have finished."""
        watch = self._watch_tasks.get(request_id)
        if watch is not None:
            await watch
        await self.wait_for_ui_decisions()

    async def wait_for_ui_decisions(self) -> None:
        """Wait for Allow / Block / close tasks that have already been scheduled."""
        if self._decision_tasks:
            await asyncio.gather(*list(self._decision_tasks))

    async def shutdown(self) -> None:
        """Fail closed: cancel anything still pending, then stop watchers.

        In-flight button tasks are allowed to finish so a click is not lost.
        """
        if self._decision_tasks:
            await asyncio.gather(*list(self._decision_tasks), return_exceptions=True)

        for request_id in list(self._dialogs):
            try:
                current = await self._coordinator.get_request(request_id)
            except RequestNotFoundError:
                continue
            if current.state is not RequestState.PENDING:
                continue
            try:
                await self._coordinator.cancel(request_id, reason="ui_shutdown")
            except (DuplicateDecisionError, RequestNotFoundError):
                continue

        if self._watch_tasks:
            await asyncio.gather(*list(self._watch_tasks.values()), return_exceptions=True)

    def _on_decision(self, request_id: str, action: str) -> None:
        """Schedule coordinator work for one request. Never approve a different ID."""
        dialog = self._dialogs.get(request_id)
        try:
            if request_id in self._handled:
                logger.info("ignored duplicate ui decision request_id=%s", request_id)
                if dialog is not None:
                    dialog.mark_decision_started()
                return
            if action not in _ACTIONS:
                logger.error("ignored unknown ui action request_id=%s", request_id)
                return

            self._handled.add(request_id)
            if dialog is not None:
                dialog.mark_decision_started()

            task = asyncio.get_running_loop().create_task(self._apply(request_id, action))
            self._decision_tasks.add(task)
            task.add_done_callback(self._on_decision_task_done)
        except Exception:
            logger.error("could not start decision request_id=%s", request_id)
            if dialog is not None:
                dialog.show_error(ERROR_MESSAGE)

    def _on_decision_task_done(self, task: asyncio.Task[None]) -> None:
        self._decision_tasks.discard(task)
        self._log_task_error(task)

    async def _apply(self, request_id: str, action: str) -> None:
        dialog = self._dialogs.get(request_id)
        try:
            if action == "approve":
                await self._coordinator.approve(request_id, reason="ui_allow")
            elif action == "deny":
                await self._coordinator.deny(request_id, reason="ui_block")
            elif action == "cancel":
                await self._coordinator.cancel(request_id, reason="dialog_closed")
            else:
                return
            await self._refresh(request_id)
        except asyncio.CancelledError:
            raise
        except DuplicateDecisionError:
            logger.info("duplicate decision ignored request_id=%s", request_id)
            await self._refresh(request_id)
        except RequestNotFoundError:
            logger.info("request missing request_id=%s", request_id)
            if dialog is not None:
                dialog.show_error(UNAVAILABLE_MESSAGE)
        except Exception as exc:
            # Log the type only. Exception text might repeat argument data.
            logger.error(
                "approval action failed request_id=%s action=%s error_type=%s",
                request_id,
                action,
                type(exc).__name__,
            )
            if dialog is not None:
                dialog.show_error(ERROR_MESSAGE)

    async def _refresh(self, request_id: str) -> None:
        dialog = self._dialogs.get(request_id)
        if dialog is None:
            return
        try:
            current = await self._coordinator.get_request(request_id)
        except RequestNotFoundError:
            dialog.show_error(UNAVAILABLE_MESSAGE)
            return
        self._show_state(dialog, current.state)

    def _show_state(self, dialog: ApprovalDialog, state: RequestState) -> None:
        if state is RequestState.PENDING:
            dialog.show_error(ERROR_MESSAGE)
            return
        message = {
            RequestState.APPROVED: self._copy.approve,
            RequestState.DENIED: self._copy.deny,
            RequestState.TIMED_OUT: self._copy.timeout,
            RequestState.CANCELLED: self._copy.cancel,
        }.get(state, ERROR_MESSAGE)
        status_text = state.value if state in {
            RequestState.APPROVED,
            RequestState.DENIED,
            RequestState.TIMED_OUT,
            RequestState.CANCELLED,
        } else "error"
        dialog.show_terminal(status_text, message)

    async def _watch(self, request_id: str, timeout: float | None) -> None:
        """Reflect timeout, cancel, allow, or block for this request only."""
        dialog = self._dialogs.get(request_id)
        try:
            outcome = await self._coordinator.wait_for_decision(
                request_id,
                timeout=timeout,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("watch failed request_id=%s", request_id)
            if dialog is not None:
                dialog.show_error(ERROR_MESSAGE)
            return

        if dialog is not None:
            self._show_state(dialog, outcome.request.state)

    @staticmethod
    def _log_task_error(task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            return
        if exc is not None:
            logger.error(
                "background approval task failed error_type=%s",
                type(exc).__name__,
            )
