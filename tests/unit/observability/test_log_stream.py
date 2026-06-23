import tempfile
import unittest
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
from crewplane.observability.log_stream import NodeLogStreamTracker
from tests.helpers.observability import (
    make_execution_event,
    topology_from_workflow,
)


class NodeLogStreamTrackerTests(unittest.TestCase):
    def test_tracker_tails_latest_lines_and_skips_header(self) -> None:
        workflow = WorkflowPlan(
            name="log-stream",
            nodes=[
                WorkflowNode(
                    id="node.a",
                    mode="parallel",
                    prompt_segments=[
                        PromptSegment(role=PromptSegmentRole.SHARED, content="run")
                    ],
                    providers=[ProviderSpec(provider="alpha")],
                )
            ],
        )
        state = build_initial_state(topology_from_workflow(workflow), run_id="run-1")

        with tempfile.TemporaryDirectory() as tmp_dir:
            log_path = Path(tmp_dir) / "node.log"
            log_path.write_text(
                (
                    "started_at: 2026-04-10T00:00:00+00:00\n"
                    "cli_executable: alpha\n"
                    "model: m\n"
                    "output_file: out.md\n"
                    "---\n"
                    "line-1\n"
                    "line-2\n"
                    "line-3\n"
                ),
                encoding="utf-8",
            )

            apply_event(
                state,
                make_execution_event(
                    event_type="invocation_started",
                    workflow_name=workflow.name,
                    run_id="run-1",
                    node_id="node.a",
                    provider="alpha",
                    role=ProviderRole.EXECUTOR,
                    model="m",
                    task_id="alpha_executor_0",
                    log_file=str(log_path),
                ),
            )

            tracker = NodeLogStreamTracker(lines_per_node=2)
            tracker.refresh(state)
            lines = tracker.get_node_lines().get("node.a", [])
            self.assertEqual(lines, ["line-2", "line-3"])

            with log_path.open("a", encoding="utf-8") as handle:
                handle.write("line-4\n")
                handle.write("line-5")
            tracker.refresh(state)
            lines = tracker.get_node_lines().get("node.a", [])
            self.assertEqual(lines, ["line-3", "line-4"])

            with log_path.open("a", encoding="utf-8") as handle:
                handle.write("\n")
            tracker.refresh(state)
            lines = tracker.get_node_lines().get("node.a", [])
            self.assertEqual(lines, ["line-4", "line-5"])

    def test_zero_lines_disables_stream_capture(self) -> None:
        workflow = WorkflowPlan(
            name="log-disabled",
            nodes=[
                WorkflowNode(
                    id="node.a",
                    mode="parallel",
                    prompt_segments=[
                        PromptSegment(role=PromptSegmentRole.SHARED, content="run")
                    ],
                    providers=[ProviderSpec(provider="alpha")],
                )
            ],
        )
        state = build_initial_state(topology_from_workflow(workflow), run_id="run-2")
        tracker = NodeLogStreamTracker(lines_per_node=0)
        tracker.refresh(state)
        self.assertEqual(tracker.get_node_lines(), {})

    def test_tracker_tails_logs_without_header(self) -> None:
        workflow = WorkflowPlan(
            name="log-no-header",
            nodes=[
                WorkflowNode(
                    id="node.a",
                    mode="parallel",
                    prompt_segments=[
                        PromptSegment(role=PromptSegmentRole.SHARED, content="run")
                    ],
                    providers=[ProviderSpec(provider="alpha")],
                )
            ],
        )
        state = build_initial_state(topology_from_workflow(workflow), run_id="run-3")

        with tempfile.TemporaryDirectory() as tmp_dir:
            log_path = Path(tmp_dir) / "node.log"
            log_path.write_text(
                "line-1\nline-2\nline-3\n",
                encoding="utf-8",
            )

            apply_event(
                state,
                make_execution_event(
                    event_type="invocation_started",
                    workflow_name=workflow.name,
                    run_id="run-3",
                    node_id="node.a",
                    provider="alpha",
                    role=ProviderRole.EXECUTOR,
                    model="m",
                    task_id="alpha_executor_0",
                    log_file=str(log_path),
                ),
            )

            tracker = NodeLogStreamTracker(lines_per_node=2)
            tracker.refresh(state)
            lines = tracker.get_node_lines().get("node.a", [])
            self.assertEqual(lines, ["line-2", "line-3"])
