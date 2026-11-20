"""User-facing copy for the approval UI.

These strings are presentation only. They do not decide whether a tool may run.
Phase 2 sample dialogs keep the original wording. Dialogs opened from the proxy
use ``IPC_COPY``, which tells the truth: Allow lets that one call run.
"""

from __future__ import annotations

from dataclasses import dataclass

WAITING_MESSAGE = "Waiting for your decision."
APPROVE_MESSAGE = "Allowed. This phase does not execute the tool."
DENY_MESSAGE = "Blocked. The tool was not executed."
TIMEOUT_MESSAGE = "This request timed out and was not approved."
CANCEL_MESSAGE = "This request was cancelled and was not approved."
ERROR_MESSAGE = "Something went wrong. The request was not approved."
UNAVAILABLE_MESSAGE = "This request is no longer available and was not approved."
CLOSE_HINT = "Closing this window cancels the request. Allow does not execute the tool."

IPC_APPROVE_MESSAGE = "Allowed. The proxy may run this tool."
IPC_CLOSE_HINT = (
    "Closing this window cancels the request. The tool runs only if you press Allow."
)


@dataclass(frozen=True, slots=True)
class ApprovalCopy:
    """The sentences a dialog shows. Swapping this does not approve anything."""

    approve: str
    deny: str
    timeout: str
    cancel: str
    error: str
    unavailable: str
    close_hint: str
    waiting: str


PHASE2_COPY = ApprovalCopy(
    approve=APPROVE_MESSAGE,
    deny=DENY_MESSAGE,
    timeout=TIMEOUT_MESSAGE,
    cancel=CANCEL_MESSAGE,
    error=ERROR_MESSAGE,
    unavailable=UNAVAILABLE_MESSAGE,
    close_hint=CLOSE_HINT,
    waiting=WAITING_MESSAGE,
)

IPC_COPY = ApprovalCopy(
    approve=IPC_APPROVE_MESSAGE,
    deny=DENY_MESSAGE,
    timeout=TIMEOUT_MESSAGE,
    cancel=CANCEL_MESSAGE,
    error=ERROR_MESSAGE,
    unavailable=UNAVAILABLE_MESSAGE,
    close_hint=IPC_CLOSE_HINT,
    waiting=WAITING_MESSAGE,
)
