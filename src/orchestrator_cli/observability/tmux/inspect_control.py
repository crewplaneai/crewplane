from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Literal

from orchestrator_cli.observability.log_presentation.limits import (
    DEFAULT_FORMATTED_INSPECT_LINE_BUDGET,
)
from orchestrator_cli.observability.tmux.inspect_snapshot import (
    InspectView,
    has_valid_presentation,
    read_snapshot,
    selected_snapshot_is_current,
    write_inspect_snapshot,
)
from orchestrator_cli.observability.tmux.runtime_files import (
    MODE_INSPECT,
    RuntimeFiles,
    read_runtime_mode,
    write_atomic,
)
from orchestrator_cli.observability.tmux.selection_control import (
    read_selection_control,
)

RequestedView = Literal["auto", "raw", "formatted"]


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    runtime_files = RuntimeFiles.from_root(args.runtime_root)
    requested_view = args.view
    source_snapshot = inspect_source_snapshot(
        runtime_files,
        requested_view,
        read_runtime_mode(runtime_files.mode),
    )
    if source_snapshot is None:
        return 0
    if not source_snapshot.get("log_file"):
        return 0

    inspect_view = resolve_requested_view(requested_view, source_snapshot)
    if inspect_view is None:
        return 0
    previous_snapshot = read_optional_text(runtime_files.inspect_invocation)
    previous_mode = read_optional_text(runtime_files.mode)
    write_inspect_snapshot(
        runtime_files,
        source_snapshot,
        inspect_view=inspect_view,
        line_budget=max(1, args.line_budget),
    )
    if not respawn_inspect_pane(args, runtime_files):
        restore_runtime_file(runtime_files.inspect_invocation, previous_snapshot)
        restore_runtime_file(runtime_files.mode, previous_mode)
        return 0
    write_atomic(runtime_files.mode, MODE_INSPECT)
    activate_inspect_controls(args)
    return 0


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Control compact tmux inspect mode.")
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--tmux-executable", required=True)
    parser.add_argument("--socket-name")
    parser.add_argument("--session-name", required=True)
    parser.add_argument("--right-pane-id", required=True)
    parser.add_argument("--view", choices=("auto", "raw", "formatted"), required=True)
    parser.add_argument(
        "--line-budget",
        type=int,
        default=DEFAULT_FORMATTED_INSPECT_LINE_BUDGET,
    )
    return parser.parse_args(argv)


def inspect_source_snapshot(
    runtime_files: RuntimeFiles,
    requested_view: RequestedView,
    mode: str,
) -> dict[str, object] | None:
    if mode == MODE_INSPECT and requested_view in {"raw", "formatted"}:
        return read_snapshot(runtime_files.inspect_invocation)

    selected = read_snapshot(runtime_files.selected_invocation)
    if selected is None:
        return None
    control = read_selection_control(runtime_files)
    if not selected_snapshot_is_current(selected, control):
        return None
    return selected


def resolve_requested_view(
    requested_view: RequestedView,
    selected: dict[str, object],
) -> InspectView | None:
    if requested_view == "raw":
        return "raw"
    if requested_view == "formatted":
        return "formatted" if has_valid_presentation(selected) else None
    return "formatted" if has_valid_presentation(selected) else "raw"


def respawn_inspect_pane(
    args: argparse.Namespace,
    runtime_files: RuntimeFiles,
) -> bool:
    launcher = shlex.join(
        [
            sys.executable,
            "-m",
            "orchestrator_cli.observability.tmux.inspect_launcher",
            "--snapshot",
            str(runtime_files.inspect_invocation),
        ]
    )
    tmux = tmux_command(args.tmux_executable, args.socket_name)
    try:
        subprocess.run(
            [
                *tmux,
                "respawn-pane",
                "-k",
                "-t",
                args.right_pane_id,
                launcher,
            ],
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def activate_inspect_controls(args: argparse.Namespace) -> None:
    tmux = tmux_command(args.tmux_executable, args.socket_name)
    run_tmux_best_effort(
        [
            *tmux,
            "set-option",
            "-t",
            args.session_name,
            "key-table",
            "orchestrator-inspect",
        ]
    )
    run_tmux_best_effort([*tmux, "select-pane", "-t", args.right_pane_id])


def run_tmux_best_effort(command: list[str]) -> None:
    try:
        subprocess.run(command, check=False)
    except OSError:
        return


def tmux_command(tmux_executable: str, socket_name: str | None) -> list[str]:
    command = [tmux_executable]
    if socket_name:
        command.extend(["-L", socket_name])
    return command


def read_optional_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def restore_runtime_file(path: Path, content: str | None) -> None:
    if content is None:
        try:
            path.unlink()
        except FileNotFoundError:
            return
        return
    write_atomic(path, content)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
