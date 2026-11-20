"""Endpoint file rules and the proxy CLI. These tests do not open a window."""

from __future__ import annotations

import json
import subprocess

import pytest

from mcp_guardian.ipc.endpoint import (
    EndpointRejected,
    IpcEndpoint,
    load_endpoint,
    save_endpoint,
)
from mcp_guardian.ipc.payload import arguments_for_transport
from mcp_guardian.ipc.protocol import LOOPBACK_HOST
from mcp_guardian.presentation.redaction import REDACTED, format_arguments_for_display
from mcp_guardian.proxy.__main__ import build_arg_parser, config_from_namespace

TOKEN = "endpoint-token-value1"
SECRET = "super-secret-value"


def test_endpoint_rejects_any_host_except_loopback(tmp_path) -> None:
    with pytest.raises(ValueError):
        IpcEndpoint(host="0.0.0.0", port=47321, token=TOKEN)
    with pytest.raises(ValueError):
        IpcEndpoint(host="localhost", port=47321, token=TOKEN)

    path = tmp_path / "ipc-endpoint.json"
    save_endpoint(path, IpcEndpoint(host=LOOPBACK_HOST, port=47321, token=TOKEN))
    loaded = load_endpoint(path)
    assert loaded.host == LOOPBACK_HOST
    assert loaded.port == 47321
    assert loaded.token == TOKEN

    tampered = json.loads(path.read_text(encoding="utf-8"))
    tampered["host"] = "10.1.2.3"
    path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(EndpointRejected):
        load_endpoint(path)


def test_endpoint_file_is_restricted_to_the_current_user(tmp_path) -> None:
    path = tmp_path / "nested" / "ipc-endpoint.json"
    save_endpoint(path, IpcEndpoint(host=LOOPBACK_HOST, port=9, token=TOKEN))
    completed = subprocess.run(
        ["icacls", str(path)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0
    listing = completed.stdout
    assert "Everyone" not in listing
    assert "BUILTIN\\Users" not in listing


def test_transport_payload_redacts_without_mutating_or_leaking() -> None:
    original = {"path": "notes.txt", "api_key": SECRET, "nested": {"password": SECRET}}
    snapshot = json.dumps(original)
    safe = arguments_for_transport(original)
    shown = format_arguments_for_display(safe)

    assert json.dumps(original) == snapshot
    assert SECRET not in json.dumps(safe)
    assert SECRET not in shown
    assert safe["api_key"] == REDACTED
    assert safe["path"] == "notes.txt"
    assert "notes.txt" in shown


def _parse(extra: list[str]):
    parser = build_arg_parser()
    return parser.parse_args(
        ["--server-name", "harmless-echo", "--upstream-command", "python", *extra]
    )


def test_cli_defaults_to_deny_all_and_ipc_rejects_a_public_host() -> None:
    config = config_from_namespace(_parse([]))
    assert config.approval_mode == "deny-all"

    ipc = config_from_namespace(_parse(["--approval", "ipc"]))
    assert ipc.approval_mode == "ipc"
    assert ipc.ipc_port is None
    assert ipc.ipc_token is None

    with pytest.raises(ValueError):
        config_from_namespace(
            _parse(
                [
                    "--approval",
                    "ipc",
                    "--ipc-host",
                    "0.0.0.0",
                    "--ipc-port",
                    "47321",
                    "--ipc-token",
                    TOKEN,
                ]
            )
        )

    with pytest.raises(ValueError):
        config_from_namespace(
            _parse(["--approval", "ipc", "--ipc-port", "47321"])
        )
