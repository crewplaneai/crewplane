from __future__ import annotations

from orchestrator_cli.observability.events.dashboard_state import (
    InvocationRuntimeState,
    NodeRuntimeState,
    RunDashboardState,
)
from orchestrator_cli.observability.events.execution_event import ExecutionEvent
from orchestrator_cli.observability.events.payloads import RuntimeLogEventPayload
from orchestrator_cli.observability.timing import format_elapsed_seconds


def apply_event(state: RunDashboardState, event: ExecutionEvent) -> None:
    """Apply one execution event to dashboard state in place."""

    context = event.context
    if context.workflow_name != state.workflow_name:
        raise ValueError(
            "Execution event workflow mismatch: "
            f"expected '{state.workflow_name}', got '{event.workflow_name}'."
        )
    if context.run_id != state.run_id:
        raise ValueError(
            "Execution event run_id mismatch: "
            f"expected '{state.run_id}', got '{event.run_id}'."
        )

    match event.event_type:
        case "workflow_started":
            state.workflow_status = "running"
            state.workflow_started_at = event.timestamp
        case "workflow_finished":
            state.workflow_status = "succeeded"
            state.workflow_finished_at = event.timestamp
        case "workflow_failed":
            state.workflow_status = "failed"
            state.workflow_finished_at = event.timestamp
        case "runtime_log":
            if context.node_id is None:
                return
            node = require_node(state, context.node_id)
            apply_runtime_log_event(node, event)
        case "node_started":
            node = require_node(state, context.node_id)
            node.status = "running"
            node.started_at = event.timestamp
            node.finished_at = None
        case "node_finished":
            node = require_node(state, context.node_id)
            node.status = "succeeded"
            node.finished_at = event.timestamp
        case "node_failed":
            node = require_node(state, context.node_id)
            node.status = "failed"
            node.finished_at = event.timestamp
            if event.error:
                node.recent_events.append(f"FAIL {event.error}")
        case "node_blocked":
            node = require_node(state, context.node_id)
            node.status = "blocked"
            if event.error:
                node.recent_events.append(f"BLOCKED {event.error}")
        case "invocation_started":
            node = require_node(state, context.node_id)
            invocation = require_invocation(node, event)
            invocation.status = "running"
            invocation.started_at = event.timestamp
            invocation.finished_at = None
            record_node_event(node, f"RUN {invocation.task_id}")
        case "invocation_finished":
            node = require_node(state, context.node_id)
            invocation = require_invocation(node, event)
            invocation.status = "succeeded"
            invocation.finished_at = event.timestamp
            invocation.duration_ms = event.duration_ms
            suffix = (
                f" ({format_elapsed_seconds(event.duration_ms / 1000)})"
                if event.duration_ms is not None
                else ""
            )
            record_node_event(node, f"DONE {invocation.task_id}{suffix}")
        case "invocation_failed":
            node = require_node(state, context.node_id)
            invocation = require_invocation(node, event)
            invocation.status = "failed"
            invocation.finished_at = event.timestamp
            invocation.duration_ms = event.duration_ms
            invocation.error = event.error
            duration_label = (
                f" ({format_elapsed_seconds(event.duration_ms / 1000)})"
                if event.duration_ms is not None
                else ""
            )
            error_label = clip(event.error or "error", 50)
            record_node_event(
                node, f"FAIL {invocation.task_id}{duration_label}: {error_label}"
            )
        case _:
            raise ValueError(f"Unsupported event type: {event.event_type}")


def require_node(state: RunDashboardState, node_id: str | None) -> NodeRuntimeState:
    if not node_id:
        raise ValueError("Execution event missing node_id.")
    node = state.nodes.get(node_id)
    if node is None:
        raise ValueError(f"Execution event references unknown node '{node_id}'.")
    return node


def require_invocation(
    node: NodeRuntimeState,
    event: ExecutionEvent,
) -> InvocationRuntimeState:
    if not event.task_id:
        raise ValueError("Invocation event missing task_id.")
    if not event.provider:
        raise ValueError("Invocation event missing provider.")
    if not event.role:
        raise ValueError("Invocation event missing role.")

    context = event.context
    invocation_key = invocation_key_for(
        context.task_id,
        context.audit_round_num,
        context.round_num,
    )
    invocation = node.invocations.get(invocation_key)
    if invocation is None:
        invocation = InvocationRuntimeState(
            task_id=context.task_id,
            provider=context.provider or "",
            role=context.role or "",
            model=context.model,
            audit_round_num=context.audit_round_num,
            round_num=context.round_num,
            output_file=context.output_file,
            log_file=context.log_file,
        )
        node.invocations[invocation_key] = invocation

    if context.output_file is not None:
        invocation.output_file = context.output_file
    if context.log_file is not None:
        invocation.log_file = context.log_file

    return invocation


def invocation_key_for(
    task_id: str,
    audit_round_num: int | None,
    round_num: int | None,
) -> str:
    if audit_round_num is None and round_num is None:
        return task_id
    audit_part = f"audit{audit_round_num}" if audit_round_num is not None else "audit?"
    round_part = f"round{round_num}" if round_num is not None else "round?"
    return f"{task_id}#{audit_part}#{round_part}"


def record_node_event(node: NodeRuntimeState, message: str) -> None:
    node.recent_events.append(clip(message, 80))


def apply_runtime_log_event(
    node: NodeRuntimeState,
    event: ExecutionEvent,
) -> None:
    payload = event.payload
    if not isinstance(payload, RuntimeLogEventPayload):
        return
    if payload.level not in {"warning", "error"} or not payload.message:
        return
    prefix = "WARN" if payload.level == "warning" else "ERROR"
    record_node_event(node, f"{prefix} {clip(payload.message, 64)}")


def clip(value: str, max_len: int) -> str:
    if len(value) <= max_len:
        return value
    if max_len <= 3:
        return value[:max_len]
    return f"{value[: max_len - 3]}..."
