from __future__ import annotations

from pathlib import Path

from orchestrator_cli.observability.tmux.bindings import TmuxCompactKeyBindings
from orchestrator_cli.observability.tmux.runtime_files import (
    MODE_DASHBOARD,
    MODE_INSPECT,
    RuntimeFiles,
    initial_runtime_file_contents,
    write_atomic,
)
from orchestrator_cli.observability.tmux.session import TmuxSessionTargets
from tests.integration.observability.tmux_fakes import FakeTmuxClient


def test_bindings_install_dashboard_inspect_and_copy_mode_tables(
    tmp_path: Path,
) -> None:
    client = FakeTmuxClient()
    runtime_files = initialized_runtime_files(tmp_path)
    session = tmux_targets()
    bindings = TmuxCompactKeyBindings(refresh_interval_seconds=1000.0)

    bindings.install(client, runtime_files, session)

    table_keys = binding_keys(client.calls)
    assert ("orchestrator-dashboard", "Enter") in table_keys
    assert ("orchestrator-inspect", "Escape") in table_keys
    assert ("copy-mode", "Up") in table_keys
    assert ("copy-mode-vi", "WheelDownPane") in table_keys
    enter_binding = table_keys[("orchestrator-dashboard", "Enter")]
    assert "inspect-enter.sh" in enter_binding
    assert "respawn-pane" not in enter_binding


def test_copy_mode_bindings_switch_between_dashboard_and_inspect(
    tmp_path: Path,
) -> None:
    client = FakeTmuxClient()
    runtime_files = initialized_runtime_files(tmp_path)
    session = tmux_targets()
    bindings = TmuxCompactKeyBindings(refresh_interval_seconds=1000.0)
    bindings.install(client, runtime_files, session)

    client.calls.clear()
    bindings.sync_copy_mode_bindings(client, runtime_files, session, MODE_INSPECT)
    inspect_bindings = binding_keys(client.calls)
    assert "send-keys -X cursor-up" in inspect_bindings[("copy-mode", "Up")]
    assert (
        "switch-client -T orchestrator-dashboard"
        not in inspect_bindings[("copy-mode", "Up")]
    )

    client.calls.clear()
    bindings.sync_copy_mode_bindings(client, runtime_files, session, MODE_DASHBOARD)
    dashboard_bindings = binding_keys(client.calls)
    assert (
        "switch-client -T orchestrator-dashboard"
        in dashboard_bindings[("copy-mode", "Up")]
    )


def initialized_runtime_files(root: Path) -> RuntimeFiles:
    runtime_files = RuntimeFiles.from_root(root)
    for path, content in initial_runtime_file_contents(runtime_files).items():
        write_atomic(path, content)
    return runtime_files


def tmux_targets() -> TmuxSessionTargets:
    return TmuxSessionTargets(
        session_name="orchestrator-test",
        socket_name="socket",
        window_target="orchestrator-test:dashboard",
        left_pane_id="%10",
        right_pane_id="%20",
    )


def binding_keys(
    calls: list[tuple[list[str], bool, bool]],
) -> dict[tuple[str, str], str]:
    return {
        (args[2], args[3]): " ".join(args[4:])
        for args, _, _ in calls
        if len(args) >= 4 and args[0] == "bind-key" and args[1] == "-T"
    }
