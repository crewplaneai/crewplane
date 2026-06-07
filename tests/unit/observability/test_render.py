import unittest
from time import monotonic

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
from orchestrator_cli.observability.render import RenderConfig, render_dashboard_text
from orchestrator_cli.observability.text_layout import display_width
from tests.helpers.observability import (
    make_execution_event,
    topology_from_workflow,
)


def provider(name: str) -> ProviderSpec:
    return ProviderSpec(provider=name)


def _build_sample_workflow() -> WorkflowPlan:
    return WorkflowPlan(
        name="render.sample",
        nodes=[
            WorkflowNode(
                id="node1",
                mode="parallel",
                prompt_segments=[PromptSegment(role="shared", content="a")],
                providers=[provider("p")],
            ),
            WorkflowNode(
                id="node2",
                mode="parallel",
                prompt_segments=[PromptSegment(role="shared", content="b")],
                providers=[provider("p")],
            ),
            WorkflowNode(
                id="node3",
                mode="parallel",
                prompt_segments=[PromptSegment(role="shared", content="c")],
                providers=[provider("p")],
            ),
            WorkflowNode(
                id="node5",
                mode="sequential",
                prompt_segments=[PromptSegment(role="shared", content="merge")],
                needs=["node2", "node3"],
                providers=[ProviderSpec(provider="p", role="executor")],
            ),
        ],
    )


class DashboardRenderTests(unittest.TestCase):
    def test_render_config_rejects_invalid_lane_width(self) -> None:
        with self.assertRaisesRegex(ValueError, "min_lane_width"):
            RenderConfig(min_lane_width=0, divider="")

    def test_shared_divider_output_shape(self) -> None:
        workflow = _build_sample_workflow()
        state = build_initial_state(topology_from_workflow(workflow), run_id="run-1")
        layout = compute_topology_layout(topology_from_workflow(workflow))
        apply_event(
            state,
            make_execution_event(
                event_type="workflow_started",
                workflow_name=workflow.name,
                run_id="run-1",
            ),
        )

        rendered = render_dashboard_text(
            state=state,
            layout=layout,
            width=120,
            height=40,
            now=monotonic(),
        )
        self.assertIn(" │ ", rendered)
        self.assertNotIn("┌", rendered)
        self.assertNotIn("┐", rendered)

    def test_span_width_keeps_full_row_width(self) -> None:
        workflow = _build_sample_workflow()
        state = build_initial_state(topology_from_workflow(workflow), run_id="run-1")
        layout = compute_topology_layout(topology_from_workflow(workflow))
        apply_event(
            state,
            make_execution_event(
                event_type="node_finished",
                workflow_name=workflow.name,
                run_id="run-1",
                node_id="node1",
            ),
        )
        apply_event(
            state,
            make_execution_event(
                event_type="node_started",
                workflow_name=workflow.name,
                run_id="run-1",
                node_id="node5",
            ),
        )

        width = 96
        rendered = render_dashboard_text(
            state=state,
            layout=layout,
            width=width,
            height=40,
            now=monotonic(),
        )
        for line in rendered.splitlines():
            self.assertLessEqual(display_width(line), width)
        self.assertIn("node5 [sequential]", rendered)

    def test_sticky_lane_renders_for_empty_lane_in_following_wave(self) -> None:
        workflow = _build_sample_workflow()
        state = build_initial_state(
            topology_from_workflow(workflow), run_id="run-sticky"
        )
        layout = compute_topology_layout(topology_from_workflow(workflow))

        rendered = render_dashboard_text(
            state=state,
            layout=layout,
            width=120,
            height=40,
            now=monotonic(),
            config=RenderConfig(display_mode="waves"),
        )
        self.assertIn("Wave 2", rendered)
        self.assertIn("node5 [sequential]", rendered)
        self.assertIn("node1 [sticky]", rendered)

    def test_sticky_lane_uses_collapsed_wave_context(self) -> None:
        workflow = WorkflowPlan(
            name="sticky.collapsed",
            nodes=[
                WorkflowNode(
                    id="a",
                    mode="parallel",
                    prompt_segments=[PromptSegment(role="shared", content="a")],
                    providers=[provider("p")],
                ),
                WorkflowNode(
                    id="b",
                    mode="parallel",
                    prompt_segments=[PromptSegment(role="shared", content="b")],
                    providers=[provider("p")],
                ),
                WorkflowNode(
                    id="c",
                    mode="sequential",
                    prompt_segments=[PromptSegment(role="shared", content="c")],
                    needs=["b"],
                    providers=[provider("p")],
                ),
                WorkflowNode(
                    id="d",
                    mode="sequential",
                    prompt_segments=[PromptSegment(role="shared", content="d")],
                    needs=["c"],
                    providers=[provider("p")],
                ),
            ],
        )
        state = build_initial_state(
            topology_from_workflow(workflow), run_id="run-collapsed-sticky"
        )
        layout = compute_topology_layout(topology_from_workflow(workflow))
        apply_event(
            state,
            make_execution_event(
                event_type="node_started",
                workflow_name=workflow.name,
                run_id="run-collapsed-sticky",
                node_id="d",
            ),
        )

        rendered = render_dashboard_text(
            state=state,
            layout=layout,
            width=120,
            height=9,
            now=monotonic(),
            config=RenderConfig(display_mode="waves"),
        )
        self.assertIn("Wave 3", rendered)
        self.assertIn("a [sticky]", rendered)
        self.assertIn("d [sequential]", rendered)

    def test_unicode_cell_content_preserves_lane_overlay_alignment(self) -> None:
        workflow = WorkflowPlan(
            name="unicode.overlay",
            nodes=[
                WorkflowNode(
                    id="emoji🙂",
                    mode="parallel",
                    prompt_segments=[PromptSegment(role="shared", content="a")],
                    providers=[provider("p")],
                ),
                WorkflowNode(
                    id="漢字",
                    mode="parallel",
                    prompt_segments=[PromptSegment(role="shared", content="b")],
                    providers=[provider("p")],
                ),
            ],
        )
        state = build_initial_state(
            topology_from_workflow(workflow), run_id="run-unicode-overlay"
        )
        layout = compute_topology_layout(topology_from_workflow(workflow))
        rendered = render_dashboard_text(
            state=state,
            layout=layout,
            width=72,
            height=12,
            now=monotonic(),
            node_live_lines={
                "emoji🙂": ["e\u0301cho status"],
                "漢字": ["混合🙂 output"],
            },
            config=RenderConfig(display_mode="waves", stream_lines_per_node=1),
        )

        lines = rendered.splitlines()
        self.assertTrue(
            any(
                "emoji🙂 [parallel]" in line and "漢字 [parallel]" in line
                for line in lines
            )
        )
        self.assertTrue(
            any(
                "e\u0301cho status" in line and "混合🙂 output" in line
                for line in lines
            )
        )
        self.assertTrue(
            any("emoji🙂 [parallel]" in line and " │ " in line for line in lines)
        )
        for line in lines:
            self.assertLessEqual(display_width(line), 72)

    def test_narrow_terminal_uses_overflow_footer(self) -> None:
        workflow = WorkflowPlan(
            name="overflow",
            nodes=[
                WorkflowNode(
                    id="a",
                    mode="parallel",
                    prompt_segments=[PromptSegment(role="shared", content="a")],
                    providers=[provider("p")],
                ),
                WorkflowNode(
                    id="b",
                    mode="parallel",
                    prompt_segments=[PromptSegment(role="shared", content="b")],
                    providers=[provider("p")],
                ),
                WorkflowNode(
                    id="c",
                    mode="parallel",
                    prompt_segments=[PromptSegment(role="shared", content="c")],
                    providers=[provider("p")],
                ),
                WorkflowNode(
                    id="d",
                    mode="parallel",
                    prompt_segments=[PromptSegment(role="shared", content="d")],
                    providers=[provider("p")],
                ),
            ],
        )
        state = build_initial_state(
            topology_from_workflow(workflow), run_id="run-overflow"
        )
        layout = compute_topology_layout(topology_from_workflow(workflow))
        rendered = render_dashboard_text(
            state=state,
            layout=layout,
            width=72,
            height=20,
            now=monotonic(),
        )
        self.assertIn("Showing lanes", rendered)

    def test_small_viewport_respects_requested_dimensions(self) -> None:
        workflow = _build_sample_workflow()
        state = build_initial_state(
            topology_from_workflow(workflow), run_id="run-small"
        )
        layout = compute_topology_layout(topology_from_workflow(workflow))

        rendered = render_dashboard_text(
            state=state,
            layout=layout,
            width=10,
            height=1,
            now=monotonic(),
        )

        lines = rendered.splitlines()
        self.assertEqual(len(lines), 1)
        self.assertLessEqual(display_width(lines[0]), 10)

    def test_timeline_mode_hides_wave_headers(self) -> None:
        workflow = _build_sample_workflow()
        state = build_initial_state(
            topology_from_workflow(workflow), run_id="run-timeline"
        )
        layout = compute_topology_layout(topology_from_workflow(workflow))
        rendered = render_dashboard_text(
            state=state,
            layout=layout,
            width=120,
            height=40,
            now=monotonic(),
            config=RenderConfig(display_mode="timeline"),
        )
        self.assertNotIn("Wave 1", rendered)
        self.assertNotIn("Wave 2", rendered)
        self.assertIn("node5 [sequential]", rendered)
        self.assertIn("┼", rendered)

    def test_timeline_separator_applies_only_to_active_wave_lanes(self) -> None:
        workflow = WorkflowPlan(
            name="timeline.separator",
            nodes=[
                WorkflowNode(
                    id="backend.pages",
                    mode="parallel",
                    prompt_segments=[PromptSegment(role="shared", content="a")],
                    providers=[provider("p")],
                ),
                WorkflowNode(
                    id="backend.workspaces",
                    mode="parallel",
                    prompt_segments=[PromptSegment(role="shared", content="b")],
                    providers=[provider("p")],
                ),
                WorkflowNode(
                    id="summary.final",
                    mode="sequential",
                    prompt_segments=[PromptSegment(role="shared", content="c")],
                    needs=["backend.pages", "backend.workspaces"],
                    providers=[provider("p")],
                ),
                WorkflowNode(
                    id="design.plan",
                    mode="parallel",
                    prompt_segments=[PromptSegment(role="shared", content="d")],
                    providers=[provider("p")],
                ),
            ],
        )
        state = build_initial_state(
            topology_from_workflow(workflow), run_id="run-timeline-separator"
        )
        layout = compute_topology_layout(topology_from_workflow(workflow))
        rendered = render_dashboard_text(
            state=state,
            layout=layout,
            width=150,
            height=40,
            now=monotonic(),
            config=RenderConfig(display_mode="timeline"),
        )

        separator_line = next(line for line in rendered.splitlines() if "┼" in line)
        self.assertIn("┼", separator_line)
        self.assertIn(" │ ", separator_line)

    def test_invocation_done_duration_uses_human_readable_format(self) -> None:
        workflow = WorkflowPlan(
            name="elapsed",
            nodes=[
                WorkflowNode(
                    id="node.elapsed",
                    mode="sequential",
                    prompt_segments=[PromptSegment(role="shared", content="run")],
                    providers=[ProviderSpec(provider="p", role="executor")],
                )
            ],
        )
        state = build_initial_state(
            topology_from_workflow(workflow), run_id="run-elapsed"
        )
        layout = compute_topology_layout(topology_from_workflow(workflow))
        apply_event(
            state,
            make_execution_event(
                event_type="node_started",
                workflow_name=workflow.name,
                run_id="run-elapsed",
                node_id="node.elapsed",
            ),
        )
        apply_event(
            state,
            make_execution_event(
                event_type="invocation_started",
                workflow_name=workflow.name,
                run_id="run-elapsed",
                node_id="node.elapsed",
                provider="p",
                role="executor",
                model="m",
                task_id="p_executor_0",
            ),
        )
        apply_event(
            state,
            make_execution_event(
                event_type="invocation_finished",
                workflow_name=workflow.name,
                run_id="run-elapsed",
                node_id="node.elapsed",
                provider="p",
                role="executor",
                model="m",
                task_id="p_executor_0",
                duration_ms=186878,
            ),
        )

        rendered = render_dashboard_text(
            state=state,
            layout=layout,
            width=120,
            height=30,
            now=monotonic(),
            config=RenderConfig(stream_lines_per_node=0),
        )
        self.assertIn("DONE p_executor_0 (3m06s)", rendered)
        self.assertNotIn("ms)", rendered)

    def test_render_includes_live_stream_lines_when_enabled(self) -> None:
        workflow = WorkflowPlan(
            name="stream.render",
            nodes=[
                WorkflowNode(
                    id="node.stream",
                    mode="parallel",
                    prompt_segments=[PromptSegment(role="shared", content="run")],
                    providers=[provider("p")],
                )
            ],
        )
        state = build_initial_state(
            topology_from_workflow(workflow), run_id="run-stream"
        )
        layout = compute_topology_layout(topology_from_workflow(workflow))

        rendered = render_dashboard_text(
            state=state,
            layout=layout,
            width=120,
            height=30,
            now=monotonic(),
            node_live_lines={"node.stream": ["out-1", "[stderr] err-1"]},
            config=RenderConfig(stream_lines_per_node=2),
        )
        self.assertIn("out-1", rendered)
        self.assertIn("[stderr] err-1", rendered)

    def test_sticky_cell_includes_live_stream_lines_when_enabled(self) -> None:
        workflow = _build_sample_workflow()
        state = build_initial_state(
            topology_from_workflow(workflow), run_id="run-stream-sticky"
        )
        layout = compute_topology_layout(topology_from_workflow(workflow))
        apply_event(
            state,
            make_execution_event(
                event_type="node_started",
                workflow_name=workflow.name,
                run_id="run-stream-sticky",
                node_id="node1",
            ),
        )
        apply_event(
            state,
            make_execution_event(
                event_type="node_started",
                workflow_name=workflow.name,
                run_id="run-stream-sticky",
                node_id="node5",
            ),
        )

        rendered = render_dashboard_text(
            state=state,
            layout=layout,
            width=120,
            height=11,
            now=monotonic(),
            node_live_lines={"node1": ["sticky-log-1", "sticky-log-2"]},
            config=RenderConfig(display_mode="waves", stream_lines_per_node=2),
        )
        self.assertIn("node1 [sticky]", rendered)
        self.assertIn("sticky-log-1", rendered)
        self.assertIn("sticky-log-2", rendered)

    def test_sticky_cell_does_not_duplicate_live_lines_when_primary_visible(
        self,
    ) -> None:
        workflow = _build_sample_workflow()
        state = build_initial_state(
            topology_from_workflow(workflow), run_id="run-stream-duplicate"
        )
        layout = compute_topology_layout(topology_from_workflow(workflow))
        apply_event(
            state,
            make_execution_event(
                event_type="node_started",
                workflow_name=workflow.name,
                run_id="run-stream-duplicate",
                node_id="node1",
            ),
        )

        rendered = render_dashboard_text(
            state=state,
            layout=layout,
            width=120,
            height=40,
            now=monotonic(),
            node_live_lines={
                "node1": ["line-1", "line-2", "line-3", "line-4"],
            },
            config=RenderConfig(display_mode="timeline", stream_lines_per_node=1),
        )
        self.assertNotIn("node1 [sticky]", rendered)
        self.assertEqual(rendered.count("line-4"), 1)
        self.assertEqual(rendered.count("line-3"), 1)

    def test_rounds_with_same_task_id_track_as_distinct_invocations(self) -> None:
        workflow = WorkflowPlan(
            name="rounds",
            nodes=[
                WorkflowNode(
                    id="node.seq",
                    mode="sequential",
                    prompt_segments=[PromptSegment(role="shared", content="work")],
                    providers=[ProviderSpec(provider="p", role="executor")],
                ),
            ],
        )
        state = build_initial_state(
            topology_from_workflow(workflow), run_id="run-rounds"
        )

        for round_num in (1, 2):
            apply_event(
                state,
                make_execution_event(
                    event_type="invocation_started",
                    workflow_name=workflow.name,
                    run_id="run-rounds",
                    node_id="node.seq",
                    provider="p",
                    role="executor",
                    model="m",
                    task_id="p_executor_0",
                    round_num=round_num,
                ),
            )
            apply_event(
                state,
                make_execution_event(
                    event_type="invocation_finished",
                    workflow_name=workflow.name,
                    run_id="run-rounds",
                    node_id="node.seq",
                    provider="p",
                    role="executor",
                    model="m",
                    task_id="p_executor_0",
                    round_num=round_num,
                    duration_ms=1,
                ),
            )

        node_state = state.nodes["node.seq"]
        self.assertEqual(node_state.total_invocations, 2)
        self.assertEqual(node_state.succeeded_invocations, 2)
        tracked_rounds = sorted(
            invocation.round_num for invocation in node_state.invocations.values()
        )
        self.assertEqual(tracked_rounds, [1, 2])

    def test_audit_rounds_with_same_local_round_track_as_distinct_invocations(
        self,
    ) -> None:
        workflow = WorkflowPlan(
            name="audit-rounds",
            nodes=[
                WorkflowNode(
                    id="node.seq",
                    mode="sequential",
                    prompt_segments=[PromptSegment(role="shared", content="work")],
                    providers=[
                        ProviderSpec(provider="p", role="executor"),
                        ProviderSpec(provider="q", role="reviewer"),
                    ],
                ),
            ],
        )
        state = build_initial_state(
            topology_from_workflow(workflow), run_id="run-audit-rounds"
        )

        for audit_round_num in (1, 2):
            apply_event(
                state,
                make_execution_event(
                    event_type="invocation_started",
                    workflow_name=workflow.name,
                    run_id="run-audit-rounds",
                    node_id="node.seq",
                    provider="q",
                    role="reviewer",
                    model="m",
                    task_id="q_reviewer_0",
                    audit_round_num=audit_round_num,
                    round_num=1,
                ),
            )
            apply_event(
                state,
                make_execution_event(
                    event_type="invocation_finished",
                    workflow_name=workflow.name,
                    run_id="run-audit-rounds",
                    node_id="node.seq",
                    provider="q",
                    role="reviewer",
                    model="m",
                    task_id="q_reviewer_0",
                    audit_round_num=audit_round_num,
                    round_num=1,
                    duration_ms=1,
                ),
            )

        node_state = state.nodes["node.seq"]
        self.assertEqual(node_state.total_invocations, 2)
        tracked_audit_rounds = sorted(
            invocation.audit_round_num for invocation in node_state.invocations.values()
        )
        self.assertEqual(tracked_audit_rounds, [1, 2])

    def test_render_handles_import_prefixed_node_ids(self) -> None:
        workflow = WorkflowPlan(
            name="import.render",
            nodes=[
                WorkflowNode(
                    id="auth.plan",
                    mode="parallel",
                    prompt_segments=[PromptSegment(role="shared", content="plan")],
                    providers=[provider("p")],
                ),
                WorkflowNode(
                    id="summary.final",
                    mode="sequential",
                    prompt_segments=[
                        PromptSegment(
                            role="shared", content="summary {{auth.plan.output}}"
                        )
                    ],
                    needs=["auth.plan"],
                    providers=[ProviderSpec(provider="p", role="executor")],
                ),
            ],
        )
        state = build_initial_state(
            topology_from_workflow(workflow), run_id="run-import-render"
        )
        layout = compute_topology_layout(topology_from_workflow(workflow))

        rendered = render_dashboard_text(
            state=state,
            layout=layout,
            width=120,
            height=30,
            now=monotonic(),
        )
        self.assertIn("auth.plan", rendered)
        self.assertIn("summary.final", rendered)
