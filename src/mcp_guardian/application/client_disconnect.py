"""HTTP client-disconnect signal shared by the gateway and the proxy.

The gateway sets an ``asyncio.Event`` on the ASGI scope when it wraps
``receive``. The proxy reads that event from ``ServerRequestContext.request``
and cancels a pending approval if the client goes away.
"""

from __future__ import annotations

import asyncio
from typing import Any

DISCONNECT_STATE_KEY = "mcp_guardian_client_disconnect"


def disconnect_event_from_request(request: Any) -> asyncio.Event | None:
    """Return the disconnect Event attached to an HTTP request, if any."""
    if request is None:
        return None
    scope = getattr(request, "scope", None)
    if not isinstance(scope, dict):
        return None
    state = scope.get("state")
    event = None
    if isinstance(state, dict):
        event = state.get(DISCONNECT_STATE_KEY)
    else:
        event = getattr(state, DISCONNECT_STATE_KEY, None)
    if isinstance(event, asyncio.Event):
        return event
    return None
