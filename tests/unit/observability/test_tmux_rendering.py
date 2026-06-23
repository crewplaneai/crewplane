from __future__ import annotations

from dataclasses import replace
from pathlib import Path

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
from crewplane.observability.layout import compute_topology_layout
from crewplane.observability.log_presentation import (
    LogPresentationNotice,
    LogPresentationSnapshot,
)
from crewplane.observability.text_layout import display_width
from crewplane.observability.tmux.log_tail import LogSnapshot
from crewplane.observability.tmux.rendering import (
    CompactDashboardRenderContext,
    SelectedOutputRenderContext,
    render_compact_dashboard,
    render_left_dashboard,
    render_selected_output,
    viewport_dag_lines,
)
from crewplane.observability.tmux.selected_invocation import (
    PreparedSelectedInvocation,
)
from crewplane.observability.types import DashboardSnapshot
from tests.helpers.observability import (
    make_execution_event,
    topology_from_workflow,
)
from tests.helpers.render_fixtures import read_render_fixture

FIXTURES = Path(__file__).with_name("fixtures") / "compact_render"


def provider(name: str, role: ProviderRole = ProviderRole.EXECUTOR) -> ProviderSpec:
    return ProviderSpec(provider=name, role=role)


def _chain_snapshot() -> DashboardSnapshot:
    workflow = WorkflowPlan(
        name="compact.chain",
        nodes=[
            WorkflowNode(
                id="node.a",
                mode="parallel",
                prompt_segments=[
                    PromptSegment(role=PromptSegmentRole.SHARED, content="a")
                ],
                providers=[provider("alpha")],
            ),
            WorkflowNode(
                id="node.b",
                mode="parallel",
                prompt_segments=[
                    PromptSegment(role=PromptSegmentRole.SHARED, content="b")
                ],
                needs=["node.a"],
                providers=[provider("beta")],
            ),
            WorkflowNode(
                id="node.c",
                mode="parallel",
                prompt_segments=[
                    PromptSegment(role=PromptSegmentRole.SHARED, content="c")
                ],
                needs=["node.b"],
                providers=[provider("gamma")],
            ),
            WorkflowNode(
                id="node.d",
                mode="parallel",
                prompt_segments=[
                    PromptSegment(role=PromptSegmentRole.SHARED, content="d")
                ],
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
                prompt_segments=[
                    PromptSegment(role=PromptSegmentRole.SHARED, content="x")
                ],
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
            role=ProviderRole.EXECUTOR,
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


def _compact_fixture(case_id: str, artifact_name: str) -> str:
    return read_render_fixture(FIXTURES, case_id, artifact_name)


def _prepared_invocation(
    context: SelectedOutputRenderContext,
) -> PreparedSelectedInvocation:
    prepared_invocation = context.prepared_invocation
    if prepared_invocation is None:
        raise AssertionError("test context must have a prepared invocation")
    return prepared_invocation


def test_viewport_height_two_prefers_below_marker_on_tie() -> None:
    expected = _compact_fixture("height_two_below", "expected-left.txt")
    assert (
        "\n".join(
            viewport_dag_lines(["row-1", "row-2", "row-3", "row-4", "row-5"], 2, 1)
        )
        == expected
    )


def test_viewport_height_two_prefers_larger_hidden_side_above() -> None:
    expected = _compact_fixture("height_two_above", "expected-left.txt")
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
    expected = _compact_fixture("selected_middle", "expected-left.txt")
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
    expected = _compact_fixture("selected_output", "expected-right.txt")
    assert rendered == expected


def test_render_selected_output_clips_to_final_height() -> None:
    context = replace(_running_output_context(), pane_height=5)

    rendered = "\n".join(render_selected_output(context))

    assert rendered == _compact_fixture("selected_output_clipped", "expected-right.txt")


def test_render_selected_output_zero_height_returns_no_rows() -> None:
    context = replace(_running_output_context(), pane_height=0)

    rendered = "\n".join(render_selected_output(context))

    assert rendered == _compact_fixture(
        "selected_output_zero_height", "expected-right.txt"
    )


def test_render_selected_output_one_height_returns_notice_only() -> None:
    context = replace(_running_output_context(), pane_height=1)

    rendered = "\n".join(render_selected_output(context))

    assert rendered == _compact_fixture(
        "selected_output_one_height", "expected-right.txt"
    )


def test_render_selected_output_quiet_running_matches_exact_golden() -> None:
    context = _running_output_context()
    prepared_invocation = _prepared_invocation(context)
    quiet_context = replace(
        context,
        width=80,
        pane_height=16,
        prepared_invocation=PreparedSelectedInvocation(
            invocation=prepared_invocation.invocation,
            log_snapshot=LogSnapshot(
                size_bytes=2048,
                updated_age_seconds=130.0,
                tail_lines=("quiet-tail-line",),
            ),
        ),
    )

    rendered = "\n".join(render_selected_output(quiet_context))

    assert rendered == _compact_fixture("quiet_running", "expected-right.txt")


def test_render_selected_output_empty_log_matches_exact_golden() -> None:
    context = _running_output_context()
    prepared_invocation = _prepared_invocation(context)
    prepared_invocation.invocation.status = "succeeded"
    empty_log_context = replace(
        context,
        prepared_invocation=PreparedSelectedInvocation(
            invocation=prepared_invocation.invocation,
            log_snapshot=LogSnapshot(
                size_bytes=0,
                updated_age_seconds=1.0,
                tail_lines=(),
            ),
        ),
    )

    rendered = "\n".join(render_selected_output(empty_log_context))

    assert rendered == _compact_fixture("empty_log", "expected-right.txt")


def test_render_selected_output_unavailable_log_matches_exact_golden() -> None:
    context = _running_output_context()
    prepared_invocation = _prepared_invocation(context)
    unavailable_context = replace(
        context,
        prepared_invocation=PreparedSelectedInvocation(
            invocation=prepared_invocation.invocation,
            log_unavailable_message="Log file not found: /tmp/missing-provider.log",
        ),
    )

    rendered = "\n".join(render_selected_output(unavailable_context))

    assert rendered == _compact_fixture("unavailable_log", "expected-right.txt")


def test_render_selected_output_presentation_notice_matches_exact_golden() -> None:
    context = _running_output_context()
    prepared_invocation = _prepared_invocation(context)
    presentation_context = replace(
        context,
        pane_height=12,
        prepared_invocation=PreparedSelectedInvocation(
            invocation=prepared_invocation.invocation,
            log_snapshot=LogSnapshot(
                size_bytes=1024,
                updated_age_seconds=2.0,
                tail_lines=("raw provider line",),
            ),
            presentation_snapshot=LogPresentationSnapshot(
                size_bytes=1024,
                updated_age_seconds=2.0,
                notices=(
                    LogPresentationNotice(
                        level="warning",
                        message="Skipped malformed JSON log line.",
                    ),
                ),
                lines=("formatted provider message",),
            ),
        ),
    )

    rendered = "\n".join(render_selected_output(presentation_context))

    assert rendered == _compact_fixture("presentation_notice", "expected-right.txt")


def test_render_compact_dashboard_matches_exact_golden() -> None:
    rendered = render_compact_dashboard(
        CompactDashboardRenderContext(
            snapshot=_chain_snapshot(),
            selected_node_id="node.c",
            inspect_mode=False,
            left_width=80,
            left_height=7,
            right_width=40,
            right_height=6,
            monotonic_now=0.0,
            quiet_after_seconds=120.0,
            log_tail_lines=None,
        )
    )

    assert "\n".join(rendered.left_lines) == _compact_fixture(
        "pure_dashboard",
        "expected-left.txt",
    )
    assert rendered.right_lines is not None
    assert "\n".join(rendered.right_lines) == _compact_fixture(
        "pure_dashboard",
        "expected-right.txt",
    )
