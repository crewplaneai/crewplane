from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

MODE_DASHBOARD = "dashboard"
MODE_INSPECT = "inspect"


@dataclass(frozen=True)
class RuntimeFiles:
    root: Path
    left_content: Path
    right_content: Path
    node_count: Path
    mode: Path
    selection_control: Path
    selected_invocation: Path
    inspect_invocation: Path
    quit_requested: Path

    @classmethod
    def from_root(cls, root: Path) -> RuntimeFiles:
        return cls(
            root=root,
            left_content=root / "left.txt",
            right_content=root / "right.txt",
            node_count=root / "node-count.txt",
            mode=root / "mode.txt",
            selection_control=root / "selection-control.json",
            selected_invocation=root / "selected-invocation.json",
            inspect_invocation=root / "inspect-invocation.json",
            quit_requested=root / "quit-requested.txt",
        )


def initial_runtime_file_contents(runtime_files: RuntimeFiles) -> dict[Path, str]:
    return {
        runtime_files.left_content: "Preparing dashboard...",
        runtime_files.right_content: "Waiting for node output...",
        runtime_files.node_count: "0",
        runtime_files.mode: MODE_DASHBOARD,
        runtime_files.selection_control: (
            '{"schema_version":1,"selected_index":-1,'
            '"selection_generation":0,"updated_at":0.0}\n'
        ),
        runtime_files.selected_invocation: "",
        runtime_files.inspect_invocation: "",
        runtime_files.quit_requested: "",
    }


def read_runtime_value(path: Path) -> str | None:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def read_runtime_mode(path: Path) -> str:
    value = read_runtime_value(path)
    if value in {MODE_DASHBOARD, MODE_INSPECT}:
        return value
    return MODE_DASHBOARD


def read_index(path: Path) -> int:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        return -1
    try:
        return int(value)
    except ValueError:
        return -1


def write_atomic(path: Path, content: str) -> None:
    temp_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    temp_path.write_text(content, encoding="utf-8")
    temp_path.replace(path)


def write_json_atomic(path: Path, value: object) -> None:
    write_atomic(path, json.dumps(value, sort_keys=True) + "\n")
