from __future__ import annotations

import shutil
from collections.abc import Callable

from orchestrator_cli.architecture.contracts import (
    AgentInvoker,
    CanonicalIntegrationConfig,
    CliInvokerOptions,
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
) -> list[str]:
    """Collect missing executable errors for configured CLI providers."""

    executable_lookup = shutil.which if which_fn is None else which_fn
    missing_cli_locations: dict[str, list[str]] = {}
    for node in workflow.nodes:
        for provider in node.providers:
            agent_config = config.agents.get(provider.provider)
            if agent_config is None:
                continue
            cli_executable = agent_config.cli_cmd[0]
            if executable_lookup(cli_executable) is not None:
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
        errors.append(
            f"CLI '{cli_executable}' not found in PATH for provider "
            f"'{provider_name}', referenced in: {', '.join(unique_locations)}"
        )
    return errors


class CliInvokerAdapter:
    """Create the default CLI-backed agent invoker."""

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
