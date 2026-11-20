"""One approval window for exactly one request ID.

The dialog displays a request and reports what the human did. It does not
call the coordinator and it does not execute MCP tools. Allow, Block, and
closing each emit ``decision_requested`` once. Closing emits cancel, never
approve.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QCloseEvent, QFont
from PySide6.QtWidgets import (
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from mcp_guardian.presentation.messages import CANCEL_MESSAGE, CLOSE_HINT, WAITING_MESSAGE

logger = logging.getLogger(__name__)

_DECISIONS = frozenset({"approve", "deny", "cancel"})


def _plain_label(text: str) -> QLabel:
    """Label that shows ``text`` literally, including any ``<`` characters."""
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    label.setWordWrap(True)
    return label


class ApprovalDialog(QDialog):
    """Always-on-top approval prompt bound to one request ID."""

    decision_requested = Signal(str, str)

    def __init__(
        self,
        *,
        request_id: str,
        server_name: str,
        tool_name: str,
        arguments_text: str,
        status_text: str,
        close_hint: str = CLOSE_HINT,
        waiting_message: str = WAITING_MESSAGE,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.request_id = request_id
        self._decision_sent = False

        self.setWindowTitle(f"MCP Guardian — {server_name} / {tool_name}")
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.WindowTitleHint
            | Qt.WindowType.WindowSystemMenuHint
            | Qt.WindowType.WindowCloseButtonHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setMinimumWidth(520)

        title = _plain_label("Approval required")
        title_font = QFont(title.font())
        title_font.setBold(True)
        title_font.setPointSize(title_font.pointSize() + 2)
        title.setFont(title_font)

        hint = _plain_label(close_hint)

        self.request_id_value = _plain_label(request_id)
        self.server_value = _plain_label(server_name)
        self.tool_value = _plain_label(tool_name)
        self.status_label = _plain_label(status_text)
        self.status_label.setObjectName("approval_status")

        form = QFormLayout()
        form.addRow("Request ID", self.request_id_value)
        form.addRow("MCP server", self.server_value)
        form.addRow("Tool", self.tool_value)
        form.addRow("Status", self.status_label)

        arguments_caption = _plain_label("Tool arguments")
        self.arguments_view = QPlainTextEdit()
        self.arguments_view.setReadOnly(True)
        self.arguments_view.setPlainText(arguments_text)
        self.arguments_view.setMinimumHeight(140)
        self.arguments_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        mono = QFont("Consolas")
        mono.setStyleHint(QFont.StyleHint.Monospace)
        self.arguments_view.setFont(mono)

        self.message_label = _plain_label(waiting_message)
        self.message_label.setObjectName("approval_message")

        # Neither button is the default, so Enter cannot approve by accident.
        self.block_button = QPushButton("Block")
        self.allow_button = QPushButton("Allow")
        for button in (self.block_button, self.allow_button):
            button.setAutoDefault(False)
            button.setDefault(False)
        self.block_button.clicked.connect(self._on_block_clicked)
        self.allow_button.clicked.connect(self._on_allow_clicked)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.block_button)
        buttons.addWidget(self.allow_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)
        layout.addWidget(title)
        layout.addWidget(hint)
        layout.addLayout(form)
        layout.addWidget(arguments_caption)
        layout.addWidget(self.arguments_view, stretch=1)
        layout.addWidget(self.message_label)
        layout.addLayout(buttons)

        self.arguments_view.setFocus(Qt.FocusReason.OtherFocusReason)

    def request_decision(self, action: str) -> None:
        """Emit one decision for this dialog's request ID.

        A second call is ignored. ``action`` is ``approve``, ``deny``, or
        ``cancel``. This method does not approve on its own; the controller
        must handle the signal.
        """
        if self._decision_sent:
            return
        if action not in _DECISIONS:
            return

        self.mark_decision_started()
        if action == "cancel":
            self.status_label.setText("cancelled")
            self.message_label.setText(CANCEL_MESSAGE)
        elif action == "approve":
            self.message_label.setText("Recording allow...")
        else:
            self.message_label.setText("Recording block...")
        self.decision_requested.emit(self.request_id, action)

    def mark_decision_started(self) -> None:
        """Ignore later clicks. Does not emit another decision."""
        self._decision_sent = True
        self.allow_button.setEnabled(False)
        self.block_button.setEnabled(False)

    def show_terminal(self, status_text: str, message: str) -> None:
        """Show a finished status. Buttons stay disabled."""
        self.mark_decision_started()
        self.status_label.setText(status_text)
        self.message_label.setText(message)

    def show_error(self, message: str) -> None:
        """Show a failure without describing it as approval."""
        self.show_terminal("error", message)

    def reject(self) -> None:
        """Escape closes the dialog. Closing cancels; it does not approve."""
        self._cancel_if_still_open()
        super().reject()

    def closeEvent(self, event: QCloseEvent) -> None:
        """The title-bar close button cancels the request, then the window hides."""
        try:
            self._cancel_if_still_open()
        except Exception:
            logger.error("dialog close failed request_id=%s", self.request_id)
            self.mark_decision_started()
            self.message_label.setText(CANCEL_MESSAGE)
        event.accept()

    def _cancel_if_still_open(self) -> None:
        if self._decision_sent:
            return
        self.request_decision("cancel")

    def _on_allow_clicked(self) -> None:
        try:
            self.request_decision("approve")
        except Exception:
            logger.error("allow click failed request_id=%s", self.request_id)
            self.show_error("Something went wrong. The request was not approved.")

    def _on_block_clicked(self) -> None:
        try:
            self.request_decision("deny")
        except Exception:
            logger.error("block click failed request_id=%s", self.request_id)
            self.show_error("Something went wrong. The request was not approved.")
