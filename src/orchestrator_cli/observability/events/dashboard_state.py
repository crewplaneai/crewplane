from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from time import monotonic

from orchestrator_cli.observability.events.types import (
    InvocationStatus,
    NodeStatus,
    WorkflowStatus,
)
from orchestrator_cli.observability.types import WorkflowTopology


@dataclass
class InvocationRuntimeState:
    """Mutable dashboard state for one provider invocation."""

    task_id: str
    provider: str
    role: str
    model: str | None
    audit_round_num: int | None
    round_num: int | None
    status: InvocationStatus = "pending"
    started_at: float | None = None
    finished_at: float | None = None
    duration_ms: int | None = None
    error: str | None = None
    output_file: str | None = None
    log_file: str | None = None
    log_presentation_format: str | None = None
    log_presentation_profile: str | None = None


@dataclass
class NodeRuntimeState:
    """Mutable dashboard state for one workflow node and its invocations."""

    node_id: str
    mode: str
    configured_providers: tuple[str, ...]
    status: NodeStatus = "pending"
    started_at: float | None = None
    finished_at: float | None = None
    invocations: dict[str, InvocationRuntimeState] = field(default_factory=dict)
    recent_events: deque[str] = field(default_factory=lambda: deque(maxlen=5))

    @property
    def total_invocations(self) -> int:
        return len(self.invocations)

    @property
    def running_invocations(self) -> int:
        return sum(inv.status == "running" for inv in self.invocations.values())

    @property
    def succeeded_invocations(self) -> int:
        return sum(inv.status == "succeeded" for inv in self.invocations.values())

    @property
    def failed_invocations(self) -> int:
        return sum(inv.status == "failed" for inv in self.invocations.values())


@dataclass
class RunDashboardState:
    """Mutable aggregate state used to render live workflow dashboards."""

    workflow_name: str
    run_id: str
    node_order: dict[str, int]
    workflow_status: WorkflowStatus = "pending"
    workflow_started_at: float | None = None
    workflow_finished_at: float | None = None
    nodes: dict[str, NodeRuntimeState] = field(default_factory=dict)

    @property
    def elapsed_seconds(self) -> float:
        if self.workflow_started_at is None:
            return 0.0
        end = (
            self.workflow_finished_at
            if self.workflow_finished_at is not None
            else monotonic()
        )
        return max(0.0, end - self.workflow_started_at)

    @property
    def pending_nodes(self) -> int:
        return sum(node.status == "pending" for node in self.nodes.values())

    @property
    def running_nodes(self) -> int:
        return sum(node.status == "running" for node in self.nodes.values())

    @property
    def succeeded_nodes(self) -> int:
        return sum(node.status == "succeeded" for node in self.nodes.values())

    @property
    def failed_nodes(self) -> int:
        return sum(node.status == "failed" for node in self.nodes.values())

    @property
    def blocked_nodes(self) -> int:
        return sum(node.status == "blocked" for node in self.nodes.values())


def build_initial_state(topology: WorkflowTopology, run_id: str) -> RunDashboardState:
    """Create an empty dashboard state for a workflow run."""

    node_order = dict(topology.node_order)
    nodes = {
        node.id: NodeRuntimeState(
            node_id=node.id,
            mode=node.mode,
            configured_providers=tuple(
                provider.provider for provider in node.providers
            ),
        )
        for node in topology.nodes
    }
    return RunDashboardState(
        workflow_name=topology.workflow_name,
        run_id=run_id,
        node_order=node_order,
        nodes=nodes,
    )
