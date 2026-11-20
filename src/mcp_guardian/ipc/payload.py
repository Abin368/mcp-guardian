"""Build the argument object that is allowed to leave the proxy process.

The original tool arguments stay in the proxy and are forwarded upstream after
an explicit approve. The copy that crosses IPC, and the copy stored in the UI
process, go through the Phase 2 redactor first.
"""

from __future__ import annotations

import copy
import json
from typing import Any, Mapping

from mcp_guardian.ipc.protocol import IpcProtocolError, ensure_bounded_shape
from mcp_guardian.presentation.redaction import redact_arguments

# Leave room for the rest of the JSON frame (ids, names, newline).
_MAX_ARGUMENT_BYTES = 48_000

_OMITTED = {
    "_notice": "arguments omitted because the redacted payload was too large"
}


class PayloadError(IpcProtocolError):
    """Arguments could not be made safe to send or show. Fail closed."""

    def __init__(self, code: str) -> None:
        super().__init__(code)


def arguments_for_transport(parameters: Mapping[str, Any]) -> dict[str, Any]:
    """Return a JSON-safe redacted copy. Does not modify ``parameters``.

    Oversized objects are replaced with a fixed placeholder. Truncating the
    JSON would risk sending the start of a secret that lived in an ordinary
    field. A redaction failure raises ``PayloadError`` so the caller can
    cancel the request instead of sending the raw value.
    """
    try:
        copied = copy.deepcopy(dict(parameters))
    except Exception as exc:
        raise PayloadError("argument_copy_failed") from exc

    try:
        redacted = redact_arguments(copied)
    except Exception as exc:
        raise PayloadError("redaction_failed") from exc

    if not isinstance(redacted, dict):
        raise PayloadError("arguments_not_object")

    try:
        encoded = json.dumps(
            redacted,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise PayloadError("arguments_not_json") from exc

    if len(encoded.encode("utf-8")) > _MAX_ARGUMENT_BYTES:
        return dict(_OMITTED)

    try:
        parsed = json.loads(encoded)
    except json.JSONDecodeError as exc:
        raise PayloadError("arguments_not_json") from exc
    if not isinstance(parsed, dict):
        raise PayloadError("arguments_not_object")
    try:
        ensure_bounded_shape(parsed)
    except IpcProtocolError as exc:
        raise PayloadError(exc.code) from exc
    return parsed
