from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Mapping
from functools import cache
from pathlib import Path

from crewplane.architecture.contracts import (
    AgentInvoker,
    CanonicalIntegrationConfig,
    InvokerAdapterCapabilities,
    JsonObject,
    ProviderKind,
)
from crewplane.core.config import Config
from crewplane.core.workflow.models import WorkflowPlan
from crewplane.runtime.agent.invoker import PlannedAgentInvoker

from .cli_invoker import build_cli_invocation_plan, build_cli_log_presentation
from .cli_invoker.env_command import EnvCommandContext, parse_env_command_context
from .cli_invoker.reasoning import validate_reasoning_request

_PLATFORM_ENV_EXECUTABLES = (Path("/bin/env"), Path("/usr/bin/env"))


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
            for cli_executable, search_path, base_dir in _required_cli_executables(
                agent_config.cli_cmd,
                executable_base_dir,
                executable_lookup,
            ):
                if _cli_executable_available(
                    cli_executable,
                    executable_lookup,
                    base_dir,
                    search_path,
                ):
                    continue
                location = f"workflow '{workflow.name}' -> node '{node.id}'"
                missing_cli_locations.setdefault(
                    (provider.provider, cli_executable), []
                ).append(f"{location} (CLI: {cli_executable})")
    return _format_missing_cli_errors(missing_cli_locations)


def collect_cli_reasoning_errors(
    workflow: WorkflowPlan,
    config: Config,
    environment: Mapping[str, str] | None = None,
    working_directory: Path | None = None,
) -> list[str]:
    errors: list[str] = []
    for node in workflow.nodes:
        for provider in node.providers:
            if provider.reasoning is None:
                continue
            agent_config = config.agents.get(provider.provider)
            if agent_config is None:
                continue
            try:
                validate_reasoning_request(
                    agent_config,
                    provider.reasoning,
                    environment,
                    working_directory,
                )
            except ValueError as exc:
                errors.append(
                    f"workflow '{workflow.name}' -> node '{node.id}' -> provider "
                    f"'{provider.provider}': {exc}"
                )
    return errors


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
) -> tuple[tuple[str, str | None, Path], ...]:
    wrapper_executable = cli_command[0]
    wrapper_path = Path(wrapper_executable)
    resolved_wrapper = (
        str(executable_base_dir / wrapper_path)
        if not wrapper_path.is_absolute()
        and _contains_path_separator(wrapper_executable)
        else executable_lookup(wrapper_executable)
    )
    if wrapper_path.name != "env" or not _is_platform_env_executable(resolved_wrapper):
        return ((wrapper_executable, None, executable_base_dir),)
    try:
        command_context = parse_env_command_context(
            cli_command,
            inherited_value=os.environ.get("PATH"),
            tracked_environment_name="PATH",
        )
    except ValueError:
        return ((wrapper_executable, None, executable_base_dir),)
    wrapped_executable = command_context.command_executable
    if wrapped_executable is None:
        return ((wrapper_executable, None, executable_base_dir),)
    wrapped_base_dir = _env_command_base_dir(command_context, executable_base_dir)
    search_path = _env_command_search_path(command_context)
    if command_context.command_working_directory is not None and search_path is None:
        search_path = command_context.tracked_environment_value
        if search_path is None:
            search_path = os.defpath
    if (
        wrapped_executable == wrapper_executable
        and search_path is None
        and wrapped_base_dir == executable_base_dir
    ):
        return ((wrapper_executable, None, executable_base_dir),)
    return (
        (wrapper_executable, None, executable_base_dir),
        (wrapped_executable, search_path, wrapped_base_dir),
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
    if not command_context.tracked_environment_changed:
        inherited_search_path = command_context.tracked_environment_value
        if inherited_search_path is not None and _has_relative_search_path_entry(
            inherited_search_path
        ):
            return inherited_search_path
        return None
    if command_context.tracked_environment_value is None:
        return os.defpath
    return command_context.tracked_environment_value


def _has_relative_search_path_entry(search_path: str) -> bool:
    return any(not Path(entry).is_absolute() for entry in search_path.split(os.pathsep))


def _cli_executable_available(
    executable: str,
    executable_lookup: Callable[[str], str | None],
    executable_base_dir: Path,
    search_path: str | None,
) -> bool:
    executable_path = Path(executable)
    if executable_path.is_absolute():
        return _is_executable_file(executable_path)
    if _contains_path_separator(executable):
        return _is_executable_file(executable_base_dir / executable_path)
    if search_path is None:
        return executable_lookup(executable) is not None
    return _search_path_executable_available(
        executable, search_path, executable_base_dir
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
