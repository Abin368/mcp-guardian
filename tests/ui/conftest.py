"""Qt fixtures for Phase 2 UI tests.

QT_QPA_PLATFORM must be set before PySide6 creates a QApplication. The
offscreen plugin lets tests click dialogs without a visible desktop session.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from mcp_guardian.application.pending_coordinator import PendingRequestCoordinator
from mcp_guardian.presentation.approval_controller import ApprovalController


@pytest.fixture(autouse=True)
def qapp() -> QApplication:
    """One QApplication for the whole UI test session."""
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture
async def controller(coordinator: PendingRequestCoordinator) -> ApprovalController:
    """Controller with a short default timeout so a missed click cannot hang a test."""
    ui = ApprovalController(coordinator, timeout=5.0)
    yield ui
    await ui.shutdown()
