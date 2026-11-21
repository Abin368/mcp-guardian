"""Phase 1 loopback gateway. No tunnel and no OAuth.

The MCP client is in this process. It speaks Streamable HTTP to 127.0.0.1.
Upstream is the in-process fake server with an execution counter.
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager

import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from mcp_guardian.application.pending_coordinator import PendingRequestCoordinator
from mcp_guardian.domain.models import RequestState
from mcp_guardian.gateway.app import MCP_PATH, build_gateway_app
from mcp_guardian.gateway.auth import ENV_GATEWAY_TOKEN, GatewayStartupError, require_gateway_token
from mcp_guardian.gateway.listener import open_loopback_listener, running_loopback
from mcp_guardian.gateway.__main__ import (
    build_gateway_parser,
    gateway_settings_from_args,
    run_main,
)
from mcp_guardian.ipc.endpoint import IpcEndpoint
from mcp_guardian.ipc.protocol import LOOPBACK_HOST, ApprovalNotice, DecisionNotice
from mcp_guardian.ipc.server import ApprovalIpcServer
from mcp_guardian.proxy.__main__ import build_arg_parser
from mcp_guardian.proxy.approval_provider import make_deny_all_provider
from mcp_guardian.proxy.ipc_approval import IpcApprovalProvider
from mcp_guardian.proxy.proxy import GuardianProxy
from tests.proxy.fake_upstream import ExecutionCounter, make_fake_upstream

GATEWAY_TOKEN = "gateway-token-aaaaaaaa"
BAD_TOKEN = "gateway-token-bbbbbbbb"
IPC_TOKEN = "ipc-token-cccccccccccc"
PROTOCOL_VERSION = "2025-03-26"


def _base(port: int) -> str:
    return f"http://{LOOPBACK_HOST}:{port}{MCP_PATH}"


@asynccontextmanager
async def _gateway(
    approval,
    coordinator: PendingRequestCoordinator,
    counter: ExecutionCounter,
    *,
    token: str = GATEWAY_TOKEN,
    timeout: float = 5.0,
    allowed_hosts: Sequence[str] | None = None,
) -> AsyncIterator[int]:
    upstream_cm = Client(make_fake_upstream(counter))
    upstream = await upstream_cm.__aenter__()
    proxy = GuardianProxy(
        upstream=upstream,
        approval=approval,
        server_name="fake-upstream",
        approval_timeout=timeout,
        coordinator=coordinator,
    )
    app = build_gateway_app(
        proxy.build_server(),
        token=token,
        allowed_hosts=allowed_hosts,
    )
    assert token not in repr(app)
    try:
        async with running_loopback(app, 0) as port:
            yield port
    finally:
        await proxy.shutdown()
        closer = getattr(approval, "aclose", None)
        if closer is not None:
            await closer()
        await upstream_cm.__aexit__(None, None, None)


@asynccontextmanager
async def _mcp(port: int, token: str = GATEWAY_TOKEN) -> AsyncIterator[Client]:
    timeout = httpx2.Timeout(30.0, read=60.0)
    async with httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {token}"},
        timeout=timeout,
        trust_env=False,
    ) as http:
        transport = streamable_http_client(_base(port), http_client=http)
        async with Client(transport, mode="legacy", read_timeout_seconds=20) as client:
            yield client


async def _post(port: int, headers: dict[str, str] | None) -> httpx2.Response:
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "echo", "arguments": {"message": "should-not-run"}},
    }
    async with httpx2.AsyncClient(trust_env=False, timeout=5.0) as http:
        return await http.post(_base(port), headers=headers, json=body)


def _closed_port() -> int:
    sock = open_loopback_listener(0)
    port = int(sock.getsockname()[1])
    sock.close()
    return port


def _secrets_absent(*blobs: str) -> None:
    combined = "\n".join(blobs)
    assert GATEWAY_TOKEN not in combined
    assert BAD_TOKEN not in combined
    assert IPC_TOKEN not in combined


@pytest.mark.asyncio
async def test_missing_bearer_is_401_and_does_not_execute() -> None:
    coordinator = PendingRequestCoordinator()
    counter = ExecutionCounter()
    approval = make_deny_all_provider(coordinator, timeout=2.0)
    async with _gateway(approval, coordinator, counter) as port:
        response = await _post(port, headers=None)

    assert response.status_code == 401
    assert response.json()["error"] == "unauthorized"
    assert counter.calls == 0
    assert coordinator._slots == {}
    _secrets_absent(response.text, str(response.headers))


@pytest.mark.asyncio
async def test_invalid_bearer_is_401_and_does_not_echo_the_token(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="mcp_guardian")
    caplog.set_level(logging.DEBUG, logger="uvicorn")
    coordinator = PendingRequestCoordinator()
    counter = ExecutionCounter()
    approval = make_deny_all_provider(coordinator, timeout=2.0)
    async with _gateway(approval, coordinator, counter) as port:
        response = await _post(port, headers={"Authorization": f"Bearer {BAD_TOKEN}"})
        ipc_as_bearer = await _post(port, headers={"Authorization": f"Bearer {IPC_TOKEN}"})

    assert response.status_code == 401
    assert ipc_as_bearer.status_code == 401
    assert counter.calls == 0
    assert coordinator._slots == {}
    _secrets_absent(
        response.text,
        ipc_as_bearer.text,
        str(response.headers),
        str(ipc_as_bearer.headers),
        caplog.text,
    )


@pytest.mark.asyncio
async def test_valid_bearer_allows_initialization() -> None:
    coordinator = PendingRequestCoordinator()
    counter = ExecutionCounter()
    approval = make_deny_all_provider(coordinator, timeout=2.0)
    async with _gateway(approval, coordinator, counter) as port:
        async with _mcp(port) as client:
            assert client.protocol_version
            assert client.server_capabilities.tools is not None
    assert counter.calls == 0
    assert coordinator._slots == {}


@pytest.mark.asyncio
async def test_tools_list_does_not_trigger_approval() -> None:
    coordinator = PendingRequestCoordinator()
    counter = ExecutionCounter()
    # Point IPC at a closed port. tools/list must not wait on it.
    approval = IpcApprovalProvider(
        coordinator=coordinator,
        endpoint=IpcEndpoint(host=LOOPBACK_HOST, port=_closed_port(), token=IPC_TOKEN),
        server_name="fake-upstream",
        default_timeout=2.0,
        connect_timeout=0.3,
    )
    async with _gateway(approval, coordinator, counter, timeout=2.0) as port:
        async with _mcp(port) as client:
            listed = await client.list_tools()

    names = sorted(tool.name for tool in listed.tools)
    assert names == ["echo", "fail"]
    assert counter.calls == 0
    assert coordinator._slots == {}


@pytest.mark.asyncio
async def test_tools_call_goes_through_the_approval_system() -> None:
    coordinator = PendingRequestCoordinator()
    counter = ExecutionCounter()
    seen: list[ApprovalNotice] = []

    async def handler(notice: ApprovalNotice) -> DecisionNotice:
        seen.append(notice)
        return DecisionNotice(notice.request_id, "approve", "ui_allow")

    ipc = ApprovalIpcServer(token=IPC_TOKEN, handler=handler, port=0)
    endpoint = await ipc.start()
    approval = IpcApprovalProvider(
        coordinator=coordinator,
        endpoint=endpoint,
        server_name="fake-upstream",
        default_timeout=5.0,
        connect_timeout=2.0,
    )
    try:
        async with _gateway(approval, coordinator, counter, timeout=5.0) as port:
            async with _mcp(port) as client:
                result = await client.call_tool(
                    "echo",
                    {"message": "hello", "api_key": "super-secret-value"},
                )
    finally:
        await ipc.close()

    assert result.is_error is False
    assert "echo:hello" in result.content[0].text
    assert counter.calls == 1
    assert len(seen) == 1
    request = await coordinator.get_request(seen[0].request_id)
    assert request.state is RequestState.APPROVED
    # The UI copy is redacted. The gateway bearer is not that token.
    assert seen[0].arguments["api_key"] == "***REDACTED***"
    assert "super-secret-value" not in str(seen[0].arguments)
    assert counter.last_arguments is not None
    assert counter.last_arguments["api_key"] == "super-secret-value"


@pytest.mark.asyncio
async def test_missing_ui_fails_closed_and_never_executes(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="mcp_guardian")
    coordinator = PendingRequestCoordinator()
    counter = ExecutionCounter()
    approval = IpcApprovalProvider(
        coordinator=coordinator,
        endpoint=IpcEndpoint(host=LOOPBACK_HOST, port=_closed_port(), token=IPC_TOKEN),
        server_name="fake-upstream",
        default_timeout=2.0,
        connect_timeout=0.4,
    )
    async with _gateway(approval, coordinator, counter, timeout=2.0) as port:
        async with _mcp(port) as client:
            result = await client.call_tool("echo", {"message": "nope"})

    assert result.is_error is True
    assert "Blocked by MCP Guardian" in result.content[0].text
    assert counter.calls == 0
    assert len(coordinator._slots) == 1
    request = next(iter(coordinator._slots.values())).request
    assert request.state is RequestState.CANCELLED
    _secrets_absent(result.content[0].text, caplog.text)


def test_startup_requires_the_gateway_token(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(GatewayStartupError, match="required"):
        require_gateway_token({})
    with pytest.raises(GatewayStartupError, match="invalid"):
        require_gateway_token({ENV_GATEWAY_TOKEN: "short"})

    argv = ["--server-name", "echo", "--upstream-command", "python", "--listen-port", "9"]
    missing = run_main(argv, environ={})
    invalid = run_main(argv, environ={ENV_GATEWAY_TOKEN: "not a token"})
    captured = capsys.readouterr()
    assert missing == 2
    assert invalid == 2
    assert "MCP_GUARDIAN_GATEWAY_TOKEN is required" in captured.err
    assert "MCP_GUARDIAN_GATEWAY_TOKEN is invalid" in captured.err
    assert "not a token" not in captured.err
    assert IPC_TOKEN not in captured.err


def test_gateway_cli_defaults_and_refuses_a_public_host() -> None:
    parser = build_gateway_parser()
    args = parser.parse_args(["--server-name", "echo", "--upstream-command", "python"])
    assert args.approval == "ipc"
    assert args.listen_host == LOOPBACK_HOST
    assert args.listen_port == 8080
    config, port, allowed_hosts = gateway_settings_from_args(args)
    assert config.approval_mode == "ipc"
    assert port == 8080
    assert allowed_hosts == ()

    # Building the gateway parser must not change the STDIO proxy default.
    stdio = build_arg_parser().parse_args(
        ["--server-name", "echo", "--upstream-command", "python"]
    )
    assert stdio.approval == "deny-all"

    public = parser.parse_args(
        [
            "--server-name",
            "echo",
            "--upstream-command",
            "python",
            "--listen-host",
            "0.0.0.0",
        ]
    )
    with pytest.raises(ValueError, match="127.0.0.1"):
        gateway_settings_from_args(public)

    slow = parser.parse_args(
        [
            "--server-name",
            "echo",
            "--upstream-command",
            "python",
            "--approval-timeout",
            "181",
        ]
    )
    with pytest.raises(ValueError, match="180"):
        gateway_settings_from_args(slow)

    with_tunnel_host = parser.parse_args(
        [
            "--server-name",
            "echo",
            "--upstream-command",
            "python",
            "--allowed-host",
            "tunnel.example.test",
        ]
    )
    _, _, extras = gateway_settings_from_args(with_tunnel_host)
    assert extras == ("tunnel.example.test",)


def test_listener_binds_only_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[object] = []
    real_socket = socket.socket

    def tracking(*args, **kwargs):
        created.append(args)
        return real_socket(*args, **kwargs)

    monkeypatch.setattr(socket, "socket", tracking)
    for host in ("0.0.0.0", "localhost", "::1"):
        with pytest.raises(ValueError, match="127.0.0.1"):
            open_loopback_listener(8080, host=host)
    assert created == []

    sock = open_loopback_listener(0)
    try:
        host, port = sock.getsockname()[:2]
        assert host == LOOPBACK_HOST
        assert sock.family == socket.AF_INET
        assert isinstance(port, int) and port > 0
    finally:
        sock.close()


def _auth_headers(token: str = GATEWAY_TOKEN, **extra: str) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": PROTOCOL_VERSION,
    }
    headers.update(extra)
    return headers


async def _initialize_session(http: httpx2.AsyncClient, port: int) -> str:
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "gateway-test", "version": "0"},
        },
    }
    response = await http.post(_base(port), headers=_auth_headers(), json=body)
    assert response.status_code == 200
    session_id = response.headers.get("mcp-session-id")
    assert session_id
    return session_id


@pytest.mark.asyncio
async def test_client_disconnect_cancels_pending_approval_and_blocks_late_allow() -> None:
    """Stateful POST path: dropping the client must cancel before upstream runs."""
    coordinator = PendingRequestCoordinator()
    counter = ExecutionCounter()
    seen: list[ApprovalNotice] = []
    pending = asyncio.Event()
    release_ui = asyncio.Event()

    async def handler(notice: ApprovalNotice) -> DecisionNotice:
        seen.append(notice)
        pending.set()
        await release_ui.wait()
        return DecisionNotice(notice.request_id, "approve", "late_allow")

    ipc = ApprovalIpcServer(token=IPC_TOKEN, handler=handler, port=0)
    endpoint = await ipc.start()
    approval = IpcApprovalProvider(
        coordinator=coordinator,
        endpoint=endpoint,
        server_name="fake-upstream",
        default_timeout=30.0,
        connect_timeout=2.0,
    )
    try:
        async with _gateway(approval, coordinator, counter, timeout=30.0) as port:
            async with httpx2.AsyncClient(trust_env=False, timeout=30.0) as http:
                session_id = await _initialize_session(http, port)
                call_body = {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "echo",
                        "arguments": {"message": "should-not-run"},
                    },
                }
                request = http.build_request(
                    "POST",
                    _base(port),
                    headers=_auth_headers(**{"Mcp-Session-Id": session_id}),
                    json=call_body,
                )
                stream = await http.send(request, stream=True)
                try:
                    await asyncio.wait_for(pending.wait(), timeout=5.0)
                    assert len(seen) == 1
                    request_id = seen[0].request_id
                    assert await coordinator.pending_ids() == [request_id]

                    # Drop the HTTP client while approval is still waiting.
                    await stream.aclose()

                    async def _cancelled() -> None:
                        while True:
                            req = await coordinator.get_request(request_id)
                            if req.state is not RequestState.PENDING:
                                return
                            await asyncio.sleep(0.05)

                    await asyncio.wait_for(_cancelled(), timeout=5.0)
                    cancelled = await coordinator.get_request(request_id)
                    assert cancelled.state is RequestState.CANCELLED
                    assert cancelled.decision is not None
                    assert cancelled.decision.reason == "client_disconnect"
                    assert await coordinator.pending_ids() == []
                    assert counter.calls == 0

                    # A late Allow must not execute upstream.
                    release_ui.set()
                    await asyncio.sleep(0.3)
                    assert counter.calls == 0
                    late = await coordinator.get_request(request_id)
                    assert late.state is RequestState.CANCELLED
                    assert late.state is not RequestState.PENDING
                finally:
                    if not stream.is_closed:
                        await stream.aclose()
    finally:
        release_ui.set()
        await ipc.close()


async def _raw_http_post(
    port: int,
    *,
    host: str,
    body: dict,
    token: str = GATEWAY_TOKEN,
) -> tuple[int, str]:
    """POST with an explicit Host header (httpx rewrites Host from the URL)."""
    payload = json.dumps(body).encode("utf-8")
    request = (
        f"POST {MCP_PATH} HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        f"Authorization: Bearer {token}\r\n"
        f"Content-Type: application/json\r\n"
        f"Accept: application/json, text/event-stream\r\n"
        f"Content-Length: {len(payload)}\r\n"
        f"Connection: close\r\n"
        f"\r\n"
    ).encode("ascii") + payload
    reader, writer = await asyncio.open_connection(LOOPBACK_HOST, port)
    try:
        writer.write(request)
        await writer.drain()
        raw = await asyncio.wait_for(reader.read(), timeout=5.0)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
    text = raw.decode("latin-1", errors="replace")
    status_line = text.split("\r\n", 1)[0]
    status = int(status_line.split(" ", 2)[1])
    return status, text


@pytest.mark.asyncio
async def test_approval_task_cancellation_does_not_leave_pending_slot() -> None:
    """Modern Streamable HTTP cancels the handler; the slot must not stay PENDING."""
    coordinator = PendingRequestCoordinator()
    seen: list[ApprovalNotice] = []
    pending = asyncio.Event()
    release_ui = asyncio.Event()

    async def handler(notice: ApprovalNotice) -> DecisionNotice:
        seen.append(notice)
        pending.set()
        await release_ui.wait()
        return DecisionNotice(notice.request_id, "approve", "late_allow")

    ipc = ApprovalIpcServer(token=IPC_TOKEN, handler=handler, port=0)
    endpoint = await ipc.start()
    approval = IpcApprovalProvider(
        coordinator=coordinator,
        endpoint=endpoint,
        server_name="fake-upstream",
        default_timeout=30.0,
        connect_timeout=2.0,
    )
    try:
        task = asyncio.create_task(
            approval.await_decision(
                server_name="fake-upstream",
                tool_name="echo",
                arguments={"message": "cancel-me"},
                timeout=30.0,
            )
        )
        await asyncio.wait_for(pending.wait(), timeout=5.0)
        request_id = seen[0].request_id
        assert await coordinator.pending_ids() == [request_id]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        request = await coordinator.get_request(request_id)
        assert request.state is RequestState.CANCELLED
        assert request.decision is not None
        assert request.decision.reason == "client_disconnect"
        assert await coordinator.pending_ids() == []
        release_ui.set()
        await asyncio.sleep(0.2)
        late = await coordinator.get_request(request_id)
        assert late.state is RequestState.CANCELLED
    finally:
        release_ui.set()
        await approval.aclose()
        await ipc.close()


@pytest.mark.asyncio
async def test_bad_host_header_is_rejected() -> None:
    coordinator = PendingRequestCoordinator()
    counter = ExecutionCounter()
    approval = make_deny_all_provider(coordinator, timeout=2.0)
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "echo", "arguments": {"message": "should-not-run"}},
    }
    async with _gateway(approval, coordinator, counter) as port:
        status, text = await _raw_http_post(port, host="evil.example.com", body=body)

    assert status == 421
    assert counter.calls == 0
    assert "Invalid Host header" in text


@pytest.mark.asyncio
async def test_configured_allowed_host_is_accepted() -> None:
    coordinator = PendingRequestCoordinator()
    counter = ExecutionCounter()
    approval = make_deny_all_provider(coordinator, timeout=2.0)
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "gateway-test", "version": "0"},
        },
    }
    async with _gateway(
        approval,
        coordinator,
        counter,
        allowed_hosts=["tunnel.example.test"],
    ) as port:
        status, text = await _raw_http_post(
            port,
            host="tunnel.example.test",
            body=body,
        )

    assert status == 200
    assert "mcp-session-id" in text.lower()
    assert counter.calls == 0
