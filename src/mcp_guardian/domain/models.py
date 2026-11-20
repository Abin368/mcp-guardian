"""Domain models for MCP Guardian approval requests.

These types are pure data. They do not talk to MCP servers, networks, or the UI.
Later adapters (proxy, gateway, companion) will create and consume them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4


class RequestState(str, Enum):
    """Lifecycle states for one approval request.

    Only PENDING may change. Every other state is terminal (final).
    """

    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        """True when no further transitions are allowed."""
        return self is not RequestState.PENDING


class DecisionAction(str, Enum):
    """What the decision-maker chose for a pending request."""

    APPROVE = "approve"
    DENY = "deny"
    TIMEOUT = "timeout"
    CANCEL = "cancel"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_request_id() -> str:
    """Create a unique request ID (UUID4 string)."""
    return str(uuid4())


@dataclass(frozen=True, slots=True)
class Decision:
    """An immutable decision bound to exactly one request ID."""

    request_id: str
    action: DecisionAction
    decided_at: datetime = field(default_factory=_utc_now)
    reason: str | None = None

    @property
    def allows_execution(self) -> bool:
        """Fail-closed: only an explicit APPROVE may lead to tool execution.

        Phase 1 never calls upstream MCP. Later phases must check this flag
        before forwarding a tools/call.
        """
        return self.action is DecisionAction.APPROVE


@dataclass(slots=True)
class ApprovalRequest:
    """One human-approval gate for a single MCP tool invocation.

    Each instance has its own request_id and independent state. Decisions for
    other requests must never mutate this object.
    """

    server_name: str
    tool_name: str
    parameters: dict[str, Any]
    request_id: str = field(default_factory=new_request_id)
    state: RequestState = RequestState.PENDING
    created_at: datetime = field(default_factory=_utc_now)
    decision: Decision | None = None

    def snapshot_parameters(self) -> dict[str, Any]:
        """Return a shallow copy so callers cannot mutate stored params."""
        return dict(self.parameters)
