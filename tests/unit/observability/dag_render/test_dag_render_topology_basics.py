import tempfile
import unittest
from pathlib import Path

from crewplane.cli.templates import render_template_content
from crewplane.core.prompt_segments import PromptSegmentRole
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.core.workflow.loading import load_tasks
from crewplane.core.workflow.models import (
    PromptSegment,
    ProviderSpec,
    WorkflowNode,
    WorkflowPlan,
)
from crewplane.observability.dag_render import render_dag_summary
from crewplane.observability.events import (
    apply_event,
    build_initial_state,
)
from crewplane.observability.layout import compute_topology_layout
from tests.helpers.observability import (
    make_execution_event,
    topology_from_workflow,
)
from tests.helpers.render_fixtures import read_render_fixture

PROJECT_ROOT = Path(__file__).resolve().parents[4]
FIXTURES = Path(__file__).parents[1] / "fixtures" / "dag_render"


def provider(name: str) -> ProviderSpec:
    return ProviderSpec(provider=name)


def expected_render(case_id: str) -> str:
    return read_render_fixture(FIXTURES, case_id, "expected.txt")


class DagRenderTopologyBasicTests(unittest.TestCase):
    def test_code_review_example_renders_parallel_provider_list_and_linear_chain(
        self,
    ) -> None:
        template_path = (
            PROJECT_ROOT
            / "src"
            / "crewplane"
            / "example_templates"
            / "code-review-example.task.md"
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            workflow_path = Path(tmp_dir) / "code-review-example.task.md"
            workflow_path.write_text(
                render_template_content(template_path.read_text(encoding="utf-8")),
                encoding="utf-8",
            )
            workflow = load_tasks(workflow_path)
        state = build_initial_state(
            topology_from_workflow(workflow), run_id="run-code-review"
        )
        layout = compute_topology_layout(topology_from_workflow(workflow))

        lines = render_dag_summary(
            state=state,
            layout=layout,
            selected_node_id="review.context",
            width=120,
        )

        self.assertEqual("\n".join(lines), expected_render("code_review_example"))

    def test_parallel_roots_render_without_connector_rows(self) -> None:
        workflow = WorkflowPlan(
            name="parallel",
            nodes=[
                WorkflowNode(
                    id="backend.auth",
                    mode="parallel",
                    prompt_segments=[
                        PromptSegment(role=PromptSegmentRole.SHARED, content="a")
                    ],
                    providers=[provider("codex")],
                ),
                WorkflowNode(
                    id="backend.billing",
                    mode="parallel",
                    prompt_segments=[
                        PromptSegment(role=PromptSegmentRole.SHARED, content="b")
                    ],
                    providers=[provider("claude")],
                ),
                WorkflowNode(
                    id="frontend.ui",
                    mode="parallel",
                    prompt_segments=[
                        PromptSegment(role=PromptSegmentRole.SHARED, content="c")
                    ],
                    providers=[provider("gemini")],
                ),
            ],
        )
        state = build_initial_state(
            topology_from_workflow(workflow), run_id="run-parallel"
        )
        layout = compute_topology_layout(topology_from_workflow(workflow))

        lines = render_dag_summary(
            state=state,
            layout=layout,
            selected_node_id="backend.billing",
            width=120,
        )

        self.assertEqual("\n".join(lines), expected_render("parallel_roots"))

    def test_linear_chain_renders_connector_rows_between_nodes(self) -> None:
        workflow = WorkflowPlan(
            name="linear",
            nodes=[
                WorkflowNode(
                    id="design.discovery",
                    mode="parallel",
                    prompt_segments=[
                        PromptSegment(role=PromptSegmentRole.SHARED, content="a")
                    ],
                    providers=[provider("claude")],
                ),
                WorkflowNode(
                    id="design.iteration",
                    mode="sequential",
                    prompt_segments=[
                        PromptSegment(role=PromptSegmentRole.SHARED, content="b")
                    ],
                    needs=["design.discovery"],
                    providers=[
                        ProviderSpec(provider="codex", role=ProviderRole.EXECUTOR),
                        ProviderSpec(provider="gemini", role=ProviderRole.REVIEWER),
                    ],
                ),
                WorkflowNode(
                    id="design.decision",
                    mode="parallel",
                    prompt_segments=[
                        PromptSegment(role=PromptSegmentRole.SHARED, content="c")
                    ],
                    needs=["design.iteration"],
                    providers=[provider("claude")],
                ),
            ],
        )
        state = build_initial_state(
            topology_from_workflow(workflow), run_id="run-linear"
        )
        layout = compute_topology_layout(topology_from_workflow(workflow))

        lines = render_dag_summary(
            state=state,
            layout=layout,
            selected_node_id="design.iteration",
            width=120,
        )

        self.assertEqual("\n".join(lines), expected_render("linear_chain"))

    def test_short_transitive_dependency_renders_as_linear_chain(self) -> None:
        workflow = WorkflowPlan(
            name="transitive.chain",
            nodes=[
                WorkflowNode(
                    id="design.init",
                    mode="sequential",
                    prompt_segments=[
                        PromptSegment(role=PromptSegmentRole.SHARED, content="a")
                    ],
                    providers=[
                        ProviderSpec(provider="copilot", role=ProviderRole.EXECUTOR)
                    ],
                ),
                WorkflowNode(
                    id="design.iteration",
                    mode="sequential",
                    prompt_segments=[
                        PromptSegment(role=PromptSegmentRole.SHARED, content="b")
                    ],
                    needs=["design.init"],
                    providers=[
                        ProviderSpec(provider="codex", role=ProviderRole.EXECUTOR),
                        ProviderSpec(provider="copilot", role=ProviderRole.REVIEWER),
                    ],
                ),
                WorkflowNode(
                    id="design.decision",
                    mode="sequential",
                    prompt_segments=[
                        PromptSegment(role=PromptSegmentRole.SHARED, content="c")
                    ],
                    needs=["design.init", "design.iteration"],
                    providers=[
                        ProviderSpec(provider="copilot", role=ProviderRole.EXECUTOR)
                    ],
                ),
            ],
        )
        state = build_initial_state(
            topology_from_workflow(workflow), run_id="run-transitive-chain"
        )
        layout = compute_topology_layout(topology_from_workflow(workflow))

        lines = render_dag_summary(
            state=state,
            layout=layout,
            selected_node_id="design.iteration",
            width=120,
        )

        self.assertEqual("\n".join(lines), expected_render("short_transitive_chain"))

    def test_shortcut_fanin_with_fanout_does_not_render_consumed_lanes(
        self,
    ) -> None:
        workflow = WorkflowPlan(
            name="plan.and.implement",
            nodes=[
                WorkflowNode(
                    id="plan.plan.init",
                    mode="sequential",
                    prompt_segments=[
                        PromptSegment(role=PromptSegmentRole.SHARED, content="a")
                    ],
                    providers=[provider("codex")],
                ),
                WorkflowNode(
                    id="plan.plan.iteration",
                    mode="sequential",
                    prompt_segments=[
                        PromptSegment(role=PromptSegmentRole.SHARED, content="b")
                    ],
                    needs=["plan.plan.init"],
                    providers=[
                        ProviderSpec(provider="copilot", role=ProviderRole.EXECUTOR),
                        ProviderSpec(provider="codex", role=ProviderRole.REVIEWER),
                    ],
                ),
                WorkflowNode(
                    id="plan.plan.final",
                    mode="sequential",
                    prompt_segments=[
                        PromptSegment(role=PromptSegmentRole.SHARED, content="c")
                    ],
                    needs=["plan.plan.init", "plan.plan.iteration"],
                    providers=[provider("copilot")],
                ),
                WorkflowNode(
                    id="implement.implement.build",
                    mode="sequential",
                    prompt_segments=[
                        PromptSegment(role=PromptSegmentRole.SHARED, content="d")
                    ],
                    needs=["plan.plan.final"],
                    providers=[provider("codex")],
                ),
                WorkflowNode(
                    id="implement.implement.review",
                    mode="sequential",
                    prompt_segments=[
                        PromptSegment(role=PromptSegmentRole.SHARED, content="e")
                    ],
                    needs=["plan.plan.final", "implement.implement.build"],
                    providers=[
                        ProviderSpec(provider="copilot", role=ProviderRole.EXECUTOR),
                        ProviderSpec(provider="codex", role=ProviderRole.REVIEWER),
                    ],
                ),
                WorkflowNode(
                    id="implement.implement.handoff",
                    mode="sequential",
                    prompt_segments=[
                        PromptSegment(role=PromptSegmentRole.SHARED, content="f")
                    ],
                    needs=[
                        "implement.implement.build",
                        "implement.implement.review",
                    ],
                    providers=[provider("codex")],
                ),
                WorkflowNode(
                    id="handoff.final",
                    mode="sequential",
                    prompt_segments=[
                        PromptSegment(role=PromptSegmentRole.SHARED, content="g")
                    ],
                    needs=["plan.plan.final", "implement.implement.handoff"],
                    providers=[provider("codex")],
                ),
            ],
        )
        state = build_initial_state(
            topology_from_workflow(workflow), run_id="run-plan-implement"
        )
        layout = compute_topology_layout(topology_from_workflow(workflow))

        lines = render_dag_summary(
            state=state,
            layout=layout,
            selected_node_id="plan.plan.init",
            width=120,
        )

        self.assertEqual(
            "\n".join(lines),
            expected_render("shortcut_fanin_with_fanout"),
        )

    def test_consumed_left_branch_does_not_render_shifted_stale_lane(self) -> None:
        workflow = WorkflowPlan(
            name="independent.terminal.shift",
            nodes=[
                WorkflowNode(
                    id="root.a",
                    mode="parallel",
                    prompt_segments=[
                        PromptSegment(role=PromptSegmentRole.SHARED, content="a")
                    ],
                    providers=[provider("codex")],
                ),
                WorkflowNode(
                    id="root.b",
                    mode="parallel",
                    prompt_segments=[
                        PromptSegment(role=PromptSegmentRole.SHARED, content="b")
                    ],
                    providers=[provider("codex")],
                ),
                WorkflowNode(
                    id="a.terminal",
                    mode="parallel",
                    prompt_segments=[
                        PromptSegment(role=PromptSegmentRole.SHARED, content="c")
                    ],
                    needs=["root.a"],
                    providers=[provider("codex")],
                ),
                WorkflowNode(
                    id="b.chain",
                    mode="parallel",
                    prompt_segments=[
                        PromptSegment(role=PromptSegmentRole.SHARED, content="d")
                    ],
                    needs=["root.b"],
                    providers=[provider("codex")],
                ),
                WorkflowNode(
                    id="c.after-b",
                    mode="parallel",
                    prompt_segments=[
                        PromptSegment(role=PromptSegmentRole.SHARED, content="e")
                    ],
                    needs=["b.chain"],
                    providers=[provider("codex")],
                ),
            ],
        )
        state = build_initial_state(
            topology_from_workflow(workflow), run_id="run-shifted-lane"
        )
        layout = compute_topology_layout(topology_from_workflow(workflow))

        lines = render_dag_summary(
            state=state,
            layout=layout,
            selected_node_id=None,
            width=120,
        )

        self.assertEqual("\n".join(lines), expected_render("consumed_left_branch"))

    def test_composed_review_fix_graph_keeps_node_columns_aligned(self) -> None:
        workflow = WorkflowPlan(
            name="composed.review.fix",
            nodes=[
                WorkflowNode(
                    id="quality.review.quality",
                    mode="parallel",
                    prompt_segments=[
                        PromptSegment(role=PromptSegmentRole.SHARED, content="q")
                    ],
                    providers=[provider("codex")],
                ),
                WorkflowNode(
                    id="fix.implement.execute",
                    mode="sequential",
                    prompt_segments=[
                        PromptSegment(role=PromptSegmentRole.SHARED, content="e")
                    ],
                    needs=["quality.review.quality"],
                    providers=[provider("codex")],
                ),
                WorkflowNode(
                    id="fix.implement.review",
                    mode="sequential",
                    prompt_segments=[
                        PromptSegment(role=PromptSegmentRole.SHARED, content="r")
                    ],
                    needs=["fix.implement.execute"],
                    providers=[
                        ProviderSpec(provider="codex", role=ProviderRole.EXECUTOR),
                        ProviderSpec(provider="codex", role=ProviderRole.REVIEWER),
                    ],
                ),
                WorkflowNode(
                    id="fix.implement.summary",
                    mode="sequential",
                    prompt_segments=[
                        PromptSegment(role=PromptSegmentRole.SHARED, content="s")
                    ],
                    needs=["fix.implement.execute", "fix.implement.review"],
                    providers=[provider("codex")],
                ),
                WorkflowNode(
                    id="handoff.final",
                    mode="sequential",
                    prompt_segments=[
                        PromptSegment(role=PromptSegmentRole.SHARED, content="h")
                    ],
                    needs=["fix.implement.summary", "quality.review.quality"],
                    providers=[provider("gemini")],
                ),
            ],
        )
        state = build_initial_state(
            topology_from_workflow(workflow), run_id="run-review-fix"
        )
        for node_id in (
            "quality.review.quality",
            "fix.implement.execute",
            "fix.implement.review",
        ):
            apply_event(
                state,
                make_execution_event(
                    event_type="node_started",
                    workflow_name=workflow.name,
                    run_id="run-review-fix",
                    node_id=node_id,
                    timestamp=0.0,
                ),
            )
        for node_id in ("quality.review.quality", "fix.implement.execute"):
            apply_event(
                state,
                make_execution_event(
                    event_type="node_finished",
                    workflow_name=workflow.name,
                    run_id="run-review-fix",
                    node_id=node_id,
                    timestamp=5.3,
                ),
            )
        layout = compute_topology_layout(topology_from_workflow(workflow))

        lines = render_dag_summary(
            state=state,
            layout=layout,
            selected_node_id="quality.review.quality",
            width=120,
            now=7.4,
        )

        self.assertEqual("\n".join(lines), expected_render("composed_review_fix"))
