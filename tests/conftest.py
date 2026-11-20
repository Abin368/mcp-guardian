"""Shared pytest fixtures for MCP Guardian Phase 1 tests."""

from __future__ import annotations

import pytest

from mcp_guardian.application.pending_coordinator import PendingRequestCoordinator


@pytest.fixture
def coordinator() -> PendingRequestCoordinator:
    """Fresh coordinator for each test (no shared state)."""
    return PendingRequestCoordinator()
