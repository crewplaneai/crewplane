from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from orchestrator_cli.observability.tmux.runtime_files import (
    RuntimeFiles,
    initial_runtime_file_contents,
    write_atomic,
    write_json_atomic,
)
from orchestrator_cli.observability.tmux.selection_control import (
    SelectionControlState,
    move_selection,
    read_selection_control,
    write_selection_control,
)


@pytest.mark.parametrize(
    ("direction", "expected_index"),
    [
        ("up", 2),
        ("down", 1),
    ],
)
def test_move_selection_applies_direction_after_invalid_selection_normalization(
    tmp_path: Path,
    direction: str,
    expected_index: int,
) -> None:
    runtime_files = initialized_runtime_files(tmp_path)
    write_atomic(runtime_files.node_count, "3")
    write_selection_control(
        runtime_files,
        SelectionControlState(
            selected_index=99,
            selection_generation=4,
            updated_at=0.0,
        ),
    )

    move_selection(runtime_files, direction)

    state = read_selection_control(runtime_files)
    assert state.selected_index == expected_index
    assert state.selection_generation == 5


@pytest.mark.parametrize(
    ("direction", "expected_index"),
    [
        ("up", 1),
        ("down", 0),
    ],
)
def test_move_selection_starts_from_resolved_auto_selection(
    tmp_path: Path,
    direction: str,
    expected_index: int,
) -> None:
    runtime_files = initialized_runtime_files(tmp_path)
    write_atomic(runtime_files.node_count, "3")
    write_json_atomic(
        runtime_files.selected_invocation,
        {
            "schema_version": 1,
            "selection_generation": 0,
            "requested_selected_index": -1,
            "resolved_selected_index": 2,
            "node_id": "node.c",
        },
    )

    move_selection(runtime_files, direction)

    state = read_selection_control(runtime_files)
    assert state.selected_index == expected_index
    assert state.selection_generation == 1


def test_selection_control_module_runs_without_reimport_warning(tmp_path: Path) -> None:
    runtime_files = initialized_runtime_files(tmp_path)
    write_atomic(runtime_files.node_count, "2")

    result = subprocess.run(
        [
            sys.executable,
            "-W",
            "error",
            "-m",
            "orchestrator_cli.observability.tmux.selection_control",
            "--runtime-root",
            str(tmp_path),
            "--direction",
            "up",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def initialized_runtime_files(root: Path) -> RuntimeFiles:
    runtime_files = RuntimeFiles.from_root(root)
    for path, content in initial_runtime_file_contents(runtime_files).items():
        write_atomic(path, content)
    return runtime_files
