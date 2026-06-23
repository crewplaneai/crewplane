from __future__ import annotations

import shlex
from collections.abc import Mapping
from pathlib import Path


def tmux_command_string(*commands: list[str]) -> str:
    return " ; ".join(shlex.join(command) for command in commands)


def pane_render_command(content_path: Path, refresh_interval_seconds: float) -> str:
    return shlex.join(pane_render_args(content_path, refresh_interval_seconds))


def pane_render_args(
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


def tmux_bash_array(tmux_executable: str, socket_name: str | None) -> str:
    command = [tmux_executable]
    if socket_name:
        command.extend(["-L", socket_name])
    return shlex.join(command)


def shell_read_value_function() -> str:
    return 'read_value() { local path="$1"; cat "$path" 2>/dev/null || true; }'


def shell_write_value_function() -> str:
    return (
        "write_value() { "
        'local path="$1"; '
        'local value="$2"; '
        'printf "%s" "$value" > "${path}.tmp" && mv "${path}.tmp" "$path"; '
        "}"
    )


def write_runtime_shell_script(script_path: Path, script_lines: list[str]) -> str:
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


def shell_bind_key_lines(
    table: str,
    bindings: Mapping[str, list[list[str]]],
) -> list[str]:
    return [
        '"${tmux_cmd[@]}" bind-key -T '
        f"{shlex.quote(table)} {shlex.quote(key)} if-shell -F 1 "
        f"{shlex.quote(tmux_command_string(*commands))}"
        for key, commands in bindings.items()
    ]
