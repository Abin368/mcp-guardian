"""Redact obvious secrets before tool arguments are shown.

Redaction is for display only. It does not change the request stored by the
coordinator, and it never writes argument values to the log.
"""

from __future__ import annotations

import copy
import json
from typing import Any

REDACTED = "***REDACTED***"

# Match whole key segments (api_key -> api, key is handled via substrings below).
_SENSITIVE_PARTS = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "secret",
        "token",
        "apikey",
        "credential",
        "credentials",
        "authorization",
        "cookie",
        "cookies",
    }
)

_PART_MARKERS = (
    "password",
    "passwd",
    "secret",
    "token",
    "apikey",
    "credential",
)

_SENSITIVE_SUBSTRINGS = (
    "api_key",
    "private_key",
    "access_key",
    "auth_header",
    "client_secret",
)

_MAX_DISPLAY_CHARS = 8000


def is_sensitive_key(key: object) -> bool:
    """Return True when a displayed argument name looks like a secret.

    Unknown keys that cannot be read safely are treated as sensitive so the
    value is hidden rather than shown.
    """
    try:
        text = str(key)
    except Exception:
        return True

    normalized = text.strip().lower().replace("-", "_").replace(" ", "_")
    parts = [part for part in normalized.split("_") if part]
    for part in parts:
        if part in _SENSITIVE_PARTS:
            return True
        if any(part.startswith(marker) or part.endswith(marker) for marker in _PART_MARKERS):
            return True

    compact = normalized.replace("_", "")
    if "apikey" in compact or "privatekey" in compact or "accesskey" in compact:
        return True
    return any(marker in normalized for marker in _SENSITIVE_SUBSTRINGS)


def redact_arguments(value: Any) -> Any:
    """Return a copy of ``value`` with secret-like fields replaced.

    The input is not modified. Bytes are not decoded, because decoding could
    surface a secret that was not meant to be shown as text.
    """
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            try:
                key_text = str(key)
            except Exception:
                key_text = "<key>"
            if is_sensitive_key(key):
                redacted[key_text] = REDACTED
            else:
                redacted[key_text] = redact_arguments(item)
        return redacted

    if isinstance(value, (list, tuple, set, frozenset)):
        return [redact_arguments(item) for item in value]

    if isinstance(value, (bytes, bytearray)):
        return "<bytes>"

    return value


def _json_default(value: Any) -> str:
    """Replace values JSON cannot encode without calling ``repr`` (may leak)."""
    return f"<{type(value).__name__}>"


def format_arguments_for_display(parameters: dict[str, Any]) -> str:
    """Pretty-print arguments with secrets removed.

    Raises only if the copy itself cannot be built. Callers must catch that
    and show a fixed placeholder — never ``str(parameters)``.
    """
    redacted = redact_arguments(copy.deepcopy(parameters))
    text = json.dumps(
        redacted,
        indent=2,
        sort_keys=True,
        ensure_ascii=False,
        default=_json_default,
    )
    if len(text) > _MAX_DISPLAY_CHARS:
        return text[:_MAX_DISPLAY_CHARS] + "\n… (truncated for display)"
    return text
