from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from orchestrator_cli.observability.tmux import inspect_control
from orchestrator_cli.observability.tmux.runtime_files import (
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
    assert "orchestrator_cli.observability.tmux.inspect_launcher" in command[-1]
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
        {
            "schema_version": 1,
            "selection_generation": 0,
            "requested_selected_index": -1,
            "resolved_selected_index": 0,
            "node_id": "node.a",
            "log_file": str(locked_log),
            "log_presentation_format": "json_lines",
            "log_presentation_profile": "mock",
            "inspect_view": "formatted",
        },
    )
    write_json_atomic(
        runtime_files.selected_invocation,
        {
            "schema_version": 1,
            "selection_generation": 0,
            "requested_selected_index": -1,
            "resolved_selected_index": 1,
            "node_id": "node.b",
            "log_file": str(selected_log),
            "log_presentation_format": "json_lines",
            "log_presentation_profile": "mock",
        },
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


def initialized_runtime_files(root: Path) -> RuntimeFiles:
    runtime_files = RuntimeFiles.from_root(root)
    for path, content in initial_runtime_file_contents(runtime_files).items():
        write_atomic(path, content)
    return runtime_files


def write_selected_snapshot(runtime_files: RuntimeFiles, log_file: Path) -> None:
    write_json_atomic(
        runtime_files.selected_invocation,
        {
            "schema_version": 1,
            "selection_generation": 0,
            "requested_selected_index": -1,
            "resolved_selected_index": 0,
            "node_id": "node.a",
            "log_file": str(log_file),
        },
    )


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
