"""Newline-delimited JSON for the Guardian approval socket.

One JSON object per line. The line is the framing boundary, so ``json.dumps``
is used without indentation (newlines inside values are escaped by the encoder).

Nothing in this module treats a missing, unknown, or malformed message as
approval. Callers map only the exact action ``approve`` to tool execution,
and only after the Phase 1 coordinator accepts it.
"""

from __future__ import annotations

import hmac
import json
import re
from dataclasses import dataclass
from typing import Any

PROTOCOL_VERSION = 1
LOOPBACK_HOST = "127.0.0.1"

# Large enough for a redacted argument object, small enough that a hostile
# peer cannot pin the UI on a multi-megabyte frame.
MAX_MESSAGE_BYTES = 64 * 1024
MAX_DEPTH = 8
MAX_COLLECTION = 64
MAX_NAME_CHARS = 256
MAX_TOKEN_CHARS = 256
MAX_REASON_CHARS = 64

# Canonical text produced by uuid.uuid4().
REQUEST_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
TOKEN_RE = re.compile(r"^[A-Za-z0-9_\-]{16,256}$")
REASON_RE = re.compile(r"^[a-z0-9_:\-]{1,64}$")

ACTION_APPROVE = "approve"
ACTION_DENY = "deny"
ACTION_CANCEL = "cancel"
ACTION_TIMEOUT = "timeout"
DECISION_ACTIONS = frozenset(
    {ACTION_APPROVE, ACTION_DENY, ACTION_CANCEL, ACTION_TIMEOUT}
)

TYPE_HELLO = "hello"
TYPE_HELLO_OK = "hello_ok"
TYPE_REQUEST = "request"
TYPE_DECISION = "decision"
TYPE_ERROR = "error"


class IpcError(Exception):
    """Base class for IPC failures. Never an approval."""


class IpcUnavailable(IpcError):
    """The UI socket could not be reached or refused the hello."""


class IpcDisconnected(IpcError):
    """The socket closed before a decision for this request."""


class IpcProtocolError(IpcError):
    """The peer sent a frame this process will not act on.

    ``code`` is a short stable token. It is safe to log. The raw frame is not
    stored on the exception, because a frame may contain tool arguments.
    """

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class HelloMessage:
    token: str
    server_name: str


@dataclass(frozen=True, slots=True)
class HelloOkMessage:
    pass


@dataclass(frozen=True, slots=True)
class ApprovalNotice:
    """What the UI needs in order to show one dialog. Arguments are untrusted."""

    request_id: str
    server_name: str
    tool_name: str
    arguments: dict[str, Any]
    timeout_seconds: float


@dataclass(frozen=True, slots=True)
class DecisionNotice:
    """A decision for exactly one request ID. ``approve`` is the only allow."""

    request_id: str
    action: str
    reason: str


@dataclass(frozen=True, slots=True)
class ErrorNotice:
    code: str
    request_id: str | None = None


Message = HelloMessage | HelloOkMessage | ApprovalNotice | DecisionNotice | ErrorNotice


def tokens_match(expected: str, presented: object) -> bool:
    """Constant-time compare. A different length is just a rejection."""
    if not isinstance(presented, str) or not presented or len(presented) != len(expected):
        return False
    return hmac.compare_digest(expected, presented)


def ensure_bounded_shape(value: Any) -> None:
    """Raise ``IpcProtocolError`` when a JSON value is too deep or too wide."""
    _reject_deep_or_wide(value, depth=0)


def validate_token(token: str) -> str:
    """Reject tokens that are empty, huge, or not safe to keep out of logs."""
    if not isinstance(token, str) or TOKEN_RE.fullmatch(token) is None:
        raise ValueError("IPC token must be 16–256 characters of [A-Za-z0-9_-]")
    return token


def validate_request_id(request_id: object) -> str:
    if not isinstance(request_id, str) or REQUEST_ID_RE.fullmatch(request_id) is None:
        raise IpcProtocolError("invalid_request_id")
    return request_id


def validate_label(value: object, code: str) -> str:
    """Server and tool names are shown in the window title, so control chars go."""
    if not isinstance(value, str) or not value or len(value) > MAX_NAME_CHARS:
        raise IpcProtocolError(code)
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise IpcProtocolError(code)
    return value


def validate_reason(value: object) -> str:
    if value is None:
        return "unspecified"
    if not isinstance(value, str) or REASON_RE.fullmatch(value) is None:
        raise IpcProtocolError("invalid_reason")
    if len(value) > MAX_REASON_CHARS:
        raise IpcProtocolError("invalid_reason")
    return value


def action_to_apply(expected_request_id: str, decision: DecisionNotice) -> str:
    """Return the action the proxy may apply to ``expected_request_id``.

    A decision that names a different ID, or an unknown action, becomes
    ``cancel``. It must never be rewritten into ``approve`` for the waiter.
    """
    if decision.request_id != expected_request_id:
        return ACTION_CANCEL
    if decision.action not in DECISION_ACTIONS:
        return ACTION_CANCEL
    return decision.action


def encode_message(message: Message) -> bytes:
    payload = _encode_payload(message)
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    frame = encoded + b"\n"
    if len(frame) > MAX_MESSAGE_BYTES:
        raise IpcProtocolError("message_too_large")
    return frame


def decode_message(frame: bytes) -> Message:
    if not isinstance(frame, (bytes, bytearray)):
        raise IpcProtocolError("invalid_frame")
    if frame.endswith(b"\n"):
        frame = frame[:-1]
    if frame.endswith(b"\r"):
        frame = frame[:-1]
    if not frame or len(frame) > MAX_MESSAGE_BYTES:
        raise IpcProtocolError("message_too_large" if frame else "empty_message")
    try:
        text = frame.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise IpcProtocolError("invalid_encoding") from exc
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise IpcProtocolError("malformed_json") from exc
    if not isinstance(value, dict):
        raise IpcProtocolError("message_not_object")
    _reject_deep_or_wide(value, depth=0)
    return _parse_object(value)


def _encode_payload(message: Message) -> dict[str, Any]:
    if isinstance(message, HelloMessage):
        return {
            "v": PROTOCOL_VERSION,
            "type": TYPE_HELLO,
            "token": message.token,
            "server_name": message.server_name,
        }
    if isinstance(message, HelloOkMessage):
        return {"v": PROTOCOL_VERSION, "type": TYPE_HELLO_OK}
    if isinstance(message, ApprovalNotice):
        return {
            "v": PROTOCOL_VERSION,
            "type": TYPE_REQUEST,
            "request_id": message.request_id,
            "server_name": message.server_name,
            "tool_name": message.tool_name,
            "arguments": message.arguments,
            "timeout_seconds": message.timeout_seconds,
        }
    if isinstance(message, DecisionNotice):
        return {
            "v": PROTOCOL_VERSION,
            "type": TYPE_DECISION,
            "request_id": message.request_id,
            "action": message.action,
            "reason": message.reason,
        }
    if isinstance(message, ErrorNotice):
        payload: dict[str, Any] = {
            "v": PROTOCOL_VERSION,
            "type": TYPE_ERROR,
            "code": message.code,
        }
        if message.request_id is not None:
            payload["request_id"] = message.request_id
        return payload
    raise IpcProtocolError("unknown_message")


def _parse_object(value: dict[str, Any]) -> Message:
    version = value.get("v")
    if version != PROTOCOL_VERSION:
        raise IpcProtocolError("unsupported_version")
    kind = value.get("type")
    if kind == TYPE_HELLO:
        token = value.get("token")
        if not isinstance(token, str) or len(token) > MAX_TOKEN_CHARS:
            raise IpcProtocolError("invalid_token")
        return HelloMessage(
            token=token,
            server_name=validate_label(value.get("server_name"), "invalid_server_name"),
        )
    if kind == TYPE_HELLO_OK:
        return HelloOkMessage()
    if kind == TYPE_REQUEST:
        timeout = value.get("timeout_seconds")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise IpcProtocolError("invalid_timeout")
        timeout_value = float(timeout)
        if not (0.05 <= timeout_value <= 600):
            raise IpcProtocolError("invalid_timeout")
        arguments = value.get("arguments")
        if not isinstance(arguments, dict):
            raise IpcProtocolError("invalid_arguments")
        return ApprovalNotice(
            request_id=validate_request_id(value.get("request_id")),
            server_name=validate_label(value.get("server_name"), "invalid_server_name"),
            tool_name=validate_label(value.get("tool_name"), "invalid_tool_name"),
            arguments=arguments,
            timeout_seconds=timeout_value,
        )
    if kind == TYPE_DECISION:
        action = value.get("action")
        if action not in DECISION_ACTIONS:
            raise IpcProtocolError("invalid_action")
        return DecisionNotice(
            request_id=validate_request_id(value.get("request_id")),
            action=action,
            reason=validate_reason(value.get("reason")),
        )
    if kind == TYPE_ERROR:
        code = value.get("code")
        if not isinstance(code, str) or REASON_RE.fullmatch(code) is None:
            raise IpcProtocolError("invalid_error")
        request_id = value.get("request_id")
        parsed_id = None if request_id is None else validate_request_id(request_id)
        return ErrorNotice(code=code, request_id=parsed_id)
    raise IpcProtocolError("unknown_type")


def _reject_deep_or_wide(value: Any, *, depth: int) -> None:
    if depth > MAX_DEPTH:
        raise IpcProtocolError("payload_too_deep")
    if isinstance(value, dict):
        if len(value) > MAX_COLLECTION:
            raise IpcProtocolError("payload_too_wide")
        for item in value.values():
            _reject_deep_or_wide(item, depth=depth + 1)
        return
    if isinstance(value, list):
        if len(value) > MAX_COLLECTION:
            raise IpcProtocolError("payload_too_wide")
        for item in value:
            _reject_deep_or_wide(item, depth=depth + 1)
