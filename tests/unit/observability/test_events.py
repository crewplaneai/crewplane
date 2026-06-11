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
    invocation_event,
    runtime_log_event,
    workflow_event,
)
from orchestrator_cli.observability.run_summary.issues import issue_summaries
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

    assert event.payload.message == "payload message"
    assert execution_event_log_record(event)["message"] == "payload message"


def test_workflow_event_exposes_payload_error() -> None:
    event = workflow_event(
        event_type="workflow_failed",
        workflow_name="workflow",
        run_id="run-1",
        error="workflow failed",
    )

    assert event.payload.error == "workflow failed"
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


def test_invocation_event_records_log_presentation_context() -> None:
    event = invocation_event(
        event_type="invocation_started",
        workflow_name="workflow",
        run_id="run-1",
        node_id="node.a",
        provider="alpha",
        role="executor",
        task_id="alpha_executor_0",
        log_presentation_format="json_lines",
        log_presentation_profile="mock",
    )

    record = execution_event_log_record(event)

    assert record["log_presentation_format"] == "json_lines"
    assert record["log_presentation_profile"] == "mock"


def test_reducer_does_not_clear_log_presentation_context() -> None:
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

    apply_event(
        state,
        invocation_event(
            event_type="invocation_started",
            workflow_name="workflow",
            run_id="run-1",
            node_id="node.a",
            provider="alpha",
            role="executor",
            task_id="alpha_executor_0",
            log_presentation_format="json_lines",
            log_presentation_profile="mock",
        ),
    )
    apply_event(
        state,
        invocation_event(
            event_type="invocation_finished",
            workflow_name="workflow",
            run_id="run-1",
            node_id="node.a",
            provider="alpha",
            role="executor",
            task_id="alpha_executor_0",
            duration_ms=1,
        ),
    )

    invocation = state.nodes["node.a"].invocations["alpha_executor_0"]
    assert invocation.log_presentation_format == "json_lines"
    assert invocation.log_presentation_profile == "mock"


def test_summary_issues_ignore_log_presentation_context() -> None:
    event = runtime_log_event(
        workflow_name="workflow",
        run_id="run-1",
        node_id="node.a",
        level="warning",
        message="Log presentation metadata was unavailable; using plain log display.",
        operation="log_presentation_descriptor_invalid",
        log_presentation_format="json_lines",
        log_presentation_profile="mock",
        attributes={"reason": "ValueError"},
    )

    issues = issue_summaries([event])

    assert len(issues) == 1
    assert "json_lines" not in issues[0].message
    assert "mock" not in issues[0].message
    assert "ValueError" not in issues[0].message
