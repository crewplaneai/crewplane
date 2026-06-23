import tempfile
import unittest
from pathlib import Path

from crewplane.core.prompt_segments import PromptSegmentRole
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.core.workflow.models import (
    PromptSegment,
    WorkflowNode,
    WorkflowPlan,
)
from crewplane.observability.events import (
    apply_event,
    build_initial_state,
)
from crewplane.observability.layout import compute_topology_layout
from crewplane.observability.types import (
    DashboardSnapshot,
    RunContext,
    RunResult,
)
from tests.helpers.observability import (
    make_execution_event,
    topology_from_workflow,
)
from tests.integration.observability.runtime.observability_runtime_helpers import (
    pane_option_write_count,
    provider,
    provider_label_workflow,
    single_node_workflow,
    status_option_write_count,
)
from tests.integration.observability.tmux_fakes import SimulatedTmuxRuntime


class CompactRuntimeSelectionAndCachingTests(unittest.TestCase):
    def test_compact_runtime_left_pane_uses_configured_provider_labels(self) -> None:
        workflow = provider_label_workflow()
        runtime = SimulatedTmuxRuntime(auto_close_session=True)
        runtime.start(
            RunContext(
                workflow_topology=topology_from_workflow(workflow),
                run_id="compact-provider-labels",
                refresh_per_second=0,
            )
        )

        state = build_initial_state(
            topology_from_workflow(workflow), run_id="compact-provider-labels"
        )
        snapshot = DashboardSnapshot(
            state=state,
            layout=compute_topology_layout(topology_from_workflow(workflow)),
            now=0.0,
        )
        runtime.on_snapshot(None, snapshot)
        runtime.refresh_once()

        left_text = runtime.runtime_files.left_content.read_text(encoding="utf-8")  # type: ignore[union-attr]
        self.assertIn("alpha, beta", left_text)
        self.assertIn("gamma", left_text)
        self.assertIn("│", left_text)
        self.assertNotIn("⏸  parallel", left_text)
        self.assertNotIn("⏸  sequential", left_text)

        runtime.stop(RunResult(status="succeeded"))

    def test_compact_runtime_default_selection_uses_topological_order(self) -> None:
        workflow = WorkflowPlan(
            name="runtime.topological",
            nodes=[
                WorkflowNode(
                    id="merge",
                    mode="parallel",
                    prompt_segments=[
                        PromptSegment(role=PromptSegmentRole.SHARED, content="merge")
                    ],
                    needs=["root"],
                    providers=[provider("alpha")],
                ),
                WorkflowNode(
                    id="root",
                    mode="parallel",
                    prompt_segments=[
                        PromptSegment(role=PromptSegmentRole.SHARED, content="root")
                    ],
                    providers=[provider("alpha")],
                ),
            ],
        )
        runtime = SimulatedTmuxRuntime(auto_close_session=True)
        runtime.start(
            RunContext(
                workflow_topology=topology_from_workflow(workflow),
                run_id="compact-topological-select",
                refresh_per_second=0,
            )
        )

        state = build_initial_state(
            topology_from_workflow(workflow), run_id="compact-topological-select"
        )
        snapshot = DashboardSnapshot(
            state=state,
            layout=compute_topology_layout(topology_from_workflow(workflow)),
            now=0.0,
        )
        runtime.on_snapshot(None, snapshot)
        runtime.refresh_once()

        right_text = runtime.runtime_files.right_content.read_text(encoding="utf-8")  # type: ignore[union-attr]
        self.assertIn("Node Output: root", right_text)

        runtime.stop(RunResult(status="succeeded"))

    def test_compact_runtime_skips_redundant_status_option_updates(self) -> None:
        workflow = single_node_workflow()
        runtime = SimulatedTmuxRuntime()
        runtime.start(
            RunContext(
                workflow_topology=topology_from_workflow(workflow),
                run_id="compact-status",
                refresh_per_second=0,
            )
        )

        state = build_initial_state(
            topology_from_workflow(workflow), run_id="compact-status"
        )
        snapshot = DashboardSnapshot(
            state=state,
            layout=compute_topology_layout(topology_from_workflow(workflow)),
            now=0.0,
        )
        runtime.on_snapshot(None, snapshot)
        runtime.refresh_once()
        first_count = status_option_write_count(runtime.calls)

        runtime.refresh_once()
        second_count = status_option_write_count(runtime.calls)
        self.assertEqual(first_count, second_count)

        runtime.stop(RunResult(status="succeeded"))

    def test_compact_runtime_skips_redundant_pane_title_updates(self) -> None:
        workflow = single_node_workflow()
        runtime = SimulatedTmuxRuntime()
        runtime.start(
            RunContext(
                workflow_topology=topology_from_workflow(workflow),
                run_id="compact-title-cache",
                refresh_per_second=0,
            )
        )

        state = build_initial_state(
            topology_from_workflow(workflow), run_id="compact-title-cache"
        )
        snapshot = DashboardSnapshot(
            state=state,
            layout=compute_topology_layout(topology_from_workflow(workflow)),
            now=0.0,
        )
        runtime.on_snapshot(None, snapshot)
        runtime.refresh_once()
        first_count = pane_option_write_count(
            runtime.calls,
            option="@crewplane_title",
        )

        runtime.refresh_once()
        second_count = pane_option_write_count(
            runtime.calls,
            option="@crewplane_title",
        )
        self.assertEqual(first_count, second_count)

        runtime.stop(RunResult(status="succeeded"))

    def test_compact_runtime_prefers_running_invocation_for_output_tail(self) -> None:
        workflow = single_node_workflow()
        runtime = SimulatedTmuxRuntime(auto_close_session=True)
        runtime.start(
            RunContext(
                workflow_topology=topology_from_workflow(workflow),
                run_id="compact-running-preferred",
                refresh_per_second=0,
            )
        )

        state = build_initial_state(
            topology_from_workflow(workflow), run_id="compact-running-preferred"
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            first_log = Path(tmp_dir) / "round1.log"
            second_log = Path(tmp_dir) / "round2.log"
            first_log.write_text("header\\n---\\nround1-line\\n", encoding="utf-8")
            second_log.write_text("header\\n---\\nround2-line\\n", encoding="utf-8")

            apply_event(
                state,
                make_execution_event(
                    event_type="invocation_started",
                    workflow_name=workflow.name,
                    run_id="compact-running-preferred",
                    node_id="node.a",
                    provider="alpha",
                    role=ProviderRole.EXECUTOR,
                    model="m",
                    task_id="alpha_executor_0",
                    round_num=1,
                    log_file=str(first_log),
                ),
            )
            apply_event(
                state,
                make_execution_event(
                    event_type="invocation_finished",
                    workflow_name=workflow.name,
                    run_id="compact-running-preferred",
                    node_id="node.a",
                    provider="alpha",
                    role=ProviderRole.EXECUTOR,
                    model="m",
                    task_id="alpha_executor_0",
                    round_num=1,
                ),
            )
            apply_event(
                state,
                make_execution_event(
                    event_type="invocation_started",
                    workflow_name=workflow.name,
                    run_id="compact-running-preferred",
                    node_id="node.a",
                    provider="alpha",
                    role=ProviderRole.EXECUTOR,
                    model="m",
                    task_id="alpha_executor_0",
                    round_num=2,
                    log_file=str(second_log),
                ),
            )

            snapshot = DashboardSnapshot(
                state=state,
                layout=compute_topology_layout(topology_from_workflow(workflow)),
                now=0.0,
            )
            runtime.on_snapshot(None, snapshot)
            runtime.refresh_once()

            right_text = runtime.runtime_files.right_content.read_text(encoding="utf-8")  # type: ignore[union-attr]
            self.assertIn("round2", right_text)
            self.assertIn("round2-line", right_text)
            self.assertNotIn("round1-line", right_text)

        runtime.stop(RunResult(status="succeeded"))

    def test_compact_runtime_prefers_newest_running_invocation_in_same_round(
        self,
    ) -> None:
        workflow = single_node_workflow()
        runtime = SimulatedTmuxRuntime(auto_close_session=True)
        runtime.start(
            RunContext(
                workflow_topology=topology_from_workflow(workflow),
                run_id="compact-newest-running",
                refresh_per_second=0,
            )
        )

        state = build_initial_state(
            topology_from_workflow(workflow), run_id="compact-newest-running"
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            alpha_log = Path(tmp_dir) / "alpha.log"
            beta_log = Path(tmp_dir) / "beta.log"
            alpha_log.write_text("header\n---\nalpha-line\n", encoding="utf-8")
            beta_log.write_text("header\n---\nbeta-line\n", encoding="utf-8")

            apply_event(
                state,
                make_execution_event(
                    event_type="node_started",
                    workflow_name=workflow.name,
                    run_id="compact-newest-running",
                    node_id="node.a",
                    timestamp=9.0,
                ),
            )
            apply_event(
                state,
                make_execution_event(
                    event_type="invocation_started",
                    workflow_name=workflow.name,
                    run_id="compact-newest-running",
                    node_id="node.a",
                    provider="beta",
                    role=ProviderRole.EXECUTOR,
                    model="m",
                    task_id="beta_executor_0",
                    round_num=1,
                    log_file=str(beta_log),
                    timestamp=10.0,
                ),
            )
            apply_event(
                state,
                make_execution_event(
                    event_type="invocation_started",
                    workflow_name=workflow.name,
                    run_id="compact-newest-running",
                    node_id="node.a",
                    provider="alpha",
                    role=ProviderRole.EXECUTOR,
                    model="m",
                    task_id="alpha_executor_0",
                    round_num=1,
                    log_file=str(alpha_log),
                    timestamp=20.0,
                ),
            )

            snapshot = DashboardSnapshot(
                state=state,
                layout=compute_topology_layout(topology_from_workflow(workflow)),
                now=0.0,
            )
            runtime.on_snapshot(None, snapshot)
            runtime.refresh_once()

            right_text = runtime.runtime_files.right_content.read_text(encoding="utf-8")  # type: ignore[union-attr]
            self.assertIn("alpha-line", right_text)
            self.assertNotIn("beta-line", right_text)

        runtime.stop(RunResult(status="succeeded"))
