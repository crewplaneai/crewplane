from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from orchestrator_cli.observability.events import RunDashboardState
    from orchestrator_cli.observability.layout import TopologyLayout


@dataclass(frozen=True)
class TopologyProvider:
    """Provider metadata needed for observer display."""

    provider: str
    model: str | None = None
    role: str | None = None


@dataclass(frozen=True)
class TopologyNode:
    """Plan-derived node metadata needed for observer display."""

    id: str
    mode: str
    dependencies: tuple[str, ...] = ()
    providers: tuple[TopologyProvider, ...] = ()


@dataclass(frozen=True)
class WorkflowTopology:
    """Narrow observer view of the compiled workflow DAG."""

    workflow_name: str
    nodes: tuple[TopologyNode, ...]

    @property
    def node_order(self) -> Mapping[str, int]:
        return MappingProxyType(
            {node.id: index for index, node in enumerate(self.nodes)}
        )


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

    failed: bool
