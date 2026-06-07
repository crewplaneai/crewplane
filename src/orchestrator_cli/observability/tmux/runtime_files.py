from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

MODE_DASHBOARD = "dashboard"
MODE_INSPECT = "inspect"


@dataclass(frozen=True)
class RuntimeFiles:
    root: Path
    left_content: Path
    right_content: Path
    selection_index: Path
    node_count: Path
    mode: Path
    selected_node_id: Path
    selected_log: Path
    inspect_log: Path
    inspect_node_id: Path
    quit_requested: Path

    @classmethod
    def from_root(cls, root: Path) -> RuntimeFiles:
        return cls(
            root=root,
            left_content=root / "left.txt",
            right_content=root / "right.txt",
            selection_index=root / "selected-index.txt",
            node_count=root / "node-count.txt",
            mode=root / "mode.txt",
            selected_node_id=root / "selected-node-id.txt",
            selected_log=root / "selected-log-path.txt",
            inspect_log=root / "inspect-log-path.txt",
            inspect_node_id=root / "inspect-node-id.txt",
            quit_requested=root / "quit-requested.txt",
        )


def initial_runtime_file_contents(runtime_files: RuntimeFiles) -> dict[Path, str]:
    return {
        runtime_files.left_content: "Preparing dashboard...",
        runtime_files.right_content: "Waiting for node output...",
        runtime_files.selection_index: "-1",
        runtime_files.node_count: "0",
        runtime_files.mode: MODE_DASHBOARD,
        runtime_files.selected_node_id: "",
        runtime_files.selected_log: "",
        runtime_files.inspect_log: "",
        runtime_files.inspect_node_id: "",
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
    temp_path = path.with_suffix(f"{path.suffix}.tmp")
    temp_path.write_text(content, encoding="utf-8")
    temp_path.replace(path)
