from types import SimpleNamespace

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
    WorkspaceEventPayload,
    apply_event,
    build_initial_state,
    execution_event_log_record,
    invocation_event,
    node_event,
    runtime_log_event,
    workflow_event,
    workspace_event,
)
from orchestrator_cli.observability.run_summary.builder import workflow_status
from orchestrator_cli.observability.run_summary.issues import issue_summaries
from orchestrator_cli.observability.types import RunResult
from orchestrator_cli.version import SCHEMA_VERSION
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


def test_workflow_status_failed_result_overrides_succeeded_snapshot() -> None:
    snapshot = SimpleNamespace(state=SimpleNamespace(workflow_status="succeeded"))

    assert workflow_status(snapshot, RunResult(status="failed")) == "failed"


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


def test_node_event_records_node_context_and_payload_error() -> None:
    event = node_event(
        event_type="node_failed",
        workflow_name="workflow",
        run_id="run-1",
        node_id="node.a",
        error="node failed",
        timestamp=12.5,
        timestamp_utc="2026-06-22T00:00:00+00:00",
    )

    record = execution_event_log_record(event)

    assert event.context.node_id == "node.a"
    assert event.payload.error == "node failed"
    assert event.timestamp == 12.5
    assert event.timestamp_utc == "2026-06-22T00:00:00+00:00"
    assert record["node_id"] == "node.a"
    assert record["error"] == "node failed"


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
            level="info",
            message="informational",
            operation="runtime_info",
            context=ExecutionEventContext(
                workflow_name="workflow",
                run_id="run-1",
                node_id="node.a",
            ),
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
            level="error",
            message="node warning",
            operation="runtime_error",
            context=ExecutionEventContext(
                workflow_name="workflow",
                run_id="run-1",
                node_id="node.a",
            ),
        ),
    )

    assert state.workflow_status == "running"
    assert list(state.nodes["node.a"].recent_events) == ["ERROR node warning"]


def test_invocation_event_records_log_presentation_context() -> None:
    event = invocation_event(
        event_type="invocation_started",
        workflow_name="workflow",
        run_id="run-1",
        context=ExecutionEventContext(
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

    record = execution_event_log_record(event)

    assert record["log_presentation_format"] == "json_lines"
    assert record["log_presentation_profile"] == "mock"


def test_workspace_event_records_workspace_payload() -> None:
    event = workspace_event(
        "workspace_context_recorded",
        workflow_name="workflow",
        run_id="run-1",
        context=ExecutionEventContext(
            workflow_name="workflow",
            run_id="run-1",
            node_id="node.a",
            provider="alpha",
            role="executor",
            task_id="alpha_executor_0",
        ),
        payload=WorkspaceEventPayload(
            status="running",
            workspace_kind="worktree",
            workspace_logical_worktree_name="implementation",
            workspace_materialization="worktree_checkout",
            workspace_source_kind="project",
            workspace_source_commit="a" * 40,
            workspace_source_tree="b" * 40,
            worktree_contract_mode="blob_exact",
            worktree_contract_schema_version=SCHEMA_VERSION,
            workspace_state_path="node.a/workspace-state.json",
            workspace_writable=True,
            workspace_lineage_producer=True,
            workspace_child_environment_required=True,
            workspace_child_environment_applied=False,
        ),
    )

    record = execution_event_log_record(event)

    assert record["workspace_kind"] == "worktree"
    assert record["workspace_materialization"] == "worktree_checkout"
    assert record["workspace_source_kind"] == "project"
    assert record["worktree_contract_mode"] == "blob_exact"
    assert record["workspace_child_environment_required"] is True


def test_invocation_event_omits_workspace_payload_fields() -> None:
    event = invocation_event(
        event_type="invocation_started",
        workflow_name="workflow",
        run_id="run-1",
        context=ExecutionEventContext(
            workflow_name="workflow",
            run_id="run-1",
            node_id="node.a",
            provider="alpha",
            role="executor",
            task_id="alpha_executor_0",
        ),
    )

    record = execution_event_log_record(event)

    assert "workspace_kind" not in record
    assert "worktree_contract_mode" not in record


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
            context=ExecutionEventContext(
                workflow_name="workflow",
                run_id="run-1",
                node_id="node.a",
                provider="alpha",
                role="executor",
                task_id="alpha_executor_0",
                log_presentation_format="json_lines",
                log_presentation_profile="mock",
            ),
        ),
    )
    apply_event(
        state,
        invocation_event(
            event_type="invocation_finished",
            workflow_name="workflow",
            run_id="run-1",
            context=ExecutionEventContext(
                workflow_name="workflow",
                run_id="run-1",
                node_id="node.a",
                provider="alpha",
                role="executor",
                task_id="alpha_executor_0",
            ),
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
        level="warning",
        message="Log presentation metadata was unavailable; using plain log display.",
        operation="log_presentation_descriptor_invalid",
        context=ExecutionEventContext(
            workflow_name="workflow",
            run_id="run-1",
            node_id="node.a",
            log_presentation_format="json_lines",
            log_presentation_profile="mock",
        ),
        attributes={"reason": "ValueError"},
    )

    issues = issue_summaries([event])

    assert len(issues) == 1
    assert "json_lines" not in issues[0].message
    assert "mock" not in issues[0].message
    assert "ValueError" not in issues[0].message
