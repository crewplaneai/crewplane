import unittest
from pathlib import Path

from crewplane.core.workflow.models import (
    PromptSegment,
    ProviderSpec,
    WorkflowNode,
    WorkflowPlan,
)
from crewplane.observability.dag_render import render_dag_summary
from crewplane.observability.events import (
    build_initial_state,
)
from crewplane.observability.layout import compute_topology_layout
from tests.helpers.observability import topology_from_workflow

PROJECT_ROOT = Path(__file__).resolve().parents[4]


def provider(name: str) -> ProviderSpec:
    return ProviderSpec(provider=name)


class DagRenderFaninOverflowTests(unittest.TestCase):
    def test_input_root_renders_mode_label_without_providers(self) -> None:
        workflow = WorkflowPlan(
            name="input.render",
            nodes=[
                WorkflowNode(
                    id="review-input",
                    mode="input",
                    source="{{file:.crewplane/inputs/review-findings.md}}",
                ),
                WorkflowNode(
                    id="implement",
                    mode="sequential",
                    prompt_segments=[
                        PromptSegment(
                            role="shared", content="Use {{review-input.output}}"
                        )
                    ],
                    needs=["review-input"],
                    providers=[ProviderSpec(provider="claude", role="executor")],
                ),
            ],
        )
        state = build_initial_state(
            topology_from_workflow(workflow), run_id="run-input"
        )
        layout = compute_topology_layout(topology_from_workflow(workflow))

        lines = render_dag_summary(
            state=state,
            layout=layout,
            selected_node_id="review-input",
            width=120,
        )

        self.assertTrue(
            any("review-input" in line and "input" in line for line in lines)
        )
        self.assertTrue(any("implement" in line and "claude" in line for line in lines))

    def test_fanout_side_chain_and_fanin_render_open_and_close_rows(self) -> None:
        workflow = WorkflowPlan(
            name="fanout.chain.fanin",
            nodes=[
                WorkflowNode(
                    id="implement.plan",
                    mode="parallel",
                    prompt_segments=[PromptSegment(role="shared", content="a")],
                    providers=[provider("claude")],
                ),
                WorkflowNode(
                    id="implement.build",
                    mode="parallel",
                    prompt_segments=[PromptSegment(role="shared", content="b")],
                    needs=["implement.plan"],
                    providers=[provider("codex"), provider("gemini")],
                ),
                WorkflowNode(
                    id="implement.review",
                    mode="sequential",
                    prompt_segments=[PromptSegment(role="shared", content="c")],
                    needs=["implement.build"],
                    providers=[
                        ProviderSpec(provider="codex", role="executor"),
                        ProviderSpec(provider="claude", role="reviewer"),
                    ],
                ),
                WorkflowNode(
                    id="implement.fixes",
                    mode="parallel",
                    prompt_segments=[PromptSegment(role="shared", content="d")],
                    needs=["implement.review"],
                    providers=[provider("codex")],
                ),
                WorkflowNode(
                    id="implement.handoff",
                    mode="sequential",
                    prompt_segments=[PromptSegment(role="shared", content="e")],
                    needs=["implement.plan", "implement.fixes"],
                    providers=[ProviderSpec(provider="claude", role="executor")],
                ),
            ],
        )
        state = build_initial_state(topology_from_workflow(workflow), run_id="run-fan")
        layout = compute_topology_layout(topology_from_workflow(workflow))

        lines = render_dag_summary(
            state=state,
            layout=layout,
            selected_node_id=None,
            width=120,
        )
        rendered = "\n".join(lines)

        self.assertIn("├─┐", rendered)
        self.assertIn("├─┘", rendered)
        self.assertIn("● │ implement.build", rendered)
        self.assertIn("● │ implement.review", rendered)
        self.assertIn("● │ implement.fixes", rendered)

    def test_diamond_prefers_leftmost_branch_as_trunk_when_depth_is_equal(self) -> None:
        workflow = WorkflowPlan(
            name="diamond",
            nodes=[
                WorkflowNode(
                    id="root",
                    mode="parallel",
                    prompt_segments=[PromptSegment(role="shared", content="r")],
                    providers=[provider("p")],
                ),
                WorkflowNode(
                    id="left",
                    mode="parallel",
                    prompt_segments=[PromptSegment(role="shared", content="l")],
                    needs=["root"],
                    providers=[provider("p")],
                ),
                WorkflowNode(
                    id="right",
                    mode="parallel",
                    prompt_segments=[PromptSegment(role="shared", content="r")],
                    needs=["root"],
                    providers=[provider("p")],
                ),
                WorkflowNode(
                    id="merge",
                    mode="sequential",
                    prompt_segments=[PromptSegment(role="shared", content="m")],
                    needs=["left", "right"],
                    providers=[ProviderSpec(provider="p", role="executor")],
                ),
            ],
        )
        state = build_initial_state(
            topology_from_workflow(workflow), run_id="run-diamond"
        )
        layout = compute_topology_layout(topology_from_workflow(workflow))

        lines = render_dag_summary(
            state=state,
            layout=layout,
            selected_node_id=None,
            width=120,
        )
        rendered = "\n".join(lines)

        self.assertIn("● │ left", rendered)
        self.assertIn("│ ● right", rendered)

    def test_interleaved_independent_root_uses_new_column(self) -> None:
        workflow = WorkflowPlan(
            name="interleaved.root",
            nodes=[
                WorkflowNode(
                    id="a",
                    mode="parallel",
                    prompt_segments=[PromptSegment(role="shared", content="a")],
                    providers=[provider("p")],
                ),
                WorkflowNode(
                    id="x",
                    mode="parallel",
                    prompt_segments=[PromptSegment(role="shared", content="x")],
                    providers=[provider("p")],
                ),
                WorkflowNode(
                    id="b",
                    mode="parallel",
                    prompt_segments=[PromptSegment(role="shared", content="b")],
                    needs=["a"],
                    providers=[provider("p")],
                ),
                WorkflowNode(
                    id="c",
                    mode="parallel",
                    prompt_segments=[PromptSegment(role="shared", content="c")],
                    needs=["a"],
                    providers=[provider("p")],
                ),
                WorkflowNode(
                    id="y",
                    mode="parallel",
                    prompt_segments=[PromptSegment(role="shared", content="y")],
                    needs=["x"],
                    providers=[provider("p")],
                ),
            ],
        )
        state = build_initial_state(
            topology_from_workflow(workflow), run_id="run-interleaved-root"
        )
        layout = compute_topology_layout(topology_from_workflow(workflow))

        lines = render_dag_summary(
            state=state,
            layout=layout,
            selected_node_id=None,
            width=120,
        )
        x_line = next(line for line in lines if " x" in line)
        self.assertTrue(x_line.startswith(" │ │ ● x"))

    def test_two_independent_roots_fanin_matches_proposal_shape(self) -> None:
        workflow = WorkflowPlan(
            name="roots.merge",
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
                    id="merge",
                    mode="sequential",
                    prompt_segments=[PromptSegment(role="shared", content="m")],
                    needs=["a", "b"],
                    providers=[ProviderSpec(provider="p", role="executor")],
                ),
            ],
        )
        state = build_initial_state(
            topology_from_workflow(workflow), run_id="run-roots-merge"
        )
        layout = compute_topology_layout(topology_from_workflow(workflow))

        lines = render_dag_summary(
            state=state,
            layout=layout,
            selected_node_id=None,
            width=120,
        )
        self.assertEqual(
            lines,
            [
                " ●   a                        ⏸  p",
                " │ ● b                        ⏸  p",
                " ├─┘",
                " ●   merge                    ⏸  p",
            ],
        )

    def test_three_independent_roots_fanin_renders_without_spacer_rows(self) -> None:
        workflow = WorkflowPlan(
            name="review.merge",
            nodes=[
                WorkflowNode(
                    id="review.business",
                    mode="parallel",
                    prompt_segments=[PromptSegment(role="shared", content="a")],
                    providers=[provider("gemini")],
                ),
                WorkflowNode(
                    id="review.architecture",
                    mode="parallel",
                    prompt_segments=[PromptSegment(role="shared", content="b")],
                    providers=[provider("gemini")],
                ),
                WorkflowNode(
                    id="review.quality",
                    mode="parallel",
                    prompt_segments=[PromptSegment(role="shared", content="c")],
                    providers=[provider("gemini")],
                ),
                WorkflowNode(
                    id="review.summary",
                    mode="sequential",
                    prompt_segments=[PromptSegment(role="shared", content="d")],
                    needs=[
                        "review.business",
                        "review.architecture",
                        "review.quality",
                    ],
                    providers=[ProviderSpec(provider="gemini", role="executor")],
                ),
            ],
        )
        state = build_initial_state(
            topology_from_workflow(workflow), run_id="run-review-merge"
        )
        layout = compute_topology_layout(topology_from_workflow(workflow))

        lines = render_dag_summary(
            state=state,
            layout=layout,
            selected_node_id="review.business",
            width=120,
        )

        self.assertEqual(
            lines,
            [
                "▸●     review.business          ⏸  gemini",
                " │ ●   review.architecture      ⏸  gemini",
                " │ │ ● review.quality           ⏸  gemini",
                " ├─┴─┘",
                " ●     review.summary           ⏸  gemini",
            ],
        )

    def test_overflow_cap_adds_hidden_branch_summary(self) -> None:
        children = [
            WorkflowNode(
                id=f"child.{index}",
                mode="parallel",
                prompt_segments=[PromptSegment(role="shared", content="x")],
                needs=["root"],
                providers=[provider("codex")],
            )
            for index in range(8)
        ]
        workflow = WorkflowPlan(
            name="overflow",
            nodes=[
                WorkflowNode(
                    id="root",
                    mode="parallel",
                    prompt_segments=[PromptSegment(role="shared", content="r")],
                    providers=[provider("codex")],
                ),
                *children,
                WorkflowNode(
                    id="merge",
                    mode="sequential",
                    prompt_segments=[PromptSegment(role="shared", content="m")],
                    needs=["root", *(child.id for child in children)],
                    providers=[ProviderSpec(provider="claude", role="executor")],
                ),
            ],
        )
        state = build_initial_state(
            topology_from_workflow(workflow), run_id="run-overflow"
        )
        layout = compute_topology_layout(topology_from_workflow(workflow))

        lines = render_dag_summary(
            state=state,
            layout=layout,
            selected_node_id="root",
            width=120,
        )

        self.assertIn("... +2 more", lines[-1])
        self.assertTrue(any("├─┬" in line for line in lines))
