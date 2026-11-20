"""Fixtures for Phase 3 STDIO proxy tests (in-process MCP, no real Claude)."""

from __future__ import annotations

import pytest

from mcp_guardian.application.pending_coordinator import PendingRequestCoordinator
from tests.proxy.fake_upstream import ExecutionCounter


@pytest.fixture
def coordinator() -> PendingRequestCoordinator:
    return PendingRequestCoordinator()


@pytest.fixture
def execution_counter() -> ExecutionCounter:
    return ExecutionCounter()
