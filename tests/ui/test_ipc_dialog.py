"""Phase 4 dialogs driven by the real IPC server, on the offscreen Qt platform.

The upstream execution counter lives in the IPC tests. This file checks the
window: redaction, request IDs, button disable, the GUI thread, and shutdown.
"""

from __future__ import annotations

import asyncio
import os
import threading
from uuid import uuid4

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
import qasync
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from mcp_guardian.application.pending_coordinator import PendingRequestCoordinator
from mcp_guardian.ipc.client import IpcApprovalClient
from mcp_guardian.ipc.protocol import ApprovalNotice, DecisionNotice
from mcp_guardian.ipc.server import ApprovalIpcServer
from mcp_guardian.presentation.approval_controller import ApprovalController
from mcp_guardian.presentation.ipc_bridge import IpcUiBridge
from mcp_guardian.presentation.messages import IPC_APPROVE_MESSAGE, IPC_COPY
from mcp_guardian.presentation.redaction import REDACTED

TOKEN = "ui-test-token-value-ok"
SECRET = "super-secret-value"


def _notice(request_id: str, tool_name: str, arguments: dict, timeout: float = 4.0) -> ApprovalNotice:
    return ApprovalNotice(
        request_id=request_id,
        server_name="demo-fs",
        tool_name=tool_name,
        arguments=arguments,
        timeout_seconds=timeout,
    )


async def _wait_for_dialogs(controller: ApprovalController, expected: int, qapp: QApplication) -> None:
    for _ in range(50):
        qapp.processEvents()
        if len(controller.open_request_ids()) >= expected:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"expected {expected} dialogs, saw {controller.open_request_ids()}")


@pytest.mark.asyncio
async def test_dialogs_redact_args_and_keep_request_ids_apart(
    qapp: QApplication,
    coordinator: PendingRequestCoordinator,
) -> None:
    controller = ApprovalController(coordinator, timeout=5, copy=IPC_COPY)
    bridge = IpcUiBridge(coordinator, controller)
    server = ApprovalIpcServer(token=TOKEN, handler=bridge.handle_request, port=0)
    endpoint = await server.start()
    client = IpcApprovalClient(endpoint=endpoint, server_name="demo-fs", connect_timeout=2)
    id_a = str(uuid4())
    id_b = str(uuid4())
    try:
        task_a = asyncio.create_task(
            client.request_decision(
                _notice(id_a, "read_file", {"path": "a.txt", "api_key": SECRET})
            )
        )
        task_b = asyncio.create_task(
            client.request_decision(
                _notice(id_b, "delete_file", {"path": "b.txt", "token": "secret-b"})
            )
        )
        await _wait_for_dialogs(controller, 2, qapp)

        dialog_a = controller.dialog_for(id_a)
        dialog_b = controller.dialog_for(id_b)
        assert dialog_a is not None and dialog_b is not None
        assert dialog_a.request_id == id_a
        assert dialog_b.request_id == id_b
        assert dialog_a.server_value.text() == "demo-fs"
        assert dialog_a.tool_value.text() == "read_file"
        assert dialog_b.tool_value.text() == "delete_file"
        assert SECRET not in dialog_a.arguments_view.toPlainText()
        assert "secret-b" not in dialog_b.arguments_view.toPlainText()
        assert REDACTED in dialog_a.arguments_view.toPlainText()
        assert dialog_a.windowFlags() & Qt.WindowType.WindowStaysOnTopHint
        assert dialog_b.windowFlags() & Qt.WindowType.WindowStaysOnTopHint
        assert bridge.last_present_thread_id == threading.get_ident()
        # The UI coordinator must not keep the raw secret either.
        stored = await coordinator.get_request(id_a)
        assert stored.parameters["api_key"] == REDACTED

        dialog_a.allow_button.click()
        dialog_b.block_button.click()
        decision_a = await asyncio.wait_for(task_a, timeout=5)
        decision_b = await asyncio.wait_for(task_b, timeout=5)
        await controller.wait_until_settled(id_a)
        await controller.wait_until_settled(id_b)

        assert decision_a.request_id == id_a
        assert decision_a.action == "approve"
        assert decision_b.request_id == id_b
        assert decision_b.action == "deny"
        assert dialog_a.allow_button.isEnabled() is False
        assert dialog_a.block_button.isEnabled() is False
        assert dialog_b.allow_button.isEnabled() is False
        assert dialog_b.block_button.isEnabled() is False
        assert dialog_a.message_label.text() == IPC_APPROVE_MESSAGE
        assert "not executed" in dialog_b.message_label.text().lower() or "blocked" in dialog_b.message_label.text().lower()
    finally:
        await controller.shutdown()
        await client.aclose()
        await server.close()
        for request_id in controller.open_request_ids():
            dialog = controller.dialog_for(request_id)
            if dialog is not None:
                dialog.close()


@pytest.mark.asyncio
async def test_ui_shutdown_cancels_a_visible_pending_request(
    qapp: QApplication,
    coordinator: PendingRequestCoordinator,
) -> None:
    controller = ApprovalController(coordinator, timeout=5, copy=IPC_COPY)
    bridge = IpcUiBridge(coordinator, controller)
    server = ApprovalIpcServer(token=TOKEN, handler=bridge.handle_request, port=0)
    endpoint = await server.start()
    client = IpcApprovalClient(endpoint=endpoint, server_name="demo-fs", connect_timeout=2)
    request_id = str(uuid4())
    try:
        task = asyncio.create_task(
            client.request_decision(_notice(request_id, "read_file", {"path": "pending.txt"}))
        )
        await _wait_for_dialogs(controller, 1, qapp)
        dialog = controller.dialog_for(request_id)
        assert dialog is not None
        assert dialog.allow_button.isEnabled() is True

        await controller.shutdown()
        decision = await asyncio.wait_for(task, timeout=5)
        assert decision.request_id == request_id
        assert decision.action == "cancel"
        assert decision.action != "approve"
        stored = await coordinator.get_request(request_id)
        assert stored.decision is not None
        assert stored.decision.allows_execution is False
    finally:
        await client.aclose()
        await server.close()


def test_qasync_loop_accepts_a_loopback_approval(qapp: QApplication) -> None:
    """The real app uses qasync. The listener has to work on that loop."""

    async def work() -> str:
        async def handler(notice: ApprovalNotice) -> DecisionNotice:
            return DecisionNotice(notice.request_id, "deny", "ui_block")

        server = ApprovalIpcServer(token=TOKEN, handler=handler, port=0)
        endpoint = await server.start()
        client = IpcApprovalClient(endpoint=endpoint, server_name="demo-fs", connect_timeout=2)
        try:
            decision = await client.request_decision(
                _notice(str(uuid4()), "echo", {"message": "from-qasync"}, timeout=2)
            )
            return decision.action
        finally:
            await client.aclose()
            await server.close()

    assert qasync.run(work()) == "deny"
