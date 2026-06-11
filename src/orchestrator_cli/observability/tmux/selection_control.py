from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from time import time
from typing import Any

from orchestrator_cli.observability.tmux.runtime_files import (
    RuntimeFiles,
    read_index,
    write_json_atomic,
)

SELECTION_CONTROL_SCHEMA_VERSION = 1
_SELECTED_INVOCATION_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class SelectionControlState:
    selected_index: int
    selection_generation: int
    updated_at: float
    schema_version: int = SELECTION_CONTROL_SCHEMA_VERSION


def read_selection_control(runtime_files: RuntimeFiles) -> SelectionControlState:
    try:
        value = json.loads(runtime_files.selection_control.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return initial_selection_control()
    if not isinstance(value, dict):
        return initial_selection_control()
    return selection_control_from_mapping(value)


def write_selection_control(
    runtime_files: RuntimeFiles,
    state: SelectionControlState,
) -> None:
    write_json_atomic(runtime_files.selection_control, asdict(state))


def move_selection(runtime_files: RuntimeFiles, direction: str) -> None:
    if direction not in {"up", "down"}:
        raise ValueError("selection move direction must be 'up' or 'down'")
    count = read_index(runtime_files.node_count)
    if count <= 0:
        return
    current = read_selection_control(runtime_files)
    selected_index = _starting_selected_index(runtime_files, current, count)
    if direction == "up":
        selected_index = (selected_index - 1 + count) % count
    else:
        selected_index = (selected_index + 1) % count
    write_selection_control(
        runtime_files,
        SelectionControlState(
            selected_index=selected_index,
            selection_generation=current.selection_generation + 1,
            updated_at=time(),
        ),
    )


def _starting_selected_index(
    runtime_files: RuntimeFiles,
    current: SelectionControlState,
    count: int,
) -> int:
    if _is_valid_index(current.selected_index, count):
        return current.selected_index
    resolved_index = _resolved_snapshot_index(runtime_files, current)
    if resolved_index is not None and _is_valid_index(resolved_index, count):
        return resolved_index
    return 0


def _resolved_snapshot_index(
    runtime_files: RuntimeFiles,
    current: SelectionControlState,
) -> int | None:
    try:
        value = json.loads(
            runtime_files.selected_invocation.read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    if value.get("schema_version") != _SELECTED_INVOCATION_SCHEMA_VERSION:
        return None
    if value.get("selection_generation") != current.selection_generation:
        return None
    if value.get("requested_selected_index") != current.selected_index:
        return None
    return _optional_int_value(value.get("resolved_selected_index"))


def _is_valid_index(index: int, count: int) -> bool:
    return 0 <= index < count


def initial_selection_control() -> SelectionControlState:
    return SelectionControlState(
        selected_index=-1,
        selection_generation=0,
        updated_at=0.0,
    )


def selection_control_from_mapping(value: dict[str, Any]) -> SelectionControlState:
    if value.get("schema_version") != SELECTION_CONTROL_SCHEMA_VERSION:
        return initial_selection_control()
    selected_index = _int_value(value.get("selected_index"), -1)
    selection_generation = max(0, _int_value(value.get("selection_generation"), 0))
    updated_at = _float_value(value.get("updated_at"), 0.0)
    return SelectionControlState(
        selected_index=selected_index,
        selection_generation=selection_generation,
        updated_at=updated_at,
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    move_selection(RuntimeFiles.from_root(args.runtime_root), args.direction)
    return 0


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Move compact tmux selection.")
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--direction", choices=("up", "down"), required=True)
    return parser.parse_args(argv)


def _int_value(value: object, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return value


def _optional_int_value(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _float_value(value: object, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
