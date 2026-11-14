"""Reduce execution events into mutable dashboard state.

``apply_event`` is the package-level entry point. Other non-private functions
remain callable through this module for focused state operations. Mutators act
only on supplied dashboard objects; callers own synchronization and snapshots.
"""

from __future__ import annotations

from dataclasses import replace

from crewplane.architecture.contracts import (
    ExecutionStatus,
    LogLevel,
    validate_log_presentation_format,
)
from crewplane.architecture.contracts.execution_event import TopologyExpansionPayload
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.observability.events.dashboard_state import (
    InvocationRuntimeState,
    NodeRuntimeState,
    RunDashboardState,
)
from crewplane.observability.events.execution_event import ExecutionEvent
from crewplane.observability.events.payloads import (
    InvocationEventPayload,
    NodeEventPayload,
    RuntimeLogEventPayload,
)
from crewplane.observability.events.types import EventType
from crewplane.observability.timing import format_elapsed_seconds


def apply_event(state: RunDashboardState, event: ExecutionEvent) -> None:
    """Apply an event in place, validating its run before any state changes.

    Raises:
        ValueError: The run, node, invocation identity, payload, or event type is
            invalid. Changes made before a later error remain in state.
        TypeError: Invocation log presentation metadata has an invalid type.
    """

    _validate_event_identity(state, event)
    if isinstance(event.payload, TopologyExpansionPayload):
        apply_topology_expansion(state, event.payload)
        return
    match event.event_type:
        case (
            EventType.WORKFLOW_STARTED
            | EventType.WORKFLOW_FINISHED
            | EventType.WORKFLOW_FAILED
            | EventType.WORKFLOW_CANCELLED
        ):
            _apply_workflow_lifecycle(state, event)
        case EventType.RUNTIME_LOG:
            if event.context.node_id is None:
                return
            node = require_node(state, event.context.node_id)
            apply_runtime_log_event(node, event)
        case (
            EventType.NODE_STARTED
            | EventType.NODE_FINISHED
            | EventType.NODE_FAILED
            | EventType.NODE_BLOCKED
        ):
            _apply_node_lifecycle(require_node(state, event.context.node_id), event)
        case (
            EventType.INVOCATION_STARTED
            | EventType.INVOCATION_FINISHED
            | EventType.INVOCATION_FAILED
        ):
            _apply_invocation_lifecycle(
                require_node(state, event.context.node_id), event
            )
        case EventType.WORKSPACE_CONTEXT_RECORDED:
            return
        case _:
            raise ValueError(f"Unsupported event type: {event.event_type}")


def _validate_event_identity(state: RunDashboardState, event: ExecutionEvent) -> None:
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


def _apply_workflow_lifecycle(state: RunDashboardState, event: ExecutionEvent) -> None:
    match event.event_type:
        case EventType.WORKFLOW_STARTED:
            state.workflow_status = ExecutionStatus.RUNNING
            state.workflow_started_at = event.timestamp
        case EventType.WORKFLOW_FINISHED:
            state.workflow_status = ExecutionStatus.SUCCEEDED
            state.workflow_finished_at = event.timestamp
        case EventType.WORKFLOW_FAILED:
            state.workflow_status = ExecutionStatus.FAILED
            state.workflow_finished_at = event.timestamp
        case EventType.WORKFLOW_CANCELLED:
            state.workflow_status = ExecutionStatus.CANCELLED
            state.workflow_finished_at = event.timestamp
            cancel_unfinished_children(state, event.timestamp)


def _apply_node_lifecycle(node: NodeRuntimeState, event: ExecutionEvent) -> None:
    match event.event_type:
        case EventType.NODE_STARTED:
            node.status = ExecutionStatus.RUNNING
            node.started_at = event.timestamp
            node.finished_at = None
        case EventType.NODE_FINISHED:
            node.status = ExecutionStatus.SUCCEEDED
            node.finished_at = event.timestamp
        case EventType.NODE_FAILED:
            node.status = ExecutionStatus.FAILED
            node.finished_at = event.timestamp
            node_payload = _node_payload(event)
            if node_payload.error:
                node.recent_events.append(f"FAIL {node_payload.error}")
        case EventType.NODE_BLOCKED:
            node.status = ExecutionStatus.BLOCKED
            node_payload = _node_payload(event)
            if node_payload.error:
                node.recent_events.append(f"BLOCKED {node_payload.error}")


def _apply_invocation_lifecycle(node: NodeRuntimeState, event: ExecutionEvent) -> None:
    invocation = require_invocation(node, event)
    if event.event_type == EventType.INVOCATION_STARTED:
        invocation.status = ExecutionStatus.RUNNING
        invocation.started_at = event.timestamp
        invocation.finished_at = None
        record_node_event(node, f"RUN {invocation.task_id}")
        return

    payload = _invocation_payload(event)
    failed = event.event_type == EventType.INVOCATION_FAILED
    invocation.status = ExecutionStatus.FAILED if failed else ExecutionStatus.SUCCEEDED
    invocation.finished_at = event.timestamp
    invocation.duration_ms = payload.duration_ms
    if failed:
        invocation.error = payload.error
    duration_label = (
        f" ({format_elapsed_seconds(payload.duration_ms / 1000)})"
        if payload.duration_ms is not None
        else ""
    )
    if failed:
        error_label = clip(payload.error or "error", 50)
        record_node_event(
            node, f"FAIL {invocation.task_id}{duration_label}: {error_label}"
        )
    else:
        record_node_event(node, f"DONE {invocation.task_id}{duration_label}")


def cancel_unfinished_children(state: RunDashboardState, timestamp: float) -> None:
    """Cancel pending/running children, leaving terminal children unchanged.

    Set finish times and, for invocations with a start time, a nonnegative
    duration in milliseconds. Children without a start time keep their duration.
    """

    for node in state.nodes.values():
        if node.status in {ExecutionStatus.PENDING, ExecutionStatus.RUNNING}:
            node.status = ExecutionStatus.CANCELLED
            node.finished_at = timestamp
        for invocation in node.invocations.values():
            if invocation.status not in {
                ExecutionStatus.PENDING,
                ExecutionStatus.RUNNING,
            }:
                continue
            invocation.status = ExecutionStatus.CANCELLED
            invocation.finished_at = timestamp
            if invocation.started_at is not None:
                invocation.duration_ms = int(
                    max(0.0, timestamp - invocation.started_at) * 1000
                )


def require_node(state: RunDashboardState, node_id: str | None) -> NodeRuntimeState:
    """Return an existing node without mutation.

    Raises:
        ValueError: The node ID is empty, missing, or unknown to this run.
    """

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
    """Return or create the task/round invocation, merging present metadata.

    Validate identity, role, and log format before inserting or updating state.
    Existing provider, role, and model are retained; optional reasoning, file,
    and log presentation fields update only when their incoming value is not None.
    Lifecycle status, timing, and error fields are untouched.

    Raises:
        ValueError: Required identity is missing, or role/log format is invalid.
        TypeError: The log presentation format is not a string.
    """

    context = event.context
    if not context.task_id:
        raise ValueError("Invocation event missing task_id.")
    if not context.provider:
        raise ValueError("Invocation event missing provider.")
    if not context.role:
        raise ValueError("Invocation event missing role.")
    role = ProviderRole(context.role)
    presentation_format = (
        validate_log_presentation_format(context.log_presentation_format)
        if context.log_presentation_format is not None
        else None
    )

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
            role=role,
            model=context.model,
            audit_round_num=context.audit_round_num,
            round_num=context.round_num,
        )
        node.invocations[invocation_key] = invocation

    if context.requested_reasoning is not None:
        invocation.requested_reasoning = context.requested_reasoning
    if context.output_file is not None:
        invocation.output_file = context.output_file
    if context.log_file is not None:
        invocation.log_file = context.log_file
    if context.log_presentation_format is not None:
        invocation.log_presentation_format = presentation_format
    if context.log_presentation_profile is not None:
        invocation.log_presentation_profile = context.log_presentation_profile

    return invocation


def invocation_key_for(
    task_id: str,
    audit_round_num: int | None,
    round_num: int | None,
) -> str:
    """Return the task ID, qualified by rounds when either round is supplied.

    Missing round components use ``audit?`` or ``round?`` placeholders. Inputs
    are formatted as supplied without validation or state changes.
    """

    if audit_round_num is None and round_num is None:
        return task_id
    audit_part = f"audit{audit_round_num}" if audit_round_num is not None else "audit?"
    round_part = f"round{round_num}" if round_num is not None else "round?"
    return f"{task_id}#{audit_part}#{round_part}"


def record_node_event(node: NodeRuntimeState, message: str) -> None:
    """Append an 80-character-clipped message to the node's bounded history."""

    node.recent_events.append(clip(message, 80))


def apply_runtime_log_event(
    node: NodeRuntimeState,
    event: ExecutionEvent,
) -> None:
    """Record nonempty warning/error logs without changing lifecycle state.

    Ignore other payloads and levels. Clip log text to 64 characters before
    prefixing it and recording it through ``record_node_event``.
    """

    payload = event.payload
    if not isinstance(payload, RuntimeLogEventPayload):
        return
    if payload.level not in {LogLevel.WARNING, LogLevel.ERROR} or not payload.message:
        return
    prefix = "WARN" if payload.level == LogLevel.WARNING else "ERROR"
    record_node_event(node, f"{prefix} {clip(payload.message, 64)}")


def clip(value: str, max_len: int) -> str:
    """Return text clipped to the limit, using ``...`` when the limit exceeds 3.

    Limits of 3 or less use a plain slice, including Python's negative slicing.
    """

    if len(value) <= max_len:
        return value
    if max_len <= 3:
        return value[:max_len]
    return f"{value[: max_len - 3]}..."


def _invocation_payload(event: ExecutionEvent) -> InvocationEventPayload:
    payload = event.payload
    if not isinstance(payload, InvocationEventPayload):
        raise ValueError("Invocation event must carry InvocationEventPayload.")
    return payload


def _node_payload(event: ExecutionEvent) -> NodeEventPayload:
    payload = event.payload
    if not isinstance(payload, NodeEventPayload):
        raise ValueError("Node event must carry NodeEventPayload.")
    return payload


def apply_topology_expansion(
    state: RunDashboardState, payload: TopologyExpansionPayload
) -> None:
    """Insert expanded topology before its group and rebuild display ordering.

    Replace the group's dependencies and create missing runtime nodes while
    preserving existing runtime progress when an expansion is replayed.

    Raises:
        ValueError: The expansion group is unknown; state is left unchanged.
    """

    group = state.topology_nodes.get(payload.group_id)
    if group is None:
        raise ValueError(f"Unknown expansion group '{payload.group_id}'")
    additions = {node.id: node for node in payload.additions}
    ordered = {}
    for node_id, node in state.topology_nodes.items():
        if node_id == payload.group_id:
            ordered.update(additions)
            node = replace(node, dependencies=payload.group_dependencies)
        ordered[node_id] = node
    state.topology_nodes = ordered
    state.node_order = {node_id: index for index, node_id in enumerate(ordered)}
    for node in payload.additions:
        state.nodes.setdefault(
            node.id,
            NodeRuntimeState(
                node.id,
                node.mode,
                tuple(provider.provider for provider in node.providers),
            ),
        )
