from __future__ import annotations

from pathlib import Path

import pytest

from crewplane.artifacts import OutputManager
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.observability.events import EventType
from crewplane.observability.events.dashboard_state import (
    InvocationRuntimeState,
    NodeRuntimeState,
    RunDashboardState,
)
from crewplane.observability.layout import TopologyLayout
from crewplane.observability.run_summary.accumulator import RunSummaryAccumulator
from crewplane.observability.run_summary.builder import build_run_summary
from crewplane.observability.types import DashboardSnapshot, RunResult
from tests.helpers.observability import make_execution_event


def snapshot_for(nodes: tuple[NodeRuntimeState, ...]) -> DashboardSnapshot:
    order = {node.node_id: index for index, node in enumerate(nodes)}
    state = RunDashboardState(
        "flow",
        "run",
        order,
        workflow_status="succeeded",
        workflow_started_at=10.0,
        workflow_finished_at=20.0,
        nodes={node.node_id: node for node in nodes},
    )
    return DashboardSnapshot(state, TopologyLayout((), {}, 0, order, {}, {}), 1000.0)


def test_builder_keeps_missing_state_distinct_from_known_zero(tmp_path: Path) -> None:
    output = OutputManager("flow", base_dir=tmp_path)
    missing = build_run_summary(
        output, None, [], RunResult(status="failed"), "flow", "run"
    )
    assert missing.node_counts is None
    assert missing.invocation_count is None
    assert missing.elapsed_seconds is None
    empty = build_run_summary(
        output, snapshot_for(()), [], RunResult(status="succeeded"), "flow", "run"
    )
    assert empty.node_counts is not None
    assert empty.node_counts.succeeded == 0
    assert empty.invocation_count == 0
    assert empty.spend is None
    unstarted = snapshot_for(())
    unstarted.state.workflow_started_at = None
    assert (
        build_run_summary(
            output, unstarted, [], RunResult(status="failed"), "flow", "run"
        ).elapsed_seconds
        is None
    )


def test_builder_preserves_statuses_numeric_timing_and_complete_calls(
    tmp_path: Path,
) -> None:
    nodes = tuple(
        NodeRuntimeState(
            status,
            "sequential",
            (),
            status=status,
            started_at=10.0,
            finished_at=15.0 if status not in {"pending", "running"} else None,
        )
        for status in (
            "succeeded",
            "failed",
            "cancelled",
            "blocked",
            "pending",
            "running",
        )
    )
    for index, duration in enumerate((12_345, 0, None, -1)):
        nodes[0].invocations[str(index)] = InvocationRuntimeState(
            str(index),
            "provider",
            ProviderRole.EXECUTOR,
            "recorded-model" if index == 0 else None,
            1,
            index,
            status="cancelled" if index == 0 else "succeeded",
            duration_ms=duration,
        )
    summary = build_run_summary(
        OutputManager("flow", base_dir=tmp_path),
        snapshot_for(nodes),
        [],
        RunResult(status="succeeded"),
        "flow",
        "run",
    )
    assert summary.elapsed_seconds == 10
    assert summary.node_counts is not None
    assert (
        summary.node_counts.succeeded,
        summary.node_counts.failed,
        summary.node_counts.cancelled,
        summary.node_counts.blocked,
        summary.node_counts.pending,
        summary.node_counts.running,
    ) == (1, 1, 1, 1, 1, 1)
    assert [node.duration_seconds for node in summary.node_outcomes] == [
        5,
        5,
        5,
        5,
        None,
        None,
    ]
    assert summary.invocation_count == 4
    assert [row.duration_ms for row in summary.invocation_timings] == [
        12345,
        0,
        None,
        None,
    ]
    assert summary.invocation_timings[0].status == "cancelled"
    assert summary.invocation_timings[0].model == "recorded-model"
    assert summary.invocation_timings[1].model is None
    assert summary.spend is None


@pytest.mark.parametrize(
    "operation, attributes",
    [
        ("review_loop_consensus_exhausted", {"continued": True}),
        ("review_loop_stopped", {"continued": True, "stop_reason": "no_progress"}),
    ],
)
def test_resume_and_unresolved_review_facts_survive_event_eviction(
    tmp_path: Path, operation: str, attributes: dict[str, object]
) -> None:
    accumulator = RunSummaryAccumulator()
    for node_id, resume_operation in (
        ("reused", "node_resumed"),
        ("checkpoint", "review_checkpoint_resumed"),
    ):
        accumulator.record(
            make_execution_event(
                EventType.RUNTIME_LOG,
                workflow_name="flow",
                run_id="run",
                node_id=node_id,
                operation=resume_operation,
                level="info",
                message="restored",
            )
        )
    accumulator.record(
        make_execution_event(
            EventType.RUNTIME_LOG,
            workflow_name="flow",
            run_id="run",
            operation=operation,
            level="warning",
            message="unresolved",
            attributes=attributes,
        )
    )
    events = [
        make_execution_event(
            EventType.RUNTIME_LOG,
            workflow_name="flow",
            run_id="run",
            level="info",
            message="later",
            operation="later",
        )
        for _ in range(2001)
    ]
    for event in events:
        accumulator.record(event)
    nodes = tuple(
        NodeRuntimeState(
            name,
            "sequential",
            (),
            status="succeeded",
            started_at=10.0,
            finished_at=15.0,
        )
        for name in ("reused", "checkpoint", "input")
    )
    summary = build_run_summary(
        OutputManager("flow", base_dir=tmp_path),
        snapshot_for(nodes),
        events[-2000:],
        RunResult(status="succeeded"),
        "flow",
        "run",
        dropped_event_count=4,
        summary_facts=accumulator.snapshot(),
    )
    assert summary.review_consensus_unresolved
    assert summary.workflow_status == "succeeded"
    assert [
        (node.resume_kind, node.duration_seconds) for node in summary.node_outcomes
    ] == [("reused", None), ("checkpoint", 5.0), (None, 5.0)]
    assert summary.invocation_count == 0
    assert "earlier event(s)" in summary.issues[-1].message


@pytest.mark.parametrize(
    "attributes",
    [
        {"continued": False, "stop_reason": "no_progress"},
        {"continued": True, "stop_reason": "cancelled"},
        {},
    ],
)
def test_unresolved_review_is_not_inferred(attributes: dict[str, object]) -> None:
    accumulator = RunSummaryAccumulator()
    accumulator.record(
        make_execution_event(
            EventType.RUNTIME_LOG,
            workflow_name="flow",
            run_id="run",
            operation="review_loop_stopped",
            level="info",
            message="stopped",
            attributes=attributes,
        )
    )
    assert not accumulator.snapshot().review_consensus_unresolved


def test_retention_warning_cannot_displace_actual_failures(tmp_path: Path) -> None:
    event = make_execution_event(
        EventType.WORKFLOW_FAILED,
        workflow_name="flow",
        run_id="run",
        error="actual failure",
    )
    summary = build_run_summary(
        OutputManager("flow", base_dir=tmp_path),
        None,
        [event],
        RunResult(status="failed"),
        "flow",
        "run",
        dropped_event_count=2000,
    )
    assert summary.issues[0].level == "error"
    assert "actual failure" in summary.issues[0].message
    assert summary.issues[-1].level == "warning"


@pytest.mark.parametrize(
    "start, finish",
    [
        (None, 20.0),
        (10.0, None),
        (20.0, 10.0),
        (float("nan"), 20.0),
        (10.0, float("inf")),
    ],
)
def test_missing_or_invalid_stage_timing_is_unknown(
    tmp_path: Path, start: float | None, finish: float | None
) -> None:
    node = NodeRuntimeState(
        "node",
        "sequential",
        (),
        status="succeeded",
        started_at=start,
        finished_at=finish,
    )
    summary = build_run_summary(
        OutputManager("flow", base_dir=tmp_path),
        snapshot_for((node,)),
        [],
        RunResult(status="succeeded"),
        "flow",
        "run",
    )
    assert summary.node_outcomes[0].duration_seconds is None


def test_complete_timings_and_counts_are_independent_of_usage_retention(
    tmp_path: Path,
) -> None:
    node = NodeRuntimeState(
        "node", "parallel", (), status="succeeded", started_at=10.0, finished_at=20.0
    )
    events = []
    for index in range(205):
        task = f"task-{index}"
        node.invocations[task] = InvocationRuntimeState(
            task,
            "provider",
            ProviderRole.EXECUTOR,
            None,
            None,
            None,
            status="succeeded",
            duration_ms=index * 1000,
        )
        events.append(
            make_execution_event(
                EventType.INVOCATION_FINISHED,
                workflow_name="flow",
                run_id="run",
                node_id="node",
                task_id=task,
                provider="provider",
                role="executor",
                attempt_count=1,
            )
        )
    summary = build_run_summary(
        OutputManager("flow", base_dir=tmp_path),
        snapshot_for((node,)),
        events,
        RunResult(status="succeeded"),
        "flow",
        "run",
    )
    assert summary.invocation_count == len(summary.invocation_timings) == 205
    assert len(summary.invocation_usages) == 200
    assert summary.omitted_invocation_usage_count == 5
    assert summary.spend is not None
    assert summary.spend.terminal_invocations == summary.spend.total_attempts == 205
    assert summary.spend.visible_estimate_tokens is None
    assert summary.elapsed_seconds == 10.0
    assert (
        sum(timing.duration_ms or 0 for timing in summary.invocation_timings) / 1000
        > summary.elapsed_seconds
    )
