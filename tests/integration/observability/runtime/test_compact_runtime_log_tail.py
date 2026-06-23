import os
import tempfile
import unittest
from pathlib import Path

from crewplane.observability.events import (
    apply_event,
    build_initial_state,
)
from crewplane.observability.layout import compute_topology_layout
from crewplane.observability.tmux.log_tail import read_log_tail
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
    single_node_workflow,
)
from tests.integration.observability.tmux_fakes import SimulatedTmuxRuntime


class CompactRuntimeLogTailTests(unittest.TestCase):
    def test_compact_runtime_omits_elapsed_when_started_at_missing(self) -> None:
        workflow = single_node_workflow()
        runtime = SimulatedTmuxRuntime(auto_close_session=True)
        runtime.monotonic_now_override = 40.0
        runtime.wall_time_now_override = 200.0
        runtime.start(
            RunContext(
                workflow_topology=topology_from_workflow(workflow),
                run_id="compact-no-started-at",
                refresh_per_second=0,
            )
        )

        state = build_initial_state(
            topology_from_workflow(workflow), run_id="compact-no-started-at"
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
                    ]
                ),
                encoding="utf-8",
            )
            os.utime(log_path, (195.0, 195.0))

            apply_event(
                state,
                make_execution_event(
                    event_type="node_started",
                    workflow_name=workflow.name,
                    run_id="compact-no-started-at",
                    node_id="node.a",
                    timestamp=9.0,
                ),
            )
            apply_event(
                state,
                make_execution_event(
                    event_type="invocation_started",
                    workflow_name=workflow.name,
                    run_id="compact-no-started-at",
                    node_id="node.a",
                    provider="alpha",
                    role="executor",
                    model="m",
                    task_id="alpha_executor_0",
                    round_num=1,
                    log_file=str(log_path),
                    timestamp=10.0,
                ),
            )
            invocation = next(iter(state.nodes["node.a"].invocations.values()))
            invocation.started_at = None

            snapshot = DashboardSnapshot(
                state=state,
                layout=compute_topology_layout(topology_from_workflow(workflow)),
                now=0.0,
            )
            runtime.on_snapshot(None, snapshot)
            runtime.refresh_once()

            right_text = runtime.runtime_files.right_content.read_text(encoding="utf-8")  # type: ignore[union-attr]
            self.assertNotIn("Running for", right_text)
            self.assertIn("Awaiting first output from provider...", right_text)

        runtime.stop(RunResult(status="succeeded"))

    def test_compact_runtime_preserves_missing_log_file_messages(self) -> None:
        workflow = single_node_workflow()
        runtime = SimulatedTmuxRuntime(auto_close_session=True)
        runtime.start(
            RunContext(
                workflow_topology=topology_from_workflow(workflow),
                run_id="compact-missing-log",
                refresh_per_second=0,
            )
        )

        state = build_initial_state(
            topology_from_workflow(workflow), run_id="compact-missing-log"
        )
        apply_event(
            state,
            make_execution_event(
                event_type="node_started",
                workflow_name=workflow.name,
                run_id="compact-missing-log",
                node_id="node.a",
            ),
        )

        apply_event(
            state,
            make_execution_event(
                event_type="invocation_started",
                workflow_name=workflow.name,
                run_id="compact-missing-log",
                node_id="node.a",
                provider="alpha",
                role="executor",
                model="m",
                task_id="alpha_executor_0",
                round_num=1,
            ),
        )
        snapshot = DashboardSnapshot(
            state=state,
            layout=compute_topology_layout(topology_from_workflow(workflow)),
            now=0.0,
        )
        runtime.on_snapshot(None, snapshot)
        runtime.refresh_once()
        unavailable_text = runtime.runtime_files.right_content.read_text(
            encoding="utf-8"
        )  # type: ignore[union-attr]
        self.assertIn("Log file unavailable for this invocation.", unavailable_text)

        with tempfile.TemporaryDirectory() as tmp_dir:
            missing_path = Path(tmp_dir) / "missing.log"
            invocation = next(iter(state.nodes["node.a"].invocations.values()))
            invocation.log_file = str(missing_path)

            snapshot = DashboardSnapshot(
                state=state,
                layout=compute_topology_layout(topology_from_workflow(workflow)),
                now=0.0,
            )
            runtime.on_snapshot(None, snapshot)
            runtime.refresh_once()

            missing_text = runtime.runtime_files.right_content.read_text(
                encoding="utf-8"
            )  # type: ignore[union-attr]
            self.assertIn(f"Log file not found: {missing_path}", missing_text)

        runtime.stop(RunResult(status="succeeded"))

    def test_compact_runtime_wraps_missing_log_message(self) -> None:
        workflow = single_node_workflow()
        runtime = SimulatedTmuxRuntime(auto_close_session=True)
        runtime.right_pane_width = 20
        runtime.start(
            RunContext(
                workflow_topology=topology_from_workflow(workflow),
                run_id="compact-missing-log-wrap",
                refresh_per_second=0,
            )
        )

        state = build_initial_state(
            topology_from_workflow(workflow), run_id="compact-missing-log-wrap"
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            missing_path = Path(tmp_dir) / "missing.log"

            apply_event(
                state,
                make_execution_event(
                    event_type="invocation_started",
                    workflow_name=workflow.name,
                    run_id="compact-missing-log-wrap",
                    node_id="node.a",
                    provider="alpha",
                    role="executor",
                    model="m",
                    task_id="alpha_executor_0",
                    round_num=1,
                    log_file=str(missing_path),
                ),
            )
            snapshot = DashboardSnapshot(
                state=state,
                layout=compute_topology_layout(topology_from_workflow(workflow)),
                now=0.0,
            )
            runtime.on_snapshot(None, snapshot)
            runtime.refresh_once()

            missing_text = runtime.runtime_files.right_content.read_text(
                encoding="utf-8"
            )  # type: ignore[union-attr]
            self.assertIn("Log file not found: ", missing_text)
            self.assertIn(str(missing_path)[0:20], missing_text)
            self.assertNotIn("Log file not foun...", missing_text)

        runtime.stop(RunResult(status="succeeded"))

    def test_read_log_tail_preserves_yaml_like_small_log_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            log_path = Path(tmp_dir) / "yaml-small.log"
            log_path.write_text(
                "\n".join(
                    [
                        "name: review",
                        "status: running",
                        "---",
                        "provider-output",
                    ]
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                read_log_tail(log_path, 10),
                ["name: review", "status: running", "---", "provider-output"],
            )

    def test_read_log_tail_preserves_yaml_like_large_log_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            log_path = Path(tmp_dir) / "yaml-large.log"
            repeated_lines = [f"body-line-{index}" for index in range(9000)]
            log_path.write_text(
                "\n".join(
                    [
                        "name: review",
                        "status: running",
                        "---",
                        *repeated_lines,
                    ]
                ),
                encoding="utf-8",
            )

            tail_lines = read_log_tail(log_path, 9005)
            self.assertEqual(
                tail_lines[:4],
                ["name: review", "status: running", "---", "body-line-0"],
            )
            self.assertEqual(tail_lines[-1], "body-line-8999")

    def test_read_log_tail_bounded_returns_last_body_lines_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            log_path = Path(tmp_dir) / "yaml-bounded.log"
            # Header needs to match prefixes to be recognized as header
            header_lines = [
                "started_at: 2026-04-10T12:00:00",
                "cli_executable: /usr/bin/echo",
                "model: mock-model",
                "output_file: output.md",
            ]
            body_lines = [f"body-line-{index}" for index in range(10000)]
            log_path.write_text(
                "\n".join(
                    [
                        *header_lines,
                        "---",
                        *body_lines,
                    ]
                ),
                encoding="utf-8",
            )

            # Test bounded tail (4 lines) returns only last 4 body lines
            tail_lines = read_log_tail(log_path, 4)
            self.assertEqual(
                tail_lines,
                [
                    "body-line-9996",
                    "body-line-9997",
                    "body-line-9998",
                    "body-line-9999",
                ],
            )

            # Test bounded tail (10005 lines) returns full body but skips header
            tail_lines = read_log_tail(log_path, 10005)
            self.assertEqual(tail_lines[0], "body-line-0")
            self.assertEqual(len(tail_lines), 10000)
