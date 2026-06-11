import json
import tempfile
import unittest
from pathlib import Path

from orchestrator_cli.core.workflow_models import (
    PromptSegment,
    WorkflowNode,
    WorkflowPlan,
)
from orchestrator_cli.observability.events import (
    apply_event,
    build_initial_state,
)
from orchestrator_cli.observability.layout import compute_topology_layout
from orchestrator_cli.observability.types import (
    DashboardSnapshot,
    RunContext,
    RunResult,
)
from tests.helpers.observability import (
    make_execution_event,
    topology_from_workflow,
)
from tests.integration.observability.runtime.observability_runtime_helpers import (
    provider,
    single_node_workflow,
    two_node_workflow,
)
from tests.integration.observability.tmux_fakes import SimulatedTmuxRuntime


class CompactRuntimeRenderingTests(unittest.TestCase):
    def test_compact_runtime_renders_selected_node_log_output(self) -> None:
        workflow = two_node_workflow()
        runtime = SimulatedTmuxRuntime(auto_close_session=True)
        runtime.start(
            RunContext(
                workflow_topology=topology_from_workflow(workflow),
                run_id="compact-render",
                refresh_per_second=0,
            )
        )

        state = build_initial_state(
            topology_from_workflow(workflow), run_id="compact-render"
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            log_path = Path(tmp_dir) / "node.log"
            log_path.write_text(
                "\n".join(
                    [
                        "started_at: 2026-04-10T00:00:00+00:00",
                        "cli_executable: alpha",
                        "model: m",
                        "output_file: out.md",
                        "---",
                        "line-1",
                        "line-2",
                        "line-3",
                    ]
                ),
                encoding="utf-8",
            )

            apply_event(
                state,
                make_execution_event(
                    event_type="node_started",
                    workflow_name=workflow.name,
                    run_id="compact-render",
                    node_id="node.b",
                ),
            )
            apply_event(
                state,
                make_execution_event(
                    event_type="invocation_started",
                    workflow_name=workflow.name,
                    run_id="compact-render",
                    node_id="node.b",
                    provider="beta",
                    role="executor",
                    model="m",
                    task_id="beta_executor_0",
                    round_num=1,
                    log_file=str(log_path),
                ),
            )

            snapshot = DashboardSnapshot(
                state=state,
                layout=compute_topology_layout(topology_from_workflow(workflow)),
                now=0.0,
            )
            runtime.on_snapshot(None, snapshot)
            runtime.refresh_once()

            left_text = runtime.runtime_files.left_content.read_text(encoding="utf-8")  # type: ignore[union-attr]
            right_text = runtime.runtime_files.right_content.read_text(encoding="utf-8")  # type: ignore[union-attr]
            self.assertIn("▸", left_text)
            self.assertIn("node.b", left_text)
            self.assertIn("Node Output: node.b", right_text)
            self.assertIn("line-3", right_text)
            self.assertNotIn("Running for", right_text)
            self.assertNotIn("Log file:", right_text)
            self.assertNotIn(
                "Provider still running; waiting for new output.", right_text
            )

        runtime.stop(RunResult(status="succeeded"))

    def test_compact_runtime_renders_codex_nested_jsonl_log_content(self) -> None:
        workflow = single_node_workflow()
        runtime = SimulatedTmuxRuntime(
            auto_close_session=True,
            log_tail_lines=10,
        )
        runtime.start(
            RunContext(
                workflow_topology=topology_from_workflow(workflow),
                run_id="compact-codex-jsonl",
                refresh_per_second=0,
            )
        )

        state = build_initial_state(
            topology_from_workflow(workflow), run_id="compact-codex-jsonl"
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            log_path = Path(tmp_dir) / "codex.log"
            records = [
                {
                    "type": "item.completed",
                    "item": {
                        "type": "agent_message",
                        "text": "Nested Codex answer",
                    },
                },
                {
                    "type": "item.completed",
                    "item": {
                        "type": "web_search",
                        "action": {
                            "type": "search",
                            "query": "codex jsonl presentation",
                        },
                    },
                },
                {
                    "type": "item.completed",
                    "status": "completed",
                    "exit_code": 0,
                    "item": {
                        "type": "local_shell",
                        "command": "uv run python -m pytest -q tests/unit",
                        "stdout": "2 passed",
                    },
                },
            ]
            log_path.write_text(
                "\n".join(json.dumps(record) for record in records),
                encoding="utf-8",
            )

            apply_event(
                state,
                make_execution_event(
                    event_type="node_started",
                    workflow_name=workflow.name,
                    run_id="compact-codex-jsonl",
                    node_id="node.a",
                ),
            )
            apply_event(
                state,
                make_execution_event(
                    event_type="invocation_started",
                    workflow_name=workflow.name,
                    run_id="compact-codex-jsonl",
                    node_id="node.a",
                    provider="codex",
                    role="executor",
                    model="gpt-5",
                    task_id="codex_executor_0",
                    round_num=1,
                    log_file=str(log_path),
                    log_presentation_format="json_lines",
                    log_presentation_profile="codex",
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
            flattened_right_text = "".join(right_text.splitlines())
            right_lines = set(right_text.splitlines())
            self.assertIn("Nested Codex answer", right_text)
            self.assertIn("command: uv run python -m pytest -q tests/unit", right_text)
            self.assertIn("status: completed", right_text)
            self.assertIn("exit_code: 0", flattened_right_text)
            self.assertIn("stdout: 2 passed", right_text)
            self.assertIn(
                "web_search completed: codex jsonl presentation",
                right_text,
            )
            self.assertNotIn("agent_message completed", right_lines)
            self.assertNotIn("web_search completed", right_lines)

        runtime.stop(RunResult(status="succeeded"))

    def test_compact_runtime_left_pane_elapsed_uses_live_monotonic_time(self) -> None:
        workflow = single_node_workflow()
        runtime = SimulatedTmuxRuntime(auto_close_session=True)
        runtime.monotonic_now_override = 40.0
        runtime.start(
            RunContext(
                workflow_topology=topology_from_workflow(workflow),
                run_id="compact-left-elapsed",
                refresh_per_second=0,
            )
        )

        state = build_initial_state(
            topology_from_workflow(workflow), run_id="compact-left-elapsed"
        )
        apply_event(
            state,
            make_execution_event(
                event_type="node_started",
                workflow_name=workflow.name,
                run_id="compact-left-elapsed",
                node_id="node.a",
                timestamp=10.0,
            ),
        )

        snapshot = DashboardSnapshot(
            state=state,
            layout=compute_topology_layout(topology_from_workflow(workflow)),
            now=10.0,
        )
        runtime.on_snapshot(None, snapshot)
        runtime.refresh_once()

        left_text = runtime.runtime_files.left_content.read_text(encoding="utf-8")  # type: ignore[union-attr]
        self.assertIn("⏳ 30.0s alpha", left_text)
        self.assertNotIn("⏳ 0.0s alpha", left_text)

        runtime.stop(RunResult(status="succeeded"))

    def test_compact_runtime_respects_fixed_log_tail_limit(self) -> None:
        workflow = single_node_workflow()
        runtime = SimulatedTmuxRuntime(
            auto_close_session=True,
            log_tail_lines=2,
        )
        runtime.right_pane_height = 40
        runtime.start(
            RunContext(
                workflow_topology=topology_from_workflow(workflow),
                run_id="compact-fixed-tail",
                refresh_per_second=0,
            )
        )

        state = build_initial_state(
            topology_from_workflow(workflow), run_id="compact-fixed-tail"
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            log_path = Path(tmp_dir) / "node.log"
            log_path.write_text(
                "\n".join(
                    [
                        "started_at: 2026-04-10T00:00:00+00:00",
                        "cli_executable: alpha",
                        "model: m",
                        "output_file: out.md",
                        "---",
                        "line-1",
                        "line-2",
                        "line-3",
                        "line-4",
                    ]
                ),
                encoding="utf-8",
            )

            apply_event(
                state,
                make_execution_event(
                    event_type="invocation_started",
                    workflow_name=workflow.name,
                    run_id="compact-fixed-tail",
                    node_id="node.a",
                    provider="alpha",
                    role="executor",
                    model="m",
                    task_id="alpha_executor_0",
                    round_num=1,
                    log_file=str(log_path),
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
            self.assertIn("line-3", right_text)
            self.assertIn("line-4", right_text)
            self.assertNotIn("line-2", right_text)

        runtime.stop(RunResult(status="succeeded"))

    def test_compact_runtime_wraps_long_fixed_tail_lines(self) -> None:
        workflow = single_node_workflow()
        runtime = SimulatedTmuxRuntime(
            auto_close_session=True,
            log_tail_lines=1,
        )
        runtime.right_pane_width = 20
        runtime.right_pane_height = 40
        runtime.start(
            RunContext(
                workflow_topology=topology_from_workflow(workflow),
                run_id="compact-fixed-wrap",
                refresh_per_second=0,
            )
        )

        state = build_initial_state(
            topology_from_workflow(workflow), run_id="compact-fixed-wrap"
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            log_path = Path(tmp_dir) / "node.log"
            log_path.write_text(
                "\n".join(
                    [
                        "started_at: 2026-04-10T00:00:00+00:00",
                        "cli_executable: alpha",
                        "model: m",
                        "output_file: out.md",
                        "---",
                        "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcdefghijklmnop",
                    ]
                ),
                encoding="utf-8",
            )

            apply_event(
                state,
                make_execution_event(
                    event_type="invocation_started",
                    workflow_name=workflow.name,
                    run_id="compact-fixed-wrap",
                    node_id="node.a",
                    provider="alpha",
                    role="executor",
                    model="m",
                    task_id="alpha_executor_0",
                    round_num=1,
                    log_file=str(log_path),
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
            self.assertIn("ABCDEFGHIJKLMNOPQRST", right_text)
            self.assertIn("UVWXYZ0123456789abcd", right_text)
            self.assertIn("efghijklmnop", right_text)
            self.assertNotIn("...", right_text)

        runtime.stop(RunResult(status="succeeded"))

    def test_compact_runtime_wraps_header_status_and_invocation_header(self) -> None:
        workflow = WorkflowPlan(
            name="runtime.compact.wrap.headers",
            nodes=[
                WorkflowNode(
                    id="node.with.long.identifier",
                    mode="parallel",
                    prompt_segments=[PromptSegment(role="shared", content="a")],
                    providers=[provider("alpha")],
                ),
            ],
        )
        runtime = SimulatedTmuxRuntime(
            auto_close_session=True,
            log_tail_lines=1,
        )
        runtime.right_pane_width = 12
        runtime.right_pane_height = 40
        runtime.start(
            RunContext(
                workflow_topology=topology_from_workflow(workflow),
                run_id="compact-wrap-headers",
                refresh_per_second=0,
            )
        )

        state = build_initial_state(
            topology_from_workflow(workflow), run_id="compact-wrap-headers"
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            log_path = Path(tmp_dir) / "node.log"
            log_path.write_text(
                "\n".join(
                    [
                        "started_at: 2026-04-10T00:00:00+00:00",
                        "cli_executable: alpha",
                        "model: m",
                        "output_file: out.md",
                        "---",
                        "tail-line",
                    ]
                ),
                encoding="utf-8",
            )

            apply_event(
                state,
                make_execution_event(
                    event_type="node_started",
                    workflow_name=workflow.name,
                    run_id="compact-wrap-headers",
                    node_id="node.with.long.identifier",
                    timestamp=1.0,
                ),
            )
            apply_event(
                state,
                make_execution_event(
                    event_type="invocation_started",
                    workflow_name=workflow.name,
                    run_id="compact-wrap-headers",
                    node_id="node.with.long.identifier",
                    provider="alpha",
                    role="executor",
                    model="m",
                    task_id="alpha_executor_with_long_name_0",
                    round_num=1,
                    log_file=str(log_path),
                    timestamp=2.0,
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
            flattened_right_text = "".join(right_text.splitlines())
            self.assertIn(
                "Node Output: node.with.long.identifier", flattened_right_text
            )
            self.assertIn("Status: running", flattened_right_text)
            self.assertIn(
                "alpha/executor/alpha_executor_with_long_name_0 (round1) [running]",
                flattened_right_text,
            )
            self.assertNotIn("Node Outp...", right_text)
            self.assertNotIn("Status: ...", right_text)

        runtime.stop(RunResult(status="succeeded"))

    def test_compact_runtime_auto_sizes_log_tail_from_pane_height(self) -> None:
        workflow = single_node_workflow()
        runtime = SimulatedTmuxRuntime(
            auto_close_session=True,
            log_tail_lines=None,
        )
        runtime.right_pane_height = 8
        runtime.start(
            RunContext(
                workflow_topology=topology_from_workflow(workflow),
                run_id="compact-auto-tail",
                refresh_per_second=0,
            )
        )

        state = build_initial_state(
            topology_from_workflow(workflow), run_id="compact-auto-tail"
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            log_path = Path(tmp_dir) / "node.log"
            log_path.write_text(
                "\n".join(
                    [
                        "started_at: 2026-04-10T00:00:00+00:00",
                        "cli_executable: alpha",
                        "model: m",
                        "output_file: out.md",
                        "---",
                        "line-1",
                        "line-2",
                        "line-3",
                        "line-4",
                        "line-5",
                        "line-6",
                    ]
                ),
                encoding="utf-8",
            )

            apply_event(
                state,
                make_execution_event(
                    event_type="invocation_started",
                    workflow_name=workflow.name,
                    run_id="compact-auto-tail",
                    node_id="node.a",
                    provider="alpha",
                    role="executor",
                    model="m",
                    task_id="alpha_executor_0",
                    round_num=1,
                    log_file=str(log_path),
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
            self.assertIn("line-4", right_text)
            self.assertIn("line-5", right_text)
            self.assertIn("line-6", right_text)
            self.assertNotIn("line-3", right_text)

        runtime.stop(RunResult(status="succeeded"))

    def test_compact_runtime_auto_tail_budgets_wrapped_visual_rows(self) -> None:
        workflow = single_node_workflow()
        runtime = SimulatedTmuxRuntime(
            auto_close_session=True,
            log_tail_lines=None,
        )
        runtime.right_pane_width = 40
        runtime.right_pane_height = 9
        runtime.start(
            RunContext(
                workflow_topology=topology_from_workflow(workflow),
                run_id="compact-auto-wrap-budget",
                refresh_per_second=0,
            )
        )

        state = build_initial_state(
            topology_from_workflow(workflow), run_id="compact-auto-wrap-budget"
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            log_path = Path(tmp_dir) / "node.log"
            log_path.write_text(
                "\n".join(
                    [
                        "started_at: 2026-04-10T00:00:00+00:00",
                        "cli_executable: alpha",
                        "model: m",
                        "output_file: out.md",
                        "---",
                        "short-1",
                        "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcdefghijklmnop",
                        "short-2",
                    ]
                ),
                encoding="utf-8",
            )

            apply_event(
                state,
                make_execution_event(
                    event_type="invocation_started",
                    workflow_name=workflow.name,
                    run_id="compact-auto-wrap-budget",
                    node_id="node.a",
                    provider="alpha",
                    role="executor",
                    model="m",
                    task_id="alpha_executor_0",
                    round_num=1,
                    log_file=str(log_path),
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
            self.assertNotIn("short-1", right_text)
            self.assertIn("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcd", right_text)
            self.assertIn("efghijklmnop", right_text)
            self.assertIn("short-2", right_text)
            self.assertEqual(len(right_text.splitlines()), runtime.right_pane_height)

        runtime.stop(RunResult(status="succeeded"))
