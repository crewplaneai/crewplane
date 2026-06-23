import os
import tempfile
import unittest
from pathlib import Path

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
    single_node_workflow,
)
from tests.integration.observability.tmux_fakes import SimulatedTmuxRuntime


class CompactRuntimeNodeStateTests(unittest.TestCase):
    def test_compact_runtime_pending_node_shows_dependency_wait_message(self) -> None:
        workflow = single_node_workflow()
        runtime = SimulatedTmuxRuntime(auto_close_session=True)
        runtime.start(
            RunContext(
                workflow_topology=topology_from_workflow(workflow),
                run_id="compact-pending",
                refresh_per_second=0,
            )
        )

        state = build_initial_state(
            topology_from_workflow(workflow), run_id="compact-pending"
        )
        snapshot = DashboardSnapshot(
            state=state,
            layout=compute_topology_layout(topology_from_workflow(workflow)),
            now=0.0,
        )
        runtime.on_snapshot(None, snapshot)
        runtime.refresh_once()

        right_text = runtime.runtime_files.right_content.read_text(encoding="utf-8")  # type: ignore[union-attr]
        self.assertIn("Waiting for dependencies to complete...", right_text)

        runtime.stop(RunResult(status="succeeded"))

    def test_compact_runtime_wraps_pending_message_without_ellipsis(self) -> None:
        workflow = single_node_workflow()
        runtime = SimulatedTmuxRuntime(auto_close_session=True)
        runtime.right_pane_width = 18
        runtime.start(
            RunContext(
                workflow_topology=topology_from_workflow(workflow),
                run_id="compact-pending-wrap",
                refresh_per_second=0,
            )
        )

        state = build_initial_state(
            topology_from_workflow(workflow), run_id="compact-pending-wrap"
        )
        snapshot = DashboardSnapshot(
            state=state,
            layout=compute_topology_layout(topology_from_workflow(workflow)),
            now=0.0,
        )
        runtime.on_snapshot(None, snapshot)
        runtime.refresh_once()

        right_text = runtime.runtime_files.right_content.read_text(encoding="utf-8")  # type: ignore[union-attr]
        self.assertIn(
            "Waiting for dependencies to complete...",
            "".join(right_text.splitlines()),
        )
        self.assertNotIn("Waiting for depe...", right_text)

        runtime.stop(RunResult(status="succeeded"))

    def test_compact_runtime_blocked_node_shows_causality(self) -> None:
        workflow = single_node_workflow()
        runtime = SimulatedTmuxRuntime(auto_close_session=True)
        runtime.start(
            RunContext(
                workflow_topology=topology_from_workflow(workflow),
                run_id="compact-blocked-reason",
                refresh_per_second=0,
            )
        )

        state = build_initial_state(
            topology_from_workflow(workflow), run_id="compact-blocked-reason"
        )
        apply_event(
            state,
            make_execution_event(
                event_type="node_blocked",
                workflow_name=workflow.name,
                run_id="compact-blocked-reason",
                node_id="node.a",
                error="unsatisfied dependencies: node.root",
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
        self.assertIn("Blocked: unsatisfied dependencies: node.root", right_text)

        runtime.stop(RunResult(status="succeeded"))

    def test_compact_runtime_running_invocation_without_output_shows_liveness(
        self,
    ) -> None:
        workflow = single_node_workflow()
        runtime = SimulatedTmuxRuntime(auto_close_session=True)
        runtime.monotonic_now_override = 40.0
        runtime.wall_time_now_override = 200.0
        runtime.start(
            RunContext(
                workflow_topology=topology_from_workflow(workflow),
                run_id="compact-no-output",
                refresh_per_second=0,
            )
        )

        state = build_initial_state(
            topology_from_workflow(workflow), run_id="compact-no-output"
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
                    run_id="compact-no-output",
                    node_id="node.a",
                    timestamp=9.0,
                ),
            )
            apply_event(
                state,
                make_execution_event(
                    event_type="invocation_started",
                    workflow_name=workflow.name,
                    run_id="compact-no-output",
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

            snapshot = DashboardSnapshot(
                state=state,
                layout=compute_topology_layout(topology_from_workflow(workflow)),
                now=0.0,
            )
            runtime.on_snapshot(None, snapshot)
            runtime.refresh_once()

            right_text = runtime.runtime_files.right_content.read_text(encoding="utf-8")  # type: ignore[union-attr]
            self.assertIn("Running for 30.0s", right_text)
            self.assertIn("Log file:", right_text)
            self.assertIn("updated 5.0s ago", right_text)
            self.assertIn("Awaiting first output from provider...", right_text)

        runtime.stop(RunResult(status="succeeded"))

    def test_compact_runtime_running_invocation_shows_quiet_state(self) -> None:
        workflow = single_node_workflow()
        runtime = SimulatedTmuxRuntime(
            auto_close_session=True,
            quiet_after_seconds=120.0,
        )
        runtime.monotonic_now_override = 210.0
        runtime.wall_time_now_override = 400.0
        runtime.start(
            RunContext(
                workflow_topology=topology_from_workflow(workflow),
                run_id="compact-quiet",
                refresh_per_second=0,
            )
        )

        state = build_initial_state(
            topology_from_workflow(workflow), run_id="compact-quiet"
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
            os.utime(log_path, (250.0, 250.0))

            apply_event(
                state,
                make_execution_event(
                    event_type="node_started",
                    workflow_name=workflow.name,
                    run_id="compact-quiet",
                    node_id="node.a",
                    timestamp=9.0,
                ),
            )
            apply_event(
                state,
                make_execution_event(
                    event_type="invocation_started",
                    workflow_name=workflow.name,
                    run_id="compact-quiet",
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

            snapshot = DashboardSnapshot(
                state=state,
                layout=compute_topology_layout(topology_from_workflow(workflow)),
                now=0.0,
            )
            runtime.on_snapshot(None, snapshot)
            runtime.refresh_once()

            right_text = runtime.runtime_files.right_content.read_text(encoding="utf-8")  # type: ignore[union-attr]
            self.assertIn("Running for 3m20s", right_text)
            self.assertIn("No new output for 2m30s.", right_text)
            self.assertIn("Provider still running; waiting for new output.", right_text)
            self.assertIn("tail-line", right_text)

        runtime.stop(RunResult(status="succeeded"))

    def test_compact_runtime_wraps_quiet_state_metadata(self) -> None:
        workflow = single_node_workflow()
        runtime = SimulatedTmuxRuntime(
            auto_close_session=True,
            quiet_after_seconds=120.0,
        )
        runtime.right_pane_width = 22
        runtime.monotonic_now_override = 210.0
        runtime.wall_time_now_override = 400.0
        runtime.start(
            RunContext(
                workflow_topology=topology_from_workflow(workflow),
                run_id="compact-quiet-wrap",
                refresh_per_second=0,
            )
        )

        state = build_initial_state(
            topology_from_workflow(workflow), run_id="compact-quiet-wrap"
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
            os.utime(log_path, (250.0, 250.0))

            apply_event(
                state,
                make_execution_event(
                    event_type="node_started",
                    workflow_name=workflow.name,
                    run_id="compact-quiet-wrap",
                    node_id="node.a",
                    timestamp=9.0,
                ),
            )
            apply_event(
                state,
                make_execution_event(
                    event_type="invocation_started",
                    workflow_name=workflow.name,
                    run_id="compact-quiet-wrap",
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

            snapshot = DashboardSnapshot(
                state=state,
                layout=compute_topology_layout(topology_from_workflow(workflow)),
                now=0.0,
            )
            runtime.on_snapshot(None, snapshot)
            runtime.refresh_once()

            right_text = runtime.runtime_files.right_content.read_text(encoding="utf-8")  # type: ignore[union-attr]
            self.assertIn("Provider still runnin", right_text)
            self.assertIn("Provider still running", right_text)
            self.assertIn("; waiting for new outp", right_text)
            self.assertIn("ut.", right_text)
            self.assertNotIn("running; waiting...", right_text)

        runtime.stop(RunResult(status="succeeded"))

    def test_compact_runtime_running_invocation_shows_quiet_state_at_threshold(
        self,
    ) -> None:
        workflow = single_node_workflow()
        runtime = SimulatedTmuxRuntime(
            auto_close_session=True,
            quiet_after_seconds=120.0,
        )
        runtime.monotonic_now_override = 130.0
        runtime.wall_time_now_override = 320.0
        runtime.start(
            RunContext(
                workflow_topology=topology_from_workflow(workflow),
                run_id="compact-quiet-threshold",
                refresh_per_second=0,
            )
        )

        state = build_initial_state(
            topology_from_workflow(workflow), run_id="compact-quiet-threshold"
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
            os.utime(log_path, (200.0, 200.0))

            apply_event(
                state,
                make_execution_event(
                    event_type="node_started",
                    workflow_name=workflow.name,
                    run_id="compact-quiet-threshold",
                    node_id="node.a",
                    timestamp=9.0,
                ),
            )
            apply_event(
                state,
                make_execution_event(
                    event_type="invocation_started",
                    workflow_name=workflow.name,
                    run_id="compact-quiet-threshold",
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

            snapshot = DashboardSnapshot(
                state=state,
                layout=compute_topology_layout(topology_from_workflow(workflow)),
                now=0.0,
            )
            runtime.on_snapshot(None, snapshot)
            runtime.refresh_once()

            right_text = runtime.runtime_files.right_content.read_text(encoding="utf-8")  # type: ignore[union-attr]
            self.assertIn("No new output for 2m00s.", right_text)
            self.assertIn("Provider still running; waiting for new output.", right_text)

        runtime.stop(RunResult(status="succeeded"))
