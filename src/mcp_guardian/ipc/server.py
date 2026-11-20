"""UI-side listener. Accepts proxy connections on 127.0.0.1 only.

The handler decides what the human did. This module only checks the token,
binds the socket, and writes the handler's decision back on the same
connection, with the same request ID. A handler failure becomes ``cancel``,
never ``approve``.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from pathlib import Path

from mcp_guardian.ipc.endpoint import (
    IpcEndpoint,
    remove_endpoint_if_token_matches,
    save_endpoint,
)
from mcp_guardian.ipc.protocol import (
    LOOPBACK_HOST,
    MAX_MESSAGE_BYTES,
    ACTION_CANCEL,
    ApprovalNotice,
    DecisionNotice,
    ErrorNotice,
    HelloMessage,
    HelloOkMessage,
    IpcProtocolError,
    decode_message,
    encode_message,
    tokens_match,
    validate_reason,
    validate_token,
)

logger = logging.getLogger("mcp_guardian.ipc")

ApprovalHandler = Callable[[ApprovalNotice], Awaitable[DecisionNotice]]

_MAX_PENDING_PER_CONNECTION = 16
_MAX_COMPLETED = 256
_HELLO_TIMEOUT_SECONDS = 3.0


class ApprovalIpcServer:
    """One loopback listener. Safe to run on the same loop as Qt (qasync)."""

    def __init__(
        self,
        *,
        token: str,
        handler: ApprovalHandler,
        port: int = 0,
        endpoint_file: Path | None = None,
    ) -> None:
        self._token = validate_token(token)
        self._handler = handler
        self._requested_port = port
        self._endpoint_file = endpoint_file
        self.endpoint: IpcEndpoint | None = None
        self._tcp: asyncio.Server | None = None
        self._writers: set[asyncio.StreamWriter] = set()
        self._sessions: set[asyncio.Task[None]] = set()
        self._completed: OrderedDict[str, None] = OrderedDict()
        self._answered: set[str] = set()
        self._closing = False
        self._wrote_file = False

    async def start(self) -> IpcEndpoint:
        if self._requested_port != 0 and not 1 <= self._requested_port <= 65535:
            raise ValueError("IPC port is out of range")
        self._tcp = await asyncio.start_server(
            self._on_connection,
            host=LOOPBACK_HOST,
            port=self._requested_port,
            limit=MAX_MESSAGE_BYTES,
            reuse_address=False,
            family=socket.AF_INET,
        )
        sockets = list(self._tcp.sockets or [])
        if len(sockets) != 1:
            await self.close()
            raise RuntimeError("IPC server must bind exactly one IPv4 socket")
        bound_host, bound_port = sockets[0].getsockname()[:2]
        if bound_host != LOOPBACK_HOST:
            await self.close()
            raise RuntimeError(
                "IPC server refused to keep a socket that is not bound to 127.0.0.1"
            )
        self.endpoint = IpcEndpoint(
            host=LOOPBACK_HOST,
            port=int(bound_port),
            token=self._token,
        )
        try:
            if self._endpoint_file is not None:
                save_endpoint(self._endpoint_file, self.endpoint)
                self._wrote_file = True
        except Exception:
            await self.close()
            raise
        logger.info("ipc_listening host=%s port=%s", LOOPBACK_HOST, bound_port)
        return self.endpoint

    async def close(self) -> None:
        """Stop accepting, cancel in-flight dialogs, then drop sockets.

        Cancelling a handler makes ``_serve_request`` send ``cancel`` when it
        still can. If the write fails, the proxy sees a disconnect and also
        fails closed.
        """
        if self._closing:
            return
        self._closing = True
        tcp = self._tcp
        self._tcp = None
        # Stop accepting first, but do not wait for handlers yet. wait_closed()
        # blocks until every connection callback returns, and those callbacks
        # are the handlers we still need to cancel.
        if tcp is not None:
            tcp.close()

        sessions = list(self._sessions)
        for task in sessions:
            task.cancel()
        if sessions:
            await asyncio.wait(sessions, timeout=2.0)

        for writer in list(self._writers):
            writer.close()
        for writer in list(self._writers):
            try:
                await writer.wait_closed()
            except Exception:
                pass
        self._writers.clear()

        if tcp is not None:
            try:
                await asyncio.wait_for(tcp.wait_closed(), timeout=2.0)
            except TimeoutError:
                logger.warning("ipc_server_close_timed_out")

        if self._wrote_file and self._endpoint_file is not None and self.endpoint is not None:
            remove_endpoint_if_token_matches(self._endpoint_file, self.endpoint.token)

    async def _on_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        if self._closing or not _peer_is_loopback(writer.get_extra_info("peername")):
            writer.close()
            return
        _nodelay(writer)
        self._writers.add(writer)
        task = asyncio.create_task(self._session(reader, writer), name="mcp-guardian-ipc-session")
        self._sessions.add(task)
        try:
            await task
        finally:
            self._sessions.discard(task)
            self._writers.discard(writer)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def _session(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        pending: dict[str, asyncio.Task[None]] = {}
        try:
            if not await self._accept_hello(reader, writer):
                return
            while not self._closing:
                notice = await self._read_notice(reader)
                if notice.request_id in pending or notice.request_id in self._completed:
                    await self._send(
                        writer,
                        ErrorNotice(code="duplicate_request", request_id=notice.request_id),
                    )
                    continue
                if len(pending) >= _MAX_PENDING_PER_CONNECTION:
                    await self._send(
                        writer,
                        ErrorNotice(code="too_many_pending", request_id=notice.request_id),
                    )
                    continue
                child = asyncio.create_task(
                    self._serve_request(writer, notice),
                    name=f"approval-{notice.request_id}",
                )
                pending[notice.request_id] = child

                def _done(task: asyncio.Task[None], request_id: str = notice.request_id) -> None:
                    pending.pop(request_id, None)
                    self._remember(request_id)
                    if task.cancelled():
                        return
                    exc = task.exception()
                    if exc is not None and not isinstance(exc, asyncio.CancelledError):
                        logger.error(
                            "ipc_request_task_failed request_id=%s error_type=%s",
                            request_id,
                            type(exc).__name__,
                        )

                child.add_done_callback(_done)
        except asyncio.CancelledError:
            for task in pending.values():
                task.cancel()
            if pending:
                await asyncio.wait(pending.values(), timeout=2.0)
            raise
        except (IpcProtocolError, asyncio.IncompleteReadError, asyncio.LimitOverrunError) as exc:
            code = exc.code if isinstance(exc, IpcProtocolError) else "malformed_frame"
            logger.warning("ipc_session_rejected code=%s", code)
            for task in pending.values():
                task.cancel()
            if pending:
                await asyncio.wait(pending.values(), timeout=2.0)
        except Exception as exc:
            logger.error("ipc_session_failed error_type=%s", type(exc).__name__)
            for task in pending.values():
                task.cancel()
            if pending:
                await asyncio.wait(pending.values(), timeout=2.0)

    async def _accept_hello(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> bool:
        try:
            line = await asyncio.wait_for(reader.readuntil(b"\n"), timeout=_HELLO_TIMEOUT_SECONDS)
            message = decode_message(line)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, IpcProtocolError, asyncio.LimitOverrunError):
            logger.info("ipc_hello_rejected")
            return False
        if not isinstance(message, HelloMessage) or not tokens_match(self._token, message.token):
            logger.info("ipc_hello_rejected")
            return False
        try:
            await self._send(writer, HelloOkMessage())
        except Exception:
            return False
        logger.info("ipc_proxy_connected server=%s", message.server_name)
        return True

    async def _read_notice(self, reader: asyncio.StreamReader) -> ApprovalNotice:
        try:
            line = await reader.readuntil(b"\n")
        except asyncio.LimitOverrunError as exc:
            raise IpcProtocolError("message_too_large") from exc
        except asyncio.IncompleteReadError as exc:
            raise IpcProtocolError("truncated_message") from exc
        message = decode_message(line)
        if not isinstance(message, ApprovalNotice):
            raise IpcProtocolError("expected_request")
        return message

    async def _serve_request(
        self,
        writer: asyncio.StreamWriter,
        notice: ApprovalNotice,
    ) -> None:
        try:
            try:
                produced = await self._handler(notice)
            except asyncio.CancelledError:
                produced = DecisionNotice(
                    request_id=notice.request_id,
                    action=ACTION_CANCEL,
                    reason="ui_shutdown",
                )
                try:
                    await self._send_decision(writer, notice.request_id, produced)
                except Exception:
                    pass
                raise
            except Exception as exc:
                logger.error(
                    "ipc_handler_failed request_id=%s error_type=%s",
                    notice.request_id,
                    type(exc).__name__,
                )
                produced = DecisionNotice(
                    request_id=notice.request_id,
                    action=ACTION_CANCEL,
                    reason="ui_error",
                )
            await self._send_decision(writer, notice.request_id, produced)
        except asyncio.CancelledError:
            raise

    async def _send_decision(
        self,
        writer: asyncio.StreamWriter,
        request_id: str,
        produced: DecisionNotice,
    ) -> None:
        # The ID on the wire is the ID from the request we accepted, not
        # whatever the handler wrote. A mixed-up return value cannot approve
        # a different request.
        if request_id in self._answered:
            logger.info("ipc_duplicate_decision_suppressed request_id=%s", request_id)
            return
        self._answered.add(request_id)
        action = (
            produced.action
            if produced.action in {"approve", "deny", "cancel", "timeout"}
            else ACTION_CANCEL
        )
        try:
            reason = validate_reason(produced.reason)
        except IpcProtocolError:
            reason = "ui_decision"
        decision = DecisionNotice(request_id=request_id, action=action, reason=reason)
        await self._send(writer, decision)

    async def _send(self, writer: asyncio.StreamWriter, message: object) -> None:
        frame = encode_message(message)  # type: ignore[arg-type]
        writer.write(frame)
        await writer.drain()

    def _remember(self, request_id: str) -> None:
        self._completed[request_id] = None
        while len(self._completed) > _MAX_COMPLETED:
            self._completed.popitem(last=False)


def _peer_is_loopback(peer: object) -> bool:
    if not isinstance(peer, tuple) or not peer:
        return False
    return peer[0] == LOOPBACK_HOST


def _nodelay(writer: asyncio.StreamWriter) -> None:
    sock = writer.get_extra_info("socket")
    if sock is None:
        return
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError:
        return
