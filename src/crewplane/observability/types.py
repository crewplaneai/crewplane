from __future__ import annotations

from crewplane.architecture.contracts import (
    DashboardInvocationState,
    DashboardLayout,
    DashboardNodePlacement,
    DashboardNodeState,
    DashboardState,
    RunContext,
    RunResult,
    TopologyNode,
    TopologyProvider,
    WorkflowTopology,
)
from crewplane.architecture.contracts import (
    DashboardSnapshot as _DashboardSnapshot,
)
from crewplane.observability.events import RunDashboardState
from crewplane.observability.layout import TopologyLayout

DashboardSnapshot = _DashboardSnapshot[RunDashboardState, TopologyLayout]

__all__ = [
    "DashboardInvocationState",
    "DashboardLayout",
    "DashboardNodePlacement",
    "DashboardNodeState",
    "DashboardSnapshot",
    "DashboardState",
    "RunContext",
    "RunDashboardState",
    "RunResult",
    "TopologyLayout",
    "TopologyNode",
    "TopologyProvider",
    "WorkflowTopology",
]
