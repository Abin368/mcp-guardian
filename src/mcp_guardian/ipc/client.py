"""Proxy-side client. Connects to the UI, never the other way around.

A missing UI, a bad token, a dropped socket, or a garbled frame raises an
``IpcError``. The approval provider turns that into a coordinator cancel or
timeout. This module does not call ``approve``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from mcp_guardian.ipc.endpoint import (
    EndpointNotReady,
    EndpointRejected,
    IpcEndpoint,
    load_endpoint,
)
from mcp_guardian.ipc.protocol import (
    LOOPBACK_HOST,
    MAX_MESSAGE_BYTES,
    ApprovalNotice,
    DecisionNotice,
    ErrorNotice,
    HelloMessage,
    HelloOkMessage,
    IpcDisconnected,
    IpcProtocolError,
    IpcUnavailable,
    decode_message,
    encode_message,
    validate_token,
)

logger = logging.getLogger("mcp_guardian.proxy.ipc")

_MAX_PENDING = 32


class IpcApprovalClient:
    """One connection, many in-flight requests, demuxed by request ID."""

    def __init__(
        self,
        *,
        endpoint: IpcEndpoint | None = None,
        endpoint_file: Path | None = None,
        server_name: str = "mcp-guardian",
        connect_timeout: float = 5.0,
    ) -> None:
        if endpoint is None and endpoint_file is None:
            raise ValueError("IPC client needs an endpoint or an endpoint file")
        if endpoint is not None and endpoint.host != LOOPBACK_HOST:
            raise ValueError("IPC client refuses to connect anywhere but 127.0.0.1")
        if connect_timeout <= 0 or connect_timeout > 30:
            raise ValueError("IPC connect timeout must be between 0 and 30 seconds")
        self._fixed = endpoint
        self._endpoint_file = None if endpoint is not None else endpoint_file
        self._server_name = server_name
        self._connect_timeout = connect_timeout
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._pending: dict[str, asyncio.Future[DecisionNotice]] = {}
        self._connect_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._closed = False

    async def request_decision(self, notice: ApprovalNotice) -> DecisionNotice:
        """Send one request and wait for the decision that names its ID.

        The Future is registered before the write, so a fast UI cannot reply
        into the void. Cancellation (the proxy timed out or shut down) drops
        the Future so a late decision cannot be delivered to this waiter.
        """
        if self._closed:
            raise IpcUnavailable("ipc_client_closed")
        await self._ensure_connected()

        loop = asyncio.get_running_loop()
        if notice.request_id in self._pending:
            raise IpcProtocolError("duplicate_request")
        if len(self._pending) >= _MAX_PENDING:
            raise IpcProtocolError("too_many_pending")

        future: asyncio.Future[DecisionNotice] = loop.create_future()
        self._pending[notice.request_id] = future
        try:
            async with self._write_lock:
                await self._write(encode_message(notice))
            decision = await future
        except asyncio.CancelledError:
            raise
        finally:
            current = self._pending.get(notice.request_id)
            if current is future:
                self._pending.pop(notice.request_id, None)

        task = asyncio.current_task()
        # If the proxy already gave up, do not hand an Allow back to the hook.
        # Python delivers CancelledError at the *next* await, so a decision
        # that won the race with cancellation would otherwise look usable.
        if task is not None and task.cancelling():
            raise asyncio.CancelledError()
        if decision.request_id != notice.request_id:
            raise IpcProtocolError("mismatched_decision")
        return decision

    async def aclose(self) -> None:
        self._closed = True
        await self._fail_pending(IpcDisconnected("ipc_client_closed"))
        await self._close_socket()

    async def _ensure_connected(self) -> None:
        async with self._connect_lock:
            if self._healthy():
                return
            await self._close_socket()
            await self._connect_with_retries()

    def _healthy(self) -> bool:
        if self._writer is None or self._reader_task is None:
            return False
        if self._writer.is_closing() or self._reader_task.done():
            return False
        return True

    async def _connect_with_retries(self) -> None:
        deadline = time.monotonic() + self._connect_timeout
        delay = 0.05
        last_error: BaseException | None = None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise IpcUnavailable("ui_unavailable") from last_error
            writer: asyncio.StreamWriter | None = None
            try:
                try:
                    endpoint = self._resolve_endpoint()
                except EndpointRejected as exc:
                    raise IpcUnavailable("endpoint_rejected") from exc
                except EndpointNotReady as exc:
                    raise IpcUnavailable("ui_not_ready") from exc
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(
                        LOOPBACK_HOST,
                        endpoint.port,
                        limit=MAX_MESSAGE_BYTES,
                    ),
                    timeout=min(1.0, remaining),
                )
                _nodelay(writer)
                peer = writer.get_extra_info("peername")
                if not _peer_is_loopback(peer):
                    raise IpcUnavailable("refused_non_loopback_peer")
                await _write_frame(
                    writer,
                    encode_message(
                        HelloMessage(token=endpoint.token, server_name=self._server_name)
                    ),
                )
                line = await asyncio.wait_for(
                    reader.readuntil(b"\n"),
                    timeout=min(2.0, max(0.1, deadline - time.monotonic())),
                )
                hello = decode_message(line)
                if not isinstance(hello, HelloOkMessage):
                    raise IpcUnavailable("hello_rejected")
            except asyncio.CancelledError:
                if writer is not None:
                    writer.close()
                raise
            except Exception as exc:
                if writer is not None:
                    writer.close()
                last_error = exc
                # A public host, or a broken token, will not heal itself.
                if isinstance(exc, IpcUnavailable) and str(exc) == "endpoint_rejected":
                    raise
                logger.info("ipc_connect_retry error_type=%s", type(exc).__name__)
                await asyncio.sleep(min(delay, max(0.0, deadline - time.monotonic())))
                delay = min(delay * 2, 0.4)
                continue

            self._reader = reader
            self._writer = writer
            self._reader_task = asyncio.create_task(
                self._read_loop(),
                name="mcp-guardian-ipc-reader",
            )
            logger.info("ipc_connected port=%s", endpoint.port)
            return

    def _resolve_endpoint(self) -> IpcEndpoint:
        if self._fixed is not None:
            validate_token(self._fixed.token)
            return self._fixed
        assert self._endpoint_file is not None
        return load_endpoint(self._endpoint_file)

    async def _read_loop(self) -> None:
        reader = self._reader
        if reader is None:
            return
        try:
            while not self._closed:
                try:
                    line = await reader.readuntil(b"\n")
                except asyncio.LimitOverrunError as exc:
                    raise IpcProtocolError("message_too_large") from exc
                except asyncio.IncompleteReadError as exc:
                    if exc.partial:
                        raise IpcProtocolError("truncated_message") from exc
                    raise IpcDisconnected("ui_disconnected") from exc
                message = decode_message(line)
                self._dispatch(message)
        except asyncio.CancelledError:
            raise
        except IpcDisconnected as exc:
            await self._fail_pending(exc)
        except IpcProtocolError as exc:
            logger.warning("ipc_malformed code=%s", exc.code)
            await self._fail_pending(exc)
        except Exception as exc:
            logger.error("ipc_reader_failed error_type=%s", type(exc).__name__)
            await self._fail_pending(IpcDisconnected("ui_disconnected"))
        finally:
            await self._close_socket()

    def _dispatch(self, message: object) -> None:
        if isinstance(message, DecisionNotice):
            future = self._pending.get(message.request_id)
            if future is None or future.done():
                # Unknown, duplicate, or late. Do not complete a different ID.
                logger.info(
                    "ipc_ignored_decision request_id=%s",
                    message.request_id,
                )
                return
            _settle(future, message)
            return
        if isinstance(message, ErrorNotice):
            logger.info("ipc_peer_error code=%s", message.code)
            if message.code == "duplicate_request" or message.request_id is None:
                # A duplicate frame must not cancel the original waiter.
                return
            future = self._pending.get(message.request_id)
            if future is None or future.done():
                return
            # An error is not an approval. Wake the waiter so it can cancel.
            _settle(future, IpcProtocolError(message.code))
            return
        raise IpcProtocolError("unexpected_message")

    async def _fail_pending(self, exc: Exception) -> None:
        waiters = list(self._pending.values())
        self._pending.clear()
        for future in waiters:
            if not future.done():
                future.set_exception(exc)

    async def _write(self, frame: bytes) -> None:
        if self._writer is None:
            raise IpcDisconnected("ui_disconnected")
        await _write_frame(self._writer, frame)

    async def _close_socket(self) -> None:
        writer = self._writer
        self._writer = None
        self._reader = None
        task = self._reader_task
        self._reader_task = None
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass


def _settle(
    future: asyncio.Future[DecisionNotice],
    outcome: DecisionNotice | IpcProtocolError,
) -> None:
    """Complete a waiter unless it was cancelled between the check and now."""
    if future.done():
        return
    try:
        if isinstance(outcome, DecisionNotice):
            future.set_result(outcome)
        else:
            future.set_exception(outcome)
    except asyncio.InvalidStateError:
        return


async def _write_frame(writer: asyncio.StreamWriter, frame: bytes) -> None:
    writer.write(frame)
    await writer.drain()


def _nodelay(writer: asyncio.StreamWriter) -> None:
    sock = writer.get_extra_info("socket")
    if sock is None:
        return
    try:
        import socket

        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError:
        return


def _peer_is_loopback(peer: object) -> bool:
    if not isinstance(peer, tuple) or not peer:
        return False
    return peer[0] == LOOPBACK_HOST
