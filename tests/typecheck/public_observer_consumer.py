from __future__ import annotations

from typing import assert_type

from crewplane.architecture.contracts import (
    DashboardSnapshot,
    EventType,
    ExecutionEvent,
    ExecutionEventContext,
    InvocationEventType,
    NodeEventType,
    ObserverCapabilities,
    RunContext,
    RunResult,
    RuntimeObserver,
    WorkflowEventType,
    WorkspaceEventPayload,
    is_invocation_event_type,
    is_node_event_type,
    is_workflow_event_type,
)
from crewplane.architecture.ports.runtime import UIRuntimePlan
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.observability.events import (
    invocation_event,
    node_event,
    workflow_event,
    workspace_event,
)


class ExternalObserver:
    capabilities = ObserverCapabilities(required=True)

    @property
    def stop_requested(self) -> bool:
        return False

    def start(self, context: RunContext) -> None:  # noqa: ARG002 - Observer protocol.
        pass

    def on_snapshot(
        self,
        event: ExecutionEvent | None,  # noqa: ARG002 - Observer protocol.
        snapshot: DashboardSnapshot,
    ) -> None:
        if event is not None:
            assert_type(event.event_type, EventType)
            assert_event_type_narrowing(event.event_type)
        workflow_name: str = snapshot.state.workflow_name
        workflow_status: str = snapshot.state.workflow_status
        ordered_nodes: list[str] = [
            node_id
            for wave in snapshot.layout.waves
            for node_id in wave
            if node_id in snapshot.state.nodes
        ]
        lane_count: int = snapshot.layout.lane_count
        placements: list[tuple[str, int]] = [
            (placement.node_id, placement.wave_index)
            for placement in snapshot.layout.placements.values()
        ]
        first_log: str | None = None
        for node in snapshot.state.nodes.values():
            for invocation in node.invocations.values():
                first_log = invocation.log_file
                break
            if first_log is not None:
                break
        assert_type(workflow_name, str)
        assert_type(workflow_status, str)
        assert_type(ordered_nodes, list[str])
        assert_type(lane_count, int)
        assert_type(placements, list[tuple[str, int]])
        assert_type(first_log, str | None)

    def stop(self, result: RunResult) -> None:  # noqa: ARG002 - Observer protocol.
        pass


def assert_event_type_narrowing(event_type: EventType) -> None:
    if is_workflow_event_type(event_type):
        assert_type(event_type, WorkflowEventType)
    elif is_node_event_type(event_type):
        assert_type(event_type, NodeEventType)
    elif is_invocation_event_type(event_type):
        assert_type(event_type, InvocationEventType)


class IncompatibleObserver:
    capabilities = ObserverCapabilities(required=True)

    @property
    def stop_requested(self) -> bool:
        return False

    def start(self, context: int) -> None:  # noqa: ARG002 - Observer protocol.
        pass

    def on_snapshot(  # noqa: ARG002 - Observer protocol.
        self, event: str | None, snapshot: bytes
    ) -> None:
        pass

    def stop(self, result: float) -> None:  # noqa: ARG002 - Observer protocol.
        pass


def install_observer(
    observer: RuntimeObserver,  # noqa: ARG001 - Static compatibility fixture.
) -> None:
    pass


install_observer(ExternalObserver())
install_observer(IncompatibleObserver())  # type: ignore[arg-type]
UIRuntimePlan(
    observers=(IncompatibleObserver(),),  # type: ignore[arg-type]
    suppress_progress_output=False,
)

event_context = ExecutionEventContext(
    workflow_name="workflow",
    run_id="run-1",
    node_id="node.a",
    provider="codex",
    role=ProviderRole.EXECUTOR,
    task_id="codex_executor_0",
)
workflow_event(EventType.NODE_STARTED, "workflow", "run-1")  # type: ignore[arg-type]
node_event(EventType.INVOCATION_STARTED, "workflow", "run-1", "node.a")  # type: ignore[arg-type]
invocation_event(EventType.WORKFLOW_STARTED, "workflow", "run-1", event_context)  # type: ignore[arg-type]
workspace_event(
    EventType.RUNTIME_LOG,  # type: ignore[arg-type]
    "workflow",
    "run-1",
    event_context,
    WorkspaceEventPayload(),
)
