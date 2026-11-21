"""Host allowlist for the gateway's DNS-rebinding protection.

The SDK enables Host checks only when ``transport_security`` is set, or when
``host`` is a loopback name. Passing a public tunnel hostname as ``host``
turns the check off. This module always builds an explicit allowlist with
protection left on, and keeps loopback entries by default.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from mcp.server.transport_security import TransportSecuritySettings

from mcp_guardian.ipc.protocol import LOOPBACK_HOST

# Same defaults the SDK uses when host is 127.0.0.1.
DEFAULT_ALLOWED_HOSTS: tuple[str, ...] = (
    "127.0.0.1:*",
    "localhost:*",
    "[::1]:*",
)
DEFAULT_ALLOWED_ORIGINS: tuple[str, ...] = (
    "http://127.0.0.1:*",
    "http://localhost:*",
    "http://[::1]:*",
)

# Hostname / pattern for an extra allowlist entry. Port wildcards are added
# separately. Rejects anything that looks like a URL or a catch-all.
_HOST_PATTERN = re.compile(
    r"^(?:(?:\[[0-9a-fA-F:]+\])|(?:[A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?)*))$"
)

def normalize_allowed_host(value: str) -> str:
    """Return a Host-header base name, without a port suffix.

    ``example.com`` and ``example.com:*`` both become ``example.com``. The
    allowlist always stores the ``:*`` form so any port matches.
    """
    if not isinstance(value, str):
        raise ValueError("allowed host must be a string")
    host = value.strip().lower()
    if not host:
        raise ValueError("allowed host must not be empty")
    if host.endswith(":*"):
        host = host[:-2]
    if host in {"*", "0.0.0.0", "::"}:
        raise ValueError("allowed host must be an explicit hostname")
    if "://" in host or "/" in host or " " in host:
        raise ValueError("allowed host must be a hostname, not a URL")
    if _HOST_PATTERN.fullmatch(host) is None:
        raise ValueError("allowed host is not a valid hostname")
    return host


def build_transport_security(
    extra_hosts: Sequence[str] | None = None,
) -> TransportSecuritySettings:
    """DNS-rebinding settings with loopback defaults plus optional extras.

    Protection is always enabled. Extra hosts are for a future tunnel name;
    they do not change the listen bind and do not disable the Host check.
    """
    hosts = list(DEFAULT_ALLOWED_HOSTS)
    origins = list(DEFAULT_ALLOWED_ORIGINS)
    seen = {h.lower() for h in hosts}

    for raw in extra_hosts or ():
        base = normalize_allowed_host(raw)
        pattern = f"{base}:*"
        # The SDK's ``host:*`` pattern only matches when a port is present.
        # Also allow the bare hostname (implicit port 80 / 443).
        for entry in (base, pattern):
            if entry.lower() in seen:
                continue
            hosts.append(entry)
            seen.add(entry.lower())
        # Prefer https for a non-loopback name; keep http for local aliases.
        if base in {LOOPBACK_HOST, "localhost", "[::1]"}:
            for origin in (f"http://{base}", f"http://{base}:*"):
                if origin not in origins:
                    origins.append(origin)
        else:
            for origin in (
                f"https://{base}",
                f"https://{base}:*",
                f"http://{base}",
                f"http://{base}:*",
            ):
                if origin not in origins:
                    origins.append(origin)

    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=origins,
    )
