"""Launch the Guardian approval window and its loopback listener.

Qt and asyncio on one thread
----------------------------
Qt paints windows and delivers clicks only while its event loop is running.
The coordinator waits on asyncio Futures. Those two loops cannot both block
the main thread.

qasync supplies an asyncio event loop that is driven by Qt. ``await`` in a
coordinator call returns control to Qt, so a pending dialog still repaints
and accepts input. A button slot does not ``await`` inline; it schedules a
task with ``create_task``. The task resumes on this same thread, which is
required because Qt widgets must be touched from the GUI thread and the
coordinator's Futures belong to this loop.

The Phase 4 listener uses that same loop. Reading a socket is an ``await``,
so a slow proxy cannot freeze the window. Showing a dialog is a Qt slot, so
the widgets are created on the GUI thread.

Stdout is unused. This process does not speak MCP.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import secrets
import sys
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtWidgets import QApplication

from mcp_guardian.application.pending_coordinator import PendingRequestCoordinator
from mcp_guardian.ipc.endpoint import DEFAULT_IPC_PORT, default_endpoint_path
from mcp_guardian.ipc.protocol import validate_token
from mcp_guardian.ipc.server import ApprovalIpcServer
from mcp_guardian.presentation.approval_controller import ApprovalController
from mcp_guardian.presentation.ipc_bridge import IpcUiBridge
from mcp_guardian.presentation.launcher import LauncherWindow
from mcp_guardian.presentation.messages import IPC_COPY


@dataclass(frozen=True, slots=True)
class UiSettings:
    port: int = DEFAULT_IPC_PORT
    endpoint_file: Path | None = None
    token: str | None = None
    ipc: bool = True
    sample: bool = False


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mcp-guardian-ui",
        description=(
            "MCP Guardian approval window. Listens on 127.0.0.1 for the "
            "STDIO proxy and shows one dialog per tool call."
        ),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_IPC_PORT,
        help=f"Loopback port (default {DEFAULT_IPC_PORT}). Use 0 to let the OS choose.",
    )
    parser.add_argument(
        "--endpoint-file",
        type=Path,
        default=None,
        help=(
            "Where to publish host, port, and token. "
            "Default: %%LOCALAPPDATA%%\\mcp-guardian\\ipc-endpoint.json. "
            "This is not the Claude Desktop config."
        ),
    )
    parser.add_argument(
        "--token",
        default=None,
        help="Shared secret. Omit this to generate a new one at startup.",
    )
    parser.add_argument(
        "--no-ipc",
        action="store_true",
        help="Do not listen. Sample dialogs still work; proxy calls cannot.",
    )
    parser.add_argument(
        "--sample",
        action="store_true",
        help="Also open one local sample dialog. It does not run a tool.",
    )
    return parser


def settings_from_namespace(args: argparse.Namespace) -> UiSettings:
    if args.token is not None:
        validate_token(args.token)
    if args.port != 0 and not 1 <= args.port <= 65535:
        raise ValueError("port must be 0 or between 1 and 65535")
    return UiSettings(
        port=args.port,
        endpoint_file=args.endpoint_file,
        token=args.token,
        ipc=not args.no_ipc,
        sample=args.sample,
    )


async def _amain(app: QApplication, settings: UiSettings) -> None:
    coordinator = PendingRequestCoordinator()
    # Sample dialogs keep the Phase 2 wording. Proxy dialogs use IPC_COPY.
    demo_controller = ApprovalController(coordinator)
    ipc_controller = ApprovalController(coordinator, copy=IPC_COPY)
    window = LauncherWindow(coordinator, demo_controller)
    bridge = IpcUiBridge(
        coordinator,
        ipc_controller,
        on_opened=window.note_request,
    )

    server: ApprovalIpcServer | None = None
    if settings.ipc:
        token = settings.token or secrets.token_urlsafe(32)
        endpoint_file = settings.endpoint_file or default_endpoint_path()
        server = ApprovalIpcServer(
            token=token,
            handler=bridge.handle_request,
            port=settings.port,
            endpoint_file=endpoint_file,
        )
        try:
            endpoint = await server.start()
        except OSError:
            logger.error("ipc_listen_failed error_type=OSError")
            window.set_listener_status(
                f"Could not listen on 127.0.0.1:{settings.port}. "
                "If Guardian is already open, use that window. "
                "Tool calls will not be approved."
            )
            server = None
        except Exception as exc:
            logger.error("ipc_listen_failed error_type=%s", type(exc).__name__)
            window.set_listener_status(
                "Could not start the approval listener. Tool calls will not be approved."
            )
            server = None
        else:
            window.set_listener_status(
                f"Listening on {endpoint.host}:{endpoint.port}. Waiting for the proxy."
            )
    else:
        window.set_listener_status("Approval listener is off. Proxy calls cannot be approved.")

    closed: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    def _finish_when_last_window_closes() -> None:
        if not closed.done():
            closed.set_result(None)

    app.lastWindowClosed.connect(_finish_when_last_window_closes)
    window.show()

    if settings.sample:
        initial = window.queue_sample_request()
        if initial is not None:
            await initial

    try:
        await closed
    finally:
        # Cancel dialogs first so in-flight handlers can send "cancel"
        # before the listening socket goes away.
        await ipc_controller.shutdown()
        await demo_controller.shutdown()
        if server is not None:
            await server.close()


def main(argv: list[str] | None = None) -> None:
    """Start the desktop app and block until every window is closed."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    parser = build_arg_parser()
    try:
        settings = settings_from_namespace(parser.parse_args(argv))
    except (ValueError, SystemExit) as exc:
        if isinstance(exc, SystemExit):
            raise
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    # Imported here so domain tests do not need qasync. On Python 3.12+
    # qasync.run drives a QEventLoop with asyncio.run.
    import qasync

    app = QApplication.instance()
    if app is None:
        # Qt would otherwise try to parse Guardian flags such as --port.
        app = QApplication([sys.argv[0]])
    app.setApplicationName("MCP Guardian")
    # The coroutine shuts the listener down after the last window closes.
    # Quit-on-last-window would exit Qt before that cleanup runs.
    app.setQuitOnLastWindowClosed(False)
    qasync.run(_amain(app, settings))


if __name__ == "__main__":
    main()
