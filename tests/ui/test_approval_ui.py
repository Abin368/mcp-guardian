"""Phase 2 approval dialog tests.

Widgets run against the real PendingRequestCoordinator. Offscreen Qt delivers
clicks; pytest-asyncio runs the same coroutines the qasync loop runs in the app.
"""

from __future__ import annotations

import logging
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from mcp_guardian.application.pending_coordinator import PendingRequestCoordinator
from mcp_guardian.domain.models import DecisionAction, RequestState
from mcp_guardian.presentation.approval_controller import ApprovalController
from mcp_guardian.presentation.launcher import SAMPLE_API_KEY, LauncherWindow
from mcp_guardian.presentation.messages import (
    APPROVE_MESSAGE,
    CANCEL_MESSAGE,
    DENY_MESSAGE,
    ERROR_MESSAGE,
    TIMEOUT_MESSAGE,
)
from mcp_guardian.presentation.redaction import REDACTED


def _pump() -> None:
    app = QApplication.instance()
    assert app is not None
    app.processEvents()


async def test_allow_button_approves_exact_request(
    coordinator: PendingRequestCoordinator,
    controller: ApprovalController,
    caplog: pytest.LogCaptureFixture,
) -> None:
    request = await coordinator.create_request(
        server_name="fs",
        tool_name="read_file",
        parameters={"path": "notes.txt", "api_key": "super-secret-value"},
    )
    dialog = controller.open_request(request)
    dialog.show()
    _pump()

    assert dialog.request_id == request.request_id
    assert dialog.request_id_value.text() == request.request_id
    assert dialog.server_value.text() == "fs"
    assert dialog.tool_value.text() == "read_file"
    assert dialog.status_label.text() == RequestState.PENDING.value
    assert dialog.server_value.textFormat() == Qt.TextFormat.PlainText
    assert dialog.allow_button.isDefault() is False
    assert dialog.allow_button.autoDefault() is False
    assert dialog.windowFlags() & Qt.WindowType.WindowStaysOnTopHint
    assert dialog.windowModality() == Qt.WindowModality.NonModal

    shown = dialog.arguments_view.toPlainText()
    assert "notes.txt" in shown
    assert "super-secret-value" not in shown
    assert REDACTED in shown

    with caplog.at_level(logging.DEBUG):
        dialog.allow_button.click()
        await controller.wait_until_settled(request.request_id)

    stored = await coordinator.get_request(request.request_id)
    assert stored.state is RequestState.APPROVED
    assert stored.decision is not None
    assert stored.decision.request_id == request.request_id
    assert stored.decision.action is DecisionAction.APPROVE
    assert stored.decision.reason == "ui_allow"
    assert stored.decision.allows_execution is True
    assert dialog.status_label.text() == RequestState.APPROVED.value
    assert dialog.message_label.text() == APPROVE_MESSAGE
    assert dialog.allow_button.isEnabled() is False
    assert "super-secret-value" not in caplog.text
    # The original request still holds the argument; only the display was redacted.
    assert request.parameters["api_key"] == "super-secret-value"


async def test_block_button_denies_exact_request(
    coordinator: PendingRequestCoordinator,
    controller: ApprovalController,
    caplog: pytest.LogCaptureFixture,
) -> None:
    request = await coordinator.create_request(
        server_name="fs",
        tool_name="delete_file",
        parameters={"path": "notes.txt", "token": "super-secret-value"},
    )
    dialog = controller.open_request(request)
    dialog.show()
    _pump()

    assert "super-secret-value" not in dialog.arguments_view.toPlainText()

    with caplog.at_level(logging.DEBUG):
        dialog.block_button.click()
        await controller.wait_until_settled(request.request_id)

    stored = await coordinator.get_request(request.request_id)
    assert stored.state is RequestState.DENIED
    assert stored.decision is not None
    assert stored.decision.request_id == request.request_id
    assert stored.decision.action is DecisionAction.DENY
    assert stored.decision.reason == "ui_block"
    assert stored.decision.allows_execution is False
    assert dialog.status_label.text() == RequestState.DENIED.value
    assert dialog.message_label.text() == DENY_MESSAGE
    assert dialog.block_button.isEnabled() is False
    assert "super-secret-value" not in caplog.text


async def test_two_dialogs_have_independent_decisions(
    coordinator: PendingRequestCoordinator,
    controller: ApprovalController,
) -> None:
    request_a = await coordinator.create_request(
        "server-a",
        "tool_a",
        {"path": "a.txt"},
    )
    request_b = await coordinator.create_request(
        "server-b",
        "tool_b",
        {"path": "b.txt", "token": "secret-b"},
    )
    dialog_a = controller.open_request(request_a)
    dialog_b = controller.open_request(request_b)
    window = LauncherWindow(coordinator, controller)
    window.show()
    dialog_a.show()
    dialog_b.show()
    _pump()

    assert dialog_a.request_id == request_a.request_id
    assert dialog_b.request_id == request_b.request_id
    assert dialog_a.request_id != dialog_b.request_id
    assert "secret-b" not in dialog_b.arguments_view.toPlainText()
    assert window.new_request_button.isEnabled() is True
    assert dialog_a.windowFlags() & Qt.WindowType.WindowStaysOnTopHint
    assert dialog_b.windowFlags() & Qt.WindowType.WindowStaysOnTopHint

    dialog_a.allow_button.click()
    dialog_b.block_button.click()
    await controller.wait_until_settled(request_a.request_id)
    await controller.wait_until_settled(request_b.request_id)

    stored_a = await coordinator.get_request(request_a.request_id)
    stored_b = await coordinator.get_request(request_b.request_id)

    assert stored_a.state is RequestState.APPROVED
    assert stored_a.decision is not None
    assert stored_a.decision.request_id == request_a.request_id
    assert stored_a.decision.allows_execution is True
    assert dialog_a.message_label.text() == APPROVE_MESSAGE

    assert stored_b.state is RequestState.DENIED
    assert stored_b.decision is not None
    assert stored_b.decision.request_id == request_b.request_id
    assert stored_b.decision.allows_execution is False
    assert dialog_b.message_label.text() == DENY_MESSAGE

    window.close()


async def test_closing_dialog_cancels_and_does_not_approve(
    coordinator: PendingRequestCoordinator,
    controller: ApprovalController,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = await coordinator.create_request("fs", "read_file", {"path": "notes.txt"})
    dialog = controller.open_request(request)
    dialog.show()
    _pump()

    approve_calls: list[str] = []
    original_approve = coordinator.approve

    async def spy_approve(request_id: str, reason: str | None = None):
        approve_calls.append(request_id)
        return await original_approve(request_id, reason)

    monkeypatch.setattr(coordinator, "approve", spy_approve)

    dialog.close()
    await controller.wait_until_settled(request.request_id)

    stored = await coordinator.get_request(request.request_id)
    assert stored.state is RequestState.CANCELLED
    assert stored.decision is not None
    assert stored.decision.action is DecisionAction.CANCEL
    assert stored.decision.reason == "dialog_closed"
    assert stored.decision.allows_execution is False
    assert stored.decision.request_id == request.request_id
    assert dialog.message_label.text() == CANCEL_MESSAGE
    assert "cancelled" in dialog.message_label.text().lower()
    assert approve_calls == []


async def test_escape_key_cancels_and_does_not_approve(
    coordinator: PendingRequestCoordinator,
    controller: ApprovalController,
) -> None:
    request = await coordinator.create_request("fs", "read_file", {"path": "notes.txt"})
    dialog = controller.open_request(request)
    dialog.show()
    _pump()

    QTest.keyClick(dialog, Qt.Key.Key_Escape)
    await controller.wait_until_settled(request.request_id)

    stored = await coordinator.get_request(request.request_id)
    assert stored.state is RequestState.CANCELLED
    assert stored.decision is not None
    assert stored.decision.allows_execution is False
    assert dialog.message_label.text() == CANCEL_MESSAGE


async def test_enter_key_does_not_approve(
    coordinator: PendingRequestCoordinator,
    controller: ApprovalController,
) -> None:
    request = await coordinator.create_request("fs", "read_file", {"path": "notes.txt"})
    dialog = controller.open_request(request)
    dialog.show()
    dialog.arguments_view.setFocus(Qt.FocusReason.OtherFocusReason)
    _pump()

    QTest.keyClick(dialog, Qt.Key.Key_Return)
    QTest.keyClick(dialog, Qt.Key.Key_Enter)
    await controller.wait_for_ui_decisions()

    stored = await coordinator.get_request(request.request_id)
    assert stored.state is RequestState.PENDING
    assert stored.decision is None
    assert dialog.allow_button.isEnabled() is True


async def test_timeout_shows_message_and_ignores_later_clicks(
    coordinator: PendingRequestCoordinator,
    controller: ApprovalController,
) -> None:
    request = await coordinator.create_request("fs", "read_file", {"path": "notes.txt"})
    dialog = controller.open_request(request, timeout=0.05)
    dialog.show()
    _pump()

    await controller.wait_until_settled(request.request_id)

    stored = await coordinator.get_request(request.request_id)
    assert stored.state is RequestState.TIMED_OUT
    assert stored.decision is not None
    assert stored.decision.action is DecisionAction.TIMEOUT
    assert stored.decision.allows_execution is False
    assert dialog.isVisible() is True
    assert dialog.status_label.text() == RequestState.TIMED_OUT.value
    assert dialog.message_label.text() == TIMEOUT_MESSAGE
    assert "timed out" in dialog.message_label.text().lower()
    assert "not approved" in dialog.message_label.text().lower()
    assert dialog.allow_button.isEnabled() is False
    assert dialog.block_button.isEnabled() is False

    dialog.allow_button.click()
    dialog.block_button.click()
    dialog.request_decision("approve")
    await controller.wait_for_ui_decisions()

    stored_after = await coordinator.get_request(request.request_id)
    assert stored_after.state is RequestState.TIMED_OUT
    assert stored_after.decision is not None
    assert stored_after.decision.action is DecisionAction.TIMEOUT
    assert stored_after.decision.allows_execution is False


async def test_duplicate_clicks_cannot_produce_multiple_decisions(
    coordinator: PendingRequestCoordinator,
    controller: ApprovalController,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = await coordinator.create_request("fs", "read_file", {"path": "notes.txt"})
    dialog = controller.open_request(request)
    dialog.show()
    _pump()

    approve_calls: list[str] = []
    deny_calls: list[str] = []
    original_approve = coordinator.approve
    original_deny = coordinator.deny

    async def spy_approve(request_id: str, reason: str | None = None):
        approve_calls.append(request_id)
        return await original_approve(request_id, reason)

    async def spy_deny(request_id: str, reason: str | None = None):
        deny_calls.append(request_id)
        return await original_deny(request_id, reason)

    monkeypatch.setattr(coordinator, "approve", spy_approve)
    monkeypatch.setattr(coordinator, "deny", spy_deny)

    dialog.allow_button.click()
    dialog.allow_button.click()
    dialog.request_decision("approve")
    dialog.request_decision("deny")
    # Emits skip the dialog guard and must still be ignored by the controller.
    dialog.decision_requested.emit(dialog.request_id, "approve")
    dialog.decision_requested.emit(dialog.request_id, "deny")

    await controller.wait_until_settled(request.request_id)

    stored = await coordinator.get_request(request.request_id)
    assert approve_calls == [request.request_id]
    assert deny_calls == []
    assert stored.state is RequestState.APPROVED
    assert stored.decision is not None
    assert stored.decision.action is DecisionAction.APPROVE
    assert stored.decision.request_id == request.request_id
    assert dialog.allow_button.isEnabled() is False
    assert dialog.block_button.isEnabled() is False


async def test_coordinator_exception_does_not_crash_the_dialog(
    coordinator: PendingRequestCoordinator,
    controller: ApprovalController,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    request = await coordinator.create_request(
        "fs",
        "read_file",
        {"api_key": "super-secret-value"},
    )
    dialog = controller.open_request(request)
    dialog.show()
    _pump()

    async def exploding(request_id: str, reason: str | None = None):
        raise RuntimeError("boom super-secret-value")

    monkeypatch.setattr(coordinator, "approve", exploding)

    with caplog.at_level(logging.DEBUG):
        dialog.allow_button.click()
        await controller.wait_for_ui_decisions()

    assert dialog.message_label.text() == ERROR_MESSAGE
    assert dialog.status_label.text() == "error"
    assert dialog.allow_button.isEnabled() is False
    stored = await coordinator.get_request(request.request_id)
    assert stored.state is RequestState.PENDING
    assert stored.decision is None
    assert "super-secret-value" not in caplog.text


async def test_launcher_opens_redacted_sample_while_ui_stays_enabled(
    coordinator: PendingRequestCoordinator,
    controller: ApprovalController,
) -> None:
    window = LauncherWindow(coordinator, controller)
    window.show()
    _pump()

    window.new_request_button.click()
    assert window.latest_task is not None
    await window.latest_task

    assert window.new_request_button.isEnabled() is True
    ids = controller.open_request_ids()
    assert len(ids) == 1
    dialog = controller.dialog_for(ids[0])
    assert dialog is not None
    assert dialog.isVisible() is True
    shown = dialog.arguments_view.toPlainText()
    assert SAMPLE_API_KEY not in shown
    assert REDACTED in shown
    assert "notes-1.txt" in shown
    assert SAMPLE_API_KEY not in window.status_label.text()

    window.new_request_button.click()
    assert window.latest_task is not None
    await window.latest_task

    assert len(controller.open_request_ids()) == 2
    assert window.new_request_button.isEnabled() is True
    window.close()
