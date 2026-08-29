from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from crewplane.architecture.contracts import (
    AgentInvoker,
    CanonicalIntegrationConfig,
    InvokerAdapterCapabilities,
    JsonObject,
    ProviderKind,
)
from crewplane.core.config import AgentConfig, Config
from crewplane.core.workflow.models import WorkflowPlan
from crewplane.runtime.agent.invoker import PlannedAgentInvoker

from .cli_invoker import build_cli_invocation_plan, build_cli_log_presentation
from .cli_invoker.env_command import EnvCommandContext, parse_env_command_context
from .cli_invoker.reasoning import validate_reasoning_request

_PLATFORM_ENV_EXECUTABLES = (Path("/bin/env"), Path("/usr/bin/env"))


@dataclass(frozen=True, slots=True)
class _ExecutableRequirement:
    """Executable lookup parameters for one required CLI command."""

    executable: str
    base_dir: Path
    search_path: str | None = None


@dataclass(frozen=True, slots=True)
class _ReasoningValidationTarget:
    """Configured workflow provider requiring reasoning validation."""

    location: str
    agent_config: AgentConfig
    requested_reasoning: str


def collect_cli_availability_errors(
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


def collect_cli_reasoning_errors(
    workflow: WorkflowPlan,
    config: Config,
    environment: Mapping[str, str] | None = None,
    working_directory: Path | None = None,
) -> list[str]:
    """Collect reasoning validation errors for configured workflow providers."""

    errors: list[str] = []
    for target in _reasoning_validation_targets(workflow, config):
        try:
            validate_reasoning_request(
                target.agent_config,
                target.requested_reasoning,
                environment,
                working_directory,
            )
        except ValueError as exc:
            errors.append(f"{target.location}: {exc}")
    return errors


def _reasoning_validation_targets(
    workflow: WorkflowPlan,
    config: Config,
) -> Iterator[_ReasoningValidationTarget]:
    for node in workflow.nodes:
        for provider in node.providers:
            if provider.reasoning is None:
                continue
            agent_config = config.agents.get(provider.provider)
            if agent_config is None:
                continue
            yield _ReasoningValidationTarget(
                location=(
                    f"workflow '{workflow.name}' -> node '{node.id}' -> provider "
                    f"'{provider.provider}'"
                ),
                agent_config=agent_config,
                requested_reasoning=provider.reasoning,
            )


def collect_cli_model_arg_warnings(config: Config) -> list[str]:
    return [
        (
            f"Agent '{agent_key}': remove model_arg. Crewplane chooses the model "
            f"flag automatically for built-in provider '{agent.provider_kind.value}'. "
            "Set model_arg only when provider_kind is 'generic'."
        )
        for agent_key, agent in sorted(config.agents.items())
        if agent.provider_kind != ProviderKind.GENERIC
        and "model_arg" in agent.model_fields_set
    ]


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
            or _contains_path_separator(cli_executable)
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
        and _contains_path_separator(requirement.executable)
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
    executable_path = Path(requirement.executable)
    if executable_path.is_absolute():
        return _is_executable_file(executable_path)
    if _contains_path_separator(requirement.executable):
        return _is_executable_file(requirement.base_dir / executable_path)
    if requirement.search_path is None:
        return executable_lookup(requirement.executable) is not None
    return _search_path_executable_available(
        requirement.executable,
        requirement.search_path,
        requirement.base_dir,
    )


def _search_path_executable_available(
    executable: str,
    search_path: str,
    executable_base_dir: Path,
) -> bool:
    for entry in search_path.split(os.pathsep):
        directory = Path(entry) if entry else Path()
        if not directory.is_absolute():
            directory = executable_base_dir / directory
        if _is_executable_file(directory / executable):
            return True
    return False


def _is_executable_file(path: Path) -> bool:
    resolved = path.resolve(strict=False)
    return resolved.is_file() and os.access(resolved, os.X_OK)


def _contains_path_separator(value: str) -> bool:
    return "/" in value or "\\" in value


class CliInvokerAdapter:
    """Create the default CLI-backed agent invoker."""

    def collect_availability_errors(
        self,
        workflow: WorkflowPlan,
        config: Config,
        project_root: Path,
        executable_lookup: Callable[[str], str | None] | None = None,
    ) -> tuple[str, ...]:
        """Collect read-only executable availability diagnostics."""

        return tuple(
            collect_cli_availability_errors(
                workflow,
                config,
                which_fn=executable_lookup,
                project_root=project_root,
            )
        )

    def collect_reasoning_errors(
        self,
        workflow: WorkflowPlan,
        config: Config,
        working_directory: Path | None = None,
    ) -> tuple[str, ...]:
        """Collect built-in CLI reasoning eligibility diagnostics."""

        return tuple(
            collect_cli_reasoning_errors(
                workflow,
                config,
                working_directory=working_directory,
            )
        )

    def collect_model_arg_warnings(self, config: Config) -> tuple[str, ...]:
        """Collect ignored model-argument diagnostics for the built-in CLI."""

        return tuple(collect_cli_model_arg_warnings(config))

    def canonicalize_options(
        self,
        implementation: str,
        resolved_identity: str,
        options: JsonObject | None = None,
    ) -> CanonicalIntegrationConfig:
        if options:
            raise ValueError(
                "cli invoker implementation does not support options; "
                "set settings.integrations.invoker.options: {} when using "
                f'implementation: "cli"; got: {sorted(options)}'
            )
        return CanonicalIntegrationConfig(
            implementation=implementation,
            resolved_identity=resolved_identity,
            options={},
            option_scopes={},
            capabilities=InvokerAdapterCapabilities.workspace_supported(
                launch_mode="runtime_command_runner",
                controlled_child_environment=True,
            ).as_dict(),
        )

    def create_invoker(
        self,
        config: Config,
        options: JsonObject | None = None,
    ) -> AgentInvoker:
        """Build the default subprocess-based invoker."""

        _validate_config(config)
        self.canonicalize_options("cli", self.__class__.__module__, options)
        return PlannedAgentInvoker(
            plan_builder=build_cli_invocation_plan,
            log_presentation_builder=build_cli_log_presentation,
        )


def _validate_config(config: Config) -> None:
    if not isinstance(config, Config):
        raise TypeError("config must be a Config instance")
