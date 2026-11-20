"""Where the UI publishes its loopback address, and who may read it.

The file lives in the current user's profile. It is not a network service
record. The proxy refuses any host other than ``127.0.0.1`` even if the file
has been edited.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from mcp_guardian.ipc.protocol import LOOPBACK_HOST, validate_token

DEFAULT_IPC_PORT = 47321
_ENV_OVERRIDE = "MCP_GUARDIAN_IPC_ENDPOINT"
_FILE_VERSION = 1


@dataclass(frozen=True, slots=True)
class IpcEndpoint:
    """A loopback socket plus the token required to complete hello."""

    host: str
    port: int
    token: str

    def __post_init__(self) -> None:
        if self.host != LOOPBACK_HOST:
            raise ValueError(
                "IPC host must be 127.0.0.1 so the approval socket is not "
                "reachable from another machine."
            )
        if not isinstance(self.port, int) or isinstance(self.port, bool):
            raise ValueError("IPC port must be an integer")
        if not 1 <= self.port <= 65535:
            raise ValueError("IPC port is out of range")
        validate_token(self.token)


def default_endpoint_path() -> Path:
    """Profile file the UI writes and the proxy reads.

    ``MCP_GUARDIAN_IPC_ENDPOINT`` overrides the path for tests and for a
    second instance. This is not the Claude Desktop config file.
    """
    override = os.environ.get(_ENV_OVERRIDE)
    if override:
        return Path(override)
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / "mcp-guardian" / "ipc-endpoint.json"
    return Path.home() / ".mcp-guardian" / "ipc-endpoint.json"


def save_endpoint(path: Path, endpoint: IpcEndpoint) -> None:
    """Write the endpoint atomically, then tighten the file's permissions.

    The token is in this file. Callers must not log the payload.
    """
    # Re-validate so a bad endpoint is not persisted.
    IpcEndpoint(host=endpoint.host, port=endpoint.port, token=endpoint.token)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {
            "v": _FILE_VERSION,
            "host": endpoint.host,
            "port": endpoint.port,
            "token": endpoint.token,
        },
        separators=(",", ":"),
    )
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(payload, encoding="utf-8")
    _restrict_to_current_user(temporary)
    temporary.replace(path)
    _restrict_to_current_user(path)


class EndpointNotReady(ValueError):
    """The UI has not published an endpoint file yet. Callers may retry."""


class EndpointRejected(ValueError):
    """The file is present but must not be used (for example a public host)."""


def load_endpoint(path: Path) -> IpcEndpoint:
    """Load an endpoint file. Rejects any host that is not loopback."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise EndpointNotReady("IPC endpoint file is not readable") from exc
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise EndpointRejected("IPC endpoint file is not valid JSON") from exc
    if not isinstance(value, dict) or value.get("v") != _FILE_VERSION:
        raise EndpointRejected("IPC endpoint file has an unsupported version")
    host = value.get("host")
    port = value.get("port")
    token = value.get("token")
    if host != LOOPBACK_HOST:
        raise EndpointRejected("IPC endpoint host must be 127.0.0.1")
    if isinstance(port, bool) or not isinstance(port, int):
        raise EndpointRejected("IPC endpoint port is invalid")
    if not isinstance(token, str):
        raise EndpointRejected("IPC endpoint token is invalid")
    try:
        return IpcEndpoint(host=host, port=port, token=token)
    except ValueError as exc:
        raise EndpointRejected("IPC endpoint is not usable") from exc


def _restrict_to_current_user(path: Path) -> None:
    """Best-effort: only the current user may read this file.

    On Windows, icacls drops inherited ACEs and grants the current user's SID.
    A process running as that same user can still read the token. That limit
    is documented in ``docs/architecture.md``. If hardening fails, the caller
    sees the error and does not pretend the file is private.
    """
    if os.name != "nt":
        os.chmod(path, 0o600)
        return

    sid = _windows_current_user_sid()
    if not re.fullmatch(r"S-1-[0-9\-]+", sid):
        raise OSError("refusing to apply an ACL for an unexpected SID")
    completed = subprocess.run(
        ["icacls", str(path), "/inheritance:r", "/grant:r", f"*{sid}:(F)"],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise OSError(f"could not restrict IPC endpoint permissions: {detail}")


def _windows_current_user_sid() -> str:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_uint,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL

    token_query = 0x0008
    token_user_class = 1

    class _SidAndAttributes(ctypes.Structure):
        _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]

    class _TokenUser(ctypes.Structure):
        _fields_ = [("User", _SidAndAttributes)]

    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(),
        token_query,
        ctypes.byref(token),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        needed = wintypes.DWORD(0)
        advapi32.GetTokenInformation(
            token, token_user_class, None, 0, ctypes.byref(needed)
        )
        buffer = ctypes.create_string_buffer(needed.value)
        if not advapi32.GetTokenInformation(
            token,
            token_user_class,
            buffer,
            needed,
            ctypes.byref(needed),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        user = ctypes.cast(buffer, ctypes.POINTER(_TokenUser)).contents
        sid_ptr = ctypes.c_void_p()
        if not advapi32.ConvertSidToStringSidW(user.User.Sid, ctypes.byref(sid_ptr)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            value = ctypes.cast(sid_ptr, ctypes.c_wchar_p).value
        finally:
            kernel32.LocalFree(sid_ptr)
    finally:
        kernel32.CloseHandle(token)

    if not value:
        raise OSError("Windows did not return the current user SID")
    return value


def remove_endpoint_if_token_matches(path: Path, token: str) -> None:
    """Delete the file only when it still belongs to this UI process.

    A newer UI may have replaced the file. Deleting that would hide a live
    listener from the proxy.
    """
    try:
        current = load_endpoint(path)
    except (OSError, ValueError, EndpointNotReady, EndpointRejected):
        return
    if current.token != token:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        return


# Imported so a frozen build that inspects sys still sees the platform branch.
_ = sys.platform
