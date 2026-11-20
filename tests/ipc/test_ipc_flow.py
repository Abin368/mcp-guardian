"""Phase 4: proxy ↔ UI approval over loopback, without a Qt window.

The upstream server is the Phase 3 fake with an execution counter. A passing
test is not "the proxy returned an error string". The counter must stay at
zero unless the UI sent approve for that exact request.
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
from mcp import Client

from mcp_guardian.application.pending_coordinator import PendingRequestCoordinator
from mcp_guardian.domain.models import DecisionAction, RequestState
from mcp_guardian.ipc.endpoint import IpcEndpoint
from mcp_guardian.ipc.protocol import (
    LOOPBACK_HOST,
    ApprovalNotice,
    DecisionNotice,
    HelloMessage,
    HelloOkMessage,
    action_to_apply,
    decode_message,
    encode_message,
)
from mcp_guardian.ipc.server import ApprovalIpcServer
from mcp_guardian.presentation.redaction import REDACTED
from mcp_guardian.proxy.ipc_approval import IpcApprovalProvider, ui_budget_seconds
from mcp_guardian.proxy.logging_config import configure_proxy_logging
from mcp_guardian.proxy.proxy import GuardianProxy
from tests.proxy.fake_upstream import ExecutionCounter, make_fake_upstream

SECRET = "super-secret-value"
TOKEN = "test-token-not-a-secret-value"


def _text(result: object) -> str:
    content = result.content[0]  # type: ignore[attr-defined]
    return content.text  # type: ignore[no-any-return]


@asynccontextmanager
async def _proxied(
    handler,
    *,
    timeout: float = 2.0,
    connect_timeout: float = 2.0,
    endpoint: IpcEndpoint | None = None,
    token: str = TOKEN,
) -> AsyncIterator[tuple]:
    counter = ExecutionCounter()
    server: ApprovalIpcServer | None = None
    if endpoint is None:
        server = ApprovalIpcServer(token=token, handler=handler, port=0)
        endpoint = await server.start()
        assert endpoint.host == LOOPBACK_HOST

    coordinator = PendingRequestCoordinator()
    provider = IpcApprovalProvider(
        coordinator=coordinator,
        endpoint=endpoint,
        server_name="fake-upstream",
        default_timeout=timeout,
        connect_timeout=connect_timeout,
    )
    upstream_cm = Client(make_fake_upstream(counter))
    upstream = await upstream_cm.__aenter__()
    proxy = GuardianProxy(
        upstream=upstream,
        approval=provider,
        server_name="fake-upstream",
        approval_timeout=timeout,
        coordinator=coordinator,
    )
    downstream_cm = Client(proxy.build_server())
    downstream = await downstream_cm.__aenter__()
    try:
        yield downstream, counter, server, coordinator, provider, endpoint
    finally:
        await downstream_cm.__aexit__(None, None, None)
        await proxy.shutdown()
        await provider.aclose()
        await upstream_cm.__aexit__(None, None, None)
        if server is not None:
            await server.close()


class ScriptedPeer:
    """A UI stand-in that can send illegal frames on purpose."""

    def __init__(self, token: str = TOKEN) -> None:
        self.token = token
        self.endpoint: IpcEndpoint | None = None
        self._tcp: asyncio.Server | None = None

    async def start(self, on_request) -> IpcEndpoint:
        async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                hello = decode_message(await reader.readuntil(b"\n"))
                assert isinstance(hello, HelloMessage)
                if hello.token != self.token:
                    return
                writer.write(encode_message(HelloOkMessage()))
                await writer.drain()
                while True:
                    message = decode_message(await reader.readuntil(b"\n"))
                    if isinstance(message, ApprovalNotice):
                        await on_request(message, writer)
                        await reader.read()
                        return
            except (asyncio.IncompleteReadError, ConnectionError, asyncio.CancelledError):
                return
            finally:
                writer.close()

        self._tcp = await asyncio.start_server(
            _handle,
            host=LOOPBACK_HOST,
            port=0,
            family=socket.AF_INET,
        )
        bound = self._tcp.sockets[0].getsockname()
        assert bound[0] == LOOPBACK_HOST
        self.endpoint = IpcEndpoint(host=LOOPBACK_HOST, port=bound[1], token=self.token)
        return self.endpoint

    async def close(self) -> None:
        if self._tcp is not None:
            self._tcp.close()
            await self._tcp.wait_closed()


def test_ui_budget_is_shorter_than_the_proxy_wait() -> None:
    assert ui_budget_seconds(120) == 118
    assert ui_budget_seconds(0.4) < 0.4


def test_decision_for_another_id_is_not_an_approval() -> None:
    real = DecisionNotice(
        request_id="12345678-1234-4234-8234-123456789abc",
        action="approve",
        reason="ui_allow",
    )
    assert action_to_apply("12345678-1234-4234-8234-123456789abd", real) == "cancel"
    assert action_to_apply(real.request_id, real) == "approve"


def test_malformed_frame_is_rejected_without_parsing_as_approval() -> None:
    with pytest.raises(Exception):
        decode_message(b"this is not json\n")
    with pytest.raises(Exception):
        decode_message(b'{"v":1,"type":"decision","action":"approve"}\n')


@pytest.mark.asyncio
async def test_proxy_request_reaches_ui_and_approve_runs_upstream_once() -> None:
    seen: list[ApprovalNotice] = []

    async def handler(notice: ApprovalNotice) -> DecisionNotice:
        seen.append(notice)
        return DecisionNotice(notice.request_id, "approve", "ui_allow")

    async with _proxied(handler) as (client, counter, server, _coordinator, _provider, endpoint):
        assert server is not None
        assert server.endpoint is not None
        assert endpoint.host == LOOPBACK_HOST
        result = await asyncio.wait_for(
            client.call_tool(
                "echo",
                {"message": "hello", "api_key": SECRET, "nested": {"token": SECRET}},
            ),
            timeout=5,
        )

    assert result.is_error is False
    assert "echo:hello" in _text(result)
    assert counter.calls == 1
    assert counter.by_tool == {"echo": 1}
    # The real arguments still reach the tool. Only the UI copy is redacted.
    assert counter.last_arguments is not None
    assert counter.last_arguments["api_key"] == SECRET
    assert len(seen) == 1
    assert seen[0].server_name == "fake-upstream"
    assert seen[0].tool_name == "echo"
    encoded = json.dumps(seen[0].arguments)
    assert SECRET not in encoded
    assert seen[0].arguments["api_key"] == REDACTED
    assert seen[0].arguments["nested"]["token"] == REDACTED
    assert seen[0].arguments["message"] == "hello"


@pytest.mark.asyncio
async def test_block_never_executes_upstream() -> None:
    async def handler(notice: ApprovalNotice) -> DecisionNotice:
        return DecisionNotice(notice.request_id, "deny", "ui_block")

    async with _proxied(handler) as (client, counter, *_rest):
        result = await asyncio.wait_for(
            client.call_tool("echo", {"message": "should-not-run"}),
            timeout=5,
        )

    assert result.is_error is True
    assert "blocked" in _text(result).lower() or "denied" in _text(result).lower()
    assert counter.calls == 0


@pytest.mark.asyncio
async def test_ui_disconnect_never_executes_upstream() -> None:
    started = asyncio.Event()

    async def handler(notice: ApprovalNotice) -> DecisionNotice:
        started.set()
        await asyncio.Event().wait()
        return DecisionNotice(notice.request_id, "approve", "ui_allow")

    async with _proxied(handler, timeout=5) as (client, counter, server, *_rest):
        assert server is not None
        call = asyncio.create_task(client.call_tool("echo", {"message": "nope"}))
        await asyncio.wait_for(started.wait(), timeout=2)
        await server.close()
        result = await asyncio.wait_for(call, timeout=5)

    assert result.is_error is True
    assert counter.calls == 0


def _drop_proxy_ipc_socket(provider: IpcApprovalProvider) -> None:
    """Abort the proxy's approval socket without asking the UI to close."""
    writer = provider._client._writer
    assert writer is not None
    transport = writer.transport
    assert transport is not None
    transport.abort()


@pytest.mark.asyncio
async def test_proxy_ipc_socket_drop_never_executes_upstream() -> None:
    started = asyncio.Event()

    async def handler(notice: ApprovalNotice) -> DecisionNotice:
        started.set()
        await asyncio.Event().wait()
        return DecisionNotice(notice.request_id, "approve", "ui_allow")

    async with _proxied(handler, timeout=5) as (
        client,
        counter,
        _server,
        coordinator,
        provider,
        _endpoint,
    ):
        call = asyncio.create_task(client.call_tool("echo", {"message": "dropped"}))
        await asyncio.wait_for(started.wait(), timeout=2)
        _drop_proxy_ipc_socket(provider)
        result = await asyncio.wait_for(call, timeout=5)
        slots = list(coordinator._slots.values())

    assert result.is_error is True
    assert counter.calls == 0
    assert len(slots) == 1
    assert slots[0].request.state is RequestState.CANCELLED
    assert slots[0].request.decision is not None
    assert slots[0].request.decision.allows_execution is False


@pytest.mark.asyncio
async def test_two_pending_requests_fail_closed_when_ipc_disconnects() -> None:
    both_pending = asyncio.Event()
    seen: list[str] = []

    async def handler(notice: ApprovalNotice) -> DecisionNotice:
        seen.append(notice.request_id)
        if len(seen) >= 2:
            both_pending.set()
        await asyncio.Event().wait()
        return DecisionNotice(notice.request_id, "approve", "ui_allow")

    async with _proxied(handler, timeout=5) as (client, counter, server, coordinator, *_rest):
        assert server is not None
        first = asyncio.create_task(client.call_tool("echo", {"message": "one"}))
        second = asyncio.create_task(client.call_tool("fail", {"reason": "two"}))
        await asyncio.wait_for(both_pending.wait(), timeout=2)
        await server.close()
        first_result, second_result = await asyncio.wait_for(
            asyncio.gather(first, second),
            timeout=5,
        )
        states = [slot.request.state for slot in coordinator._slots.values()]
        decisions = [slot.request.decision for slot in coordinator._slots.values()]

    assert first_result.is_error is True
    assert second_result.is_error is True
    assert counter.calls == 0
    assert len(states) == 2
    assert states == [RequestState.CANCELLED, RequestState.CANCELLED]
    assert all(decision is not None and decision.allows_execution is False for decision in decisions)


@pytest.mark.asyncio
async def test_approved_call_stderr_omits_secret_arguments(capsys: pytest.CaptureFixture[str]) -> None:
    password = "pw-do-not-log"

    async def handler(notice: ApprovalNotice) -> DecisionNotice:
        return DecisionNotice(notice.request_id, "approve", "ui_allow")

    proxy_logger = logging.getLogger("mcp_guardian.proxy")
    previous_handlers = list(proxy_logger.handlers)
    previous_level = proxy_logger.level
    previous_propagate = proxy_logger.propagate
    configure_proxy_logging(logging.INFO)
    try:
        async with _proxied(handler) as (client, counter, *_rest):
            result = await asyncio.wait_for(
                client.call_tool(
                    "echo",
                    {
                        "message": "hello",
                        "api_key": SECRET,
                        "nested": {"password": password, "token": SECRET},
                    },
                ),
                timeout=5,
            )
        captured = capsys.readouterr()
    finally:
        current_handlers = list(proxy_logger.handlers)
        proxy_logger.handlers.clear()
        for stream_handler in current_handlers:
            if stream_handler not in previous_handlers:
                stream_handler.close()
        for stream_handler in previous_handlers:
            proxy_logger.addHandler(stream_handler)
        proxy_logger.setLevel(previous_level)
        proxy_logger.propagate = previous_propagate

    assert result.is_error is False
    assert "echo:hello" in _text(result)
    assert counter.calls == 1
    assert SECRET not in captured.err
    assert password not in captured.err
    assert "decision=approve" in captured.err


@pytest.mark.asyncio
async def test_approval_timeout_never_executes_upstream() -> None:
    async def handler(notice: ApprovalNotice) -> DecisionNotice:
        await asyncio.sleep(30)
        return DecisionNotice(notice.request_id, "approve", "ui_allow")

    async with _proxied(handler, timeout=0.4) as (client, counter, _server, coordinator, *_rest):
        result = await asyncio.wait_for(
            client.call_tool("echo", {"message": "too-slow"}),
            timeout=5,
        )
        pending = await coordinator.pending_ids()

    assert result.is_error is True
    assert "timed out" in _text(result).lower() or "timeout" in _text(result).lower()
    assert counter.calls == 0
    assert pending == []


@pytest.mark.asyncio
async def test_two_concurrent_requests_get_independent_decisions() -> None:
    async def handler(notice: ApprovalNotice) -> DecisionNotice:
        await asyncio.sleep(0.05)
        if notice.tool_name == "echo":
            return DecisionNotice(notice.request_id, "approve", "ui_allow")
        return DecisionNotice(notice.request_id, "deny", "ui_block")

    async with _proxied(handler) as (client, counter, *_rest):
        echo_task = asyncio.create_task(client.call_tool("echo", {"message": "ok"}))
        fail_task = asyncio.create_task(client.call_tool("fail", {"reason": "nope"}))
        echo_result, fail_result = await asyncio.wait_for(
            asyncio.gather(echo_task, fail_task),
            timeout=5,
        )

    assert echo_result.is_error is False
    assert "echo:ok" in _text(echo_result)
    assert fail_result.is_error is True
    assert counter.calls == 1
    assert counter.by_tool == {"echo": 1}


@pytest.mark.asyncio
async def test_decision_for_one_request_cannot_approve_another() -> None:
    peer = ScriptedPeer()

    async def on_request(notice: ApprovalNotice, writer: asyncio.StreamWriter) -> None:
        other = str(uuid4())
        writer.write(
            encode_message(DecisionNotice(other, "approve", "ui_allow"))
        )
        await writer.drain()
        writer.write(
            encode_message(DecisionNotice(notice.request_id, "deny", "ui_block"))
        )
        await writer.drain()

    endpoint = await peer.start(on_request)
    try:
        async with _proxied(handler=None, endpoint=endpoint) as (
            client,
            counter,
            _server,
            coordinator,
            *_rest,
        ):
            result = await asyncio.wait_for(
                client.call_tool("echo", {"message": "cross"}),
                timeout=5,
            )
            stored = [
                await coordinator.get_request(request_id)
                for request_id in list(coordinator._slots)
            ]
    finally:
        await peer.close()

    assert result.is_error is True
    assert counter.calls == 0
    assert stored
    assert stored[0].state is RequestState.DENIED
    assert stored[0].decision is not None
    assert stored[0].decision.allows_execution is False


@pytest.mark.asyncio
async def test_duplicate_and_late_decisions_are_rejected() -> None:
    peer = ScriptedPeer()
    release = asyncio.Event()
    first_seen = asyncio.Event()

    async def on_request(notice: ApprovalNotice, writer: asyncio.StreamWriter) -> None:
        decision = encode_message(
            DecisionNotice(notice.request_id, "approve", "ui_allow")
        )
        writer.write(decision)
        await writer.drain()
        writer.write(decision)
        await writer.drain()
        first_seen.set()
        await release.wait()
        writer.write(decision)
        await writer.drain()

    endpoint = await peer.start(on_request)
    try:
        async with _proxied(handler=None, endpoint=endpoint) as (
            client,
            counter,
            _server,
            coordinator,
            *_rest,
        ):
            approve_calls: list[str] = []
            original = coordinator.approve

            async def spy(request_id: str, reason: str | None = None):
                approve_calls.append(request_id)
                return await original(request_id, reason)

            coordinator.approve = spy  # type: ignore[method-assign]
            result = await asyncio.wait_for(
                client.call_tool("echo", {"message": "once"}),
                timeout=5,
            )
            await asyncio.wait_for(first_seen.wait(), timeout=2)
            assert counter.calls == 1
            assert approve_calls
            # The tool already ran. A third, late Allow must not run it again
            # and must not be able to approve a different call either.
            release.set()
            await asyncio.sleep(0.2)
            assert counter.calls == 1
            assert len(approve_calls) == 1
            assert result.is_error is False
    finally:
        await peer.close()

    peer_late = ScriptedPeer()
    late_release = asyncio.Event()
    arrived = asyncio.Event()

    async def late(notice: ApprovalNotice, writer: asyncio.StreamWriter) -> None:
        arrived.set()
        await late_release.wait()
        writer.write(
            encode_message(DecisionNotice(notice.request_id, "approve", "ui_allow"))
        )
        await writer.drain()

    late_endpoint = await peer_late.start(late)
    try:
        async with _proxied(handler=None, endpoint=late_endpoint, timeout=0.4) as (
            client,
            counter,
            _server,
            coordinator,
            *_rest,
        ):
            call = asyncio.create_task(client.call_tool("echo", {"message": "late"}))
            await asyncio.wait_for(arrived.wait(), timeout=2)
            result = await asyncio.wait_for(call, timeout=5)
            late_release.set()
            await asyncio.sleep(0.2)
            states = [
                slot.request.state
                for slot in coordinator._slots.values()
            ]
    finally:
        await peer_late.close()

    assert result.is_error is True
    assert counter.calls == 0
    assert states == [RequestState.TIMED_OUT]


@pytest.mark.asyncio
async def test_malformed_ipc_fails_closed() -> None:
    peer = ScriptedPeer()

    async def on_request(notice: ApprovalNotice, writer: asyncio.StreamWriter) -> None:
        writer.write(b"this is not json\n")
        await writer.drain()

    endpoint = await peer.start(on_request)
    try:
        async with _proxied(handler=None, endpoint=endpoint) as (client, counter, *_rest):
            result = await asyncio.wait_for(
                client.call_tool("echo", {"message": "garbage"}),
                timeout=5,
            )
    finally:
        await peer.close()

    assert result.is_error is True
    assert counter.calls == 0


@pytest.mark.asyncio
async def test_ui_shutdown_with_pending_request_fails_closed() -> None:
    started = asyncio.Event()

    async def handler(notice: ApprovalNotice) -> DecisionNotice:
        started.set()
        await asyncio.Event().wait()
        return DecisionNotice(notice.request_id, "approve", "ui_allow")

    async with _proxied(handler, timeout=5) as (client, counter, server, coordinator, *_rest):
        assert server is not None
        call = asyncio.create_task(client.call_tool("echo", {"message": "shutdown"}))
        await asyncio.wait_for(started.wait(), timeout=2)
        await server.close()
        result = await asyncio.wait_for(call, timeout=5)
        states = [slot.request.state for slot in coordinator._slots.values()]

    assert result.is_error is True
    assert counter.calls == 0
    assert states
    assert states[0] is not RequestState.APPROVED
    assert all(state is not RequestState.APPROVED for state in states)


@pytest.mark.asyncio
async def test_missing_ui_and_wrong_token_never_approve(
    caplog: pytest.LogCaptureFixture,
) -> None:
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.bind((LOOPBACK_HOST, 0))
    closed_port = holder.getsockname()[1]
    holder.close()

    coordinator = PendingRequestCoordinator()
    provider = IpcApprovalProvider(
        coordinator=coordinator,
        endpoint=IpcEndpoint(host=LOOPBACK_HOST, port=closed_port, token=TOKEN),
        connect_timeout=0.4,
        default_timeout=2,
    )
    with caplog.at_level("DEBUG"):
        outcome = await provider.await_decision(
            server_name="fake-upstream",
            tool_name="echo",
            arguments={"api_key": SECRET, "message": "nobody-home"},
        )
    await provider.aclose()
    assert outcome.allows_execution is False
    assert outcome.decision.action is not DecisionAction.APPROVE
    assert SECRET not in caplog.text

    server_ready = asyncio.Event()

    async def handler(notice: ApprovalNotice) -> DecisionNotice:
        server_ready.set()
        return DecisionNotice(notice.request_id, "approve", "ui_allow")

    server = ApprovalIpcServer(token=TOKEN, handler=handler, port=0)
    endpoint = await server.start()
    wrong = IpcEndpoint(
        host=LOOPBACK_HOST,
        port=endpoint.port,
        token="wrong-token-value-here",
    )
    bad = IpcApprovalProvider(
        coordinator=PendingRequestCoordinator(),
        endpoint=wrong,
        connect_timeout=0.5,
        default_timeout=2,
    )
    try:
        refused = await bad.await_decision(
            server_name="fake-upstream",
            tool_name="echo",
            arguments={"message": "wrong-token"},
        )
    finally:
        await bad.aclose()
        await server.close()

    assert refused.allows_execution is False
    assert server_ready.is_set() is False


@pytest.mark.asyncio
async def test_handler_cannot_retarget_a_decision_onto_another_id() -> None:
    async def handler(notice: ApprovalNotice) -> DecisionNotice:
        return DecisionNotice(str(uuid4()), "approve", "ui_allow")

    async with _proxied(handler) as (client, counter, *_rest):
        result = await asyncio.wait_for(
            client.call_tool("echo", {"message": "retarget"}),
            timeout=5,
        )

    # The server stamps the original request ID back onto the decision.
    # This call was the one the human (handler) approved, so it may run.
    # A second tool was never approved.
    assert result.is_error is False
    assert counter.calls == 1
    assert counter.by_tool == {"echo": 1}
