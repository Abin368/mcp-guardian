"""Show a real proxy request on the Phase 2 approval dialog.

The socket handler runs on the asyncio loop. Widget calls run in a Qt slot,
which is the GUI thread when this object is created on that thread (qasync
makes those the same thread). The slot does not wait for the human. The
coordinator wait is an ``await``, so the window can still repaint and take
other clicks.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable

from PySide6.QtCore import QObject, Signal

from mcp_guardian.application.pending_coordinator import (
    DuplicateDecisionError,
    PendingRequestCoordinator,
    RequestNotFoundError,
)
from mcp_guardian.domain.models import ApprovalRequest
from mcp_guardian.ipc.payload import PayloadError, arguments_for_transport
from mcp_guardian.ipc.protocol import (
    ACTION_CANCEL,
    DECISION_ACTIONS,
    ApprovalNotice,
    DecisionNotice,
)
from mcp_guardian.presentation.approval_controller import ApprovalController

logger = logging.getLogger(__name__)


def _reason_token(reason: str | None) -> str:
    """Keep a reason log-safe and inside the IPC grammar. Drop anything else."""
    if not reason:
        return "ui_decision"
    cleaned = "".join(ch for ch in reason.lower() if ch.isalnum() or ch in "_:-")
    return (cleaned or "ui_decision")[:64]


class IpcUiBridge(QObject):
    """Adapter from an ``ApprovalNotice`` to one ``ApprovalController`` dialog."""

    _present = Signal(str)

    def __init__(
        self,
        coordinator: PendingRequestCoordinator,
        controller: ApprovalController,
        *,
        on_opened: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__()
        self._coordinator = coordinator
        self._controller = controller
        self._on_opened = on_opened
        self._queued: dict[str, tuple[ApprovalRequest, float]] = {}
        # Recorded from the slot so tests can prove the dialog was built on
        # the thread that owns this QObject (the Qt thread in the real app).
        self.last_present_thread_id: int | None = None
        self._present.connect(self._present_on_gui_thread)

    async def handle_request(self, notice: ApprovalNotice) -> DecisionNotice:
        """Redact, show the dialog, and return the coordinator's decision.

        A failure here returns ``cancel``. It does not return ``approve``.
        """
        try:
            safe_arguments = arguments_for_transport(notice.arguments)
        except PayloadError:
            logger.error(
                "ipc_display_redaction_failed request_id=%s",
                notice.request_id,
            )
            return DecisionNotice(
                request_id=notice.request_id,
                action=ACTION_CANCEL,
                reason="redaction_failed",
            )

        try:
            request = await self._coordinator.create_request(
                server_name=notice.server_name,
                tool_name=notice.tool_name,
                parameters=safe_arguments,
                request_id=notice.request_id,
            )
        except (RuntimeError, ValueError):
            logger.error("ipc_request_rejected request_id=%s", notice.request_id)
            return DecisionNotice(
                request_id=notice.request_id,
                action=ACTION_CANCEL,
                reason="ui_error",
            )

        self._queued[request.request_id] = (request, notice.timeout_seconds)
        self._present.emit(request.request_id)
        try:
            outcome = await self._coordinator.wait_for_decision(
                request.request_id,
                timeout=notice.timeout_seconds,
            )
        except asyncio.CancelledError:
            try:
                await self._coordinator.cancel(request.request_id, reason="ui_disconnected")
            except (DuplicateDecisionError, RequestNotFoundError):
                pass
            raise

        action = outcome.decision.action.value
        if action not in DECISION_ACTIONS:
            action = ACTION_CANCEL
        return DecisionNotice(
            request_id=notice.request_id,
            action=action,
            reason=_reason_token(outcome.decision.reason),
        )

    def _present_on_gui_thread(self, request_id: str) -> None:
        self.last_present_thread_id = threading.get_ident()
        queued = self._queued.get(request_id)
        if queued is None:
            return
        request, timeout = queued
        try:
            dialog = self._controller.open_request(request, timeout=timeout)
            dialog.show()
            dialog.raise_()
            dialog.activateWindow()
        except Exception:
            logger.error("ipc_dialog_failed request_id=%s", request_id)
            return
        if self._on_opened is not None:
            try:
                self._on_opened(request_id)
            except Exception:
                logger.error("ipc_status_failed request_id=%s", request_id)
