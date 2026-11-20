"""Display redaction. These tests do not build widgets or log argument values."""

from __future__ import annotations

import copy

from mcp_guardian.presentation.redaction import (
    REDACTED,
    format_arguments_for_display,
    is_sensitive_key,
    redact_arguments,
)


def test_redacts_passwords_tokens_and_api_keys_without_mutating_input() -> None:
    original = {
        "path": "notes.txt",
        "api_key": "super-secret-value",
        "api-key": "other-secret",
        "Authorization": "Bearer hunter2",
        "nested": {"password": "hunter2", "mode": "r"},
        "items": [{"access_token": "tok-1", "name": "visible"}],
    }
    snapshot = copy.deepcopy(original)

    redacted = redact_arguments(original)
    text = format_arguments_for_display(original)

    assert original == snapshot
    assert redacted["path"] == "notes.txt"
    assert redacted["api_key"] == REDACTED
    assert redacted["api-key"] == REDACTED
    assert redacted["Authorization"] == REDACTED
    assert redacted["nested"]["password"] == REDACTED
    assert redacted["nested"]["mode"] == "r"
    assert redacted["items"][0]["access_token"] == REDACTED
    assert redacted["items"][0]["name"] == "visible"

    assert "super-secret-value" not in text
    assert "other-secret" not in text
    assert "hunter2" not in text
    assert "tok-1" not in text
    assert "notes.txt" in text
    assert REDACTED in text


def test_ordinary_keys_stay_visible() -> None:
    redacted = redact_arguments(
        {"author": "ada", "path": "/tmp/a.txt", "count": 2, "note": "token-word-in-value"}
    )

    assert redacted["author"] == "ada"
    assert redacted["path"] == "/tmp/a.txt"
    assert redacted["count"] == 2
    # Value text is kept. Redaction is by argument name so the reviewer can
    # still read the tool call. The word inside a normal field is not a key.
    assert redacted["note"] == "token-word-in-value"
    assert is_sensitive_key("author") is False
    assert is_sensitive_key("api_key") is True
    assert is_sensitive_key("client_secret") is True


def test_bytes_are_not_decoded() -> None:
    redacted = redact_arguments({"blob": b"super-secret-value", "token": b"super-secret-value"})

    assert redacted["blob"] == "<bytes>"
    assert redacted["token"] == REDACTED
    assert b"super-secret-value" not in str(redacted).encode()
