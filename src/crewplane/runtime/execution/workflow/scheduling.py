from __future__ import annotations

import asyncio

from rich.text import Text

from crewplane.architecture.contracts import AgentInvoker, EventType
from crewplane.architecture.ports import ArtifactStorePort
from crewplane.core.preflight.models import PreflightExecutionNode

from ..common import (
    ExecutionTelemetry,
    NodeStatus,
    RuntimeEventContext,
    WorkflowExecutionState,
    emit_runtime_log,
    emit_workflow_event,
    execution_console,
    safe_error_message,
    should_print_console,
)
from ..errors import WorkflowExecutionError, is_expected_execution_failure
from .cleanup import cleanup_successful_workspace_run_refs
from .execution_session import WorkflowExecutionSession
from .node import (
    execute_node,
    mark_node_finished_activity,
    mark_node_running_activity,
)


def _wrap_node_task(
    node: PreflightExecutionNode,
    output: ArtifactStorePort,
    invoker: AgentInvoker,
    session: WorkflowExecutionSession,
) -> asyncio.Task[None]:
    async def _run() -> None:
        if session.node_semaphore is None:
            await execute_node(
                node,
                output,
                invoker,
                session.runtime_context,
                session.telemetry,
                session.workflow_identity,
            )
            return
        async with session.node_semaphore:
            await execute_node(
                node,
                output,
                invoker,
                session.runtime_context,
                session.telemetry,
                session.workflow_identity,
            )

    return asyncio.create_task(_run())


def _schedule_ready_nodes(
    session: WorkflowExecutionSession,
    output: ArtifactStorePort,
    invoker: AgentInvoker,
) -> None:
    state = session.state
    while state.ready and (
        session.max_concurrent_nodes is None
        or len(state.running) < session.max_concurrent_nodes
    ):
        node_id = state.ready.pop(0)
        if state.statuses[node_id] != "pending":
            continue
        state.statuses[node_id] = "running"
        mark_node_running_activity(session.telemetry, node_id)
        state.running[node_id] = _wrap_node_task(
            session.nodes_by_id[node_id],
            output,
            invoker,
            session,
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
    """Wait until at least one running workflow node completes."""
    done, _ = await asyncio.wait(
        set(state.running.values()),
        return_when=asyncio.FIRST_COMPLETED,
    )
    return _completed_node_ids(state.running, done, state.node_order)


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
        execution_console(telemetry).print(
            Text.assemble(("✗", "red"), f" Node '{node_id}' failed: {exc}")
        )
    emit_workflow_event(
        telemetry,
        EventType.NODE_FAILED,
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
    emit_workflow_event(telemetry, EventType.NODE_FINISHED, node_id=node_id)
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
        if not is_expected_execution_failure(exc):
            raise
        return
    _mark_node_succeeded(node_id, state, telemetry)


async def _consume_completed_nodes(session: WorkflowExecutionSession) -> None:
    completed_node_ids = await wait_for_completed_nodes(session.state)
    unexpected_error: Exception | None = None
    for node_id in completed_node_ids:
        try:
            await _finalize_completed_node(node_id, session.state, session.telemetry)
        except Exception as exc:
            if unexpected_error is None:
                unexpected_error = exc
    if unexpected_error is not None:
        raise unexpected_error


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
            EventType.NODE_BLOCKED,
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
    raise WorkflowExecutionError(f"Workflow '{workflow_name}' failed:\n{details}")


async def run_scheduling_loop(
    session: WorkflowExecutionSession,
    output: ArtifactStorePort,
    invoker: AgentInvoker,
) -> None:
    while session.state.ready or session.state.running:
        _schedule_ready_nodes(session, output, invoker)
        if not session.state.running:
            break
        await _consume_completed_nodes(session)


async def finalize_execution(session: WorkflowExecutionSession) -> None:
    state = session.state
    blocked_nodes = _mark_blocked_nodes(
        state=state,
        telemetry=session.telemetry,
    )
    _raise_if_workflow_failed(
        workflow_name=session.runtime_context.plan.workflow_name,
        node_errors=state.node_errors,
        blocked_nodes=blocked_nodes,
        dependencies_by_node=state.dependencies_by_node,
        statuses=state.statuses,
    )
    await cleanup_successful_workspace_run_refs(
        session.runtime_context.plan,
        session.telemetry,
    )
