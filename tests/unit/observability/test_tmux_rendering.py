from __future__ import annotations

from pathlib import Path

from orchestrator_cli.core.workflow_models import (
    PromptSegment,
    ProviderSpec,
    WorkflowNode,
    WorkflowPlan,
)
from orchestrator_cli.observability.events import (
    apply_event,
    build_initial_state,
)
from orchestrator_cli.observability.layout import compute_topology_layout
from orchestrator_cli.observability.text_layout import display_width
from orchestrator_cli.observability.tmux.log_tail import LogSnapshot
from orchestrator_cli.observability.tmux.rendering import (
    PreparedSelectedInvocation,
    SelectedOutputRenderContext,
    render_left_dashboard,
    render_selected_output,
    viewport_dag_lines,
)
from orchestrator_cli.observability.types import DashboardSnapshot
from tests.helpers.observability import (
    make_execution_event,
    topology_from_workflow,
)

FIXTURES = Path(__file__).with_name("fixtures") / "compact_render"


def provider(name: str, role: str = "executor") -> ProviderSpec:
    return ProviderSpec(provider=name, role=role)


def _chain_snapshot() -> DashboardSnapshot:
    workflow = WorkflowPlan(
        name="compact.chain",
        nodes=[
            WorkflowNode(
                id="node.a",
                mode="parallel",
                prompt_segments=[PromptSegment(role="shared", content="a")],
                providers=[provider("alpha")],
            ),
            WorkflowNode(
                id="node.b",
                mode="parallel",
                prompt_segments=[PromptSegment(role="shared", content="b")],
                needs=["node.a"],
                providers=[provider("beta")],
            ),
            WorkflowNode(
                id="node.c",
                mode="parallel",
                prompt_segments=[PromptSegment(role="shared", content="c")],
                needs=["node.b"],
                providers=[provider("gamma")],
            ),
            WorkflowNode(
                id="node.d",
                mode="parallel",
                prompt_segments=[PromptSegment(role="shared", content="d")],
                needs=["node.c"],
                providers=[provider("delta")],
            ),
        ],
    )
    state = build_initial_state(topology_from_workflow(workflow), run_id="run-chain")
    return DashboardSnapshot(
        state=state,
        layout=compute_topology_layout(topology_from_workflow(workflow)),
        now=0.0,
    )


def _running_output_context() -> SelectedOutputRenderContext:
    workflow = WorkflowPlan(
        name="compact.output",
        nodes=[
            WorkflowNode(
                id="node.a",
                mode="parallel",
                prompt_segments=[PromptSegment(role="shared", content="x")],
                providers=[provider("alpha")],
            )
        ],
    )
    state = build_initial_state(topology_from_workflow(workflow), run_id="run-output")
    apply_event(
        state,
        make_execution_event(
            event_type="node_started",
            workflow_name=workflow.name,
            run_id="run-output",
            node_id="node.a",
            timestamp=10.0,
        ),
    )
    apply_event(
        state,
        make_execution_event(
            event_type="invocation_started",
            workflow_name=workflow.name,
            run_id="run-output",
            node_id="node.a",
            provider="alpha",
            role="executor",
            model="m",
            task_id="alpha_executor_0",
            round_num=1,
            timestamp=10.0,
        ),
    )
    invocation = next(iter(state.nodes["node.a"].invocations.values()))
    return SelectedOutputRenderContext(
        nodes=state.nodes,
        selected_node_id="node.a",
        width=40,
        pane_height=8,
        log_tail_lines=None,
        quiet_after_seconds=120.0,
        monotonic_now=25.0,
        prepared_invocation=PreparedSelectedInvocation(
            invocation=invocation,
            log_snapshot=LogSnapshot(
                size_bytes=2048,
                updated_age_seconds=5.0,
                tail_lines=(
                    "short-1",
                    "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcdefghijklmnop",
                    "short-2",
                ),
            ),
        ),
    )


def test_viewport_height_two_prefers_below_marker_on_tie() -> None:
    expected = (
        (FIXTURES / "height_two_below" / "expected-left.txt")
        .read_text(encoding="utf-8")
        .rstrip("\n")
    )
    assert (
        "\n".join(
            viewport_dag_lines(["row-1", "row-2", "row-3", "row-4", "row-5"], 2, 1)
        )
        == expected
    )


def test_viewport_height_two_prefers_larger_hidden_side_above() -> None:
    expected = (
        (FIXTURES / "height_two_above" / "expected-left.txt")
        .read_text(encoding="utf-8")
        .rstrip("\n")
    )
    assert (
        "\n".join(
            viewport_dag_lines(["row-1", "row-2", "row-3", "row-4", "row-5"], 2, 3)
        )
        == expected
    )


def test_render_left_dashboard_matches_exact_golden() -> None:
    rendered = "\n".join(
        render_left_dashboard(
            snapshot=_chain_snapshot(),
            selected_node_id="node.c",
            width=80,
            height=7,
            inspect_mode=False,
            now=0.0,
        )
    )
    expected = (
        (FIXTURES / "selected_middle" / "expected-left.txt")
        .read_text(encoding="utf-8")
        .rstrip("\n")
    )
    assert rendered == expected


def test_render_left_dashboard_tiny_height_fits_requested_width() -> None:
    lines = render_left_dashboard(
        snapshot=_chain_snapshot(),
        selected_node_id="node.c",
        width=5,
        height=2,
        inspect_mode=False,
        now=0.0,
    )

    assert len(lines) == 2
    assert all(display_width(line) <= 5 for line in lines)


def test_render_selected_output_matches_exact_golden() -> None:
    rendered = "\n".join(render_selected_output(_running_output_context()))
    expected = (
        (FIXTURES / "selected_output" / "expected-right.txt")
        .read_text(encoding="utf-8")
        .rstrip("\n")
    )
    assert rendered == expected
