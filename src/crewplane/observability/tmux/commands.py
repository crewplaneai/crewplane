from __future__ import annotations

import os
import shlex
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from crewplane.observability.tmux.runtime_files import (
    MODE_DASHBOARD,
    MODE_INSPECT,
    RuntimeFiles,
)
from crewplane.observability.tmux.session import TmuxSessionTargets
from crewplane.observability.tmux.shell_commands import (
    pane_render_args,
    pane_render_command,
    tmux_bash_array,
    tmux_command_string,
    write_runtime_shell_script,
)

DASHBOARD_KEY_TABLE = "crewplane-dashboard"
INSPECT_KEY_TABLE = "crewplane-inspect"
ROOT_KEY_TABLE = "root"
COPY_MODE_KEY_TABLES = ("copy-mode", "copy-mode-vi")
_MOUSE_SCROLL_REPEAT_COUNT = "5"
LIVE_MOUSE_KEYS = (
    "MouseDown1Pane",
    "MouseUp1Pane",
    "MouseDrag1Pane",
    "MouseDragEnd1Pane",
    "SecondClick1Pane",
    "DoubleClick1Pane",
    "TripleClick1Pane",
    "WheelUpPane",
    "WheelDownPane",
)
PANE_TITLE_OPTION = "@crewplane_title"

__all__ = [
    "COPY_MODE_KEY_TABLES",
    "DASHBOARD_KEY_TABLE",
    "INSPECT_KEY_TABLE",
    "InspectCommandContext",
    "LIVE_MOUSE_KEYS",
    "PANE_TITLE_OPTION",
    "ROOT_KEY_TABLE",
    "build_attach_command",
    "copy_mode_binding_commands",
    "dashboard_key_bindings",
    "focus_commands",
    "inspect_copy_mode_key_bindings",
    "inspect_enter_command",
    "inspect_exit_command",
    "inspect_formatted_command",
    "inspect_key_bindings",
    "inspect_raw_command",
    "pane_render_command",
    "quit_dashboard_commands",
    "selection_move_command",
    "tmux_command_string",
]


@dataclass(frozen=True)
class InspectCommandContext:
    tmux_executable: str
    runtime_files: RuntimeFiles
    session: TmuxSessionTargets


def build_attach_command(
    session_name: str,
    env: Mapping[str, str] | None = None,
    platform_name: str | None = None,
    tmux_executable: str = "tmux",
    socket_name: str | None = None,
) -> list[str]:
    """Build the tmux attach command for the current terminal environment."""

    resolved_env = dict(os.environ) if env is None else dict(env)
    resolved_platform = sys.platform if platform_name is None else platform_name
    command = [tmux_executable]
    if socket_name:
        command.extend(["-L", socket_name])
    if (
        resolved_platform == "darwin"
        and resolved_env.get("TERM_PROGRAM") == "iTerm.app"
    ):
        command.extend(["-CC", "attach", "-t", session_name])
        return command
    command.extend(["attach", "-t", session_name])
    return command


def focus_commands(target_pane: str) -> list[list[str]]:
    return [["select-pane", "-t", target_pane]]


def copy_mode_binding_commands(commands: list[list[str]]) -> list[list[str]]:
    return [
        ["send-keys", "-X", "cancel"],
        ["switch-client", "-T", DASHBOARD_KEY_TABLE],
        *commands,
    ]


def quit_dashboard_commands(
    session_name: str,
    quit_requested_path: Path,
) -> list[list[str]]:
    script = " ".join(
        [
            f"quit_file={shlex.quote(str(quit_requested_path))}",
            ";",
            'printf "1" > "${quit_file}.tmp"',
            ";",
            'mv "${quit_file}.tmp" "$quit_file"',
        ]
    )
    return [
        ["run-shell", f"bash -lc {shlex.quote(script)}"],
        ["kill-session", "-t", session_name],
    ]


def inspect_key_bindings(
    right_pane_id: str,
    exit_inspect: str,
    raw_inspect: str,
    formatted_inspect: str,
    quit_commands: list[list[str]],
) -> dict[str, list[list[str]]]:
    enter_copy_mode = [
        ["select-pane", "-t", right_pane_id],
        ["copy-mode", "-e", "-t", right_pane_id],
    ]
    return {
        "Up": [
            *enter_copy_mode,
            ["send-keys", "-X", "-t", right_pane_id, "cursor-up"],
        ],
        "Down": [
            *enter_copy_mode,
            ["send-keys", "-X", "-t", right_pane_id, "cursor-down"],
        ],
        "PageUp": [
            ["select-pane", "-t", right_pane_id],
            ["copy-mode", "-e", "-u", "-t", right_pane_id],
        ],
        "PageDown": [
            *enter_copy_mode,
            ["send-keys", "-X", "-t", right_pane_id, "page-down"],
        ],
        "WheelUpPane": [
            *enter_copy_mode,
            [
                "send-keys",
                "-X",
                "-N",
                _MOUSE_SCROLL_REPEAT_COUNT,
                "-t",
                right_pane_id,
                "scroll-up",
            ],
        ],
        "WheelDownPane": [
            *enter_copy_mode,
            [
                "send-keys",
                "-X",
                "-N",
                _MOUSE_SCROLL_REPEAT_COUNT,
                "-t",
                right_pane_id,
                "scroll-down",
            ],
        ],
        "r": [["run-shell", raw_inspect]],
        "f": [["run-shell", formatted_inspect]],
        "Escape": [["run-shell", exit_inspect]],
        "q": quit_commands,
    }


def inspect_copy_mode_key_bindings(
    exit_inspect: str,
    raw_inspect: str | None = None,
    formatted_inspect: str | None = None,
    quit_commands: list[list[str]] | None = None,
) -> dict[str, list[list[str]]]:
    exit_copy_mode = [
        ["send-keys", "-X", "cancel"],
        ["run-shell", exit_inspect],
    ]
    quit_copy_mode = [
        ["send-keys", "-X", "cancel"],
        *(
            quit_commands
            if quit_commands is not None
            else [["run-shell", exit_inspect]]
        ),
    ]
    raw_copy_mode = [["send-keys", "-X", "cancel"]]
    raw_copy_mode.extend([["run-shell", raw_inspect]] if raw_inspect else [])
    formatted_copy_mode = [["send-keys", "-X", "cancel"]]
    formatted_copy_mode.extend(
        [["run-shell", formatted_inspect]] if formatted_inspect else []
    )
    return {
        "Up": [["send-keys", "-X", "cursor-up"]],
        "Down": [["send-keys", "-X", "cursor-down"]],
        "PageUp": [["send-keys", "-X", "page-up"]],
        "PageDown": [["send-keys", "-X", "page-down"]],
        "Enter": [["send-keys", "-X", "cancel"]],
        "r": raw_copy_mode,
        "f": formatted_copy_mode,
        "Escape": exit_copy_mode,
        "q": quit_copy_mode,
        "MouseDown1Pane": [["select-pane", "-t", "="]],
        "MouseUp1Pane": [["select-pane", "-t", "="]],
        "MouseDrag1Pane": [
            ["select-pane", "-t", "="],
            ["send-keys", "-X", "begin-selection"],
        ],
        "MouseDragEnd1Pane": [["send-keys", "-X", "copy-pipe-and-cancel"]],
        "SecondClick1Pane": [["select-pane", "-t", "="]],
        "DoubleClick1Pane": [
            ["select-pane", "-t", "="],
            ["send-keys", "-X", "select-word"],
            ["run-shell", "-d", "0.3"],
            ["send-keys", "-X", "copy-pipe-and-cancel"],
        ],
        "TripleClick1Pane": [
            ["select-pane", "-t", "="],
            ["send-keys", "-X", "select-line"],
            ["run-shell", "-d", "0.3"],
            ["send-keys", "-X", "copy-pipe-and-cancel"],
        ],
        "WheelUpPane": [
            ["select-pane", "-t", "="],
            ["send-keys", "-X", "-N", _MOUSE_SCROLL_REPEAT_COUNT, "scroll-up"],
        ],
        "WheelDownPane": [
            ["select-pane", "-t", "="],
            ["send-keys", "-X", "-N", _MOUSE_SCROLL_REPEAT_COUNT, "scroll-down"],
        ],
    }


def dashboard_key_bindings(
    session_name: str,
    left_pane_id: str,
    move_up: str,
    move_down: str,
    enter_inspect: str,
    raw_inspect: str,
    quit_requested_path: Path,
) -> dict[str, list[list[str]]]:
    focus_left = focus_commands(left_pane_id)
    return {
        "Up": [*focus_left, ["run-shell", move_up]],
        "Down": [*focus_left, ["run-shell", move_down]],
        "Enter": [["run-shell", enter_inspect]],
        "r": [["run-shell", raw_inspect]],
        "Escape": focus_left,
        "q": quit_dashboard_commands(session_name, quit_requested_path),
    }


def inspect_enter_command(
    context: InspectCommandContext,
) -> str:
    return _inspect_control_command(context, "auto")


def inspect_raw_command(context: InspectCommandContext) -> str:
    return _inspect_control_command(context, "raw")


def inspect_formatted_command(context: InspectCommandContext) -> str:
    return _inspect_control_command(context, "formatted")


def _inspect_control_command(context: InspectCommandContext, view: str) -> str:
    session = context.session
    command = [
        sys.executable,
        "-m",
        "crewplane.observability.tmux.inspect_control",
        "--runtime-root",
        str(context.runtime_files.root),
        "--tmux-executable",
        context.tmux_executable,
        "--session-name",
        session.session_name,
        "--right-pane-id",
        session.right_pane_id,
        "--view",
        view,
    ]
    if session.socket_name:
        command.extend(["--socket-name", session.socket_name])
    return shlex.join(command)


def inspect_exit_command(
    context: InspectCommandContext,
    refresh_interval_seconds: float,
) -> str:
    runtime_files = context.runtime_files
    session = context.session
    tmux_command = tmux_bash_array(
        context.tmux_executable,
        session.socket_name,
    )
    render_args = " ".join(
        shlex.quote(arg)
        for arg in pane_render_args(
            runtime_files.right_content,
            refresh_interval_seconds,
        )
    )
    script_lines = [
        f"mode_file={shlex.quote(str(runtime_files.mode))}",
        f"inspect_snapshot={shlex.quote(str(runtime_files.inspect_invocation))}",
        f"session_name={shlex.quote(session.session_name)}",
        f"left_pane={shlex.quote(session.left_pane_id)}",
        f"right_pane={shlex.quote(session.right_pane_id)}",
        f"title_option={shlex.quote(PANE_TITLE_OPTION)}",
        f"tmux_cmd=({tmux_command})",
        (
            f'[[ "$(cat "$mode_file" 2>/dev/null || true)" == "{MODE_INSPECT}" ]] '
            "|| exit 0"
        ),
        (
            f'"${{tmux_cmd[@]}}" respawn-pane -k -t "$right_pane" {render_args} '
            "|| exit 0"
        ),
        f'printf "%s" "{MODE_DASHBOARD}" > "${{mode_file}}.tmp"',
        'mv "${mode_file}.tmp" "$mode_file"',
        'rm -f "$inspect_snapshot"',
        (
            f'"${{tmux_cmd[@]}}" set-option -t "$session_name" key-table '
            f"{DASHBOARD_KEY_TABLE}"
        ),
        '"${tmux_cmd[@]}" set-option -p -t "$right_pane" "$title_option" "Node Output"',
        '"${tmux_cmd[@]}" select-pane -t "$left_pane"',
    ]
    return write_runtime_shell_script(
        runtime_files.root / "inspect-exit.sh",
        script_lines,
    )


def selection_move_command(
    runtime_files: RuntimeFiles,
    direction: str,
) -> str:
    if direction not in {"up", "down"}:
        raise ValueError("selection move direction must be 'up' or 'down'")
    return shlex.join(
        [
            sys.executable,
            "-m",
            "crewplane.observability.tmux.selection_control",
            "--runtime-root",
            str(runtime_files.root),
            "--direction",
            direction,
        ]
    )
