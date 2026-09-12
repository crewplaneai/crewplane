from __future__ import annotations

import json
from pathlib import Path

from crewplane.architecture.contracts import EventType
from crewplane.core.prompt_segments import PromptSegmentRole
from crewplane.core.workflow.keywords import ProviderRole
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
from crewplane.observability.tmux.rendering import (
    SelectedOutputRenderContext,
    render_selected_output,
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


def test_selected_invocation_preserves_command_lines_when_wrapping(
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "codex.log"
    log_path.write_text(
        json.dumps(
            {
                "type": "item.started",
                "item": {
                    "type": "command_execution",
                    "command": (
                        "python - <<'PY'\n"
                        "def example():\n"
                        "    print('ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789')\n"
                        "    print('next line')\n"
                        "PY"
                    ),
                    "status": "in_progress",
                },
            }
        ),
        encoding="utf-8",
    )
    state = state_with_invocation(log_file=str(log_path))
    invocation = next(iter(state.nodes["node.a"].invocations.values()))
    invocation.log_presentation_format = "json_lines"
    invocation.log_presentation_profile = "codex"
    prepared = prepare_selected_invocation(
        nodes=state.nodes,
        selected_node_id="node.a",
        pane_height=20,
        log_tail_lines=None,
        wall_time_now=100.0,
    )

    lines = render_selected_output(
        SelectedOutputRenderContext(
            nodes=state.nodes,
            selected_node_id="node.a",
            width=32,
            pane_height=20,
            log_tail_lines=None,
            quiet_after_seconds=120.0,
            monotonic_now=100.0,
            prepared_invocation=prepared,
        )
    )

    command_start = lines.index("command: python - <<'PY'")
    assert lines[command_start:] == [
        "command: python - <<'PY'",
        "  def example():",
        "      print('ABCDEFGHIJKLMNOPQRS",
        "TUVWXYZ0123456789')",
        "      print('next line')",
        "  PY",
    ]
    assert len(lines) <= 20
    assert all(len(line) <= 32 for line in lines)


def state_with_invocation(log_file: str | None):
    workflow = WorkflowPlan(
        name="selected.invocation",
        nodes=[
            WorkflowNode(
                id="node.a",
                mode="parallel",
                prompt_segments=[
                    PromptSegment(role=PromptSegmentRole.SHARED, content="a")
                ],
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
            event_type=EventType.INVOCATION_STARTED,
            workflow_name=workflow.name,
            run_id="selected-invocation",
            node_id="node.a",
            provider="alpha",
            role=ProviderRole.EXECUTOR,
            model="m",
            task_id="alpha_executor_0",
            round_num=1,
            log_file=log_file,
        ),
    )
    return state
