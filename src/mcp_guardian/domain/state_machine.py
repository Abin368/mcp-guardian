"""Approval request state machine.

Rules (fail-closed):
- Only PENDING may transition.
- Allowed next states from PENDING: APPROVED, DENIED, TIMED_OUT, CANCELLED.
- Any other attempt is rejected and must not cause tool execution.
"""

from __future__ import annotations

from dataclasses import dataclass

from mcp_guardian.domain.models import (
    ApprovalRequest,
    Decision,
    DecisionAction,
    RequestState,
)

# Explicit transition table — easy to explain in an interview.
_ALLOWED_FROM_PENDING: frozenset[RequestState] = frozenset(
    {
        RequestState.APPROVED,
        RequestState.DENIED,
        RequestState.TIMED_OUT,
        RequestState.CANCELLED,
    }
)

# Maps a decision action to the matching terminal state.
_ACTION_TO_STATE: dict[DecisionAction, RequestState] = {
    DecisionAction.APPROVE: RequestState.APPROVED,
    DecisionAction.DENY: RequestState.DENIED,
    DecisionAction.TIMEOUT: RequestState.TIMED_OUT,
    DecisionAction.CANCEL: RequestState.CANCELLED,
}


class InvalidTransitionError(Exception):
    """Raised when a state change is not allowed.

    Callers must treat this as fail-closed: do not execute the tool.
    """

    def __init__(
        self,
        request_id: str,
        current_state: RequestState,
        attempted_state: RequestState,
        message: str | None = None,
    ) -> None:
        self.request_id = request_id
        self.current_state = current_state
        self.attempted_state = attempted_state
        detail = message or (
            f"Cannot transition request {request_id!r} "
            f"from {current_state.value} to {attempted_state.value}."
        )
        super().__init__(detail)


@dataclass(frozen=True, slots=True)
class TransitionResult:
    """Outcome of attempting a transition."""

    request: ApprovalRequest
    decision: Decision
    previous_state: RequestState


def can_transition(current: RequestState, target: RequestState) -> bool:
    """Return True only if current → target is a valid transition."""
    if current is not RequestState.PENDING:
        return False
    return target in _ALLOWED_FROM_PENDING


def state_for_action(action: DecisionAction) -> RequestState:
    """Map a DecisionAction to its terminal RequestState."""
    return _ACTION_TO_STATE[action]


def apply_transition(request: ApprovalRequest, decision: Decision) -> TransitionResult:
    """Apply a decision to a request, enforcing the state machine.

    Raises:
        InvalidTransitionError: if the request is not PENDING, the decision
            targets a different request ID, or the action is unknown.

    Side effects:
        Mutates ``request.state`` and ``request.decision`` only on success.
    """
    if decision.request_id != request.request_id:
        raise InvalidTransitionError(
            request_id=request.request_id,
            current_state=request.state,
            attempted_state=state_for_action(decision.action),
            message=(
                f"Decision request_id {decision.request_id!r} does not match "
                f"approval request {request.request_id!r}."
            ),
        )

    target = state_for_action(decision.action)

    if not can_transition(request.state, target):
        raise InvalidTransitionError(
            request_id=request.request_id,
            current_state=request.state,
            attempted_state=target,
            message=(
                f"Duplicate or late decision rejected: request "
                f"{request.request_id!r} is already {request.state.value}."
            ),
        )

    previous = request.state
    request.state = target
    request.decision = decision
    return TransitionResult(request=request, decision=decision, previous_state=previous)
