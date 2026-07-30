from __future__ import annotations

from crewplane.architecture.contracts import (
    DashboardInvocationState as DashboardInvocationState,
)
from crewplane.architecture.contracts import (
    DashboardLayout as DashboardLayout,
)
from crewplane.architecture.contracts import (
    DashboardNodePlacement as DashboardNodePlacement,
)
from crewplane.architecture.contracts import (
    DashboardNodeState as DashboardNodeState,
)
from crewplane.architecture.contracts import (
    DashboardSnapshot as _DashboardSnapshot,
)
from crewplane.architecture.contracts import (
    DashboardState as DashboardState,
)
from crewplane.architecture.contracts import (
    RunContext as RunContext,
)
from crewplane.architecture.contracts import (
    RunResult as RunResult,
)
from crewplane.architecture.contracts import (
    TopologyNode as TopologyNode,
)
from crewplane.architecture.contracts import (
    TopologyProvider as TopologyProvider,
)
from crewplane.architecture.contracts import (
    WorkflowTopology as WorkflowTopology,
)
from crewplane.observability.events import RunDashboardState
from crewplane.observability.layout import TopologyLayout

DashboardSnapshot = _DashboardSnapshot[RunDashboardState, TopologyLayout]
