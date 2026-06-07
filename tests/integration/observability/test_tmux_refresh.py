from __future__ import annotations

from orchestrator_cli.core.workflow_models import (
    PromptSegment,
    ProviderSpec,
    WorkflowNode,
    WorkflowPlan,
)
from orchestrator_cli.observability.events import build_initial_state
from orchestrator_cli.observability.layout import compute_topology_layout
from orchestrator_cli.observability.tmux.refresh import StopReason
from orchestrator_cli.observability.types import (
    DashboardSnapshot,
    RunContext,
    RunResult,
)
from tests.helpers.observability import topology_from_workflow
from tests.integration.observability.tmux_fakes import SimulatedTmuxRuntime


def test_refresh_tick_is_noop_until_first_snapshot() -> None:
    runtime = started_runtime("refresh-no-snapshot")
    try:
        runtime.calls.clear()

        outcome = runtime.refresh_once()

        assert outcome.stop_reason is None
        assert not any(args[0] == "display-message" for args, _, _ in runtime.calls)
    finally:
        runtime.stop(RunResult(failed=False))


def test_quit_file_detection_requests_stop() -> None:
    runtime = started_runtime("refresh-quit")
    try:
        runtime.write_runtime_file("quit_requested", "1")

        outcome = runtime.refresh_once()

        assert outcome.stop_reason == StopReason.QUIT_REQUESTED
        assert runtime.stop_requested
    finally:
        runtime.stop(RunResult(failed=False))


def test_missing_session_detection_requests_stop() -> None:
    runtime = started_runtime("refresh-session-gone")
    try:
        runtime.client.session_exists_value = False

        outcome = runtime.refresh_once()

        assert outcome.stop_reason == StopReason.SESSION_GONE
        assert runtime.stop_requested
    finally:
        runtime.stop(RunResult(failed=False))


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
        runtime.stop(RunResult(failed=False))


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
