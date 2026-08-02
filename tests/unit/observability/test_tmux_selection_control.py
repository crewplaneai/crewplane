from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from crewplane.observability.tmux.runtime_files import (
    RuntimeFiles,
    initial_runtime_file_contents,
    write_atomic,
    write_json_atomic,
)
from crewplane.observability.tmux.selection_control import (
    SelectionControlState,
    main,
    move_selection,
    read_selection_control,
    selection_control_from_mapping,
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
            "crewplane.observability.tmux.selection_control",
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


@pytest.mark.parametrize(
    "contents",
    [
        pytest.param("not-json", id="malformed-json"),
        pytest.param("[]", id="non-mapping"),
        pytest.param('{"schema_version":2}', id="wrong-schema"),
    ],
)
def test_read_selection_control_falls_back_for_invalid_state(
    tmp_path: Path,
    contents: str,
) -> None:
    runtime_files = RuntimeFiles.from_root(tmp_path)
    runtime_files.selection_control.write_text(contents, encoding="utf-8")

    assert read_selection_control(runtime_files) == SelectionControlState(-1, 0, 0.0)


def test_read_selection_control_falls_back_when_file_is_missing(tmp_path: Path) -> None:
    assert read_selection_control(RuntimeFiles.from_root(tmp_path)) == (
        SelectionControlState(-1, 0, 0.0)
    )


def test_selection_control_mapping_normalizes_invalid_scalar_fields() -> None:
    state = selection_control_from_mapping(
        {
            "schema_version": 1,
            "selected_index": True,
            "selection_generation": -3,
            "updated_at": "now",
        }
    )

    assert state == SelectionControlState(-1, 0, 0.0)
    assert selection_control_from_mapping(
        {
            "schema_version": 1,
            "selected_index": 2,
            "selection_generation": 3,
            "updated_at": 4,
        }
    ) == SelectionControlState(2, 3, 4.0)


def test_move_selection_is_noop_without_nodes(tmp_path: Path) -> None:
    runtime_files = initialized_runtime_files(tmp_path)

    move_selection(runtime_files, "up")

    assert read_selection_control(runtime_files) == SelectionControlState(-1, 0, 0.0)


def test_move_selection_wraps_valid_current_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_files = initialized_runtime_files(tmp_path)
    write_atomic(runtime_files.node_count, "3")
    write_selection_control(runtime_files, SelectionControlState(2, 4, 0.0))
    monkeypatch.setattr(
        "crewplane.observability.tmux.selection_control.time",
        lambda: 12.5,
    )

    move_selection(runtime_files, "down")

    assert read_selection_control(runtime_files) == SelectionControlState(0, 5, 12.5)


def test_move_selection_rejects_invalid_direction(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="direction must be 'up' or 'down'"):
        move_selection(initialized_runtime_files(tmp_path), "left")


@pytest.mark.parametrize(
    "selected_invocation",
    [
        pytest.param("not-json", id="malformed-json"),
        pytest.param("[]", id="non-mapping"),
        pytest.param('{"schema_version":2}', id="wrong-schema"),
        pytest.param(
            '{"schema_version":1,"selection_generation":2}',
            id="generation-mismatch",
        ),
        pytest.param(
            '{"schema_version":1,"selection_generation":0,'
            '"requested_selected_index":2}',
            id="request-mismatch",
        ),
        pytest.param(
            '{"schema_version":1,"selection_generation":0,'
            '"requested_selected_index":-1,"resolved_selected_index":true}',
            id="invalid-resolved-index",
        ),
    ],
)
def test_move_selection_ignores_invalid_resolved_snapshot(
    tmp_path: Path,
    selected_invocation: str,
) -> None:
    runtime_files = initialized_runtime_files(tmp_path)
    write_atomic(runtime_files.node_count, "3")
    runtime_files.selected_invocation.write_text(
        selected_invocation,
        encoding="utf-8",
    )

    move_selection(runtime_files, "down")

    assert read_selection_control(runtime_files).selected_index == 1


def test_main_moves_selection_from_runtime_root(tmp_path: Path) -> None:
    runtime_files = initialized_runtime_files(tmp_path)
    write_atomic(runtime_files.node_count, "2")

    assert main(["--runtime-root", str(tmp_path), "--direction", "down"]) == 0
    assert read_selection_control(runtime_files).selected_index == 1


def initialized_runtime_files(root: Path) -> RuntimeFiles:
    runtime_files = RuntimeFiles.from_root(root)
    for path, content in initial_runtime_file_contents(runtime_files).items():
        write_atomic(path, content)
    return runtime_files
