from __future__ import annotations

import asyncio

from orchestrator_cli.architecture.contracts import AgentInvoker
from orchestrator_cli.architecture.ports import ArtifactStorePort
from orchestrator_cli.core.preflight.models import (
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
from .resume import emit_resumed_node_events
from .workflow_cleanup import (
    cleanup_successful_workspace_run_refs,
    emit_cleanup_errors,
    refresh_workspace_node_manifests,
    refresh_workspace_node_manifests_for_state_paths,
)
from .workflow_node import (
    execute_node,
    mark_node_finished_activity,
    mark_node_running_activity,
)
from .workflow_state import initialize_workflow_execution_state

DEFERRED_WORKSPACE_CLEANUP_DRAIN_TIMEOUT_SECONDS = 30.0


def _wrap_node_task(
    node: PreflightExecutionNode,
    output: ArtifactStorePort,
    invoker: AgentInvoker,
    runtime_context: CompiledRuntimeContext,
    telemetry: ExecutionTelemetry | None,
    node_semaphore: asyncio.Semaphore | None,
    workflow_identity: str,
) -> asyncio.Task[None]:
    async def _run() -> None:
        if node_semaphore is None:
            await execute_node(
                node,
                output,
                invoker,
                runtime_context,
                telemetry,
                workflow_identity,
            )
            return
        async with node_semaphore:
            await execute_node(
                node,
                output,
                invoker,
                runtime_context,
                telemetry,
                workflow_identity,
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
    workflow_identity: str,
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
            workflow_identity,
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
    workflow_identity: str | None = None,
    resumed_node_ids: tuple[str, ...] = (),
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
    resolved_workflow_identity = workflow_identity or plan.workflow_name
    state = initialize_workflow_execution_state(plan, resumed_node_ids)
    try:
        for node_id in sorted(resumed_node_ids, key=state.node_order.__getitem__):
            emit_resumed_node_events(node_id, telemetry)
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
                workflow_identity=resolved_workflow_identity,
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
        await cleanup_successful_workspace_run_refs(plan, telemetry)
    except Exception as exc:
        emit_workflow_event(telemetry, "workflow_failed", error=safe_error_message(exc))
        raise
    finally:
        await _cancel_running_node_tasks(state)
        deferred_workspace_cleanup_errors = (
            await runtime_context.deferred_workspace_cleanups.drain(
                DEFERRED_WORKSPACE_CLEANUP_DRAIN_TIMEOUT_SECONDS
            )
        )
        emit_cleanup_errors(
            telemetry,
            "workspace_preparation_cancellation_cleanup",
            deferred_workspace_cleanup_errors,
        )
        generated_file_cleanup = await asyncio.to_thread(
            runtime_context.generated_file_workspaces.cleanup_all
        )
        emit_cleanup_errors(
            telemetry,
            "generated_file_workspace_cleanup",
            generated_file_cleanup.errors,
        )
        worktree_cleanup = await asyncio.to_thread(
            runtime_context.worktree_reuse_cache.cleanup_all
        )
        emit_cleanup_errors(
            telemetry,
            "worktree_reuse_cleanup",
            worktree_cleanup.errors,
        )
        await refresh_workspace_node_manifests_for_state_paths(
            plan,
            output,
            state.statuses,
            worktree_cleanup.updated_state_paths,
            telemetry,
        )
        await refresh_workspace_node_manifests(
            plan,
            output,
            state.statuses,
            set(generated_file_cleanup.cleaned_node_ids),
            telemetry,
        )
    emit_workflow_event(telemetry, "workflow_finished")
