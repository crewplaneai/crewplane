from copy import deepcopy
from dataclasses import replace
from typing import cast

import pytest

from crewplane.architecture.contracts import (
    EventType,
    InvocationEventType,
    NodeEventType,
    TopologyNode,
    WorkflowTopology,
)
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.observability.events import (
    ExecutionEventContext,
    RunDashboardState,
    apply_event,
    build_initial_state,
    invocation_event,
    node_event,
    reducer,
)


@pytest.fixture
def state() -> RunDashboardState:
    return build_initial_state(
        WorkflowTopology("workflow", (TopologyNode("node", "parallel"),)), "run"
    )


def _context() -> ExecutionEventContext:
    return ExecutionEventContext(
        workflow_name="workflow",
        run_id="run",
        node_id="node",
        provider="alpha",
        role=ProviderRole.EXECUTOR,
        model="original-model",
        task_id="task",
    )


@pytest.mark.parametrize(
    ("event_type", "error", "expected_status", "expected_error", "label"),
    [
        (EventType.INVOCATION_FINISHED, "ignored", "succeeded", "old", "DONE task"),
        (
            EventType.INVOCATION_FAILED,
            None,
            "failed",
            None,
            "FAIL task{duration}: error",
        ),
        (EventType.INVOCATION_FAILED, "", "failed", "", "FAIL task{duration}: error"),
        (
            EventType.INVOCATION_FAILED,
            "x" * 60,
            "failed",
            "x" * 60,
            "FAIL task{duration}: " + "x" * 47 + "...",
        ),
    ],
)
@pytest.mark.parametrize(
    ("duration_ms", "duration_label"),
    [(None, ""), (0, " (0.0s)"), (61_500, " (1m01s)")],
)
def test_invocation_completion_preserves_timing_errors_and_messages(
    state: RunDashboardState,
    event_type: InvocationEventType,
    error: str | None,
    expected_status: str,
    expected_error: str | None,
    label: str,
    duration_ms: int | None,
    duration_label: str,
) -> None:
    apply_event(
        state,
        invocation_event(
            EventType.INVOCATION_STARTED, "workflow", "run", _context(), timestamp=2.0
        ),
    )
    invocation = state.nodes["node"].invocations["task"]
    invocation.error = "old"
    invocation.duration_ms = 99

    apply_event(
        state,
        invocation_event(
            event_type,
            "workflow",
            "run",
            _context(),
            duration_ms=duration_ms,
            error=error,
            timestamp=4.0,
        ),
    )

    assert state.nodes["node"].invocations["task"] is invocation
    assert invocation.status == expected_status
    assert invocation.started_at == 2.0
    assert invocation.finished_at == 4.0
    assert invocation.duration_ms == duration_ms
    assert invocation.error == expected_error
    message = (
        label + duration_label
        if event_type == EventType.INVOCATION_FINISHED
        else label.format(duration=duration_label)
    )
    assert list(state.nodes["node"].recent_events) == ["RUN task", message]


@pytest.mark.parametrize(
    ("event_type", "expected_status", "expected_finished_at", "prefix"),
    [
        (EventType.NODE_FAILED, "failed", 4.0, "FAIL"),
        (EventType.NODE_BLOCKED, "blocked", 1.0, "BLOCKED"),
    ],
)
def test_node_failure_and_blocking_keep_distinct_timing_and_unclipped_errors(
    state: RunDashboardState,
    event_type: NodeEventType,
    expected_status: str,
    expected_finished_at: float,
    prefix: str,
) -> None:
    node = state.nodes["node"]
    node.started_at = 0.0
    node.finished_at = 1.0

    apply_event(
        state,
        node_event(
            event_type, "workflow", "run", "node", error="x" * 100, timestamp=4.0
        ),
    )

    assert node.status == expected_status
    assert node.started_at == 0.0
    assert node.finished_at == expected_finished_at
    assert list(node.recent_events) == [f"{prefix} " + "x" * 100]


def test_require_invocation_merges_only_present_mutable_metadata(
    state: RunDashboardState,
) -> None:
    node = state.nodes["node"]
    context = replace(
        _context(),
        requested_reasoning="high",
        output_file="output.md",
        log_file="log.txt",
        log_presentation_format="json_lines",
        log_presentation_profile="alpha",
    )
    event = invocation_event(EventType.INVOCATION_STARTED, "workflow", "run", context)

    invocation = reducer.require_invocation(node, event)

    assert invocation.requested_reasoning == "high"
    assert invocation.output_file == "output.md"
    assert invocation.log_file == "log.txt"
    assert invocation.log_presentation_format == "json_lines"
    assert invocation.log_presentation_profile == "alpha"
    before = deepcopy(invocation)
    assert (
        reducer.require_invocation(node, replace(event, context=_context()))
        is invocation
    )
    assert invocation == before

    updated_context = replace(
        context,
        provider="beta",
        role=ProviderRole.REVIEWER,
        model="other-model",
        requested_reasoning="",
        output_file="",
        log_file="",
        log_presentation_format="plain",
        log_presentation_profile="",
    )
    updated_invocation = reducer.require_invocation(
        node, replace(event, context=updated_context)
    )
    assert updated_invocation is invocation
    assert updated_invocation.provider == "alpha"
    assert updated_invocation.role == ProviderRole.EXECUTOR
    assert updated_invocation.model == "original-model"
    assert updated_invocation.requested_reasoning == ""
    assert updated_invocation.output_file == ""
    assert updated_invocation.log_file == ""
    assert updated_invocation.log_presentation_format == "plain"
    assert updated_invocation.log_presentation_profile == ""


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize(
    ("presentation_format", "exception"),
    [("unsupported", ValueError), (cast(str, 1), TypeError)],
)
def test_invocation_metadata_validation_precedes_all_mutation(
    state: RunDashboardState,
    existing: bool,
    presentation_format: str,
    exception: type[Exception],
) -> None:
    event = invocation_event(
        EventType.INVOCATION_STARTED, "workflow", "run", _context()
    )
    if existing:
        apply_event(state, event)
    before = deepcopy(state)
    invalid_context = replace(
        _context(),
        log_presentation_format=presentation_format,
        requested_reasoning="changed",
        output_file="changed.md",
    )

    with pytest.raises(exception, match="log presentation format"):
        apply_event(state, replace(event, context=invalid_context))

    assert state == before


@pytest.mark.parametrize(
    ("event_type", "expected_status", "expected_error"),
    [
        (EventType.INVOCATION_FINISHED, "succeeded", "old"),
        (EventType.INVOCATION_FAILED, "failed", "new"),
    ],
)
def test_completion_mutations_precede_duration_formatting_failure(
    state: RunDashboardState,
    monkeypatch: pytest.MonkeyPatch,
    event_type: InvocationEventType,
    expected_status: str,
    expected_error: str,
) -> None:
    def fail_formatting(seconds: float) -> str:
        assert seconds == 1.0
        raise RuntimeError("formatting failed")

    node = state.nodes["node"]
    event = invocation_event(
        event_type,
        "workflow",
        "run",
        _context(),
        duration_ms=1000,
        error="new",
        timestamp=4.0,
    )
    invocation = reducer.require_invocation(node, event)
    invocation.error = "old"
    monkeypatch.setattr(reducer, "format_elapsed_seconds", fail_formatting)

    with pytest.raises(RuntimeError, match="formatting failed"):
        apply_event(state, event)

    assert invocation.status == expected_status
    assert invocation.finished_at == 4.0
    assert invocation.duration_ms == 1000
    assert invocation.error == expected_error
    assert list(node.recent_events) == []
