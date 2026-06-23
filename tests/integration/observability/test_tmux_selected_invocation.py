from __future__ import annotations

from pathlib import Path

from crewplane.core.workflow.models import (
    PromptSegment,
    ProviderSpec,
    WorkflowNode,
    WorkflowPlan,
)
from crewplane.observability.events import (
    apply_event,
    build_initial_state,
)
from crewplane.observability.tmux.selected_invocation import (
    prepare_selected_invocation,
)
from tests.helpers.observability import (
    make_execution_event,
    topology_from_workflow,
)


def test_selected_invocation_reports_unavailable_log_when_log_file_is_absent() -> None:
    state = state_with_invocation(log_file=None)

    prepared = prepare_selected_invocation(
        nodes=state.nodes,
        selected_node_id="node.a",
        pane_height=10,
        log_tail_lines=None,
        wall_time_now=100.0,
    )

    assert prepared is not None
    assert (
        prepared.log_unavailable_message == "Log file unavailable for this invocation."
    )


def test_selected_invocation_reports_missing_log_path(tmp_path: Path) -> None:
    missing_log = tmp_path / "missing.log"
    state = state_with_invocation(log_file=str(missing_log))

    prepared = prepare_selected_invocation(
        nodes=state.nodes,
        selected_node_id="node.a",
        pane_height=10,
        log_tail_lines=None,
        wall_time_now=100.0,
    )

    assert prepared is not None
    assert prepared.log_unavailable_message == f"Log file not found: {missing_log}"


def test_selected_invocation_prepares_log_tail_snapshot(tmp_path: Path) -> None:
    log_path = tmp_path / "node.log"
    log_path.write_text("header\n---\nline-1\nline-2\nline-3\n", encoding="utf-8")
    state = state_with_invocation(log_file=str(log_path))

    prepared = prepare_selected_invocation(
        nodes=state.nodes,
        selected_node_id="node.a",
        pane_height=2,
        log_tail_lines=2,
        wall_time_now=100.0,
    )

    assert prepared is not None
    assert prepared.log_snapshot is not None
    assert prepared.log_snapshot.tail_lines == ("line-2", "line-3")


def state_with_invocation(log_file: str | None):
    workflow = WorkflowPlan(
        name="selected.invocation",
        nodes=[
            WorkflowNode(
                id="node.a",
                mode="parallel",
                prompt_segments=[PromptSegment(role="shared", content="a")],
                providers=[ProviderSpec(provider="alpha")],
            )
        ],
    )
    state = build_initial_state(
        topology_from_workflow(workflow), run_id="selected-invocation"
    )
    apply_event(
        state,
        make_execution_event(
            event_type="invocation_started",
            workflow_name=workflow.name,
            run_id="selected-invocation",
            node_id="node.a",
            provider="alpha",
            role="executor",
            model="m",
            task_id="alpha_executor_0",
            round_num=1,
            log_file=log_file,
        ),
    )
    return state
