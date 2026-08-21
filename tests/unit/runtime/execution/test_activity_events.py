from __future__ import annotations

import pytest

from crewplane.architecture.contracts import EventType, InvocationEventType
from crewplane.observability.events import (
    ExecutionEvent,
    ExecutionEventContext,
    WorkflowEventPayload,
    invocation_event,
)
from crewplane.runtime.execution.activity.events import emit_workflow_event
from crewplane.runtime.execution.activity.telemetry import ExecutionTelemetry


def test_emit_workflow_cancelled_requires_no_invocation_context() -> None:
    events: list[ExecutionEvent] = []
    telemetry = ExecutionTelemetry(
        workflow_name="workflow",
        run_id="run-1",
        event_sink=events.append,
    )

    emit_workflow_event(telemetry, EventType.WORKFLOW_CANCELLED)

    assert len(events) == 1
    assert events[0].event_type is EventType.WORKFLOW_CANCELLED
    assert events[0].context.node_id is None
    assert isinstance(events[0].payload, WorkflowEventPayload)


def test_emit_workflow_event_retains_node_context_validation() -> None:
    telemetry = ExecutionTelemetry(workflow_name="workflow", run_id="run-1")

    with pytest.raises(ValueError, match="requires node_id"):
        emit_workflow_event(telemetry, EventType.NODE_STARTED)


@pytest.mark.parametrize(
    "event_type",
    [
        EventType.INVOCATION_STARTED,
        EventType.INVOCATION_FINISHED,
        EventType.INVOCATION_FAILED,
    ],
)
def test_invocation_events_still_require_invocation_context(
    event_type: InvocationEventType,
) -> None:
    with pytest.raises(
        ValueError,
        match="requires node, provider, role, and task",
    ):
        invocation_event(
            event_type,
            "workflow",
            "run-1",
            ExecutionEventContext(workflow_name="workflow", run_id="run-1"),
        )
