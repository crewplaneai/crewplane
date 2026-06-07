import pytest

from orchestrator_cli.core.workflow_models import (
    PromptSegment,
    ProviderSpec,
    WorkflowNode,
    WorkflowPlan,
)
from orchestrator_cli.observability.events import (
    ExecutionEvent,
    ExecutionEventContext,
    InvocationEventPayload,
    RuntimeLogEventPayload,
    apply_event,
    build_initial_state,
    execution_event_log_record,
    runtime_log_event,
    workflow_event,
)
from tests.helpers.observability import topology_from_workflow


def test_execution_event_rejects_payload_type_mismatch() -> None:
    with pytest.raises(ValueError, match="not valid for event_type"):
        ExecutionEvent(
            event_type="runtime_log",
            workflow_name="workflow",
            run_id="run-1",
            context=ExecutionEventContext(workflow_name="workflow", run_id="run-1"),
            payload=InvocationEventPayload(duration_ms=10),
        )


def test_execution_event_rejects_context_mismatch() -> None:
    with pytest.raises(ValueError, match="workflow mismatch"):
        ExecutionEvent(
            event_type="runtime_log",
            workflow_name="workflow",
            run_id="run-1",
            context=ExecutionEventContext(workflow_name="other", run_id="run-1"),
            payload=RuntimeLogEventPayload(
                level="warning",
                message="payload message",
                operation="runtime_warning",
            ),
        )


def test_execution_event_log_uses_typed_payload_fields() -> None:
    event = runtime_log_event(
        workflow_name="workflow",
        run_id="run-1",
        level="warning",
        message="payload message",
        operation="runtime_warning",
    )

    assert event.message == "payload message"
    assert execution_event_log_record(event)["message"] == "payload message"


def test_workflow_event_exposes_payload_error() -> None:
    event = workflow_event(
        event_type="workflow_failed",
        workflow_name="workflow",
        run_id="run-1",
        error="workflow failed",
    )

    assert event.error == "workflow failed"
    assert execution_event_log_record(event)["error"] == "workflow failed"


def test_runtime_log_reducer_preserves_status_and_filters_recent_events() -> None:
    workflow = WorkflowPlan(
        name="workflow",
        nodes=[
            WorkflowNode(
                id="node.a",
                mode="parallel",
                prompt_segments=[PromptSegment(role="shared", content="run")],
                providers=[ProviderSpec(provider="alpha")],
            )
        ],
    )
    state = build_initial_state(topology_from_workflow(workflow), run_id="run-1")
    state.workflow_status = "running"

    apply_event(
        state,
        runtime_log_event(
            workflow_name="workflow",
            run_id="run-1",
            node_id="node.a",
            level="info",
            message="informational",
            operation="runtime_info",
        ),
    )
    apply_event(
        state,
        runtime_log_event(
            workflow_name="workflow",
            run_id="run-1",
            level="warning",
            message="global warning",
            operation="runtime_warning",
        ),
    )
    apply_event(
        state,
        runtime_log_event(
            workflow_name="workflow",
            run_id="run-1",
            node_id="node.a",
            level="error",
            message="node warning",
            operation="runtime_error",
        ),
    )

    assert state.workflow_status == "running"
    assert list(state.nodes["node.a"].recent_events) == ["ERROR node warning"]
