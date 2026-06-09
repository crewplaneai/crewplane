from __future__ import annotations

import asyncio

from orchestrator_cli.architecture.contracts import AgentInvoker
from orchestrator_cli.architecture.ports import ArtifactStorePort
from orchestrator_cli.core.preflight.models import (
    DependencyEdge,
    PreflightExecutionNode,
    PreflightExecutionPlan,
)
from orchestrator_cli.core.preflight.secrets import SecretContext
from orchestrator_cli.observability.events import EventSink

from .common import (
    CompiledRuntimeContext,
    ExecutionTelemetry,
    NodeStatus,
    RuntimeActivityTracker,
    RuntimeEventContext,
    WorkflowExecutionState,
    emit_runtime_log,
    emit_workflow_event,
    execution_console,
    safe_error_message,
    should_print_console,
)
from .workflow_node import (
    execute_node,
    mark_node_finished_activity,
    mark_node_running_activity,
)


def _validate_dependency_edge(edge: DependencyEdge, node_ids: set[str]) -> None:
    if edge.source_node not in node_ids:
        raise ValueError(
            "Compiled dependency graph references unknown source node "
            f"'{edge.source_node}'."
        )
    if edge.target_node not in node_ids:
        raise ValueError(
            "Compiled dependency graph references unknown target node "
            f"'{edge.target_node}'."
        )


def _dependencies_by_node(plan: PreflightExecutionPlan) -> dict[str, set[str]]:
    node_ids = {node.id for node in plan.nodes}
    dependencies = {node.id: set() for node in plan.nodes}
    for edge in plan.dependency_graph:
        _validate_dependency_edge(edge, node_ids)
        dependencies[edge.target_node].add(edge.source_node)
    return dependencies


def _build_dependents_map(dependencies: dict[str, set[str]]) -> dict[str, list[str]]:
    dependents: dict[str, list[str]] = {node_id: [] for node_id in dependencies}
    for node_id, node_dependencies in dependencies.items():
        for needed in node_dependencies:
            dependents[needed].append(node_id)
    return dependents


def _initialize_workflow_execution_state(
    plan: PreflightExecutionPlan,
) -> WorkflowExecutionState:
    node_order = {node_id: index for index, node_id in enumerate(plan.execution_order)}
    for node in plan.nodes:
        node_order.setdefault(node.id, len(node_order))
    dependencies_by_node = _dependencies_by_node(plan)
    remaining_dependencies = {
        node_id: len(dependencies)
        for node_id, dependencies in dependencies_by_node.items()
    }
    ready = sorted(
        (
            node_id
            for node_id, dependency_count in remaining_dependencies.items()
            if dependency_count == 0
        ),
        key=node_order.__getitem__,
    )
    return WorkflowExecutionState(
        ready=ready,
        running={},
        statuses={node.id: "pending" for node in plan.nodes},
        node_errors={},
        failed_dependencies={node.id: set() for node in plan.nodes},
        remaining_dependencies=remaining_dependencies,
        dependents=_build_dependents_map(dependencies_by_node),
        dependencies_by_node=dependencies_by_node,
        node_order=node_order,
    )


def _wrap_node_task(
    node: PreflightExecutionNode,
    output: ArtifactStorePort,
    invoker: AgentInvoker,
    runtime_context: CompiledRuntimeContext,
    telemetry: ExecutionTelemetry | None,
    node_semaphore: asyncio.Semaphore | None,
) -> asyncio.Task[None]:
    async def _run() -> None:
        if node_semaphore is None:
            await execute_node(
                node,
                output,
                invoker,
                runtime_context,
                telemetry,
            )
            return
        async with node_semaphore:
            await execute_node(
                node,
                output,
                invoker,
                runtime_context,
                telemetry,
            )

    return asyncio.create_task(_run())


def _schedule_ready_nodes(
    nodes_by_id: dict[str, PreflightExecutionNode],
    output: ArtifactStorePort,
    invoker: AgentInvoker,
    runtime_context: CompiledRuntimeContext,
    state: WorkflowExecutionState,
    telemetry: ExecutionTelemetry | None,
    max_concurrent_nodes: int | None,
    node_semaphore: asyncio.Semaphore | None,
) -> None:
    while state.ready and (
        max_concurrent_nodes is None or len(state.running) < max_concurrent_nodes
    ):
        node_id = state.ready.pop(0)
        if state.statuses[node_id] != "pending":
            continue
        state.statuses[node_id] = "running"
        mark_node_running_activity(telemetry, node_id)
        state.running[node_id] = _wrap_node_task(
            nodes_by_id[node_id],
            output,
            invoker,
            runtime_context,
            telemetry,
            node_semaphore,
        )


def _completed_node_ids(
    running: dict[str, asyncio.Task[None]],
    completed_tasks: set[asyncio.Task[None]],
    node_order: dict[str, int],
) -> list[str]:
    return sorted(
        (node_id for node_id, task in running.items() if task in completed_tasks),
        key=node_order.__getitem__,
    )


async def wait_for_completed_nodes(state: WorkflowExecutionState) -> list[str]:
    done, _ = await asyncio.wait(
        set(state.running.values()),
        return_when=asyncio.FIRST_COMPLETED,
    )
    return _completed_node_ids(state.running, done, state.node_order)


async def _cancel_running_node_tasks(state: WorkflowExecutionState) -> None:
    pending_tasks = [task for task in state.running.values() if not task.done()]
    if not pending_tasks:
        return
    for task in pending_tasks:
        task.cancel()
    await asyncio.gather(*pending_tasks, return_exceptions=True)


def _mark_node_failed(
    node_id: str,
    exc: Exception,
    state: WorkflowExecutionState,
    telemetry: ExecutionTelemetry | None,
) -> None:
    mark_node_finished_activity(telemetry, node_id)
    state.statuses[node_id] = "failed"
    state.node_errors[node_id] = exc
    if should_print_console(telemetry):
        execution_console(telemetry).print(f"[red]✗[/] Node '{node_id}' failed: {exc}")
    emit_workflow_event(
        telemetry,
        "node_failed",
        node_id=node_id,
        error=safe_error_message(exc),
    )
    for dependent_id in state.dependents[node_id]:
        state.failed_dependencies[dependent_id].add(node_id)


def _queue_satisfied_dependents(node_id: str, state: WorkflowExecutionState) -> None:
    for dependent_id in state.dependents[node_id]:
        if state.failed_dependencies[dependent_id]:
            continue
        state.remaining_dependencies[dependent_id] -= 1
        if state.remaining_dependencies[dependent_id] == 0:
            state.ready.append(dependent_id)
    state.ready.sort(key=state.node_order.__getitem__)


def _mark_node_succeeded(
    node_id: str,
    state: WorkflowExecutionState,
    telemetry: ExecutionTelemetry | None,
) -> None:
    mark_node_finished_activity(telemetry, node_id)
    state.statuses[node_id] = "succeeded"
    emit_workflow_event(telemetry, "node_finished", node_id=node_id)
    _queue_satisfied_dependents(node_id, state)


async def _finalize_completed_node(
    node_id: str,
    state: WorkflowExecutionState,
    telemetry: ExecutionTelemetry | None,
) -> None:
    task = state.running.pop(node_id)
    try:
        await task
    except Exception as exc:
        _mark_node_failed(node_id, exc, state, telemetry)
        return
    _mark_node_succeeded(node_id, state, telemetry)


def _mark_blocked_nodes(
    state: WorkflowExecutionState,
    telemetry: ExecutionTelemetry | None,
) -> list[str]:
    blocked_nodes = [
        node_id for node_id, status in state.statuses.items() if status == "pending"
    ]
    for node_id in blocked_nodes:
        state.statuses[node_id] = "blocked"
        unsatisfied = _unsatisfied_dependencies(
            node_id,
            state.statuses,
            state.dependencies_by_node,
        )
        details = ", ".join(unsatisfied)
        emit_workflow_event(
            telemetry,
            "node_blocked",
            node_id=node_id,
            error=f"unsatisfied dependencies: {details}",
        )
        emit_runtime_log(
            telemetry,
            level="warning",
            message=f"Node blocked; unsatisfied dependencies: {details}",
            operation="blocked_dependencies",
            context=RuntimeEventContext(node_id=node_id),
            attributes={
                "unsatisfied_dependency_count": len(unsatisfied),
                "unsatisfied_dependencies": details,
            },
        )
    return blocked_nodes


def _unsatisfied_dependencies(
    node_id: str,
    statuses: dict[str, NodeStatus],
    dependencies_by_node: dict[str, set[str]],
) -> list[str]:
    return [
        dependency
        for dependency in dependencies_by_node[node_id]
        if statuses[dependency] != "succeeded"
    ]


def _build_workflow_failure_details(
    node_errors: dict[str, Exception],
    blocked_nodes: list[str],
    dependencies_by_node: dict[str, set[str]],
    statuses: dict[str, NodeStatus],
) -> str:
    lines = [
        f"- failed: {node_id} ({node_errors[node_id]})"
        for node_id in sorted(node_errors)
    ]
    for node_id in sorted(blocked_nodes):
        unsatisfied = _unsatisfied_dependencies(
            node_id,
            statuses,
            dependencies_by_node,
        )
        lines.append(
            f"- blocked: {node_id} (unsatisfied dependencies: {', '.join(unsatisfied)})"
        )
    return "\n".join(lines)


def _raise_if_workflow_failed(
    workflow_name: str,
    node_errors: dict[str, Exception],
    blocked_nodes: list[str],
    dependencies_by_node: dict[str, set[str]],
    statuses: dict[str, NodeStatus],
) -> None:
    if not node_errors and not blocked_nodes:
        return
    details = _build_workflow_failure_details(
        node_errors=node_errors,
        blocked_nodes=blocked_nodes,
        dependencies_by_node=dependencies_by_node,
        statuses=statuses,
    )
    raise RuntimeError(f"Workflow '{workflow_name}' failed:\n{details}")


async def execute_workflow(
    plan: PreflightExecutionPlan,
    output: ArtifactStorePort,
    invoker: AgentInvoker,
    secret_context: SecretContext,
    event_sink: EventSink | None = None,
    run_id: str | None = None,
    suppress_progress_output: bool = False,
) -> None:
    """Execute a compiled preflight plan with optional live observability hooks."""

    telemetry = ExecutionTelemetry(
        workflow_name=plan.workflow_name,
        run_id=run_id or output.run_id,
        event_sink=event_sink,
        suppress_console_output=suppress_progress_output,
        activity_tracker=RuntimeActivityTracker(),
    )
    runtime_context = CompiledRuntimeContext(
        plan=plan,
        secret_context=secret_context,
    )
    runtime_context.validate_execution_contract()
    node_semaphore: asyncio.Semaphore | None = None
    max_concurrent_nodes = runtime_context.max_concurrent_nodes()
    if max_concurrent_nodes is not None:
        node_semaphore = asyncio.Semaphore(max_concurrent_nodes)
    if should_print_console(telemetry):
        execution_console(telemetry).print(
            f"[bold blue]Executing Workflow:[/] {plan.workflow_name}"
        )
    emit_workflow_event(telemetry, "workflow_started")

    nodes_by_id: dict[str, PreflightExecutionNode] = {
        node.id: node for node in plan.nodes
    }
    state = _initialize_workflow_execution_state(plan)
    try:
        while state.ready or state.running:
            _schedule_ready_nodes(
                nodes_by_id=nodes_by_id,
                output=output,
                invoker=invoker,
                runtime_context=runtime_context,
                state=state,
                telemetry=telemetry,
                max_concurrent_nodes=max_concurrent_nodes,
                node_semaphore=node_semaphore,
            )
            if not state.running:
                break

            completed_node_ids = await wait_for_completed_nodes(state)
            for node_id in completed_node_ids:
                await _finalize_completed_node(node_id, state, telemetry)

        blocked_nodes = _mark_blocked_nodes(
            state=state,
            telemetry=telemetry,
        )
        _raise_if_workflow_failed(
            workflow_name=plan.workflow_name,
            node_errors=state.node_errors,
            blocked_nodes=blocked_nodes,
            dependencies_by_node=state.dependencies_by_node,
            statuses=state.statuses,
        )
    except Exception as exc:
        emit_workflow_event(telemetry, "workflow_failed", error=safe_error_message(exc))
        raise
    finally:
        await _cancel_running_node_tasks(state)
    emit_workflow_event(telemetry, "workflow_finished")
