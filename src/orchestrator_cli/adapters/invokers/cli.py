from __future__ import annotations

import shutil
from collections.abc import Callable, Mapping
from typing import Any

from orchestrator_cli.architecture.api_version import EXT_API_VERSION
from orchestrator_cli.core.config import Config
from orchestrator_cli.core.preflight.runtime_config import CanonicalIntegrationConfig
from orchestrator_cli.core.workflow_models import WorkflowPlan
from orchestrator_cli.runtime.agent.invoker import DefaultAgentInvoker
from orchestrator_cli.runtime.agent.types import AgentInvoker


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
        options: Mapping[str, Any] | None = None,
    ) -> CanonicalIntegrationConfig:
        if options:
            raise ValueError(
                "cli invoker implementation does not support options; "
                f"got: {sorted(options)}"
            )
        return CanonicalIntegrationConfig(
            implementation=implementation,
            resolved_identity=resolved_identity,
            api_version=EXT_API_VERSION,
            options={},
            option_scopes={},
        )

    def create_invoker(
        self,
        config: Config,  # noqa: ARG002 - Required by callback or protocol signature.
        options: Mapping[str, Any] | None = None,
    ) -> AgentInvoker:
        """Build the default subprocess-based invoker."""

        self.canonicalize_options("cli", self.__class__.__module__, options)
        return DefaultAgentInvoker()
