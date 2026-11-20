"""Unit tests for the approval state machine (no asyncio / I/O)."""

from __future__ import annotations

import pytest

from mcp_guardian.domain.models import (
    ApprovalRequest,
    Decision,
    DecisionAction,
    RequestState,
)
from mcp_guardian.domain.state_machine import (
    InvalidTransitionError,
    apply_transition,
    can_transition,
    state_for_action,
)


def _request() -> ApprovalRequest:
    return ApprovalRequest(
        server_name="demo-server",
        tool_name="read_file",
        parameters={"path": "/tmp/a.txt"},
    )


def test_only_pending_can_transition_to_terminal_states() -> None:
    for target in (
        RequestState.APPROVED,
        RequestState.DENIED,
        RequestState.TIMED_OUT,
        RequestState.CANCELLED,
    ):
        assert can_transition(RequestState.PENDING, target) is True


def test_terminal_states_cannot_transition() -> None:
    terminals = (
        RequestState.APPROVED,
        RequestState.DENIED,
        RequestState.TIMED_OUT,
        RequestState.CANCELLED,
    )
    for current in terminals:
        for target in terminals:
            assert can_transition(current, target) is False
        assert can_transition(current, RequestState.PENDING) is False


def test_apply_transition_approve() -> None:
    request = _request()
    decision = Decision(request_id=request.request_id, action=DecisionAction.APPROVE)

    result = apply_transition(request, decision)

    assert result.previous_state is RequestState.PENDING
    assert request.state is RequestState.APPROVED
    assert request.decision is decision
    assert decision.allows_execution is True


def test_apply_transition_deny_does_not_allow_execution() -> None:
    request = _request()
    decision = Decision(request_id=request.request_id, action=DecisionAction.DENY)

    apply_transition(request, decision)

    assert request.state is RequestState.DENIED
    assert decision.allows_execution is False


def test_duplicate_transition_raises() -> None:
    request = _request()
    first = Decision(request_id=request.request_id, action=DecisionAction.APPROVE)
    apply_transition(request, first)

    second = Decision(request_id=request.request_id, action=DecisionAction.DENY)
    with pytest.raises(InvalidTransitionError) as exc_info:
        apply_transition(request, second)

    assert request.state is RequestState.APPROVED
    assert request.decision is first
    assert exc_info.value.current_state is RequestState.APPROVED


def test_mismatched_request_id_raises() -> None:
    request = _request()
    decision = Decision(request_id="other-id", action=DecisionAction.APPROVE)

    with pytest.raises(InvalidTransitionError):
        apply_transition(request, decision)

    assert request.state is RequestState.PENDING
    assert request.decision is None


@pytest.mark.parametrize(
    ("action", "expected_state"),
    [
        (DecisionAction.APPROVE, RequestState.APPROVED),
        (DecisionAction.DENY, RequestState.DENIED),
        (DecisionAction.TIMEOUT, RequestState.TIMED_OUT),
        (DecisionAction.CANCEL, RequestState.CANCELLED),
    ],
)
def test_state_for_action_mapping(
    action: DecisionAction,
    expected_state: RequestState,
) -> None:
    assert state_for_action(action) is expected_state
