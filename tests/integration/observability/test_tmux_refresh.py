from __future__ import annotations

import json

from crewplane.core.workflow.models import (
    PromptSegment,
    ProviderSpec,
    WorkflowNode,
    WorkflowPlan,
)
from crewplane.observability.events import apply_event, build_initial_state
from crewplane.observability.layout import compute_topology_layout
from crewplane.observability.tmux.refresh import StopReason
from crewplane.observability.types import (
    DashboardSnapshot,
    RunContext,
    RunResult,
)
from tests.helpers.observability import make_execution_event, topology_from_workflow
from tests.integration.observability.tmux_fakes import SimulatedTmuxRuntime


def test_refresh_tick_is_noop_until_first_snapshot() -> None:
    runtime = started_runtime("refresh-no-snapshot")
    try:
        runtime.calls.clear()

        outcome = runtime.refresh_once()

        assert outcome.stop_reason is None
        assert not any(args[0] == "display-message" for args, _, _ in runtime.calls)
    finally:
        runtime.stop(RunResult(status="succeeded"))


def test_quit_file_detection_requests_stop() -> None:
    runtime = started_runtime("refresh-quit")
    try:
        runtime.write_runtime_file("quit_requested", "1")

        outcome = runtime.refresh_once()

        assert outcome.stop_reason == StopReason.QUIT_REQUESTED
        assert runtime.stop_requested
    finally:
        runtime.stop(RunResult(status="succeeded"))


def test_missing_session_detection_requests_stop() -> None:
    runtime = started_runtime("refresh-session-gone")
    try:
        runtime.client.session_exists_value = False

        outcome = runtime.refresh_once()

        assert outcome.stop_reason == StopReason.SESSION_GONE
        assert runtime.stop_requested
    finally:
        runtime.stop(RunResult(status="succeeded"))


def test_pane_dimension_timeout_uses_fallback_without_stopping() -> None:
    workflow = workflow_plan()
    runtime = started_runtime("refresh-dimension-timeout")
    try:
        runtime.client.display_message_times_out = True
        runtime.on_snapshot(
            None,
            DashboardSnapshot(
                state=build_initial_state(
                    topology_from_workflow(workflow), run_id="refresh-dimension-timeout"
                ),
                layout=compute_topology_layout(topology_from_workflow(workflow)),
                now=0.0,
            ),
        )

        outcome = runtime.refresh_once()

        assert outcome.stop_reason is None
        assert not runtime.stop_requested
        assert "DAG Summary" in runtime.runtime_files.left_content.read_text(
            encoding="utf-8"
        )
    finally:
        runtime.stop(RunResult(status="succeeded"))


def test_refresh_writes_selected_invocation_snapshot() -> None:
    workflow = workflow_plan()
    runtime = started_runtime("refresh-selected-json")
    try:
        state = build_initial_state(
            topology_from_workflow(workflow),
            run_id="refresh-selected-json",
        )
        apply_event(
            state,
            make_execution_event(
                event_type="invocation_started",
                workflow_name=workflow.name,
                run_id="refresh-selected-json",
                node_id="node.a",
                provider="alpha",
                role="executor",
                task_id="alpha_executor_0",
                round_num=1,
                log_file="/tmp/provider.log",
                output_file="/tmp/output.md",
                log_presentation_format="json_lines",
                log_presentation_profile="mock",
            ),
        )
        runtime.on_snapshot(
            None,
            DashboardSnapshot(
                state=state,
                layout=compute_topology_layout(topology_from_workflow(workflow)),
                now=0.0,
            ),
        )

        runtime.refresh_once()

        snapshot = json.loads(
            runtime.runtime_files.selected_invocation.read_text(encoding="utf-8")
        )
        assert snapshot["schema_version"] == 1
        assert snapshot["workflow_name"] == workflow.name
        assert snapshot["selection_generation"] == 0
        assert snapshot["requested_selected_index"] == -1
        assert snapshot["resolved_selected_index"] == 0
        assert snapshot["node_id"] == "node.a"
        assert snapshot["log_file"] == "/tmp/provider.log"
        assert snapshot["log_presentation_format"] == "json_lines"
        assert snapshot["log_presentation_profile"] == "mock"

        stale_names = [
            "selected-index.txt",
            "selected-node-id.txt",
            "selected-log-path.txt",
            "inspect-log-path.txt",
            "inspect-node-id.txt",
        ]
        assert not any(
            (runtime.runtime_files.root / name).exists() for name in stale_names
        )
    finally:
        runtime.stop(RunResult(status="succeeded"))


def started_runtime(run_id: str) -> SimulatedTmuxRuntime:
    runtime = SimulatedTmuxRuntime(auto_close_session=True)
    runtime.start(
        RunContext(
            workflow_topology=topology_from_workflow(workflow_plan()),
            run_id=run_id,
            refresh_per_second=0,
        )
    )
    return runtime


def workflow_plan() -> WorkflowPlan:
    return WorkflowPlan(
        name="refresh.workflow",
        nodes=[
            WorkflowNode(
                id="node.a",
                mode="parallel",
                prompt_segments=[PromptSegment(role="shared", content="a")],
                providers=[ProviderSpec(provider="alpha")],
            )
        ],
    )
