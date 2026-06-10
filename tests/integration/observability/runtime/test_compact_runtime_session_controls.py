import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from orchestrator_cli.observability.events import (
    apply_event,
    build_initial_state,
)
from orchestrator_cli.observability.layout import compute_topology_layout
from orchestrator_cli.observability.tmux.client import TmuxCommandClient
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
    binding_map,
    pane_option_writes,
    single_node_workflow,
    two_node_workflow,
)
from tests.integration.observability.tmux_fakes import SimulatedTmuxRuntime


class CompactRuntimeSessionControlTests(unittest.TestCase):
    def test_compact_runtime_stop_respects_auto_close(self) -> None:
        workflow = single_node_workflow()
        runtime_close = SimulatedTmuxRuntime(auto_close_session=True)
        runtime_close.start(
            RunContext(
                workflow_topology=topology_from_workflow(workflow),
                run_id="compact-close",
                refresh_per_second=0,
            )
        )
        runtime_close.stop(result=RunResult(status="succeeded"))
        kill_calls = [
            args
            for args, _, _ in runtime_close.calls
            if args[:2] == ["kill-session", "-t"]
        ]
        self.assertEqual(len(kill_calls), 1)

        runtime_keep = SimulatedTmuxRuntime(auto_close_session=False)
        runtime_keep.start(
            RunContext(
                workflow_topology=topology_from_workflow(workflow),
                run_id="compact-keep",
                refresh_per_second=0,
            )
        )
        runtime_keep.stop(result=RunResult(status="succeeded"))
        keep_kill_calls = [
            args
            for args, _, _ in runtime_keep.calls
            if args[:2] == ["kill-session", "-t"]
        ]
        self.assertEqual(len(keep_kill_calls), 0)

    def test_compact_runtime_stop_renders_cancelled_status_for_preserved_session(
        self,
    ) -> None:
        workflow = single_node_workflow()
        run_id = "compact-cancelled-preserved"
        runtime = SimulatedTmuxRuntime(auto_close_session=False)
        runtime.start(
            RunContext(
                workflow_topology=topology_from_workflow(workflow),
                run_id=run_id,
                refresh_per_second=0,
            )
        )

        state = build_initial_state(topology_from_workflow(workflow), run_id=run_id)
        apply_event(
            state,
            make_execution_event(
                event_type="workflow_started",
                workflow_name=workflow.name,
                run_id=run_id,
            ),
        )
        snapshot = DashboardSnapshot(
            state=state,
            layout=compute_topology_layout(topology_from_workflow(workflow)),
            now=0.0,
        )
        runtime.on_snapshot(None, snapshot)
        runtime.refresh_once()
        runtime.write_runtime_file("quit_requested", "1")  # type: ignore[arg-type]

        runtime.stop(RunResult(status="cancelled", cancel_reason="ui_stop_requested"))

        status_left_writes = [
            args
            for args, _, _ in runtime.calls
            if len(args) >= 5 and args[0] == "set-option" and args[3] == "status-left"
        ]
        self.assertTrue(status_left_writes)
        self.assertIn("⏹ cancelled", status_left_writes[-1][4])
        self.assertNotIn("running", status_left_writes[-1][4])

    def test_compact_runtime_requests_stop_when_quit_file_is_written(self) -> None:
        workflow = single_node_workflow()
        runtime = SimulatedTmuxRuntime(auto_close_session=True)
        runtime.start(
            RunContext(
                workflow_topology=topology_from_workflow(workflow),
                run_id="compact-quit",
                refresh_per_second=0,
            )
        )
        self.assertFalse(runtime.stop_requested)

        runtime.write_runtime_file("quit_requested", "1")  # type: ignore[arg-type]
        runtime.refresh_once()

        self.assertTrue(runtime.stop_requested)

        runtime.stop(RunResult(status="succeeded"))

    def test_compact_runtime_status_bar_includes_blocked_count(self) -> None:
        workflow = single_node_workflow()
        runtime = SimulatedTmuxRuntime(auto_close_session=True)
        runtime.start(
            RunContext(
                workflow_topology=topology_from_workflow(workflow),
                run_id="compact-blocked",
                refresh_per_second=0,
            )
        )

        state = build_initial_state(
            topology_from_workflow(workflow), run_id="compact-blocked"
        )
        apply_event(
            state,
            make_execution_event(
                event_type="node_blocked",
                workflow_name=workflow.name,
                run_id="compact-blocked",
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

        status_right_writes = [
            args
            for args, _, _ in runtime.calls
            if len(args) >= 5 and args[0] == "set-option" and args[3] == "status-right"
        ]
        self.assertTrue(status_right_writes)
        self.assertIn("⛔ 1", status_right_writes[-1][4])

        runtime.stop(RunResult(status="succeeded"))

    def test_compact_runtime_stops_refresh_when_session_is_gone(self) -> None:
        workflow = single_node_workflow()
        runtime = SimulatedTmuxRuntime(auto_close_session=True)
        runtime.start(
            RunContext(
                workflow_topology=topology_from_workflow(workflow),
                run_id="compact-session-gone",
                refresh_per_second=0,
            )
        )

        state = build_initial_state(
            topology_from_workflow(workflow), run_id="compact-session-gone"
        )
        snapshot = DashboardSnapshot(
            state=state,
            layout=compute_topology_layout(topology_from_workflow(workflow)),
            now=0.0,
        )
        runtime.on_snapshot(None, snapshot)
        runtime.client.session_exists_value = False
        runtime.refresh_once()
        self.assertTrue(runtime.stop_requested)

        runtime.stop(RunResult(status="succeeded"))

    def test_compact_runtime_has_session_timeout_does_not_request_stop(self) -> None:
        workflow = single_node_workflow()
        runtime = SimulatedTmuxRuntime(auto_close_session=True)
        runtime.start(
            RunContext(
                workflow_topology=topology_from_workflow(workflow),
                run_id="compact-session-timeout",
                refresh_per_second=0,
            )
        )

        state = build_initial_state(
            topology_from_workflow(workflow), run_id="compact-session-timeout"
        )
        snapshot = DashboardSnapshot(
            state=state,
            layout=compute_topology_layout(topology_from_workflow(workflow)),
            now=0.0,
        )
        runtime.on_snapshot(None, snapshot)
        runtime.client.has_session_times_out = True
        runtime.refresh_once()

        self.assertFalse(runtime.stop_requested)

        runtime.stop(RunResult(status="succeeded"))

    def test_compact_runtime_display_message_timeout_uses_fallback_dimensions(
        self,
    ) -> None:
        workflow = single_node_workflow()
        runtime = SimulatedTmuxRuntime(auto_close_session=True)
        runtime.start(
            RunContext(
                workflow_topology=topology_from_workflow(workflow),
                run_id="compact-display-timeout",
                refresh_per_second=0,
            )
        )

        state = build_initial_state(
            topology_from_workflow(workflow), run_id="compact-display-timeout"
        )
        snapshot = DashboardSnapshot(
            state=state,
            layout=compute_topology_layout(topology_from_workflow(workflow)),
            now=0.0,
        )
        runtime.on_snapshot(None, snapshot)
        runtime.client.display_message_times_out = True
        runtime.refresh_once()

        left_text = runtime.runtime_files.left_content.read_text(encoding="utf-8")
        right_text = runtime.runtime_files.right_content.read_text(encoding="utf-8")
        self.assertIn("DAG Summary", left_text)
        self.assertTrue(right_text)
        self.assertFalse(runtime.stop_requested)

        runtime.stop(RunResult(status="succeeded"))

    def test_compact_runtime_tmux_command_times_out(self) -> None:
        warnings: list[str] = []
        tmux = TmuxCommandClient(
            tmux_executable=sys.executable,
            timeout_seconds=0.01,
            warning_sink=warnings.append,
        )

        result = tmux.run(
            ["-c", "import time; time.sleep(10)"],
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 124)
        self.assertTrue(warnings)

    def test_compact_runtime_retry_log_keeps_live_navigation_bindings(self) -> None:
        workflow = single_node_workflow()
        runtime = SimulatedTmuxRuntime(auto_close_session=True)
        runtime.start(
            RunContext(
                workflow_topology=topology_from_workflow(workflow),
                run_id="compact-retry-log",
                refresh_per_second=0,
            )
        )

        state = build_initial_state(
            topology_from_workflow(workflow), run_id="compact-retry-log"
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            log_path = Path(tmp_dir) / "node.log"
            log_path.write_text(
                "\n".join(
                    [
                        "started_at: 2026-04-10T00:00:00+00:00",
                        "cli_executable: gemini",
                        "model: gemini-3.1-pro-preview",
                        "output_file: out.md",
                        "---",
                        "[stderr] Attempt 1 failed with status 503. Retrying with backoff... GaxiosError: [{",
                        '[stderr]     "message": "The service is currently unavailable.",',
                        '[stderr]     "status": "UNAVAILABLE"',
                    ]
                ),
                encoding="utf-8",
            )

            apply_event(
                state,
                make_execution_event(
                    event_type="node_started",
                    workflow_name=workflow.name,
                    run_id="compact-retry-log",
                    node_id="node.a",
                    timestamp=9.0,
                ),
            )
            apply_event(
                state,
                make_execution_event(
                    event_type="invocation_started",
                    workflow_name=workflow.name,
                    run_id="compact-retry-log",
                    node_id="node.a",
                    provider="gemini",
                    role="executor",
                    model="gemini-3.1-pro-preview",
                    task_id="gemini_executor_0",
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
            self.assertIn(
                "Attempt 1 failed with status 503. Retrying with backoff...",
                right_text,
            )
            self.assertIn("UNAVAILABLE", right_text)

        bindings = binding_map(runtime.calls)
        self.assertNotIn(("root", "Up"), bindings)
        self.assertNotIn(("root", "Escape"), bindings)
        self.assertIn(("orchestrator-inspect", "Escape"), bindings)
        for table in {"orchestrator-dashboard", "copy-mode", "copy-mode-vi"}:
            self.assertIn((table, "Up"), bindings)
            self.assertIn((table, "Down"), bindings)
            self.assertIn((table, "Enter"), bindings)

        runtime.stop(RunResult(status="succeeded"))

    def test_compact_runtime_inspect_mode_keeps_locked_log_when_selection_changes(
        self,
    ) -> None:
        workflow = two_node_workflow()
        runtime = SimulatedTmuxRuntime(auto_close_session=True)
        runtime.start(
            RunContext(
                workflow_topology=topology_from_workflow(workflow),
                run_id="compact-inspect-lock",
                refresh_per_second=0,
            )
        )

        state = build_initial_state(
            topology_from_workflow(workflow), run_id="compact-inspect-lock"
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            first_log = Path(tmp_dir) / "node-a.log"
            second_log = Path(tmp_dir) / "node-b.log"
            first_log.write_text("header\n---\nnode-a-line\n", encoding="utf-8")
            second_log.write_text("header\n---\nnode-b-line\n", encoding="utf-8")

            apply_event(
                state,
                make_execution_event(
                    event_type="invocation_started",
                    workflow_name=workflow.name,
                    run_id="compact-inspect-lock",
                    node_id="node.a",
                    provider="alpha",
                    role="executor",
                    model="m",
                    task_id="alpha_executor_0",
                    round_num=1,
                    log_file=str(first_log),
                ),
            )
            apply_event(
                state,
                make_execution_event(
                    event_type="invocation_started",
                    workflow_name=workflow.name,
                    run_id="compact-inspect-lock",
                    node_id="node.b",
                    provider="beta",
                    role="executor",
                    model="m",
                    task_id="beta_executor_0",
                    round_num=1,
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

            runtime.write_runtime_file("mode", "inspect")  # type: ignore[arg-type]
            runtime.write_runtime_file("inspect_log", str(first_log))  # type: ignore[arg-type]
            runtime.write_runtime_file("inspect_node_id", "node.a")  # type: ignore[arg-type]
            runtime.write_runtime_file("selection_index", "1")  # type: ignore[arg-type]
            runtime.write_runtime_file("right_content", "inspection-active")  # type: ignore[arg-type]
            runtime.refresh_once()

            right_text = runtime.runtime_files.right_content.read_text(encoding="utf-8")  # type: ignore[union-attr]
            self.assertEqual(right_text, "inspection-active")
            self.assertEqual(
                runtime.runtime_files.selected_log.read_text(encoding="utf-8"),  # type: ignore[union-attr]
                str(second_log),
            )
            pane_title_writes = pane_option_writes(runtime.calls, "@orchestrator_title")
            self.assertIn(
                ("%20", "Node Log: node.a"),
                {(args[3], args[5]) for args in pane_title_writes},
            )

        runtime.stop(RunResult(status="succeeded"))

    def test_compact_runtime_switches_copy_mode_bindings_with_mode_changes(
        self,
    ) -> None:
        workflow = single_node_workflow()
        runtime = SimulatedTmuxRuntime(auto_close_session=True)
        runtime.start(
            RunContext(
                workflow_topology=topology_from_workflow(workflow),
                run_id="compact-copy-mode-sync",
                refresh_per_second=0,
            )
        )

        state = build_initial_state(
            topology_from_workflow(workflow), run_id="compact-copy-mode-sync"
        )
        snapshot = DashboardSnapshot(
            state=state,
            layout=compute_topology_layout(topology_from_workflow(workflow)),
            now=0.0,
        )
        runtime.on_snapshot(None, snapshot)

        runtime.calls.clear()
        runtime.write_runtime_file("mode", "inspect")  # type: ignore[arg-type]
        runtime.refresh_once()
        inspect_bindings = binding_map(runtime.calls)
        self.assertIn(("copy-mode", "Up"), inspect_bindings)
        self.assertIn(("copy-mode", "Down"), inspect_bindings)
        self.assertIn(("copy-mode", "PageUp"), inspect_bindings)
        self.assertIn(("copy-mode", "PageDown"), inspect_bindings)
        self.assertIn(("copy-mode", "WheelUpPane"), inspect_bindings)
        self.assertIn(("copy-mode", "WheelDownPane"), inspect_bindings)
        self.assertIn(("copy-mode", "Escape"), inspect_bindings)
        self.assertIn(("copy-mode", "q"), inspect_bindings)
        self.assertIn(("copy-mode-vi", "Enter"), inspect_bindings)
        self.assertNotIn(
            "switch-client -T orchestrator-dashboard",
            inspect_bindings[("copy-mode", "Up")],
        )
        self.assertIn("send-keys -X cursor-up", inspect_bindings[("copy-mode", "Up")])
        self.assertIn("send-keys -X page-up", inspect_bindings[("copy-mode", "PageUp")])
        self.assertIn(
            "send-keys -X -N 5 scroll-up",
            inspect_bindings[("copy-mode", "WheelUpPane")],
        )
        self.assertIn("run-shell", inspect_bindings[("copy-mode", "Escape")])
        self.assertIn("quit-requested.txt", inspect_bindings[("copy-mode", "q")])
        self.assertIn(
            "kill-session -t orchestrator-compact-copy-mode-sync",
            inspect_bindings[("copy-mode", "q")],
        )
        self.assertIn(
            "send-keys -X cancel", inspect_bindings[("copy-mode-vi", "Enter")]
        )

        runtime.calls.clear()
        runtime.write_runtime_file("mode", "dashboard")  # type: ignore[arg-type]
        runtime.refresh_once()
        restored_bindings = binding_map(runtime.calls)
        self.assertIn(("copy-mode", "Up"), restored_bindings)
        self.assertIn(("copy-mode-vi", "WheelUpPane"), restored_bindings)

        runtime.stop(RunResult(status="succeeded"))

    def test_compact_runtime_enter_binding_noops_without_selected_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            fake_tmux_log = root / "fake-tmux.log"
            fake_tmux = root / "fake-tmux.sh"

            fake_tmux.write_text(
                '#!/bin/sh\nprintf "%s\\n" "$@" >> "$FAKE_TMUX_LOG"\n',
                encoding="utf-8",
            )
            fake_tmux.chmod(0o755)

            workflow = single_node_workflow()
            runtime = SimulatedTmuxRuntime(
                auto_close_session=True,
                tmux_executable=str(fake_tmux),
            )
            runtime.start(
                RunContext(
                    workflow_topology=topology_from_workflow(workflow),
                    run_id="compact-no-log-enter",
                    refresh_per_second=0,
                )
            )
            try:
                state = build_initial_state(
                    topology_from_workflow(workflow), run_id="compact-no-log-enter"
                )
                snapshot = DashboardSnapshot(
                    state=state,
                    layout=compute_topology_layout(topology_from_workflow(workflow)),
                    now=0.0,
                )
                runtime.on_snapshot(None, snapshot)
                runtime.refresh_once()

                bindings = binding_map(runtime.calls)
                enter_binding = bindings[("orchestrator-dashboard", "Enter")]
                binding_parts = shlex.split(enter_binding)
                self.assertEqual(
                    binding_parts[:4], ["if-shell", "-F", "1", "run-shell"]
                )
                command = binding_parts[4]
                self.assertTrue(command.endswith("inspect-enter.sh"))
                self.assertTrue(Path(command).exists())
                subprocess.run(
                    [command],
                    check=True,
                    env={**os.environ, "FAKE_TMUX_LOG": str(fake_tmux_log)},
                )

                self.assertEqual(
                    runtime.runtime_files.mode.read_text(encoding="utf-8"),  # type: ignore[union-attr]
                    "dashboard",
                )
                self.assertEqual(
                    runtime.runtime_files.inspect_log.read_text(encoding="utf-8"),  # type: ignore[union-attr]
                    "",
                )
                self.assertEqual(
                    runtime.runtime_files.inspect_node_id.read_text(encoding="utf-8"),  # type: ignore[union-attr]
                    "",
                )
                self.assertFalse(fake_tmux_log.exists())
            finally:
                runtime.stop(RunResult(status="succeeded"))
