from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from crewplane.observability.tmux import inspect_control
from crewplane.observability.tmux.runtime_files import (
    MODE_DASHBOARD,
    MODE_INSPECT,
    RuntimeFiles,
    initial_runtime_file_contents,
    write_atomic,
    write_json_atomic,
)


def test_inspect_control_rolls_back_runtime_state_when_respawn_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_files = initialized_runtime_files(tmp_path)
    log_file = tmp_path / "provider.log"
    log_file.write_text("provider output\n", encoding="utf-8")
    write_selected_snapshot(runtime_files, log_file)
    write_atomic(runtime_files.inspect_invocation, "previous snapshot")

    calls: list[tuple[list[str], bool]] = []

    def fail_respawn(
        command: list[str],
        check: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        calls.append((command, check))
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(inspect_control.subprocess, "run", fail_respawn)

    result = inspect_control.main(inspect_args(tmp_path))

    assert result == 0
    assert len(calls) == 1
    command, check = calls[0]
    assert command[:5] == ["tmux", "respawn-pane", "-k", "-t", "%20"]
    assert "crewplane.observability.tmux.inspect_launcher" in command[-1]
    assert str(runtime_files.inspect_invocation) in command[-1]
    assert check is True
    assert runtime_files.mode.read_text(encoding="utf-8") == MODE_DASHBOARD
    assert (
        runtime_files.inspect_invocation.read_text(encoding="utf-8")
        == "previous snapshot"
    )


def test_inspect_control_commits_runtime_state_after_respawn_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_files = initialized_runtime_files(tmp_path)
    log_file = tmp_path / "provider.log"
    log_file.write_text("provider output\n", encoding="utf-8")
    write_selected_snapshot(runtime_files, log_file)

    calls: list[tuple[list[str], bool]] = []

    def succeed(
        command: list[str],
        check: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        calls.append((command, check))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(inspect_control.subprocess, "run", succeed)

    result = inspect_control.main(inspect_args(tmp_path))

    assert result == 0
    assert runtime_files.mode.read_text(encoding="utf-8") == MODE_INSPECT
    assert '"inspect_view": "raw"' in runtime_files.inspect_invocation.read_text(
        encoding="utf-8"
    )
    assert calls[0][0][:5] == ["tmux", "respawn-pane", "-k", "-t", "%20"]
    assert calls[0][1] is True
    assert calls[1][0][:4] == ["tmux", "set-option", "-t", "session"]
    assert calls[1][1] is False
    assert calls[2] == (["tmux", "select-pane", "-t", "%20"], False)


@pytest.mark.parametrize("view", ["raw", "formatted"])
def test_inspect_control_switches_view_on_locked_inspect_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    view: str,
) -> None:
    runtime_files = initialized_runtime_files(tmp_path)
    locked_log = tmp_path / "locked.log"
    selected_log = tmp_path / "selected.log"
    locked_log.write_text("locked output\n", encoding="utf-8")
    selected_log.write_text("selected output\n", encoding="utf-8")
    write_atomic(runtime_files.mode, MODE_INSPECT)
    write_json_atomic(
        runtime_files.inspect_invocation,
        inspect_snapshot_data(locked_log, "node.a", 0),
    )
    write_json_atomic(
        runtime_files.selected_invocation,
        selected_snapshot_data(selected_log, "node.b", 1),
    )

    def succeed(
        command: list[str],
        check: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        assert isinstance(check, bool)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(inspect_control.subprocess, "run", succeed)

    result = inspect_control.main(inspect_args(tmp_path, view))

    assert result == 0
    snapshot = json.loads(runtime_files.inspect_invocation.read_text(encoding="utf-8"))
    assert snapshot["node_id"] == "node.a"
    assert snapshot["log_file"] == str(locked_log)
    assert snapshot["inspect_view"] == view


def test_inspect_control_ignores_selected_snapshot_in_locked_inspect_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_files = initialized_runtime_files(tmp_path)
    locked_log = tmp_path / "locked.log"
    locked_log.write_text("locked output\n", encoding="utf-8")
    write_atomic(runtime_files.mode, MODE_INSPECT)
    write_json_atomic(
        runtime_files.inspect_invocation,
        selected_snapshot_data(locked_log, "node.a", 0),
    )

    calls: list[tuple[list[str], bool]] = []

    def record(
        command: list[str],
        check: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        calls.append((command, check))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(inspect_control.subprocess, "run", record)

    assert inspect_control.main(inspect_args(tmp_path, "raw")) == 0
    assert calls == []
    assert json.loads(
        runtime_files.inspect_invocation.read_text(encoding="utf-8")
    ) == selected_snapshot_data(locked_log, "node.a", 0)


def initialized_runtime_files(root: Path) -> RuntimeFiles:
    runtime_files = RuntimeFiles.from_root(root)
    for path, content in initial_runtime_file_contents(runtime_files).items():
        write_atomic(path, content)
    return runtime_files


def write_selected_snapshot(runtime_files: RuntimeFiles, log_file: Path) -> None:
    write_json_atomic(
        runtime_files.selected_invocation,
        selected_snapshot_data(log_file, "node.a", 0),
    )


def selected_snapshot_data(
    log_file: Path,
    node_id: str,
    resolved_index: int,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "workflow_name": "workflow",
        "run_id": "run",
        "dashboard_generation": 1,
        "selection_generation": 0,
        "requested_selected_index": -1,
        "resolved_selected_index": resolved_index,
        "node_count": 2,
        "node_id": node_id,
        "written_at": 0.0,
        "log_file": str(log_file),
    }


def inspect_snapshot_data(
    log_file: Path,
    node_id: str,
    resolved_index: int,
) -> dict[str, object]:
    snapshot = selected_snapshot_data(log_file, node_id, resolved_index)
    snapshot.update(
        {
            "inspect_view": "formatted",
            "line_budget": 20,
            "created_at": 0.0,
            "log_presentation_format": "json_lines",
            "log_presentation_profile": "mock",
        }
    )
    return snapshot


def inspect_args(runtime_root: Path, view: str = "auto") -> list[str]:
    return [
        "--runtime-root",
        str(runtime_root),
        "--tmux-executable",
        "tmux",
        "--session-name",
        "session",
        "--right-pane-id",
        "%20",
        "--view",
        view,
    ]


@pytest.mark.parametrize("snapshot_kind", ["missing", "stale", "no-log", "no-format"])
def test_inspect_rejects_unusable_selection_without_changing_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, snapshot_kind: str
) -> None:
    runtime_files = initialized_runtime_files(tmp_path)
    snapshot = selected_snapshot_data(tmp_path / "provider.log", "node.a", 0)
    if snapshot_kind == "stale":
        snapshot["selection_generation"] = 99
    if snapshot_kind == "no-log":
        snapshot["log_file"] = None
    if snapshot_kind != "missing":
        write_json_atomic(runtime_files.selected_invocation, snapshot)
    before = {path: path.read_bytes() for path in tmp_path.iterdir()}

    def unexpected_command(*args: object, **kwargs: object) -> None:
        del args, kwargs
        pytest.fail("An unusable selection must not launch tmux")

    view = "formatted" if snapshot_kind == "no-format" else "auto"

    with monkeypatch.context() as patch:
        patch.setattr(inspect_control.subprocess, "run", unexpected_command)
        result = inspect_control.main(inspect_args(tmp_path, view))

    assert result == 0
    assert {path: path.read_bytes() for path in tmp_path.iterdir()} == before


def test_respawn_os_error_removes_new_snapshot_and_restores_absent_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_files = initialized_runtime_files(tmp_path)
    write_selected_snapshot(runtime_files, tmp_path / "provider.log")
    runtime_files.mode.unlink()
    runtime_files.inspect_invocation.unlink()

    def fail_launch(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise FileNotFoundError("tmux was removed")

    with monkeypatch.context() as patch:
        patch.setattr(inspect_control.subprocess, "run", fail_launch)
        result = inspect_control.main(inspect_args(tmp_path))

    assert result == 0
    assert not runtime_files.mode.exists()
    assert not runtime_files.inspect_invocation.exists()


def test_inspect_uses_socket_and_survives_control_activation_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_files = initialized_runtime_files(tmp_path)
    write_selected_snapshot(runtime_files, tmp_path / "provider.log")
    commands: list[list[str]] = []

    def run(command: list[str], check: bool) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if not check:
            raise OSError("socket disconnected")
        return subprocess.CompletedProcess(command, 0)

    with monkeypatch.context() as patch:
        patch.setattr(inspect_control.subprocess, "run", run)
        result = inspect_control.main(
            [*inspect_args(tmp_path), "--socket-name", "crewplane-test"]
        )

    assert result == 0
    assert [command[:3] for command in commands] == [
        ["tmux", "-L", "crewplane-test"]
    ] * 3
    assert [command[3] for command in commands] == [
        "respawn-pane",
        "set-option",
        "select-pane",
    ]
    assert runtime_files.mode.read_text(encoding="utf-8") == MODE_INSPECT


def test_restore_absent_runtime_file_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "removed.txt"

    inspect_control.restore_runtime_file(path, None)
    inspect_control.restore_runtime_file(path, None)

    assert not path.exists()
