"""Literal argument transport for adapter-selected Windows shells."""

from __future__ import annotations

import base64
import subprocess
import sys

from .command_types import ResolvedCommand

# Safe cmd budget for the quoted executable and arguments in UTF-16 code units.
_BATCH_ARGUMENT_BUDGET_UTF16_UNITS = 7000


def prepare_windows_launcher(
    resolved: ResolvedCommand, arguments: list[str]
) -> list[str]:
    """Build launcher argv without starting processes or mutating arguments.

    Args:
        resolved: Selected executable and launcher kind. Batch and PowerShell
            launchers require a shell path in resolved.shell; paths are not
            resolved or checked here.
        arguments: Literal arguments excluding the executable.

    Returns:
        Native executable argv, PowerShell argv with a Base64-encoded UTF-16LE
        script, or Python transport argv carrying a Base64-encoded UTF-8 batch
        command line.

    Raises:
        ValueError: An argument contains NUL, a required shell is missing, or
            batch validation fails, checked in that order.
        UnicodeEncodeError: Shell command text cannot be encoded.
    """
    if any("\x00" in value for value in arguments):
        raise ValueError("Windows launcher arguments cannot contain NUL bytes.")
    if resolved.kind == "native":
        return [resolved.executable, *arguments]
    if resolved.shell is None:
        raise ValueError(f"Required shell is missing for {resolved.executable}.")
    if resolved.kind == "powershell":
        return _build_powershell_launcher(
            resolved.executable, resolved.shell, arguments
        )
    return _build_batch_launcher(resolved.executable, resolved.shell, arguments)


def validate_batch_arguments(values: list[str]) -> None:
    """Validate literal values for batch transport without mutating them.

    Args:
        values: Include the executable followed by its arguments when validating
            a launch; the executable is subject to the same restrictions.

    Raises:
        ValueError: Values contain NUL, CR/LF, % or !; combine embedded quotes
            with shell metacharacters across any values; or exceed 7000 UTF-16
            code units after quoting and joining with spaces. Checks run in
            that order and exactly 7000 units are accepted.
        UnicodeEncodeError: Quoted values cannot be encoded as UTF-16LE.
    """
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
    if (
        len(encoded_arguments.encode("utf-16le")) // 2
        > _BATCH_ARGUMENT_BUDGET_UTF16_UNITS
    ):
        raise ValueError(
            "Batch launcher arguments exceed the safe cmd command-line budget. Use stdin prompt transport or an .exe/.ps1 launcher."
        )


def _build_powershell_launcher(
    executable: str, shell: str, arguments: list[str]
) -> list[str]:
    literals = ",".join(_powershell_literal(value) for value in arguments)
    command = (
        "$ErrorActionPreference='Stop'; "
        f"$crewplaneArguments=@({literals}); "
        f"& {_powershell_literal(executable)} @crewplaneArguments; "
        "$ok=$?; if ($null -ne $LASTEXITCODE) { exit $LASTEXITCODE }; if (-not $ok) { exit 1 }"
    )
    encoded = base64.b64encode(command.encode("utf-16le")).decode("ascii")
    return [
        shell,
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-EncodedCommand",
        encoded,
    ]


def _build_batch_launcher(
    executable: str, shell: str, arguments: list[str]
) -> list[str]:
    values = [executable, *arguments]
    validate_batch_arguments(values)
    command_line = (
        f'"{shell}" /d /s /v:off /c "'
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
        shell,
        encoded,
    ]


def _batch_literal(value: str) -> str:
    encoded = subprocess.list2cmdline([value])
    if encoded.startswith('"') and encoded.endswith('"'):
        return encoded
    trailing_backslash_count = len(encoded) - len(encoded.rstrip("\\"))
    # Duplicate trailing backslashes so the closing quote stays a delimiter.
    trailing_backslashes = "\\" * trailing_backslash_count
    return f'"{encoded}{trailing_backslashes}"'


def _powershell_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"
