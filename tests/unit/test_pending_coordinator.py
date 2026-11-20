"""Unit tests for the async PendingRequestCoordinator."""

from __future__ import annotations

import asyncio

import pytest

from mcp_guardian.application.pending_coordinator import (
    DuplicateDecisionError,
    PendingRequestCoordinator,
    RequestNotFoundError,
)
from mcp_guardian.domain.models import DecisionAction, RequestState


async def test_create_pending_request(
    coordinator: PendingRequestCoordinator,
) -> None:
    request = await coordinator.create_request(
        server_name="fs",
        tool_name="read_file",
        parameters={"path": "notes.txt"},
    )

    assert request.request_id
    assert request.state is RequestState.PENDING
    assert request.server_name == "fs"
    assert request.tool_name == "read_file"
    assert request.parameters == {"path": "notes.txt"}
    assert request.request_id in await coordinator.pending_ids()


async def test_approve_pending_request(
    coordinator: PendingRequestCoordinator,
) -> None:
    request = await coordinator.create_request("fs", "write_file", {"path": "a"})

    wait_task = asyncio.create_task(
        coordinator.wait_for_decision(request.request_id, timeout=2.0)
    )
    await asyncio.sleep(0)  # let the waiter subscribe to the Future

    decision = await coordinator.approve(request.request_id, reason="user_allow")
    outcome = await wait_task

    assert decision.action is DecisionAction.APPROVE
    assert decision.allows_execution is True
    assert outcome.allows_execution is True
    assert outcome.request.state is RequestState.APPROVED
    assert outcome.decision.request_id == request.request_id


async def test_deny_pending_request(
    coordinator: PendingRequestCoordinator,
) -> None:
    request = await coordinator.create_request("fs", "delete_file", {"path": "a"})

    wait_task = asyncio.create_task(
        coordinator.wait_for_decision(request.request_id, timeout=2.0)
    )
    await asyncio.sleep(0)

    decision = await coordinator.deny(request.request_id, reason="user_block")
    outcome = await wait_task

    assert decision.action is DecisionAction.DENY
    assert decision.allows_execution is False
    assert outcome.allows_execution is False
    assert outcome.request.state is RequestState.DENIED


async def test_reject_duplicate_decisions(
    coordinator: PendingRequestCoordinator,
) -> None:
    request = await coordinator.create_request("fs", "read_file", {})
    await coordinator.approve(request.request_id)

    with pytest.raises(DuplicateDecisionError) as exc_info:
        await coordinator.deny(request.request_id)

    assert exc_info.value.current_state is RequestState.APPROVED
    stored = await coordinator.get_request(request.request_id)
    assert stored.state is RequestState.APPROVED
    assert stored.decision is not None
    assert stored.decision.action is DecisionAction.APPROVE


async def test_timeout_request_fails_closed(
    coordinator: PendingRequestCoordinator,
) -> None:
    request = await coordinator.create_request("fs", "read_file", {})

    outcome = await coordinator.wait_for_decision(
        request.request_id,
        timeout=0.05,
    )

    assert outcome.request.state is RequestState.TIMED_OUT
    assert outcome.decision.action is DecisionAction.TIMEOUT
    assert outcome.allows_execution is False


async def test_cancel_request(
    coordinator: PendingRequestCoordinator,
) -> None:
    request = await coordinator.create_request("fs", "read_file", {})

    wait_task = asyncio.create_task(
        coordinator.wait_for_decision(request.request_id, timeout=2.0)
    )
    await asyncio.sleep(0)

    decision = await coordinator.cancel(request.request_id, reason="shutdown")
    outcome = await wait_task

    assert decision.action is DecisionAction.CANCEL
    assert outcome.request.state is RequestState.CANCELLED
    assert outcome.allows_execution is False


async def test_unknown_request_raises(
    coordinator: PendingRequestCoordinator,
) -> None:
    with pytest.raises(RequestNotFoundError):
        await coordinator.approve("does-not-exist")


async def test_ten_concurrent_requests_independent_decisions(
    coordinator: PendingRequestCoordinator,
) -> None:
    """Create 10 pending requests; decide each differently; prove isolation."""
    count = 10
    requests = []
    for i in range(count):
        req = await coordinator.create_request(
            server_name="demo",
            tool_name=f"tool_{i}",
            parameters={"index": i},
        )
        requests.append(req)

    # Half approve (even), half deny (odd) — each waiter tied to one ID.
    waiters = [
        asyncio.create_task(coordinator.wait_for_decision(r.request_id, timeout=3.0))
        for r in requests
    ]
    await asyncio.sleep(0)

    for i, req in enumerate(requests):
        if i % 2 == 0:
            await coordinator.approve(req.request_id, reason=f"allow-{i}")
        else:
            await coordinator.deny(req.request_id, reason=f"deny-{i}")

    outcomes = await asyncio.gather(*waiters)

    assert len(outcomes) == count
    for i, (req, outcome) in enumerate(zip(requests, outcomes, strict=True)):
        assert outcome.decision.request_id == req.request_id
        assert outcome.request.tool_name == f"tool_{i}"
        assert outcome.request.parameters == {"index": i}
        if i % 2 == 0:
            assert outcome.request.state is RequestState.APPROVED
            assert outcome.allows_execution is True
        else:
            assert outcome.request.state is RequestState.DENIED
            assert outcome.allows_execution is False


async def test_one_request_decision_cannot_affect_another(
    coordinator: PendingRequestCoordinator,
) -> None:
    first = await coordinator.create_request("a", "tool_a", {"x": 1})
    second = await coordinator.create_request("b", "tool_b", {"x": 2})

    wait_second = asyncio.create_task(
        coordinator.wait_for_decision(second.request_id, timeout=2.0)
    )
    await asyncio.sleep(0)

    # Decide only the first request.
    await coordinator.approve(first.request_id)

    first_stored = await coordinator.get_request(first.request_id)
    second_stored = await coordinator.get_request(second.request_id)

    assert first_stored.state is RequestState.APPROVED
    assert second_stored.state is RequestState.PENDING

    # Completing the second must still be independent.
    await coordinator.deny(second.request_id)
    second_outcome = await wait_second

    assert second_outcome.request.state is RequestState.DENIED
    assert second_outcome.decision.request_id == second.request_id
    assert first_stored.state is RequestState.APPROVED


async def test_explicit_request_id_is_reused_and_duplicates_are_rejected(
    coordinator: PendingRequestCoordinator,
) -> None:
    request_id = "12345678-1234-4234-8234-123456789abc"
    request = await coordinator.create_request(
        "fs",
        "read_file",
        {"path": "a.txt"},
        request_id=request_id,
    )
    assert request.request_id == request_id

    with pytest.raises(RuntimeError):
        await coordinator.create_request("fs", "read_file", request_id=request_id)

    with pytest.raises(ValueError):
        await coordinator.create_request("fs", "read_file", request_id="not-a-uuid")


async def test_mark_timed_out_does_not_allow_execution(
    coordinator: PendingRequestCoordinator,
) -> None:
    request = await coordinator.create_request("fs", "read_file", {"path": "a.txt"})
    decision = await coordinator.mark_timed_out(request.request_id, reason="approval_timeout")
    assert decision.action is DecisionAction.TIMEOUT
    assert decision.allows_execution is False
    with pytest.raises(DuplicateDecisionError):
        await coordinator.approve(request.request_id, reason="late")
