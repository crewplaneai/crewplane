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


class CompactRuntimeControlRestoreTests(unittest.TestCase):
    def test_compact_runtime_auto_tail_tracks_pane_resize(self) -> None:
        workflow = single_node_workflow()
        runtime = SimulatedTmuxRuntime(
            auto_close_session=True,
            log_tail_lines=None,
        )
        runtime.right_pane_height = 8
        runtime.start(
            RunContext(
                workflow_topology=topology_from_workflow(workflow),
                run_id="compact-auto-resize",
                refresh_per_second=0,
            )
        )

        state = build_initial_state(
            topology_from_workflow(workflow), run_id="compact-auto-resize"
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
                    run_id="compact-auto-resize",
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
            initial_text = runtime.runtime_files.right_content.read_text(
                encoding="utf-8"
            )  # type: ignore[union-attr]

            runtime.right_pane_height = 10
            runtime.refresh_once()
            resized_text = runtime.runtime_files.right_content.read_text(
                encoding="utf-8"
            )  # type: ignore[union-attr]

            self.assertNotIn("line-2", initial_text)
            self.assertIn("line-2", resized_text)
            self.assertIn("line-6", resized_text)

        runtime.stop(RunResult(status="succeeded"))

    def test_compact_runtime_restores_dashboard_control_after_resize(self) -> None:
        resize_cases = {
            "width": lambda runtime: setattr(runtime, "right_pane_width", 78),
            "height": lambda runtime: setattr(runtime, "left_pane_height", 14),
            "both": lambda runtime: (
                setattr(runtime, "left_pane_width", 64),
                setattr(runtime, "right_pane_height", 18),
            ),
        }

        for case_name, mutate_geometry in resize_cases.items():
            with self.subTest(case_name=case_name):
                workflow = single_node_workflow()
                runtime = SimulatedTmuxRuntime(auto_close_session=True)
                try:
                    runtime.start(
                        RunContext(
                            workflow_topology=topology_from_workflow(workflow),
                            run_id=f"compact-resize-{case_name}",
                            refresh_per_second=0,
                        )
                    )

                    state = build_initial_state(
                        topology_from_workflow(workflow),
                        run_id=f"compact-resize-{case_name}",
                    )
                    snapshot = DashboardSnapshot(
                        state=state,
                        layout=compute_topology_layout(
                            topology_from_workflow(workflow)
                        ),
                        now=0.0,
                    )
                    runtime.on_snapshot(None, snapshot)
                    runtime.refresh_once()

                    runtime.calls.clear()
                    mutate_geometry(runtime)
                    runtime.refresh_once()

                    commands = [args for args, _, _ in runtime.calls]
                    self.assertIn(
                        ["send-keys", "-X", "-t", "%10", "cancel"],
                        commands,
                    )
                    self.assertIn(
                        ["send-keys", "-X", "-t", "%20", "cancel"],
                        commands,
                    )
                    self.assertIn(
                        [
                            "set-option",
                            "-t",
                            f"crewplane-compact-resize-{case_name}",
                            "key-table",
                            "crewplane-dashboard",
                        ],
                        commands,
                    )
                    self.assertIn(["select-pane", "-t", "%10"], commands)
                finally:
                    runtime.stop(RunResult(status="succeeded"))

    def test_compact_runtime_retries_failed_resize_control_restore(self) -> None:
        workflow = single_node_workflow()
        runtime = SimulatedTmuxRuntime(auto_close_session=True)
        runtime.start(
            RunContext(
                workflow_topology=topology_from_workflow(workflow),
                run_id="compact-resize-retry",
                refresh_per_second=0,
            )
        )

        try:
            state = build_initial_state(
                topology_from_workflow(workflow), run_id="compact-resize-retry"
            )
            snapshot = DashboardSnapshot(
                state=state,
                layout=compute_topology_layout(topology_from_workflow(workflow)),
                now=0.0,
            )
            runtime.on_snapshot(None, snapshot)
            runtime.refresh_once()

            runtime.calls.clear()
            runtime.client.fail_next_key_table_restore = True
            runtime.right_pane_width = 72
            runtime.refresh_once()
            first_restore_attempts = [
                args
                for args, _, _ in runtime.calls
                if args[:4]
                == [
                    "set-option",
                    "-t",
                    "crewplane-compact-resize-retry",
                    "key-table",
                ]
            ]
            self.assertEqual(len(first_restore_attempts), 1)

            runtime.calls.clear()
            runtime.refresh_once()
            retry_attempts = [
                args
                for args, _, _ in runtime.calls
                if args[:4]
                == [
                    "set-option",
                    "-t",
                    "crewplane-compact-resize-retry",
                    "key-table",
                ]
            ]
            self.assertEqual(len(retry_attempts), 1)
        finally:
            runtime.stop(RunResult(status="succeeded"))

    def test_compact_runtime_restores_control_on_first_snapshot(self) -> None:
        workflow = single_node_workflow()
        runtime = SimulatedTmuxRuntime(auto_close_session=True)
        runtime.start(
            RunContext(
                workflow_topology=topology_from_workflow(workflow),
                run_id="compact-first-snapshot-control",
                refresh_per_second=0,
            )
        )

        try:
            state = build_initial_state(
                topology_from_workflow(workflow),
                run_id="compact-first-snapshot-control",
            )
            snapshot = DashboardSnapshot(
                state=state,
                layout=compute_topology_layout(topology_from_workflow(workflow)),
                now=0.0,
            )
            runtime.calls.clear()
            runtime.on_snapshot(None, snapshot)
            runtime.refresh_once()

            commands = [args for args, _, _ in runtime.calls]
            self.assertIn(
                [
                    "set-option",
                    "-t",
                    "crewplane-compact-first-snapshot-control",
                    "key-table",
                    "crewplane-dashboard",
                ],
                commands,
            )
            self.assertIn(["select-pane", "-t", "%10"], commands)
        finally:
            runtime.stop(RunResult(status="succeeded"))

    def test_compact_runtime_preserves_inspect_control_after_resize(self) -> None:
        workflow = single_node_workflow()
        runtime = SimulatedTmuxRuntime(auto_close_session=True)
        runtime.start(
            RunContext(
                workflow_topology=topology_from_workflow(workflow),
                run_id="compact-inspect-resize",
                refresh_per_second=0,
            )
        )

        try:
            state = build_initial_state(
                topology_from_workflow(workflow), run_id="compact-inspect-resize"
            )
            snapshot = DashboardSnapshot(
                state=state,
                layout=compute_topology_layout(topology_from_workflow(workflow)),
                now=0.0,
            )
            runtime.on_snapshot(None, snapshot)
            runtime.write_runtime_file("mode", "inspect")  # type: ignore[arg-type]
            runtime.refresh_once()

            runtime.calls.clear()
            runtime.right_pane_width = 72
            runtime.right_pane_height = 20
            runtime.refresh_once()

            commands = [args for args, _, _ in runtime.calls]
            self.assertNotIn(
                ["send-keys", "-X", "-t", "%10", "cancel"],
                commands,
            )
            self.assertIn(
                [
                    "set-option",
                    "-t",
                    "crewplane-compact-inspect-resize",
                    "key-table",
                    "crewplane-inspect",
                ],
                commands,
            )
            self.assertIn(["select-pane", "-t", "%20"], commands)
        finally:
            runtime.stop(RunResult(status="succeeded"))
