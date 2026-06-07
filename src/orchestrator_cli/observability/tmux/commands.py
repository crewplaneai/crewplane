from __future__ import annotations

import os
import shlex
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from orchestrator_cli.observability.tmux.runtime_files import (
    MODE_DASHBOARD,
    MODE_INSPECT,
    RuntimeFiles,
)
from orchestrator_cli.observability.tmux.session import TmuxSessionTargets

DASHBOARD_KEY_TABLE = "orchestrator-dashboard"
INSPECT_KEY_TABLE = "orchestrator-inspect"
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
PANE_TITLE_OPTION = "@orchestrator_title"


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


def tmux_command_string(*commands: list[str]) -> str:
    return " ; ".join(shlex.join(command) for command in commands)


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
        "Escape": [["run-shell", exit_inspect]],
        "q": quit_commands,
    }


def inspect_copy_mode_key_bindings(
    exit_inspect: str,
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
    return {
        "Up": [["send-keys", "-X", "cursor-up"]],
        "Down": [["send-keys", "-X", "cursor-down"]],
        "PageUp": [["send-keys", "-X", "page-up"]],
        "PageDown": [["send-keys", "-X", "page-down"]],
        "Enter": [["send-keys", "-X", "cancel"]],
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


def pane_render_command(content_path: Path, refresh_interval_seconds: float) -> str:
    return shlex.join(_pane_render_args(content_path, refresh_interval_seconds))


def _pane_render_args(
    content_path: Path,
    refresh_interval_seconds: float,
) -> list[str]:
    script = (
        "while true; do "
        "clear; "
        f"cat {shlex.quote(str(content_path))} 2>/dev/null || true; "
        f"sleep {refresh_interval_seconds}; "
        "done"
    )
    return ["bash", "-lc", script]


def dashboard_key_bindings(
    session_name: str,
    left_pane_id: str,
    move_up: str,
    move_down: str,
    enter_inspect: str,
    quit_requested_path: Path,
) -> dict[str, list[list[str]]]:
    focus_left = focus_commands(left_pane_id)
    return {
        "Up": [*focus_left, ["run-shell", move_up]],
        "Down": [*focus_left, ["run-shell", move_down]],
        "Enter": [["run-shell", enter_inspect]],
        "Escape": focus_left,
        "q": quit_dashboard_commands(session_name, quit_requested_path),
    }


def inspect_enter_command(
    context: InspectCommandContext,
    exit_inspect: str,
) -> str:
    runtime_files = context.runtime_files
    session = context.session
    tmux_command = _tmux_bash_array(
        context.tmux_executable,
        session.socket_name,
    )
    inspect_copy_mode_bindings = inspect_copy_mode_key_bindings(
        exit_inspect,
        quit_commands=quit_dashboard_commands(
            session.session_name,
            runtime_files.quit_requested,
        ),
    )
    inspect_copy_mode_binding_lines = [
        line
        for table in COPY_MODE_KEY_TABLES
        for line in _shell_bind_key_lines(table, inspect_copy_mode_bindings)
    ]
    script_lines = [
        f"mode_file={shlex.quote(str(runtime_files.mode))}",
        f"selected_node_file={shlex.quote(str(runtime_files.selected_node_id))}",
        f"selected_log_file={shlex.quote(str(runtime_files.selected_log))}",
        f"inspect_log_file={shlex.quote(str(runtime_files.inspect_log))}",
        f"inspect_node_file={shlex.quote(str(runtime_files.inspect_node_id))}",
        f"session_name={shlex.quote(session.session_name)}",
        f"right_pane={shlex.quote(session.right_pane_id)}",
        f"title_option={shlex.quote(PANE_TITLE_OPTION)}",
        f"tmux_cmd=({tmux_command})",
        _shell_read_value_function(),
        _shell_write_value_function(),
        'log_path=$(read_value "$selected_log_file")',
        'node_id=$(read_value "$selected_node_file")',
        '[[ -n "$log_path" ]] || exit 0',
        'write_value "$inspect_log_file" "$log_path"',
        'write_value "$inspect_node_file" "$node_id"',
        '"${tmux_cmd[@]}" respawn-pane -k -t "$right_pane" tail -n +1 -F "$log_path" || exit 0',
        f'write_value "$mode_file" "{MODE_INSPECT}"',
        f'"${{tmux_cmd[@]}}" set-option -t "$session_name" key-table {INSPECT_KEY_TABLE}',
        *inspect_copy_mode_binding_lines,
        'if [[ -n "$node_id" ]]; then title="Node Log: $node_id"; else title="Node Log"; fi',
        '"${tmux_cmd[@]}" set-option -p -t "$right_pane" "$title_option" "$title"',
        '"${tmux_cmd[@]}" select-pane -t "$right_pane"',
    ]
    return _write_runtime_shell_script(
        runtime_files.root / "inspect-enter.sh",
        script_lines,
    )


def inspect_exit_command(
    context: InspectCommandContext,
    refresh_interval_seconds: float,
) -> str:
    runtime_files = context.runtime_files
    session = context.session
    tmux_command = _tmux_bash_array(
        context.tmux_executable,
        session.socket_name,
    )
    render_args = " ".join(
        shlex.quote(arg)
        for arg in _pane_render_args(
            runtime_files.right_content,
            refresh_interval_seconds,
        )
    )
    script_lines = [
        f"mode_file={shlex.quote(str(runtime_files.mode))}",
        f"inspect_log_file={shlex.quote(str(runtime_files.inspect_log))}",
        f"inspect_node_file={shlex.quote(str(runtime_files.inspect_node_id))}",
        f"session_name={shlex.quote(session.session_name)}",
        f"left_pane={shlex.quote(session.left_pane_id)}",
        f"right_pane={shlex.quote(session.right_pane_id)}",
        f"title_option={shlex.quote(PANE_TITLE_OPTION)}",
        f"tmux_cmd=({tmux_command})",
        _shell_read_value_function(),
        _shell_write_value_function(),
        f'[[ "$(read_value "$mode_file")" == "{MODE_INSPECT}" ]] || exit 0',
        f'"${{tmux_cmd[@]}}" respawn-pane -k -t "$right_pane" {render_args} || exit 0',
        f'write_value "$mode_file" "{MODE_DASHBOARD}"',
        'write_value "$inspect_log_file" ""',
        'write_value "$inspect_node_file" ""',
        f'"${{tmux_cmd[@]}}" set-option -t "$session_name" key-table {DASHBOARD_KEY_TABLE}',
        '"${tmux_cmd[@]}" set-option -p -t "$right_pane" "$title_option" "Node Output"',
        '"${tmux_cmd[@]}" select-pane -t "$left_pane"',
    ]
    return _write_runtime_shell_script(
        runtime_files.root / "inspect-exit.sh",
        script_lines,
    )


def _tmux_bash_array(tmux_executable: str, socket_name: str | None) -> str:
    command = [tmux_executable]
    if socket_name:
        command.extend(["-L", socket_name])
    return shlex.join(command)


def _shell_read_value_function() -> str:
    return 'read_value() { local path="$1"; cat "$path" 2>/dev/null || true; }'


def _shell_write_value_function() -> str:
    return (
        "write_value() { "
        'local path="$1"; '
        'local value="$2"; '
        'printf "%s" "$value" > "${path}.tmp" && mv "${path}.tmp" "$path"; '
        "}"
    )


def _write_runtime_shell_script(script_path: Path, script_lines: list[str]) -> str:
    script_body = "\n".join(
        [
            "#!/usr/bin/env bash",
            "set -euo pipefail",
            *script_lines,
            "",
        ]
    )
    temp_path = script_path.with_suffix(f"{script_path.suffix}.tmp")
    temp_path.write_text(script_body, encoding="utf-8")
    temp_path.replace(script_path)
    script_path.chmod(0o755)
    return str(script_path)


def _shell_bind_key_lines(
    table: str,
    bindings: Mapping[str, list[list[str]]],
) -> list[str]:
    return [
        '"${tmux_cmd[@]}" bind-key -T '
        f"{shlex.quote(table)} {shlex.quote(key)} if-shell -F 1 "
        f"{shlex.quote(tmux_command_string(*commands))}"
        for key, commands in bindings.items()
    ]


def selection_move_command(
    index_path: Path,
    count_path: Path,
    direction: str,
) -> str:
    if direction not in {"up", "down"}:
        raise ValueError("selection move direction must be 'up' or 'down'")
    script = " ".join(
        [
            f"index_file={shlex.quote(str(index_path))}",
            ";",
            f"count_file={shlex.quote(str(count_path))}",
            ";",
            f"direction={shlex.quote(direction)}",
            ";",
            'count=$(cat "$count_file" 2>/dev/null || echo 0)',
            ";",
            '[[ "$count" =~ ^[0-9]+$ ]] || exit 0',
            ";",
            "(( count > 0 )) || exit 0",
            ";",
            'current=$(cat "$index_file" 2>/dev/null || echo -1)',
            ";",
            '[[ "$current" =~ ^-?[0-9]+$ ]] || current=-1',
            ";",
            "if (( current < 0 || current >= count )); then current=0; fi",
            ";",
            'if [[ "$direction" == "up" ]]; then next=$(( (current - 1 + count) % count ));',
            "else next=$(( (current + 1) % count )); fi",
            ";",
            'printf "%s" "$next" > "${index_file}.tmp"',
            ";",
            'mv "${index_file}.tmp" "$index_file"',
        ]
    )
    return f"bash -lc {shlex.quote(script)}"
