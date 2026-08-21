import json
from types import SimpleNamespace
from typing import cast, get_args

import pytest

import crewplane.architecture.contracts as architecture_contracts
import crewplane.observability.events as observability_events
import crewplane.observability.events.types as observability_event_types
from crewplane.architecture.contracts import (
    TERMINAL_WORKFLOW_EVENT_TYPES,
    EventType,
    InvocationEventType,
    NodeEventType,
    WorkflowEventType,
    WorkspaceEventType,
)
from crewplane.core.prompt_segments import PromptSegmentRole
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.core.workflow.models import (
    PromptSegment,
    ProviderSpec,
    WorkflowNode,
    WorkflowPlan,
)
from crewplane.observability.events import (
    ExecutionEvent,
    ExecutionEventContext,
    InvocationEventPayload,
    NodeEventPayload,
    RunDashboardState,
    RuntimeLogEventPayload,
    WorkflowEventPayload,
    WorkspaceEventPayload,
    apply_event,
    build_initial_state,
    event_from_record,
    execution_event_log_record,
    format_execution_event_log_line,
    invocation_event,
    node_event,
    runtime_log_event,
    workflow_event,
    workspace_event,
)
from crewplane.observability.run_summary.builder import workflow_status
from crewplane.observability.run_summary.issues import issue_summaries
from crewplane.observability.types import RunResult
from crewplane.version import SCHEMA_VERSION
from tests.helpers.observability import topology_from_workflow

EXPECTED_EVENT_VALUES = {
    EventType.WORKFLOW_STARTED: "workflow_started",
    EventType.WORKFLOW_FINISHED: "workflow_finished",
    EventType.WORKFLOW_FAILED: "workflow_failed",
    EventType.WORKFLOW_CANCELLED: "workflow_cancelled",
    EventType.NODE_STARTED: "node_started",
    EventType.NODE_FINISHED: "node_finished",
    EventType.NODE_FAILED: "node_failed",
    EventType.NODE_BLOCKED: "node_blocked",
    EventType.INVOCATION_STARTED: "invocation_started",
    EventType.INVOCATION_FINISHED: "invocation_finished",
    EventType.INVOCATION_FAILED: "invocation_failed",
    EventType.WORKSPACE_CONTEXT_RECORDED: "workspace_context_recorded",
    EventType.RUNTIME_LOG: "runtime_log",
}


def test_event_type_members_and_wire_values_are_exact() -> None:
    assert tuple(EventType) == tuple(EXPECTED_EVENT_VALUES)
    assert {member: member.value for member in EventType} == EXPECTED_EVENT_VALUES


def test_event_category_aliases_are_disjoint_and_cover_lifecycle_members() -> None:
    category_members = (
        frozenset(get_args(WorkflowEventType)),
        frozenset(get_args(NodeEventType)),
        frozenset(get_args(InvocationEventType)),
        frozenset(get_args(WorkspaceEventType)),
    )

    assert all(
        left.isdisjoint(right)
        for index, left in enumerate(category_members)
        for right in category_members[index + 1 :]
    )
    assert frozenset().union(*category_members) == frozenset(EventType) - {
        EventType.RUNTIME_LOG
    }


def test_terminal_workflow_event_types_are_exact() -> None:
    assert {
        EventType.WORKFLOW_FINISHED,
        EventType.WORKFLOW_FAILED,
        EventType.WORKFLOW_CANCELLED,
    } == TERMINAL_WORKFLOW_EVENT_TYPES


@pytest.mark.parametrize(
    "export_name",
    [
        "EventType",
        "WorkflowEventType",
        "NodeEventType",
        "InvocationEventType",
        "WorkspaceEventType",
        "TERMINAL_WORKFLOW_EVENT_TYPES",
    ],
)
def test_public_event_facades_expose_architecture_contract(export_name: str) -> None:
    contract = getattr(architecture_contracts, export_name)

    assert getattr(observability_events, export_name) is contract
    assert getattr(observability_event_types, export_name) is contract


def test_execution_event_canonicalizes_valid_raw_string() -> None:
    event = ExecutionEvent(
        event_type=cast(EventType, "workflow_started"),
        workflow_name="workflow",
        run_id="run-1",
        context=ExecutionEventContext(workflow_name="workflow", run_id="run-1"),
        payload=WorkflowEventPayload(),
    )

    assert event.event_type is EventType.WORKFLOW_STARTED


def test_execution_event_rejects_payload_type_mismatch() -> None:
    with pytest.raises(ValueError, match="not valid for event_type"):
        ExecutionEvent(
            event_type=EventType.RUNTIME_LOG,
            workflow_name="workflow",
            run_id="run-1",
            context=ExecutionEventContext(workflow_name="workflow", run_id="run-1"),
            payload=InvocationEventPayload(duration_ms=10),
        )


def test_execution_event_rejects_unknown_event_type() -> None:
    with pytest.raises(ValueError, match="Unsupported execution event type"):
        ExecutionEvent(
            event_type=cast(EventType, "future_event"),
            workflow_name="workflow",
            run_id="run-1",
            context=ExecutionEventContext(workflow_name="workflow", run_id="run-1"),
            payload=RuntimeLogEventPayload(
                level="warning",
                message="payload message",
                operation="runtime_warning",
            ),
        )


def test_execution_event_rejects_every_payload_category_mismatch() -> None:
    payloads = (
        WorkflowEventPayload(),
        NodeEventPayload(),
        InvocationEventPayload(),
        WorkspaceEventPayload(),
        RuntimeLogEventPayload(
            level="info",
            message="message",
            operation="operation",
        ),
    )
    expected_payload_types = {
        **{
            event_type: WorkflowEventPayload
            for event_type in get_args(WorkflowEventType)
        },
        **{event_type: NodeEventPayload for event_type in get_args(NodeEventType)},
        **{
            event_type: InvocationEventPayload
            for event_type in get_args(InvocationEventType)
        },
        EventType.WORKSPACE_CONTEXT_RECORDED: WorkspaceEventPayload,
        EventType.RUNTIME_LOG: RuntimeLogEventPayload,
    }

    for event_type, expected_payload_type in expected_payload_types.items():
        for payload in payloads:
            if isinstance(payload, expected_payload_type):
                continue
            with pytest.raises(ValueError, match="not valid for event_type"):
                ExecutionEvent(
                    event_type=event_type,
                    workflow_name="workflow",
                    run_id="run-1",
                    context=ExecutionEventContext(
                        workflow_name="workflow",
                        run_id="run-1",
                    ),
                    payload=payload,
                )


@pytest.mark.parametrize("event_type", tuple(EventType), ids=lambda item: item.value)
def test_every_event_member_round_trips_as_lowercase_json_string(
    event_type: EventType,
) -> None:
    event = _event_for_type(event_type)

    record = execution_event_log_record(event)
    encoded_record = json.loads(format_execution_event_log_line(event))
    restored = event_from_record(record)

    assert event.event_type is event_type
    assert type(record["event_type"]) is str
    assert record["event_type"] == event_type.value
    assert encoded_record["event_type"] == event_type.value
    assert restored is not None
    assert restored.event_type is event_type
    assert type(execution_event_log_record(restored)["event_type"]) is str


def test_workflow_status_failed_result_overrides_succeeded_snapshot() -> None:
    snapshot = SimpleNamespace(state=SimpleNamespace(workflow_status="succeeded"))

    assert workflow_status(snapshot, RunResult(status="failed")) == "failed"


def test_execution_event_rejects_context_mismatch() -> None:
    with pytest.raises(ValueError, match="workflow mismatch"):
        ExecutionEvent(
            event_type=EventType.RUNTIME_LOG,
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
        event_type=EventType.WORKFLOW_FAILED,
        workflow_name="workflow",
        run_id="run-1",
        error="workflow failed",
    )

    assert event.payload.error == "workflow failed"
    assert execution_event_log_record(event)["error"] == "workflow failed"


def test_node_event_records_node_context_and_payload_error() -> None:
    event = node_event(
        event_type=EventType.NODE_FAILED,
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
                prompt_segments=[
                    PromptSegment(role=PromptSegmentRole.SHARED, content="run")
                ],
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
        event_type=EventType.INVOCATION_STARTED,
        workflow_name="workflow",
        run_id="run-1",
        context=ExecutionEventContext(
            workflow_name="workflow",
            run_id="run-1",
            node_id="node.a",
            provider="alpha",
            role=ProviderRole.EXECUTOR,
            task_id="alpha_executor_0",
            log_presentation_format="json_lines",
            log_presentation_profile="mock",
        ),
    )

    record = execution_event_log_record(event)

    assert record["log_presentation_format"] == "json_lines"
    assert record["log_presentation_profile"] == "mock"


def test_invocation_payload_preserves_existing_positional_contract() -> None:
    payload = InvocationEventPayload(
        10,
        "error",
        2,
        True,
        "success",
        "full",
        {"input": 3},
        4,
        "estimate",
        False,
        1.5,
        "full",
        "parse error",
        "provider",
        "execution",
        "adapter",
        "retry",
    )

    assert payload.provider_tokens == {"input": 3}
    assert payload.failure_advice == "retry"
    assert payload.provider_usage_report_count is None


def test_invocation_event_preserves_existing_positional_contract_and_round_trip() -> (
    None
):
    context = ExecutionEventContext(
        workflow_name="workflow",
        run_id="run-1",
        node_id="node.a",
        provider="codex",
        role=ProviderRole.EXECUTOR,
        task_id="codex_executor_0",
    )
    event = invocation_event(
        EventType.INVOCATION_FINISHED,
        "workflow",
        "run-1",
        context,
        10,
        "error",
        2,
        True,
        "success",
        "full",
        {"input": 3},
        4,
        "estimate",
        False,
        1.5,
        "full",
        "parse error",
        "provider",
        "execution",
        "adapter",
        "retry",
        12.5,
        "2026-08-04T00:00:00+00:00",
    )

    restored = event_from_record(execution_event_log_record(event))

    assert restored is not None
    assert restored.payload.provider_tokens == {"input": 3}
    assert restored.payload.failure_advice == "retry"
    assert restored.payload.provider_usage_report_count is None
    assert event.timestamp == 12.5
    assert restored.timestamp_utc == "2026-08-04T00:00:00+00:00"


def test_workspace_event_records_workspace_payload() -> None:
    event = workspace_event(
        EventType.WORKSPACE_CONTEXT_RECORDED,
        workflow_name="workflow",
        run_id="run-1",
        context=ExecutionEventContext(
            workflow_name="workflow",
            run_id="run-1",
            node_id="node.a",
            provider="alpha",
            role=ProviderRole.EXECUTOR,
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
        event_type=EventType.INVOCATION_STARTED,
        workflow_name="workflow",
        run_id="run-1",
        context=ExecutionEventContext(
            workflow_name="workflow",
            run_id="run-1",
            node_id="node.a",
            provider="alpha",
            role=ProviderRole.EXECUTOR,
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
                prompt_segments=[
                    PromptSegment(role=PromptSegmentRole.SHARED, content="run")
                ],
                providers=[ProviderSpec(provider="alpha")],
            )
        ],
    )
    state = build_initial_state(topology_from_workflow(workflow), run_id="run-1")

    apply_event(
        state,
        invocation_event(
            event_type=EventType.INVOCATION_STARTED,
            workflow_name="workflow",
            run_id="run-1",
            context=ExecutionEventContext(
                workflow_name="workflow",
                run_id="run-1",
                node_id="node.a",
                provider="alpha",
                role=ProviderRole.EXECUTOR,
                task_id="alpha_executor_0",
                log_presentation_format="json_lines",
                log_presentation_profile="mock",
            ),
        ),
    )
    apply_event(
        state,
        invocation_event(
            event_type=EventType.INVOCATION_FINISHED,
            workflow_name="workflow",
            run_id="run-1",
            context=ExecutionEventContext(
                workflow_name="workflow",
                run_id="run-1",
                node_id="node.a",
                provider="alpha",
                role=ProviderRole.EXECUTOR,
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


@pytest.mark.parametrize(
    ("event_type", "state_target", "expected_status"),
    [
        (EventType.WORKFLOW_STARTED, "workflow", "running"),
        (EventType.WORKFLOW_FINISHED, "workflow", "succeeded"),
        (EventType.WORKFLOW_FAILED, "workflow", "failed"),
        (EventType.WORKFLOW_CANCELLED, "workflow", "cancelled"),
        (EventType.NODE_STARTED, "node", "running"),
        (EventType.NODE_FINISHED, "node", "succeeded"),
        (EventType.NODE_FAILED, "node", "failed"),
        (EventType.NODE_BLOCKED, "node", "blocked"),
        (EventType.INVOCATION_STARTED, "invocation", "running"),
        (EventType.INVOCATION_FINISHED, "invocation", "succeeded"),
        (EventType.INVOCATION_FAILED, "invocation", "failed"),
    ],
)
def test_reducer_applies_every_lifecycle_transition(
    event_type: EventType,
    state_target: str,
    expected_status: str,
) -> None:
    state = _single_node_state()

    apply_event(state, _event_for_type(event_type))

    if state_target == "workflow":
        assert state.workflow_status == expected_status
    elif state_target == "node":
        assert state.nodes["node.a"].status == expected_status
    else:
        assert (
            state.nodes["node.a"].invocations["alpha_executor_0"].status
            == expected_status
        )


@pytest.mark.parametrize(
    "event_type",
    [EventType.WORKSPACE_CONTEXT_RECORDED, EventType.RUNTIME_LOG],
)
def test_reducer_preserves_status_for_non_lifecycle_events(
    event_type: EventType,
) -> None:
    state = _single_node_state()
    state.workflow_status = "running"
    state.nodes["node.a"].status = "running"

    apply_event(state, _event_for_type(event_type))

    assert state.workflow_status == "running"
    assert state.nodes["node.a"].status == "running"


def _single_node_state() -> RunDashboardState:
    workflow = WorkflowPlan(
        name="workflow",
        nodes=[
            WorkflowNode(
                id="node.a",
                mode="parallel",
                prompt_segments=[
                    PromptSegment(role=PromptSegmentRole.SHARED, content="run")
                ],
                providers=[ProviderSpec(provider="alpha")],
            )
        ],
    )
    return build_initial_state(topology_from_workflow(workflow), run_id="run-1")


def _event_for_type(event_type: EventType) -> ExecutionEvent:
    match event_type:
        case (
            EventType.WORKFLOW_STARTED
            | EventType.WORKFLOW_FINISHED
            | EventType.WORKFLOW_FAILED
            | EventType.WORKFLOW_CANCELLED
        ):
            return workflow_event(event_type, "workflow", "run-1")
        case (
            EventType.NODE_STARTED
            | EventType.NODE_FINISHED
            | EventType.NODE_FAILED
            | EventType.NODE_BLOCKED
        ):
            return node_event(event_type, "workflow", "run-1", "node.a")
        case (
            EventType.INVOCATION_STARTED
            | EventType.INVOCATION_FINISHED
            | EventType.INVOCATION_FAILED
        ):
            return invocation_event(
                event_type,
                "workflow",
                "run-1",
                ExecutionEventContext(
                    workflow_name="workflow",
                    run_id="run-1",
                    node_id="node.a",
                    provider="alpha",
                    role=ProviderRole.EXECUTOR,
                    task_id="alpha_executor_0",
                ),
            )
        case EventType.WORKSPACE_CONTEXT_RECORDED:
            return workspace_event(
                EventType.WORKSPACE_CONTEXT_RECORDED,
                "workflow",
                "run-1",
                ExecutionEventContext(
                    workflow_name="workflow",
                    run_id="run-1",
                    node_id="node.a",
                    task_id="alpha_executor_0",
                ),
                WorkspaceEventPayload(status="running"),
            )
        case EventType.RUNTIME_LOG:
            return runtime_log_event(
                "workflow",
                "run-1",
                level="info",
                message="message",
                operation="operation",
            )
