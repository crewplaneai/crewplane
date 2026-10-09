from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from crewplane.core.config import Config
from crewplane.core.workflow.models import WorkflowPlan

from .command_types import (
    CommandAvailability,
    LauncherKind,
    ResolvedCommand,
    contains_path_separator,
    request_validation_targets,
)
from .windows_launchers import prepare_windows_launcher

if TYPE_CHECKING:
    from .capability import CliInvocationRequest

_WINDOWS_EXTENSIONS: dict[str, LauncherKind] = {
    ".exe": "native",
    ".com": "native",
    ".cmd": "batch",
    ".bat": "batch",
    ".ps1": "powershell",
}


def resolve_command(
    executable: str,
    cwd: Path | None = None,
    environment: Mapping[str, str] | None = None,
    lookup: Callable[[str], str | None] | None = None,
) -> ResolvedCommand:
    del lookup
    cwd = cwd or Path.cwd()
    environment = os.environ if environment is None else environment
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


class WindowsCommandStrategy:
    def collect_availability_errors(
        self,
        workflow: WorkflowPlan,
        config: Config,
        lookup: Callable[[str], str | None],
        root: Path,
    ) -> list[str]:
        errors = []
        for target in request_validation_targets(workflow, config):
            diagnostic = self.availability(
                target.agent_config.cli_cmd, root, lookup
            ).diagnostic
            if diagnostic is not None:
                errors.append(f"{target.location}: {diagnostic}")
        return errors

    def availability(
        self, command: list[str], root: Path, lookup: Callable[[str], str | None]
    ) -> CommandAvailability:
        del lookup
        try:
            resolved = resolve_command(command[0], root)
            prepare_windows_launcher(resolved, command[1:])
        except (OSError, ValueError) as exc:
            return CommandAvailability(False, str(exc))
        return CommandAvailability(True)

    def validate_request(self, request: CliInvocationRequest) -> None:
        resolved = resolve_command(
            request.config.cli_cmd[0], request.working_directory, request.environment
        )
        prepare_windows_launcher(
            resolved, [*request.config.cli_cmd[1:], *request.config.extra_args]
        )

    def prepare_executable(self, executable: str, request: CliInvocationRequest) -> str:
        return resolve_command(
            executable, request.working_directory, request.environment
        ).executable

    def prepare_arguments(
        self, command: list[str], request: CliInvocationRequest
    ) -> list[str]:
        resolved = resolve_command(
            command[0], request.working_directory, request.environment
        )
        return prepare_windows_launcher(resolved, command[1:])
