from __future__ import annotations

import asyncio
from dataclasses import dataclass

from crewplane.architecture.ports import ArtifactStorePort
from crewplane.core.preflight.models import (
    PreflightExecutionNode,
    PreflightExecutionPlan,
)
from crewplane.core.preflight.secrets import SecretContext
from crewplane.observability.events import (
    EventSink,
    ExecutionEvent,
    format_execution_event_log_line,
)

from ..common import (
    CompiledRuntimeContext,
    ExecutionTelemetry,
    RuntimeActivityTracker,
    WorkflowExecutionState,
    emit_workflow_event,
    execution_console,
    should_print_console,
)
from ..publication_registry import RuntimePublicationRegistry
from .state import initialize_workflow_execution_state


@dataclass(frozen=True, slots=True)
class WorkflowExecutionSession:
    runtime_context: CompiledRuntimeContext
    telemetry: ExecutionTelemetry
    nodes_by_id: dict[str, PreflightExecutionNode]
    workflow_identity: str
    state: WorkflowExecutionState
    node_semaphore: asyncio.Semaphore | None
    max_concurrent_nodes: int | None


def _registered_runtime_event_sink(
    event_sink: EventSink,
    publications: RuntimePublicationRegistry,
    owner_id: str,
) -> EventSink:
    def emit(event: ExecutionEvent) -> None:
        line = format_execution_event_log_line(event).encode("utf-8")
        with publications.event_publication(owner_id, line):
            event_sink(event)

    return emit


def _build_runtime_context(
    plan: PreflightExecutionPlan,
    output: ArtifactStorePort,
    secret_context: SecretContext,
    event_sink: EventSink | None,
    run_id: str | None,
    suppress_progress_output: bool,
) -> tuple[
    CompiledRuntimeContext,
    ExecutionTelemetry,
    asyncio.Semaphore | None,
]:
    runtime_context = CompiledRuntimeContext(
        plan=plan,
        secret_context=secret_context,
    )
    registered_event_sink = (
        None
        if event_sink is None
        else _registered_runtime_event_sink(
            event_sink,
            runtime_context.runtime_publications,
            owner_id=f"workflow:{run_id or output.run_id}",
        )
    )
    telemetry = ExecutionTelemetry(
        workflow_name=plan.workflow_name,
        run_id=run_id or output.run_id,
        event_sink=registered_event_sink,
        suppress_console_output=suppress_progress_output,
        activity_tracker=RuntimeActivityTracker(),
    )
    runtime_context.validate_execution_contract()

    node_semaphore: asyncio.Semaphore | None = None
    max_concurrent_nodes = runtime_context.max_concurrent_nodes()
    if max_concurrent_nodes is not None:
        node_semaphore = asyncio.Semaphore(max_concurrent_nodes)
    return runtime_context, telemetry, node_semaphore


def _emit_workflow_started(
    telemetry: ExecutionTelemetry | None,
    workflow_name: str,
) -> None:
    if should_print_console(telemetry):
        execution_console(telemetry).print(
            f"[bold blue]Executing Workflow:[/] {workflow_name}"
        )
    emit_workflow_event(telemetry, "workflow_started")


def initialize_workflow_execution(
    plan: PreflightExecutionPlan,
    output: ArtifactStorePort,
    secret_context: SecretContext,
    event_sink: EventSink | None,
    run_id: str | None,
    suppress_progress_output: bool,
    workflow_identity: str | None,
    resumed_node_ids: tuple[str, ...],
) -> WorkflowExecutionSession:
    runtime_context, telemetry, node_semaphore = _build_runtime_context(
        plan=plan,
        output=output,
        secret_context=secret_context,
        event_sink=event_sink,
        run_id=run_id,
        suppress_progress_output=suppress_progress_output,
    )
    _emit_workflow_started(telemetry, plan.workflow_name)

    nodes_by_id = {node.id: node for node in plan.nodes}
    resolved_workflow_identity = workflow_identity or plan.workflow_name
    state = initialize_workflow_execution_state(plan, resumed_node_ids)

    return WorkflowExecutionSession(
        runtime_context=runtime_context,
        telemetry=telemetry,
        nodes_by_id=nodes_by_id,
        workflow_identity=resolved_workflow_identity,
        state=state,
        node_semaphore=node_semaphore,
        max_concurrent_nodes=runtime_context.max_concurrent_nodes(),
    )
