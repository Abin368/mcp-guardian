"""Async pending-request coordinator.

Holds every in-flight approval request and its own asyncio.Future so that:
- multiple requests can wait independently
- a decision for request A cannot complete request B
- timeouts and failures fail closed (no execution)

Phase 1 does not call any upstream MCP server.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any

from mcp_guardian.domain.models import (
    ApprovalRequest,
    Decision,
    DecisionAction,
    RequestState,
)
from mcp_guardian.domain.state_machine import (
    InvalidTransitionError,
    apply_transition,
)


class RequestNotFoundError(KeyError):
    """No pending (or known) request exists for the given ID."""


# Phase 4 reuses an ID created in the proxy process. Only the canonical UUID4
# text is accepted so a peer cannot smuggle newlines or a different request's key.
_EXPLICIT_REQUEST_ID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


class DuplicateDecisionError(Exception):
    """A second/late decision arrived for a request that is no longer PENDING."""

    def __init__(self, request_id: str, current_state: RequestState) -> None:
        self.request_id = request_id
        self.current_state = current_state
        super().__init__(
            f"Duplicate or late decision for {request_id!r}; "
            f"current state is {current_state.value}."
        )


@dataclass(slots=True)
class _PendingSlot:
    """Internal bookkeeping for one independent request."""

    request: ApprovalRequest
    future: asyncio.Future[Decision] = field(
        default_factory=lambda: asyncio.get_running_loop().create_future()
    )


@dataclass(frozen=True, slots=True)
class WaitOutcome:
    """Result of waiting for a decision (including automatic timeout)."""

    request: ApprovalRequest
    decision: Decision

    @property
    def allows_execution(self) -> bool:
        """Fail-closed helper for later phases."""
        return self.decision.allows_execution


class PendingRequestCoordinator:
    """Coordinates concurrent human-approval requests.

    Typical flow:
    1. ``create_request(...)`` → unique ApprovalRequest in PENDING
    2. ``wait_for_decision(request_id, timeout=...)`` blocks until decided/timeout
    3. ``approve`` / ``deny`` / ``cancel`` resolve that request only
    """

    def __init__(self) -> None:
        # request_id → slot. Each slot owns its own Future.
        self._slots: dict[str, _PendingSlot] = {}
        # Protects map mutations when many coroutines run together.
        self._lock = asyncio.Lock()

    async def create_request(
        self,
        server_name: str,
        tool_name: str,
        parameters: dict[str, Any] | None = None,
        *,
        request_id: str | None = None,
    ) -> ApprovalRequest:
        """Register a new PENDING approval request.

        ``request_id`` is optional. Callers that omit it get a new UUID4, which
        is what Phase 1 and Phase 2 do. Phase 4 passes the proxy's ID so the
        dialog and the later decision name the same request.
        """
        if request_id is not None and _EXPLICIT_REQUEST_ID.fullmatch(request_id) is None:
            raise ValueError("request_id must be a lowercase UUID4")

        request = ApprovalRequest(
            server_name=server_name,
            tool_name=tool_name,
            parameters=dict(parameters or {}),
            **({} if request_id is None else {"request_id": request_id}),
        )
        async with self._lock:
            if request.request_id in self._slots:
                # Astronomically unlikely with UUID4; still fail closed.
                # An explicit ID that is already pending is the same failure:
                # we will not attach a second decision channel to it.
                raise RuntimeError(f"Request ID collision: {request.request_id}")
            self._slots[request.request_id] = _PendingSlot(request=request)
        return request

    async def get_request(self, request_id: str) -> ApprovalRequest:
        """Return the request for ``request_id`` or raise RequestNotFoundError."""
        slot = await self._get_slot(request_id)
        return slot.request

    async def pending_ids(self) -> list[str]:
        """IDs that are still PENDING (useful for tests and UI later)."""
        async with self._lock:
            return [
                request_id
                for request_id, slot in self._slots.items()
                if slot.request.state is RequestState.PENDING
            ]

    async def approve(
        self,
        request_id: str,
        reason: str | None = None,
    ) -> Decision:
        """Approve one PENDING request. Duplicate calls fail closed."""
        return await self._decide(request_id, DecisionAction.APPROVE, reason)

    async def deny(
        self,
        request_id: str,
        reason: str | None = None,
    ) -> Decision:
        """Deny one PENDING request. No upstream execution will be allowed."""
        return await self._decide(request_id, DecisionAction.DENY, reason)

    async def cancel(
        self,
        request_id: str,
        reason: str | None = None,
    ) -> Decision:
        """Cancel one PENDING request (e.g. client disconnect / shutdown)."""
        return await self._decide(
            request_id,
            DecisionAction.CANCEL,
            reason or "cancelled",
        )

    async def mark_timed_out(
        self,
        request_id: str,
        reason: str | None = None,
    ) -> Decision:
        """Move one PENDING request to TIMED_OUT.

        Used when another process reports that the dialog gave up. This does
        not approve the request. A second call is a duplicate decision.
        """
        return await self._decide(
            request_id,
            DecisionAction.TIMEOUT,
            reason or "approval_timeout",
        )

    async def wait_for_decision(
        self,
        request_id: str,
        timeout: float | None = None,
    ) -> WaitOutcome:
        """Wait until this request is decided, or fail closed on timeout.

        Args:
            request_id: The independent request to wait on.
            timeout: Seconds to wait. ``None`` means wait forever.
                     On expiry the request becomes TIMED_OUT (not approved).

        Returns:
            WaitOutcome with the final decision for *this* request only.
        """
        slot = await self._get_slot(request_id)

        if slot.future.done():
            decision = slot.future.result()
            return WaitOutcome(request=slot.request, decision=decision)

        try:
            if timeout is None:
                decision = await asyncio.shield(slot.future)
            else:
                decision = await asyncio.wait_for(
                    asyncio.shield(slot.future),
                    timeout=timeout,
                )
            return WaitOutcome(request=slot.request, decision=decision)
        except TimeoutError:
            # Fail closed: mark TIMED_OUT if still PENDING.
            decision = await self._timeout_if_pending(request_id)
            return WaitOutcome(request=slot.request, decision=decision)

    async def _timeout_if_pending(self, request_id: str) -> Decision:
        """Transition to TIMED_OUT only if still PENDING; else return existing."""
        async with self._lock:
            slot = self._slots.get(request_id)
            if slot is None:
                raise RequestNotFoundError(request_id)

            if slot.request.state is not RequestState.PENDING:
                # Someone decided a moment before the timeout path ran.
                if slot.future.done():
                    return slot.future.result()
                # Should not happen; fail closed with deny-equivalent cancel.
                decision = Decision(
                    request_id=request_id,
                    action=DecisionAction.TIMEOUT,
                    reason="timeout_race_fail_closed",
                )
                return decision

            decision = Decision(
                request_id=request_id,
                action=DecisionAction.TIMEOUT,
                reason="approval_timeout",
            )
            try:
                apply_transition(slot.request, decision)
            except InvalidTransitionError as exc:
                raise DuplicateDecisionError(
                    request_id, slot.request.state
                ) from exc

            if not slot.future.done():
                slot.future.set_result(decision)
            return decision

    async def _decide(
        self,
        request_id: str,
        action: DecisionAction,
        reason: str | None,
    ) -> Decision:
        """Apply a human/system decision to exactly one request."""
        async with self._lock:
            slot = self._slots.get(request_id)
            if slot is None:
                raise RequestNotFoundError(request_id)

            decision = Decision(
                request_id=request_id,
                action=action,
                reason=reason,
            )

            try:
                apply_transition(slot.request, decision)
            except InvalidTransitionError as exc:
                # Duplicate / late decision — do not alter Future again.
                raise DuplicateDecisionError(
                    request_id, slot.request.state
                ) from exc

            if not slot.future.done():
                slot.future.set_result(decision)
            return decision

    async def _get_slot(self, request_id: str) -> _PendingSlot:
        async with self._lock:
            slot = self._slots.get(request_id)
            if slot is None:
                raise RequestNotFoundError(request_id)
            return slot
