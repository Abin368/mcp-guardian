"""Phase 3: MCP STDIO proxy behavior against a fake upstream."""

from __future__ import annotations

import asyncio

import pytest
from mcp import Client

from mcp_guardian.application.pending_coordinator import PendingRequestCoordinator
from mcp_guardian.domain.models import ApprovalRequest, DecisionAction
from mcp_guardian.proxy.approval_provider import (
    make_allow_all_provider,
    make_deny_all_provider,
    make_scripted_provider,
)
from mcp_guardian.proxy.proxy import GuardianProxy
from tests.proxy.fake_upstream import ExecutionCounter, make_fake_upstream


async def _open_proxied_client(
    *,
    counter: ExecutionCounter,
    coordinator: PendingRequestCoordinator,
    approval_factory,
    server_name: str = "fake-upstream",
    approval_timeout: float | None = 5.0,
):
    """Context-manager style helper: yields (downstream Client, proxy, counter)."""

    class _Stack:
        async def __aenter__(self):
            self.upstream_server = make_fake_upstream(counter)
            self.upstream_cm = Client(self.upstream_server)
            self.upstream = await self.upstream_cm.__aenter__()
            approval = approval_factory(coordinator)
            self.proxy = GuardianProxy(
                upstream=self.upstream,
                approval=approval,
                server_name=server_name,
                approval_timeout=approval_timeout,
                coordinator=coordinator,
            )
            proxy_server = self.proxy.build_server()
            self.downstream_cm = Client(proxy_server)
            self.downstream = await self.downstream_cm.__aenter__()
            return self.downstream, self.proxy, counter

        async def __aexit__(self, *exc):
            await self.downstream_cm.__aexit__(*exc)
            await self.proxy.shutdown()
            await self.upstream_cm.__aexit__(*exc)

    return _Stack()


@pytest.mark.asyncio
async def test_initialize_successfully(
    coordinator: PendingRequestCoordinator,
    execution_counter: ExecutionCounter,
) -> None:
    async with await _open_proxied_client(
        counter=execution_counter,
        coordinator=coordinator,
        approval_factory=make_deny_all_provider,
    ) as (client, proxy, _counter):
        # Client __aenter__ completes handshake (initialize / discover).
        assert client.protocol_version
        assert client.server_capabilities.tools is not None
        assert proxy.server_name == "fake-upstream"


@pytest.mark.asyncio
async def test_discover_and_list_upstream_tools(
    coordinator: PendingRequestCoordinator,
    execution_counter: ExecutionCounter,
) -> None:
    async with await _open_proxied_client(
        counter=execution_counter,
        coordinator=coordinator,
        approval_factory=make_deny_all_provider,
    ) as (client, _proxy, counter):
        result = await client.list_tools()
        names = sorted(t.name for t in result.tools)
        assert names == ["echo", "fail"]
        # Listing must not execute tools.
        assert counter.calls == 0


@pytest.mark.asyncio
async def test_approved_tool_call_reaches_upstream_exactly_once(
    coordinator: PendingRequestCoordinator,
    execution_counter: ExecutionCounter,
) -> None:
    async with await _open_proxied_client(
        counter=execution_counter,
        coordinator=coordinator,
        approval_factory=make_allow_all_provider,
    ) as (client, _proxy, counter):
        result = await client.call_tool("echo", {"message": "hello"})
        assert result.is_error is False
        text = result.content[0].text  # type: ignore[union-attr]
        assert "echo:hello" in text
        assert counter.calls == 1
        assert counter.by_tool.get("echo") == 1


@pytest.mark.asyncio
async def test_blocked_tool_call_never_reaches_upstream(
    coordinator: PendingRequestCoordinator,
    execution_counter: ExecutionCounter,
) -> None:
    async with await _open_proxied_client(
        counter=execution_counter,
        coordinator=coordinator,
        approval_factory=make_deny_all_provider,
    ) as (client, _proxy, counter):
        result = await client.call_tool("echo", {"message": "secret-should-not-run"})
        assert result.is_error is True
        text = result.content[0].text  # type: ignore[union-attr]
        assert "denied" in text.lower() or "blocked" in text.lower()
        assert counter.calls == 0


@pytest.mark.asyncio
async def test_timeout_never_reaches_upstream(
    coordinator: PendingRequestCoordinator,
    execution_counter: ExecutionCounter,
) -> None:
    async def hang(_request: ApprovalRequest) -> None:
        # Never approve/deny — wait_for_decision must time out fail-closed.
        await asyncio.sleep(3600)

    def factory(coord: PendingRequestCoordinator):
        return make_scripted_provider(hang, coord, timeout=0.2)

    async with await _open_proxied_client(
        counter=execution_counter,
        coordinator=coordinator,
        approval_factory=factory,
        approval_timeout=0.2,
    ) as (client, _proxy, counter):
        result = await client.call_tool("echo", {"message": "should-timeout"})
        assert result.is_error is True
        text = result.content[0].text  # type: ignore[union-attr]
        assert "timed out" in text.lower() or "timeout" in text.lower()
        assert counter.calls == 0


@pytest.mark.asyncio
async def test_two_concurrent_calls_have_independent_approval_decisions(
    coordinator: PendingRequestCoordinator,
    execution_counter: ExecutionCounter,
) -> None:
    async def script(request: ApprovalRequest) -> None:
        # Independent policy per tool name.
        if request.tool_name == "echo":
            await coordinator.approve(request.request_id, reason="allow_echo")
        else:
            await coordinator.deny(request.request_id, reason="block_fail")

    def factory(coord: PendingRequestCoordinator):
        return make_scripted_provider(script, coord, timeout=5.0)

    async with await _open_proxied_client(
        counter=execution_counter,
        coordinator=coordinator,
        approval_factory=factory,
    ) as (client, _proxy, counter):
        echo_task = asyncio.create_task(
            client.call_tool("echo", {"message": "ok"})
        )
        fail_task = asyncio.create_task(
            client.call_tool("fail", {"reason": "nope"})
        )
        echo_result, fail_result = await asyncio.gather(echo_task, fail_task)

        assert echo_result.is_error is False
        assert "echo:ok" in echo_result.content[0].text  # type: ignore[union-attr]
        assert fail_result.is_error is True
        text = fail_result.content[0].text  # type: ignore[union-attr]
        assert "blocked" in text.lower() or "denied" in text.lower()
        # Only the approved echo reached upstream; fail was blocked before execution.
        assert counter.calls == 1
        assert counter.by_tool == {"echo": 1}


@pytest.mark.asyncio
async def test_upstream_error_is_returned_appropriately(
    coordinator: PendingRequestCoordinator,
    execution_counter: ExecutionCounter,
) -> None:
    async with await _open_proxied_client(
        counter=execution_counter,
        coordinator=coordinator,
        approval_factory=make_allow_all_provider,
    ) as (client, _proxy, counter):
        result = await client.call_tool("fail", {"reason": "disk_full"})
        assert result.is_error is True
        assert result.content[0].text == "disk_full"  # type: ignore[union-attr]
        assert counter.calls == 1


@pytest.mark.asyncio
async def test_proxy_shuts_down_cleanly(
    coordinator: PendingRequestCoordinator,
    execution_counter: ExecutionCounter,
) -> None:
    release = asyncio.Event()

    async def slow_deny(request: ApprovalRequest) -> None:
        await release.wait()
        await coordinator.deny(request.request_id, reason="late")

    def factory(coord: PendingRequestCoordinator):
        return make_scripted_provider(slow_deny, coord, timeout=30.0)

    stack = await _open_proxied_client(
        counter=execution_counter,
        coordinator=coordinator,
        approval_factory=factory,
        approval_timeout=30.0,
    )
    async with stack as (client, proxy, counter):
        call_task = asyncio.create_task(
            client.call_tool("echo", {"message": "pending"})
        )
        # Let the approval request register.
        for _ in range(50):
            if await coordinator.pending_ids():
                break
            await asyncio.sleep(0.02)
        assert await coordinator.pending_ids()

        await proxy.shutdown()
        result = await call_task
        assert result.is_error is True
        assert counter.calls == 0
        # After shutdown, pending approvals were cancelled.
        pending = await coordinator.pending_ids()
        assert pending == []

    # Exiting the stack also closes clients; should not raise.


@pytest.mark.asyncio
async def test_deny_all_never_silently_approves(
    coordinator: PendingRequestCoordinator,
    execution_counter: ExecutionCounter,
) -> None:
    provider = make_deny_all_provider(coordinator, timeout=2.0)
    outcome = await provider.await_decision(
        server_name="s",
        tool_name="t",
        arguments={"api_key": "must-not-be-logged-or-executed"},
        correlation_id="corr-1",
    )
    assert outcome.allows_execution is False
    assert outcome.decision.action is DecisionAction.DENY
    assert execution_counter.calls == 0
