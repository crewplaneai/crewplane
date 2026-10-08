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
_WINDOWS_EXTENSIONS = {
    ".exe": "native",
    ".com": "native",
    ".cmd": "batch",
    ".bat": "batch",
    ".ps1": "powershell",
}


@dataclass(frozen=True)
class ResolvedCommand:
    executable: str
    kind: LauncherKind = "native"
    shell: str | None = None


def resolve_command(
    executable: str,
    cwd: Path | None = None,
    environment: Mapping[str, str] | None = None,
    lookup: Callable[[str], str | None] | None = None,
) -> ResolvedCommand:
    root = cwd or Path.cwd()
    env = os.environ if environment is None else environment
    if not is_native_windows():
        path = Path(executable)
        if path.is_absolute() or contains_path_separator(executable):
            return ResolvedCommand(_resolved_existing_executable(root / path))
        if lookup is not None:
            resolved = lookup(executable)
        else:
            search_path = os.pathsep.join(
                str(root / entry)
                for entry in env.get("PATH", os.defpath).split(os.pathsep)
            )
            resolved = shutil.which(executable, path=search_path)
        if resolved is None:
            raise ValueError(f"CLI '{executable}' not found in PATH.")
        return ResolvedCommand(resolved)
    env = {key.upper(): value for key, value in env.items()}
    path = _windows_executable(executable, root, env)
    suffix = path.suffix.lower()
    if suffix not in _WINDOWS_EXTENSIONS:
        raise ValueError(
            f"Unsupported Windows launcher '{path}'. Configure an .exe, .com, .cmd, .bat, or .ps1 command."
        )
    kind: LauncherKind = "native"
    shell = None
    if suffix in {".cmd", ".bat"}:
        kind = "batch"
        shell = str(_windows_executable("cmd.exe", root, env))
    elif suffix == ".ps1":
        kind = "powershell"
        try:
            shell = str(_windows_executable("pwsh.exe", root, env))
        except ValueError:
            try:
                shell = str(_windows_executable("powershell.exe", root, env))
            except ValueError as exc:
                raise ValueError(
                    f"PowerShell is required for '{path}'; install pwsh or configure another launcher."
                ) from exc
    return ResolvedCommand(str(path), kind, shell)


def _windows_executable(
    executable: str, cwd: Path, environment: Mapping[str, str]
) -> Path:
    path = Path(executable)
    suffixes = tuple(
        dict.fromkeys(
            ext.lower()
            for ext in environment.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";")
            if ext.lower() in _WINDOWS_EXTENSIONS
        )
    )
    if ".ps1" not in suffixes:
        suffixes += (".ps1",)
    names = (
        (path.name,)
        if path.suffix
        else tuple(path.name + suffix for suffix in suffixes)
    )
    parents: tuple[Path, ...]
    if path.is_absolute() or contains_path_separator(executable):
        parents = ((cwd / path).parent,)
    else:
        parents = tuple(
            cwd / entry.strip('"')
            for entry in environment.get("PATH", "").split(";")
            if entry
        )
    for parent in parents:
        for name in names:
            candidate = parent / name
            if candidate.is_file():
                return candidate.absolute()
    raise ValueError(
        f"Windows CLI or required shell '{executable}' not found using invocation cwd, PATH and PATHEXT."
    )


def contains_path_separator(value: str) -> bool:
    return "/" in value or "\\" in value


def resolved_cli_executable(executable: str) -> str:
    """Return an executable path suitable for subprocess invocation.

    Bare executable names are resolved through `PATH` when available. Missing
    bare names are preserved so injected command runners and subprocess launch
    handle the execution boundary consistently. Absolute paths are validated
    directly. Relative path-like commands are preserved so subprocess can
    resolve them relative to the configured working directory.
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
