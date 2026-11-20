"""Domain layer: pure business concepts with no I/O."""

from mcp_guardian.domain.models import (
    ApprovalRequest,
    Decision,
    DecisionAction,
    RequestState,
)
from mcp_guardian.domain.state_machine import (
    InvalidTransitionError,
    TransitionResult,
    apply_transition,
    can_transition,
)

__all__ = [
    "ApprovalRequest",
    "Decision",
    "DecisionAction",
    "InvalidTransitionError",
    "RequestState",
    "TransitionResult",
    "apply_transition",
    "can_transition",
]
