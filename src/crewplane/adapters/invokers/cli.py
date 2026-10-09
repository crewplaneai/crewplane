from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Mapping
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

from .cli_invoker import (
    build_cli_invocation_plan,
    build_cli_log_presentation,
    command_strategy,
)
from .cli_invoker.capabilities import get_cli_provider_capability
from .cli_invoker.capability import CliInvocationRequest
from .cli_invoker.command_types import CommandAvailability, request_validation_targets


def collect_cli_availability_errors(
    workflow: WorkflowPlan,
    config: Config,
    which_fn: Callable[[str], str | None] | None = None,
    project_root: Path | None = None,
) -> list[str]:
    """Collect read-only command diagnostics using this entry point's strategy."""
    return command_strategy.command_strategy().collect_availability_errors(
        workflow,
        config,
        shutil.which if which_fn is None else which_fn,
        Path.cwd() if project_root is None else project_root,
    )


def collect_cli_request_errors(
    workflow: WorkflowPlan,
    config: Config,
    environment: Mapping[str, str] | None = None,
    working_directory: Path | None = None,
) -> list[str]:
    """Collect request validation errors for configured workflow providers."""

    strategy = command_strategy.command_strategy()
    errors: list[str] = []
    for target in request_validation_targets(workflow, config):
        try:
            request = CliInvocationRequest(
                config=target.agent_config,
                command_strategy=strategy,
                model=target.model,
                requested_reasoning=target.requested_reasoning,
                working_directory=working_directory,
                environment=os.environ if environment is None else environment,
            )
            get_cli_provider_capability(
                target.agent_config.provider_kind
            ).validate_request(request)
            strategy.validate_request(request)
        except ValueError as exc:
            errors.append(f"{target.location}: {exc}")
    return errors


def inspect_cli_command(
    cli_command: list[str],
    project_root: Path,
    which_fn: Callable[[str], str | None],
) -> CommandAvailability:
    """Return adapter-owned availability and onboarding diagnostics."""
    return command_strategy.command_strategy().availability(
        cli_command, project_root, which_fn
    )


def collect_cli_model_arg_warnings(config: Config) -> list[str]:
    return [
        (
            f"Agent '{agent_key}': remove model_arg for provider_kind '{agent.provider_kind.value}'. "
            "The field applies only when provider_kind is 'generic'."
        )
        for agent_key, agent in sorted(config.agents.items())
        if agent.provider_kind != ProviderKind.GENERIC
        and "model_arg" in agent.model_fields_set
    ]


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

    def collect_request_errors(
        self,
        workflow: WorkflowPlan,
        config: Config,
        working_directory: Path | None = None,
    ) -> tuple[str, ...]:
        """Collect built-in CLI request diagnostics."""

        return tuple(
            collect_cli_request_errors(
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
