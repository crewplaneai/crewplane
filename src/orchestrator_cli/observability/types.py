from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from orchestrator_cli.architecture.contracts import (
    TopologyNode as TopologyNode,
)
from orchestrator_cli.architecture.contracts import (
    TopologyProvider as TopologyProvider,
)
from orchestrator_cli.architecture.contracts import (
    WorkflowTopology as WorkflowTopology,
)

if TYPE_CHECKING:
    from orchestrator_cli.observability.events import RunDashboardState
    from orchestrator_cli.observability.layout import TopologyLayout


@dataclass(frozen=True)
class RunContext:
    """Observer startup context for one workflow run."""

    workflow_topology: WorkflowTopology
    run_id: str
    refresh_per_second: int


@dataclass(frozen=True)
class DashboardSnapshot:
    """Point-in-time dashboard state delivered to observers."""

    state: RunDashboardState
    layout: TopologyLayout
    now: float


@dataclass(frozen=True)
class RunResult:
    """Terminal outcome passed to observers during shutdown."""

    status: Literal["succeeded", "failed", "cancelled"]
    cancel_reason: str | None = None
