"""Read-only executable and Windows launcher resolution for the CLI adapter."""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from crewplane.core.platform import is_native_windows

type LauncherKind = Literal["native", "batch", "powershell"]
_WINDOWS_EXTENSIONS: dict[str, LauncherKind] = {
    ".exe": "native",
    ".com": "native",
    ".cmd": "batch",
    ".bat": "batch",
    ".ps1": "powershell",
}


@dataclass(frozen=True)
class ResolvedCommand:
    """Immutable launcher metadata; construction performs no path validation.

    Attributes:
        executable: Selected executable path, or an unvalidated POSIX lookup result.
        kind: Native execution, batch execution, or PowerShell execution.
        shell: Selected shell path for Windows batch/PowerShell launchers, otherwise
            None. Callers constructing metadata directly must supply required shells.
    """

    executable: str
    kind: LauncherKind = "native"
    shell: str | None = None


def resolve_command(
    executable: str,
    cwd: Path | None = None,
    environment: Mapping[str, str] | None = None,
    lookup: Callable[[str], str | None] | None = None,
) -> ResolvedCommand:
    """Resolve a CLI launcher without starting it or changing cwd/environment.

    POSIX explicit paths are resolved strictly and must be executable files. Bare
    names use lookup when supplied, returning its result without path validation;
    otherwise PATH entries, including relative/empty entries, are based on cwd.

    Windows environment keys are case insensitive. Search visits PATH directories
    before supported PATHEXT suffixes, adding .ps1 when absent. Explicit paths use
    their directory; bare names omit empty PATH entries and do not implicitly
    search cwd. Windows candidates must be files, without POSIX execute checks.
    Batch launchers require cmd.exe; PowerShell prefers pwsh.exe and falls back to
    powershell.exe only when pwsh resolution raises ValueError.

    Unlike resolved_cli_executable, missing commands fail during resolution.
    Filesystem errors and exceptions raised by lookup propagate unchanged.

    Args:
        executable: Bare command name or explicit absolute/relative path.
        cwd: Invocation directory, defaulting to the process working directory.
        environment: Complete search environment, defaulting to os.environ.
            Missing PATH uses os.defpath on POSIX and no directories on Windows;
            missing PATHEXT uses .COM;.EXE;.BAT;.CMD on Windows.
        lookup: Override for POSIX bare-name lookup only; ignored for explicit
            paths and on Windows. It receives only executable and owns its search.

    Returns:
        Executable and launcher metadata, with a shell for Windows scripts.

    Raises:
        ValueError: A command/shell is missing or a Windows suffix is unsupported.
        FileNotFoundError: A POSIX explicit path is missing or is not a file.
        PermissionError: A POSIX explicit path lacks execute permission.
    """
    root = cwd or Path.cwd()
    env = os.environ if environment is None else environment
    if not is_native_windows():
        return _resolve_posix_command(executable, root, env, lookup)
    return _resolve_windows_command(executable, root, env)


def _resolve_posix_command(
    executable: str,
    cwd: Path,
    environment: Mapping[str, str],
    lookup: Callable[[str], str | None] | None,
) -> ResolvedCommand:
    path = Path(executable)
    if path.is_absolute() or contains_path_separator(executable):
        return ResolvedCommand(_resolved_existing_executable(cwd / path))
    if lookup is not None:
        resolved = lookup(executable)
    else:
        search_path = os.pathsep.join(
            str(cwd / entry)
            for entry in environment.get("PATH", os.defpath).split(os.pathsep)
        )
        resolved = shutil.which(executable, path=search_path)
    if resolved is None:
        raise ValueError(f"CLI '{executable}' not found in PATH.")
    return ResolvedCommand(resolved)


def _resolve_windows_command(
    executable: str, cwd: Path, environment: Mapping[str, str]
) -> ResolvedCommand:
    env = {key.upper(): value for key, value in environment.items()}
    path = _windows_executable(executable, cwd, env)
    kind = _WINDOWS_EXTENSIONS.get(path.suffix.lower())
    if kind is None:
        raise ValueError(
            f"Unsupported Windows launcher '{path}'. Configure an .exe, .com, .cmd, .bat, or .ps1 command."
        )
    shell = None
    if kind == "batch":
        shell = str(_windows_executable("cmd.exe", cwd, env))
    elif kind == "powershell":
        shell = str(_powershell_executable(path, cwd, env))
    return ResolvedCommand(str(path), kind, shell)


def _powershell_executable(
    launcher: Path, cwd: Path, environment: Mapping[str, str]
) -> Path:
    try:
        return _windows_executable("pwsh.exe", cwd, environment)
    except ValueError:
        try:
            return _windows_executable("powershell.exe", cwd, environment)
        except ValueError as exc:
            raise ValueError(
                f"PowerShell is required for '{launcher}'; install pwsh or configure another launcher."
            ) from exc


def _windows_executable(
    executable: str, cwd: Path, environment: Mapping[str, str]
) -> Path:
    path = Path(executable)
    names = _windows_candidate_names(path, environment)
    parents = _windows_search_directories(executable, cwd, environment)
    for parent in parents:
        for name in names:
            candidate = parent / name
            if candidate.is_file():
                return candidate.absolute()
    raise ValueError(
        f"Windows CLI or required shell '{executable}' not found using invocation cwd, PATH and PATHEXT."
    )


def _windows_candidate_names(
    path: Path, environment: Mapping[str, str]
) -> tuple[str, ...]:
    suffixes = tuple(
        dict.fromkeys(
            ext.lower()
            for ext in environment.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";")
            if ext.lower() in _WINDOWS_EXTENSIONS
        )
    )
    if ".ps1" not in suffixes:
        suffixes += (".ps1",)
    return (
        (path.name,)
        if path.suffix
        else tuple(path.name + suffix for suffix in suffixes)
    )


def _windows_search_directories(
    executable: str, cwd: Path, environment: Mapping[str, str]
) -> tuple[Path, ...]:
    path = Path(executable)
    if path.is_absolute() or contains_path_separator(executable):
        return ((cwd / path).parent,)
    return tuple(
        cwd / entry.strip('"')
        for entry in environment.get("PATH", "").split(";")
        if entry
    )


def contains_path_separator(value: str) -> bool:
    """Return whether value contains / or backslash, regardless of host platform."""
    return "/" in value or "\\" in value


def resolved_cli_executable(executable: str) -> str:
    """Return an executable path suitable for subprocess invocation.

    Bare executable names are resolved through `PATH` when available. Missing
    bare names are preserved so injected command runners and subprocess launch
    handle the execution boundary consistently. Absolute paths are validated
    directly. Relative path-like commands are preserved so subprocess can
    resolve them relative to the configured working directory.

    Lookup uses the process cwd and environment. Absolute paths and PATH hits
    undergo strict file/execute validation, with filesystem errors propagated.
    """
    executable_path = Path(executable)
    if executable_path.is_absolute():
        return _resolved_existing_executable(executable_path)
    if contains_path_separator(executable):
        return executable
    resolved = shutil.which(executable)
    if resolved is None:
        return executable
    return _resolved_existing_executable(Path(resolved))


def _resolved_existing_executable(executable: Path) -> str:
    """Resolve an existing executable path and fail clearly if it is unusable."""
    resolved = executable.resolve(strict=True)
    if not resolved.is_file():
        raise FileNotFoundError(
            f"CLI executable '{executable.as_posix()}' is not a file."
        )
    if not os.access(resolved, os.X_OK):
        raise PermissionError(
            f"CLI executable '{resolved.as_posix()}' is not executable."
        )
    return resolved.as_posix()
