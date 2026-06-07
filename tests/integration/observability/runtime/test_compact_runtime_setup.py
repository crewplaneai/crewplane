import unittest

from orchestrator_cli.observability.events import (
    build_initial_state,
)
from orchestrator_cli.observability.layout import compute_topology_layout
from orchestrator_cli.observability.tmux.commands import tmux_command_string
from orchestrator_cli.observability.tmux.compact import build_attach_command
from orchestrator_cli.observability.types import (
    DashboardSnapshot,
    RunContext,
    RunResult,
)
from tests.helpers.observability import topology_from_workflow
from tests.integration.observability.runtime.observability_runtime_helpers import (
    binding_map,
    pane_option_writes,
    single_node_workflow,
)
from tests.integration.observability.tmux_fakes import SimulatedTmuxRuntime


class CompactRuntimeSetupTests(unittest.TestCase):
    def test_build_attach_command_uses_iterm2_cc_on_macos(self) -> None:
        command = build_attach_command(
            session_name="run-x",
            env={"TERM_PROGRAM": "iTerm.app"},
            platform_name="darwin",
        )
        self.assertEqual(command, ["tmux", "-CC", "attach", "-t", "run-x"])

    def test_build_attach_command_defaults_to_plain_attach(self) -> None:
        command = build_attach_command(
            session_name="run-x",
            env={"TERM_PROGRAM": "Apple_Terminal"},
            platform_name="linux",
        )
        self.assertEqual(command, ["tmux", "attach", "-t", "run-x"])

    def test_build_attach_command_uses_dedicated_socket_when_provided(self) -> None:
        command = build_attach_command(
            session_name="run-x",
            env={"TERM_PROGRAM": "Apple_Terminal"},
            platform_name="linux",
            socket_name="orchestrator-test-socket",
        )
        self.assertEqual(
            command,
            ["tmux", "-L", "orchestrator-test-socket", "attach", "-t", "run-x"],
        )

    def test_tmux_command_string_uses_if_shell_command_separators(self) -> None:
        command_string = tmux_command_string(
            ["select-pane", "-t", "%10"],
            ["run-shell", "echo hi"],
        )
        self.assertEqual(command_string, "select-pane -t %10 ; run-shell 'echo hi'")

    def test_compact_runtime_builds_two_panes_and_key_bindings(self) -> None:
        workflow = single_node_workflow()
        runtime = SimulatedTmuxRuntime()
        runtime.start(
            RunContext(
                workflow_topology=topology_from_workflow(workflow),
                run_id="compact-run",
                refresh_per_second=0,
            )
        )

        commands = [args[0] for args, _, _ in runtime.calls]
        self.assertIn("new-session", commands)
        self.assertIn("split-window", commands)
        key_table_writes = [
            args
            for args, _, _ in runtime.calls
            if len(args) >= 5
            and args[:4]
            == ["set-option", "-t", "orchestrator-compact-run", "key-table"]
        ]
        self.assertEqual(key_table_writes[-1][4], "orchestrator-dashboard")

        bindings = binding_map(runtime.calls)
        self.assertNotIn(("root", "Up"), bindings)
        self.assertNotIn(("root", "Escape"), bindings)
        for key in {
            "Up",
            "Down",
            "PageUp",
            "PageDown",
            "WheelUpPane",
            "WheelDownPane",
            "Escape",
            "q",
        }:
            self.assertIn(("orchestrator-inspect", key), bindings)
        for table in {"orchestrator-dashboard", "copy-mode", "copy-mode-vi"}:
            self.assertIn((table, "Up"), bindings)
            self.assertIn((table, "Down"), bindings)
            self.assertIn((table, "Enter"), bindings)
            self.assertIn((table, "Escape"), bindings)
            self.assertIn((table, "q"), bindings)

        dashboard_up_binding = bindings[("orchestrator-dashboard", "Up")]
        self.assertIn("run-shell", dashboard_up_binding)
        self.assertNotIn("run-shell -b", dashboard_up_binding)
        self.assertIn("select-pane -t %10", dashboard_up_binding)
        self.assertNotIn("send-keys -X cancel", dashboard_up_binding)

        up_binding = bindings[("copy-mode", "Up")]
        self.assertIn("send-keys -X cancel", up_binding)
        self.assertIn("switch-client -T orchestrator-dashboard", up_binding)
        self.assertIn("run-shell", up_binding)
        self.assertNotIn("run-shell -b", up_binding)
        self.assertIn("select-pane -t %10", up_binding)
        self.assertNotIn("copy-mode -q -t", up_binding)

        dashboard_enter_binding = bindings[("orchestrator-dashboard", "Enter")]
        self.assertIn("run-shell", dashboard_enter_binding)
        self.assertIn("inspect-enter.sh", dashboard_enter_binding)
        self.assertLess(len(dashboard_enter_binding), 300)
        self.assertNotIn('[[ -n "$log_path" ]] || exit 0', dashboard_enter_binding)
        self.assertNotIn(
            'respawn-pane -k -t "$right_pane" tail -n +1 -F "$log_path"',
            dashboard_enter_binding,
        )
        self.assertNotIn(
            'set-option -t "$session_name" key-table orchestrator-inspect',
            dashboard_enter_binding,
        )
        self.assertNotIn("select-pane -t %20", dashboard_enter_binding)

        enter_binding = bindings[("copy-mode-vi", "Enter")]
        self.assertIn("send-keys -X cancel", enter_binding)
        self.assertIn("switch-client -T orchestrator-dashboard", enter_binding)
        self.assertIn("run-shell", enter_binding)
        self.assertIn("inspect-enter.sh", enter_binding)
        self.assertNotIn("select-pane -t %20", enter_binding)

        escape_binding = bindings[("orchestrator-dashboard", "Escape")]
        self.assertIn("select-pane -t %10", escape_binding)

        inspect_up_binding = bindings[("orchestrator-inspect", "Up")]
        self.assertIn("copy-mode -e -t %20", inspect_up_binding)
        self.assertIn("send-keys -X -t %20 cursor-up", inspect_up_binding)

        inspect_page_up_binding = bindings[("orchestrator-inspect", "PageUp")]
        self.assertIn("copy-mode -e -u -t %20", inspect_page_up_binding)

        inspect_wheel_binding = bindings[("orchestrator-inspect", "WheelUpPane")]
        self.assertIn("copy-mode -e -t %20", inspect_wheel_binding)
        self.assertIn("send-keys -X -N 5 -t %20 scroll-up", inspect_wheel_binding)

        inspect_escape_binding = bindings[("orchestrator-inspect", "Escape")]
        self.assertIn("run-shell", inspect_escape_binding)
        self.assertIn("inspect-exit.sh", inspect_escape_binding)
        self.assertLess(len(inspect_escape_binding), 300)
        self.assertNotIn(
            'respawn-pane -k -t "$right_pane" bash -lc', inspect_escape_binding
        )
        self.assertNotIn(
            'set-option -t "$session_name" key-table orchestrator-dashboard',
            inspect_escape_binding,
        )
        self.assertNotIn(
            'set-option -p -t "$right_pane" "$title_option" "Node Output"',
            inspect_escape_binding,
        )

        quit_binding = bindings[("copy-mode", "q")]
        self.assertIn("send-keys -X cancel", quit_binding)
        self.assertIn("switch-client -T orchestrator-dashboard", quit_binding)
        self.assertIn("quit-requested.txt", quit_binding)
        self.assertIn("kill-session -t orchestrator-compact-run", quit_binding)

        root_quit_binding = bindings[("root", "q")]
        self.assertIn("quit-requested.txt", root_quit_binding)
        self.assertIn("kill-session -t orchestrator-compact-run", root_quit_binding)
        self.assertNotIn("switch-client -T orchestrator-dashboard", root_quit_binding)

        inspect_quit_binding = bindings[("orchestrator-inspect", "q")]
        self.assertIn("quit-requested.txt", inspect_quit_binding)
        self.assertIn("kill-session -t orchestrator-compact-run", inspect_quit_binding)

        runtime.stop(RunResult(failed=False))

    def test_compact_runtime_overrides_mouse_bindings_in_all_live_tables(self) -> None:
        workflow = single_node_workflow()
        runtime = SimulatedTmuxRuntime()
        runtime.start(
            RunContext(
                workflow_topology=topology_from_workflow(workflow),
                run_id="compact-mouse",
                refresh_per_second=0,
            )
        )

        bindings = binding_map(runtime.calls)
        mouse_keys = {
            "MouseDown1Pane",
            "MouseUp1Pane",
            "MouseDrag1Pane",
            "MouseDragEnd1Pane",
            "SecondClick1Pane",
            "DoubleClick1Pane",
            "TripleClick1Pane",
            "WheelUpPane",
            "WheelDownPane",
        }
        for key in mouse_keys:
            dashboard_binding = bindings[("orchestrator-dashboard", key)]
            self.assertIn("select-pane -t =", dashboard_binding)
            self.assertNotIn("send-keys -X cancel", dashboard_binding)

        for table in {"copy-mode", "copy-mode-vi"}:
            for key in mouse_keys:
                binding = bindings[(table, key)]
                self.assertIn("send-keys -X cancel", binding)
                self.assertIn("switch-client -T orchestrator-dashboard", binding)
                self.assertIn("select-pane -t =", binding)
                self.assertNotIn("copy-mode -q -t", binding)

        runtime.stop(RunResult(failed=False))

    def test_compact_runtime_uses_pane_option_titles_without_select_pane_titles(
        self,
    ) -> None:
        workflow = single_node_workflow()
        runtime = SimulatedTmuxRuntime(auto_close_session=True)
        runtime.start(
            RunContext(
                workflow_topology=topology_from_workflow(workflow),
                run_id="compact-pane-titles",
                refresh_per_second=0,
            )
        )

        state = build_initial_state(
            topology_from_workflow(workflow), run_id="compact-pane-titles"
        )
        snapshot = DashboardSnapshot(
            state=state,
            layout=compute_topology_layout(topology_from_workflow(workflow)),
            now=0.0,
        )
        runtime.on_snapshot(None, snapshot)
        runtime.refresh_once()

        pane_border_format_writes = [
            args
            for args, _, _ in runtime.calls
            if args[:4]
            == [
                "set-window-option",
                "-t",
                "orchestrator-compact-pane-titles:dashboard",
                "pane-border-format",
            ]
        ]
        self.assertEqual(pane_border_format_writes[-1][4], "#{@orchestrator_title}")
        pane_title_writes = pane_option_writes(runtime.calls, "@orchestrator_title")
        self.assertIn(
            ("%10", "DAG Summary"), {(args[3], args[5]) for args in pane_title_writes}
        )
        self.assertIn(
            ("%20", "Node Output"), {(args[3], args[5]) for args in pane_title_writes}
        )
        self.assertIn(
            ("%20", "Node Output: node.a"),
            {(args[3], args[5]) for args in pane_title_writes},
        )
        title_select_pane_calls = [
            args
            for args, _, _ in runtime.calls
            if args and args[0] == "select-pane" and "-T" in args
        ]
        self.assertFalse(title_select_pane_calls)

        runtime.stop(RunResult(failed=False))
