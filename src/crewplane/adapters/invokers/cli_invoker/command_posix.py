from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING

from crewplane.core.config import Config
from crewplane.core.workflow.models import WorkflowPlan

from .command_types import CommandAvailability, ResolvedCommand, contains_path_separator
from .env_command import EnvCommandContext, parse_env_command_context

if TYPE_CHECKING:
    from .capability import CliInvocationRequest

_PLATFORM_ENV_EXECUTABLES = (Path("/bin/env"), Path("/usr/bin/env"))


@dataclass(frozen=True, slots=True)
class _ExecutableRequirement:
    """Executable lookup parameters for one required CLI command."""

    executable: str
    base_dir: Path
    search_path: str | None = None


def _collect_availability_errors(
    workflow: WorkflowPlan,
    config: Config,
    which_fn: Callable[[str], str | None] | None = None,
    project_root: Path | None = None,
) -> list[str]:
    """Collect missing executable errors for configured CLI providers."""

    executable_lookup = cache(shutil.which if which_fn is None else which_fn)
    executable_base_dir = Path.cwd() if project_root is None else project_root
    missing_cli_locations: dict[tuple[str, str], list[str]] = {}
    for node in workflow.nodes:
        for provider in node.providers:
            agent_config = config.agents.get(provider.provider)
            if agent_config is None:
                continue
            for requirement in _required_cli_executables(
                agent_config.cli_cmd,
                executable_base_dir,
                executable_lookup,
            ):
                if _cli_executable_available(requirement, executable_lookup):
                    continue
                location = f"workflow '{workflow.name}' -> node '{node.id}'"
                missing_cli_locations.setdefault(
                    (provider.provider, requirement.executable), []
                ).append(f"{location} (CLI: {requirement.executable})")
    return _format_missing_cli_errors(missing_cli_locations)


def _format_missing_cli_errors(
    missing_cli_locations: dict[tuple[str, str], list[str]],
) -> list[str]:
    errors: list[str] = []
    for (provider_name, cli_executable), locations in sorted(
        missing_cli_locations.items()
    ):
        unique_locations = sorted(set(locations))
        availability_message = (
            "not found or not executable"
            if Path(cli_executable).is_absolute()
            or contains_path_separator(cli_executable)
            else "not found in PATH"
        )
        errors.append(
            f"CLI '{cli_executable}' {availability_message} for provider "
            f"'{provider_name}', referenced in: {', '.join(unique_locations)}"
        )
    return errors


def _required_cli_executables(
    cli_command: list[str],
    executable_base_dir: Path,
    executable_lookup: Callable[[str], str | None],
) -> tuple[_ExecutableRequirement, ...]:
    wrapper = _ExecutableRequirement(
        executable=cli_command[0],
        base_dir=executable_base_dir,
    )
    if not _is_platform_env_wrapper(wrapper, executable_lookup):
        return (wrapper,)

    wrapped = _env_wrapped_executable_requirement(cli_command, executable_base_dir)
    if wrapped is None or wrapped == wrapper:
        return (wrapper,)
    return wrapper, wrapped


def _is_platform_env_wrapper(
    requirement: _ExecutableRequirement,
    executable_lookup: Callable[[str], str | None],
) -> bool:
    wrapper_path = Path(requirement.executable)
    if wrapper_path.name != "env":
        return False
    resolved_wrapper = (
        str(requirement.base_dir / wrapper_path)
        if not wrapper_path.is_absolute()
        and contains_path_separator(requirement.executable)
        else executable_lookup(requirement.executable)
    )
    return _is_platform_env_executable(resolved_wrapper)


def _env_wrapped_executable_requirement(
    cli_command: list[str],
    executable_base_dir: Path,
) -> _ExecutableRequirement | None:
    try:
        command_context = parse_env_command_context(
            cli_command,
            inherited_value=os.environ.get("PATH"),
            tracked_environment_name="PATH",
        )
    except ValueError:
        return None
    if command_context.command_executable is None:
        return None
    return _ExecutableRequirement(
        executable=command_context.command_executable,
        base_dir=_env_command_base_dir(command_context, executable_base_dir),
        search_path=_env_command_search_path(command_context),
    )


def _is_platform_env_executable(resolved_executable: str | None) -> bool:
    if resolved_executable is None:
        return False
    executable_path = Path(resolved_executable).resolve(strict=False)
    return any(
        executable_path == platform_path.resolve(strict=False)
        for platform_path in _PLATFORM_ENV_EXECUTABLES
    )


def _env_command_base_dir(
    command_context: EnvCommandContext,
    executable_base_dir: Path,
) -> Path:
    configured_directory = command_context.command_working_directory
    if configured_directory is None:
        return executable_base_dir
    working_directory = Path(configured_directory)
    if working_directory.is_absolute():
        return working_directory
    return executable_base_dir / working_directory


def _env_command_search_path(command_context: EnvCommandContext) -> str | None:
    if command_context.command_search_path is not None:
        return command_context.command_search_path
    tracked_search_path = command_context.tracked_environment_value
    if (
        command_context.tracked_environment_changed
        or command_context.command_working_directory is not None
    ):
        return os.defpath if tracked_search_path is None else tracked_search_path
    if tracked_search_path is not None and _has_relative_search_path_entry(
        tracked_search_path
    ):
        return tracked_search_path
    return None


def _has_relative_search_path_entry(search_path: str) -> bool:
    return any(not Path(entry).is_absolute() for entry in search_path.split(os.pathsep))


def _cli_executable_available(
    requirement: _ExecutableRequirement,
    executable_lookup: Callable[[str], str | None],
) -> bool:
    try:
        resolve_command(
            requirement.executable,
            requirement.base_dir,
            None
            if requirement.search_path is None
            else {"PATH": requirement.search_path},
            executable_lookup if requirement.search_path is None else None,
        )
    except (OSError, ValueError):
        return False
    return True


def resolve_command(
    executable: str,
    cwd: Path | None = None,
    environment: Mapping[str, str] | None = None,
    lookup: Callable[[str], str | None] | None = None,
) -> ResolvedCommand:
    cwd = cwd or Path.cwd()
    environment = os.environ if environment is None else environment
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


class PosixCommandStrategy:
    def collect_availability_errors(
        self,
        workflow: WorkflowPlan,
        config: Config,
        lookup: Callable[[str], str | None],
        root: Path,
    ) -> list[str]:
        return _collect_availability_errors(workflow, config, lookup, root)

    def availability(
        self, command: list[str], root: Path, lookup: Callable[[str], str | None]
    ) -> CommandAvailability:
        executable_lookup = cache(lookup)
        requirements = _required_cli_executables(command, root, executable_lookup)
        return CommandAvailability(
            all(
                _cli_executable_available(item, executable_lookup)
                for item in requirements
            )
        )

    def validate_request(self, request: CliInvocationRequest) -> None:
        pass

    def prepare_executable(self, executable: str, request: CliInvocationRequest) -> str:
        del request
        return resolved_cli_executable(executable)

    def prepare_arguments(
        self, command: list[str], request: CliInvocationRequest
    ) -> list[str]:
        del request
        return command
