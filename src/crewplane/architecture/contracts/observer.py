from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Generic, Literal, Protocol, TypeVar

from crewplane.core.workflow.keywords import ProviderRole

from .execution_event import (
    ExecutionEvent,
    InvocationStatus,
    NodeStatus,
    WorkflowStatus,
)
from .invocation import LogPresentationFormat

_DashboardStateT = TypeVar(
    "_DashboardStateT",
    bound="DashboardState",
    default="DashboardState",
)
_DashboardLayoutT = TypeVar(
    "_DashboardLayoutT",
    bound="DashboardLayout",
    default="DashboardLayout",
)


@dataclass(frozen=True)
class ObserverCapabilities:
    """Immutable observer delivery and failure-policy contract."""

    required: bool = False
    synchronous_snapshot_delivery: bool = False
    cleanup_after_start_timeout: bool = True


@dataclass(frozen=True)
class TopologyProvider:
    """Provider metadata needed for observer display."""

    provider: str
    model: str | None = None
    role: ProviderRole | None = None

    def __post_init__(self) -> None:
        if self.role is not None:
            object.__setattr__(self, "role", ProviderRole(self.role))


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


class DashboardInvocationState(Protocol):
    """Observer-facing state for one provider invocation."""

    @property
    def task_id(self) -> str: ...

    @property
    def provider(self) -> str: ...

    @property
    def role(self) -> ProviderRole: ...

    @property
    def model(self) -> str | None: ...

    @property
    def audit_round_num(self) -> int | None: ...

    @property
    def round_num(self) -> int | None: ...

    @property
    def status(self) -> InvocationStatus: ...

    @property
    def started_at(self) -> float | None: ...

    @property
    def finished_at(self) -> float | None: ...

    @property
    def duration_ms(self) -> int | None: ...

    @property
    def error(self) -> str | None: ...

    @property
    def output_file(self) -> str | None: ...

    @property
    def log_file(self) -> str | None: ...

    @property
    def log_presentation_format(self) -> LogPresentationFormat | None: ...

    @property
    def log_presentation_profile(self) -> str | None: ...


class DashboardNodeState(Protocol):
    """Observer-facing aggregate state for one workflow node."""

    @property
    def node_id(self) -> str: ...

    @property
    def mode(self) -> str: ...

    @property
    def configured_providers(self) -> tuple[str, ...]: ...

    @property
    def status(self) -> NodeStatus: ...

    @property
    def started_at(self) -> float | None: ...

    @property
    def finished_at(self) -> float | None: ...

    @property
    def invocations(self) -> Mapping[str, DashboardInvocationState]: ...

    @property
    def total_invocations(self) -> int: ...

    @property
    def running_invocations(self) -> int: ...

    @property
    def succeeded_invocations(self) -> int: ...

    @property
    def failed_invocations(self) -> int: ...


class DashboardNodePlacement(Protocol):
    """Observer-facing layout placement for one workflow node."""

    @property
    def node_id(self) -> str: ...

    @property
    def wave_index(self) -> int: ...

    @property
    def lane_start(self) -> int: ...

    @property
    def lane_end(self) -> int: ...


class DashboardState(Protocol):
    """Observer-facing aggregate workflow dashboard state."""

    @property
    def workflow_name(self) -> str: ...

    @property
    def run_id(self) -> str: ...

    @property
    def node_order(self) -> Mapping[str, int]: ...

    @property
    def workflow_status(self) -> WorkflowStatus: ...

    @property
    def workflow_started_at(self) -> float | None: ...

    @property
    def workflow_finished_at(self) -> float | None: ...

    @property
    def nodes(self) -> Mapping[str, DashboardNodeState]: ...

    @property
    def elapsed_seconds(self) -> float: ...

    @property
    def pending_nodes(self) -> int: ...

    @property
    def running_nodes(self) -> int: ...

    @property
    def succeeded_nodes(self) -> int: ...

    @property
    def failed_nodes(self) -> int: ...

    @property
    def blocked_nodes(self) -> int: ...


class DashboardLayout(Protocol):
    """Observer-facing DAG layout fields for dashboard rendering."""

    @property
    def waves(self) -> tuple[tuple[str, ...], ...]: ...

    @property
    def placements(self) -> Mapping[str, DashboardNodePlacement]: ...

    @property
    def lane_count(self) -> int: ...

    @property
    def node_order(self) -> Mapping[str, int]: ...

    @property
    def dependencies(self) -> Mapping[str, tuple[str, ...]]: ...

    @property
    def dependents(self) -> Mapping[str, tuple[str, ...]]: ...


@dataclass(frozen=True)
class DashboardSnapshot(Generic[_DashboardStateT, _DashboardLayoutT]):
    """Point-in-time dashboard state delivered to observers."""

    state: _DashboardStateT
    layout: _DashboardLayoutT
    now: float


@dataclass(frozen=True)
class RunResult:
    """Terminal outcome passed to observers during shutdown."""

    status: Literal["succeeded", "failed", "cancelled"]
    cancel_reason: str | None = None


class Observer(Protocol):
    """Typed lifecycle contract for runtime observers."""

    capabilities: ObserverCapabilities

    @property
    def stop_requested(self) -> bool: ...

    def start(self, context: RunContext) -> None: ...

    def on_snapshot(
        self,
        event: ExecutionEvent | None,
        snapshot: DashboardSnapshot,
    ) -> None: ...

    def stop(self, result: RunResult) -> None: ...


type RuntimeObserver = Observer
