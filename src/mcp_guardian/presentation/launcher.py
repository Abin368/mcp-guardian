"""Small window that opens sample approval requests.

This is a local demo shell for Phase 2. It does not proxy MCP, open a socket,
or execute tools. Each click creates one coordinator request and one dialog.
"""

from __future__ import annotations

import asyncio
import logging

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QPushButton, QVBoxLayout, QWidget

from mcp_guardian.application.pending_coordinator import PendingRequestCoordinator
from mcp_guardian.presentation.approval_controller import ApprovalController

logger = logging.getLogger(__name__)

# Fake value so the running UI can show redaction. Not a real credential.
SAMPLE_API_KEY = "example-not-a-real-key"


def _plain_label(text: str) -> QLabel:
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return label


class LauncherWindow(QWidget):
    """Starts sample approval dialogs. Stays usable while dialogs are open."""

    def __init__(
        self,
        coordinator: PendingRequestCoordinator,
        controller: ApprovalController,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._coordinator = coordinator
        self._controller = controller
        self._sample_index = 0
        self.latest_task: asyncio.Task[None] | None = None

        self.setWindowTitle("MCP Guardian")
        self.setMinimumWidth(420)

        self.new_request_button = QPushButton("New sample request")
        self.new_request_button.clicked.connect(self._on_new_request_clicked)
        self.status_label = _plain_label("Ready. Nothing is approved yet.")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)
        layout.addWidget(_plain_label("MCP Guardian"))
        layout.addWidget(
            _plain_label(
                "Each dialog is one request. Sample requests stay on this computer "
                "and do not run a tool. A request from the proxy runs only if you "
                "press Allow on that dialog."
            )
        )
        self.ipc_label = _plain_label("Approval listener is not started.")
        layout.addWidget(self.new_request_button)
        layout.addWidget(self.ipc_label)
        layout.addWidget(
            _plain_label(
                "Dialogs stay on top. Closing a dialog cancels that request only."
            )
        )
        layout.addWidget(self.status_label)

    def _on_new_request_clicked(self) -> None:
        self.queue_sample_request()

    def queue_sample_request(self) -> asyncio.Task[None] | None:
        """Schedule a sample request on the running asyncio loop."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.error("sample request clicked without a running event loop")
            self.status_label.setText("Could not open a request. Nothing was approved.")
            return None

        task = loop.create_task(self._open_sample_safe())
        self.latest_task = task
        return task

    async def _open_sample_safe(self) -> None:
        try:
            await self.open_sample()
        except Exception as exc:
            logger.error("sample request failed error_type=%s", type(exc).__name__)
            self.status_label.setText("Could not open a request. Nothing was approved.")

    async def open_sample(self) -> str:
        """Create one pending request and open its dialog. Returns the request ID."""
        self._sample_index += 1
        request = await self._coordinator.create_request(
            server_name="demo-server",
            tool_name="read_file",
            parameters={
                "path": f"notes-{self._sample_index}.txt",
                "api_key": SAMPLE_API_KEY,
            },
        )
        dialog = self._controller.open_request(request)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        # Request ID only — argument values are not written here.
        self.note_request(request.request_id)
        return request.request_id

    def set_listener_status(self, text: str) -> None:
        """Show whether the loopback listener is up. Do not include the token."""
        self.ipc_label.setText(text)

    def note_request(self, request_id: str) -> None:
        """Status line is the request ID only. Argument values are not written."""
        self.status_label.setText(f"Opened request {request_id}")
