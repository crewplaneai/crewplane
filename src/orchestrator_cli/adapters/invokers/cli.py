from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from pathlib import Path

from orchestrator_cli.architecture.contracts import (
    AgentInvoker,
    CanonicalIntegrationConfig,
    CliInvokerOptions,
    InvokerAdapterCapabilities,
    JsonObject,
)
from orchestrator_cli.core.config import Config
from orchestrator_cli.core.workflow_models import WorkflowPlan
from orchestrator_cli.runtime.agent.invoker import PlannedAgentInvoker

from .cli_invoker import build_cli_invocation_plan, build_cli_log_presentation


def collect_cli_availability_errors(
    workflow: WorkflowPlan,
    config: Config,
    which_fn: Callable[[str], str | None] | None = None,
    project_root: Path | None = None,
) -> list[str]:
    """Collect missing executable errors for configured CLI providers."""

    executable_lookup = shutil.which if which_fn is None else which_fn
    executable_base_dir = Path.cwd() if project_root is None else project_root
    missing_cli_locations: dict[str, list[str]] = {}
    for node in workflow.nodes:
        for provider in node.providers:
            agent_config = config.agents.get(provider.provider)
            if agent_config is None:
                continue
            cli_executable = agent_config.cli_cmd[0]
            if _cli_executable_available(
                cli_executable,
                executable_lookup,
                executable_base_dir,
            ):
                continue
            location = f"workflow '{workflow.name}' -> node '{node.id}'"
            missing_cli_locations.setdefault(provider.provider, []).append(
                f"{location} (CLI: {cli_executable})"
            )
    return _format_missing_cli_errors(config, missing_cli_locations)


def _format_missing_cli_errors(
    config: Config,
    missing_cli_locations: dict[str, list[str]],
) -> list[str]:
    errors: list[str] = []
    for provider_name, locations in sorted(missing_cli_locations.items()):
        unique_locations = sorted(set(locations))
        cli_executable = config.agents[provider_name].cli_cmd[0]
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


def _cli_executable_available(
    executable: str,
    executable_lookup: Callable[[str], str | None],
    executable_base_dir: Path,
) -> bool:
    executable_path = Path(executable)
    if executable_path.is_absolute():
        return _is_executable_file(executable_path)
    if _contains_path_separator(executable):
        return _is_executable_file(executable_base_dir / executable_path)
    return executable_lookup(executable) is not None


def _is_executable_file(path: Path) -> bool:
    resolved = path.resolve(strict=False)
    return resolved.is_file() and os.access(resolved, os.X_OK)


def _contains_path_separator(value: str) -> bool:
    return "/" in value or "\\" in value


class CliInvokerAdapter:
    """Create the default CLI-backed agent invoker."""

    def workspace_capabilities(self) -> InvokerAdapterCapabilities:
        return InvokerAdapterCapabilities.workspace_supported(
            launch_mode="runtime_command_runner",
            controlled_child_environment=True,
        )

    def canonicalize_options(
        self,
        implementation: str,
        resolved_identity: str,
        options: JsonObject | None = None,
    ) -> CanonicalIntegrationConfig:
        CliInvokerOptions()
        if options:
            raise ValueError(
                "cli invoker implementation does not support options; "
                f"got: {sorted(options)}"
            )
        return CanonicalIntegrationConfig(
            implementation=implementation,
            resolved_identity=resolved_identity,
            options={},
            option_scopes={},
            capabilities=self.workspace_capabilities().as_dict(),
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
