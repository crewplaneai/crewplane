import unittest
from pathlib import Path

from orchestrator_cli.core.workflow_models import (
    PromptSegment,
    ProviderSpec,
    WorkflowNode,
    WorkflowPlan,
)
from orchestrator_cli.observability.dag_render import render_dag_summary
from orchestrator_cli.observability.events import (
    apply_event,
    build_initial_state,
)
from orchestrator_cli.observability.layout import compute_topology_layout
from tests.helpers.observability import (
    make_execution_event,
    topology_from_workflow,
)

PROJECT_ROOT = Path(__file__).resolve().parents[4]


def provider(name: str) -> ProviderSpec:
    return ProviderSpec(provider=name)


class DagRenderStatusTests(unittest.TestCase):
    def test_running_node_elapsed_uses_node_start_time(self) -> None:
        workflow = WorkflowPlan(
            name="running-elapsed",
            nodes=[
                WorkflowNode(
                    id="node.a",
                    mode="parallel",
                    prompt_segments=[PromptSegment(role="shared", content="run")],
                    providers=[provider("alpha")],
                )
            ],
        )
        state = build_initial_state(
            topology_from_workflow(workflow), run_id="run-running-elapsed"
        )
        apply_event(
            state,
            make_execution_event(
                event_type="node_started",
                workflow_name=workflow.name,
                run_id="run-running-elapsed",
                node_id="node.a",
                timestamp=10.0,
            ),
        )
        layout = compute_topology_layout(topology_from_workflow(workflow))

        lines = render_dag_summary(
            state=state,
            layout=layout,
            selected_node_id="node.a",
            width=120,
            now=40.0,
        )

        self.assertIn("⏳ 30.0s alpha", lines[0])

    def test_parallel_node_elapsed_uses_node_wall_clock_time(self) -> None:
        workflow = WorkflowPlan(
            name="parallel-elapsed",
            nodes=[
                WorkflowNode(
                    id="node.a",
                    mode="parallel",
                    prompt_segments=[PromptSegment(role="shared", content="run")],
                    providers=[provider("alpha"), provider("beta")],
                )
            ],
        )
        state = build_initial_state(
            topology_from_workflow(workflow), run_id="run-parallel-elapsed"
        )
        apply_event(
            state,
            make_execution_event(
                event_type="node_started",
                workflow_name=workflow.name,
                run_id="run-parallel-elapsed",
                node_id="node.a",
                timestamp=10.0,
            ),
        )
        for provider_name, task_id in (
            ("alpha", "alpha_executor_0"),
            ("beta", "beta_executor_0"),
        ):
            apply_event(
                state,
                make_execution_event(
                    event_type="invocation_started",
                    workflow_name=workflow.name,
                    run_id="run-parallel-elapsed",
                    node_id="node.a",
                    provider=provider_name,
                    role="executor",
                    model="m",
                    task_id=task_id,
                    round_num=1,
                    timestamp=10.0,
                ),
            )
            apply_event(
                state,
                make_execution_event(
                    event_type="invocation_finished",
                    workflow_name=workflow.name,
                    run_id="run-parallel-elapsed",
                    node_id="node.a",
                    provider=provider_name,
                    role="executor",
                    model="m",
                    task_id=task_id,
                    round_num=1,
                    duration_ms=5000,
                    timestamp=15.0,
                ),
            )
        apply_event(
            state,
            make_execution_event(
                event_type="node_finished",
                workflow_name=workflow.name,
                run_id="run-parallel-elapsed",
                node_id="node.a",
                timestamp=15.0,
            ),
        )
        layout = compute_topology_layout(topology_from_workflow(workflow))

        lines = render_dag_summary(
            state=state,
            layout=layout,
            selected_node_id="node.a",
            width=120,
        )

        self.assertIn("✅ 5.0s alpha, beta", lines[0])
        self.assertNotIn("10.0s", lines[0])

    def test_topological_order_is_used_over_workflow_declaration_order(self) -> None:
        workflow = WorkflowPlan(
            name="topological-order",
            nodes=[
                WorkflowNode(
                    id="merge",
                    mode="parallel",
                    prompt_segments=[PromptSegment(role="shared", content="merge")],
                    needs=["root"],
                    providers=[provider("p")],
                ),
                WorkflowNode(
                    id="root",
                    mode="parallel",
                    prompt_segments=[PromptSegment(role="shared", content="root")],
                    providers=[provider("p")],
                ),
            ],
        )
        state = build_initial_state(
            topology_from_workflow(workflow), run_id="run-topological"
        )
        layout = compute_topology_layout(topology_from_workflow(workflow))
        lines = render_dag_summary(
            state=state,
            layout=layout,
            selected_node_id="root",
            width=120,
        )
        rendered = "\n".join(lines)
        self.assertLess(rendered.index("root"), rendered.index("merge"))

    def test_failed_and_blocked_status_icons_are_rendered_with_dependency_edge(
        self,
    ) -> None:
        workflow = WorkflowPlan(
            name="status",
            nodes=[
                WorkflowNode(
                    id="compile.api",
                    mode="parallel",
                    prompt_segments=[PromptSegment(role="shared", content="a")],
                    providers=[provider("codex")],
                ),
                WorkflowNode(
                    id="deploy.api",
                    mode="parallel",
                    prompt_segments=[PromptSegment(role="shared", content="b")],
                    needs=["compile.api"],
                    providers=[provider("codex")],
                ),
            ],
        )
        state = build_initial_state(
            topology_from_workflow(workflow), run_id="run-status"
        )
        apply_event(
            state,
            make_execution_event(
                event_type="node_failed",
                workflow_name=workflow.name,
                run_id="run-status",
                node_id="compile.api",
                error="boom",
            ),
        )
        apply_event(
            state,
            make_execution_event(
                event_type="node_blocked",
                workflow_name=workflow.name,
                run_id="run-status",
                node_id="deploy.api",
                error="unsatisfied dependencies: compile.api",
            ),
        )
        layout = compute_topology_layout(topology_from_workflow(workflow))

        lines = render_dag_summary(
            state=state,
            layout=layout,
            selected_node_id="compile.api",
            width=120,
        )
        rendered = "\n".join(lines)
        self.assertIn("❌", rendered)
        self.assertIn("⛔", rendered)
        self.assertIn("│", {line.strip() for line in lines})
