from __future__ import annotations

from pathlib import Path

from crewplane.observability.tmux.bindings import TmuxCompactKeyBindings
from crewplane.observability.tmux.runtime_files import (
    MODE_DASHBOARD,
    MODE_INSPECT,
    RuntimeFiles,
    initial_runtime_file_contents,
    write_atomic,
    write_json_atomic,
)
from crewplane.observability.tmux.session import TmuxSessionTargets
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
    assert ("crewplane-dashboard", "Enter") in table_keys
    assert ("crewplane-inspect", "Escape") in table_keys
    assert ("copy-mode", "Up") in table_keys
    assert ("copy-mode-vi", "WheelDownPane") in table_keys
    enter_binding = table_keys[("crewplane-dashboard", "Enter")]
    assert "crewplane.observability.tmux.inspect_control" in enter_binding
    assert "--view auto" in enter_binding
    assert "respawn-pane" not in enter_binding
    assert ("crewplane-dashboard", "r") in table_keys
    assert ("crewplane-inspect", "r") in table_keys
    assert ("crewplane-inspect", "f") in table_keys


def test_bindings_do_not_embed_dynamic_log_or_descriptor_values(
    tmp_path: Path,
) -> None:
    client = FakeTmuxClient()
    runtime_files = initialized_runtime_files(tmp_path)
    write_json_atomic(
        runtime_files.selected_invocation,
        {
            "schema_version": 1,
            "selection_generation": 0,
            "requested_selected_index": -1,
            "log_file": str(tmp_path / "bad;$(path).log"),
            "log_presentation_format": "json_lines",
            "log_presentation_profile": "mock",
        },
    )
    bindings = TmuxCompactKeyBindings(refresh_interval_seconds=1000.0)

    bindings.install(client, runtime_files, tmux_targets())

    commands = "\n".join(binding_keys(client.calls).values())
    assert "bad;$(path).log" not in commands
    assert "json_lines" not in commands
    assert "mock" not in commands


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
        "switch-client -T crewplane-dashboard"
        not in inspect_bindings[("copy-mode", "Up")]
    )

    client.calls.clear()
    bindings.sync_copy_mode_bindings(client, runtime_files, session, MODE_DASHBOARD)
    dashboard_bindings = binding_keys(client.calls)
    assert (
        "switch-client -T crewplane-dashboard"
        in dashboard_bindings[("copy-mode", "Up")]
    )


def initialized_runtime_files(root: Path) -> RuntimeFiles:
    runtime_files = RuntimeFiles.from_root(root)
    for path, content in initial_runtime_file_contents(runtime_files).items():
        write_atomic(path, content)
    return runtime_files


def tmux_targets() -> TmuxSessionTargets:
    return TmuxSessionTargets(
        session_name="crewplane-test",
        socket_name="socket",
        window_target="crewplane-test:dashboard",
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
