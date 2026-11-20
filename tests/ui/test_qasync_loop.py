"""qasync shares one thread between Qt and asyncio.

The desktop entry point uses ``qasync.run``. This test drives that same helper:
the coroutine awaits, then reads a Qt widget, which only works when both
loops are the thread that ``qasync.run`` is driving.
"""

from __future__ import annotations

import asyncio
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import qasync
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLabel


def test_qasync_run_awaits_and_updates_a_widget(qapp: QApplication) -> None:
    async def work() -> str:
        await asyncio.sleep(0)
        label = QLabel("ready")
        label.setTextFormat(Qt.TextFormat.PlainText)
        try:
            return label.text()
        finally:
            label.deleteLater()

    assert qasync.run(work()) == "ready"
