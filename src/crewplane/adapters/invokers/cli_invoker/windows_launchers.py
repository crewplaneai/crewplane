"""Literal argument transport for adapter-selected Windows shells."""

from __future__ import annotations

import base64
import subprocess
import sys

from .command_resolution import ResolvedCommand


def prepare_windows_launcher(
    resolved: ResolvedCommand, arguments: list[str]
) -> list[str]:
    if any("\x00" in value for value in arguments):
        raise ValueError("Windows launcher arguments cannot contain NUL bytes.")
    if resolved.kind == "native":
        return [resolved.executable, *arguments]
    if resolved.shell is None:
        raise ValueError(f"Required shell is missing for {resolved.executable}.")
    if resolved.kind == "powershell":
        literals = ",".join(_powershell_literal(value) for value in arguments)
        command = (
            "$ErrorActionPreference='Stop'; "
            f"$crewplaneArguments=@({literals}); "
            f"& {_powershell_literal(resolved.executable)} @crewplaneArguments; "
            "$ok=$?; if ($null -ne $LASTEXITCODE) { exit $LASTEXITCODE }; if (-not $ok) { exit 1 }"
        )
        encoded = base64.b64encode(command.encode("utf-16le")).decode("ascii")
        return [
            resolved.shell,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-EncodedCommand",
            encoded,
        ]
    values = [resolved.executable, *arguments]
    validate_batch_arguments(values)
    command_line = (
        f'"{resolved.shell}" /d /s /v:off /c "'
        + " ".join(_batch_literal(value) for value in values)
        + '"'
    )
    encoded = base64.b64encode(command_line.encode("utf-8")).decode("ascii")
    # Python's argv encoding is for native executables, not cmd's /c grammar.
    # The adapter transport passes the already escaped command line verbatim.
    return [
        sys.executable,
        "-m",
        "crewplane.adapters.invokers.cli_invoker.windows_shell_transport",
        resolved.shell,
        encoded,
    ]


def validate_batch_arguments(values: list[str]) -> None:
    if any(any(char in value for char in "\x00\r\n%!") for value in values):
        raise ValueError(
            "Batch launchers cannot safely transport NUL, newlines, % or ! in arguments. Use stdin prompt transport or an .exe/.ps1 launcher."
        )
    if any('"' in value for value in values) and any(
        any(char in value for char in "&|<>^()") for value in values
    ):
        raise ValueError(
            "Batch launchers cannot combine embedded quotes with shell metacharacters. Use stdin prompt transport or an .exe/.ps1 launcher."
        )
    encoded_arguments = " ".join(_batch_literal(value) for value in values)
    if len(encoded_arguments.encode("utf-16le")) // 2 > 7000:
        raise ValueError(
            "Batch launcher arguments exceed the safe cmd command-line budget. Use stdin prompt transport or an .exe/.ps1 launcher."
        )


def _batch_literal(value: str) -> str:
    encoded = subprocess.list2cmdline([value])
    if encoded.startswith('"') and encoded.endswith('"'):
        return encoded
    return '"' + encoded + ("\\" * (len(encoded) - len(encoded.rstrip("\\")))) + '"'


def _powershell_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"
