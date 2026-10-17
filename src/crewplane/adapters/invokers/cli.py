from __future__ import annotations

import inspect
import os
import shutil
from collections.abc import Callable, Mapping
from functools import partial
from pathlib import Path

from crewplane.architecture.contracts import (
    AgentInvoker,
    CanonicalIntegrationConfig,
    InvokerAdapterCapabilities,
    JsonObject,
    ProviderKind,
)
from crewplane.architecture.errors import AdapterContractError
from crewplane.core.config import Config
from crewplane.core.workflow.models import WorkflowPlan
from crewplane.runtime.agent.invoker import PlannedAgentInvoker

from .cli_invoker import (
    build_cli_invocation_plan,
    build_cli_log_presentation,
    command_strategy,
    unconfirmed_process_cleanup,
)
from .cli_invoker.capabilities import get_cli_provider_capability
from .cli_invoker.capability import CliInvocationRequest, CliProviderCapability
from .cli_invoker.command_types import CommandAvailability, request_validation_targets
from .cli_invoker.provider_registrations import (
    canonicalize_provider_options,
    verify_provider_options,
)


def collect_cli_availability_errors(
    workflow: WorkflowPlan,
    config: Config,
    which_fn: Callable[[str], str | None] | None = None,
    project_root: Path | None = None,
) -> list[str]:
    """Collect read-only command diagnostics using this entry point's strategy."""
    root = Path.cwd() if project_root is None else project_root
    return command_strategy.command_strategy().collect_availability_errors(
        workflow,
        config,
        _executable_lookup(root) if which_fn is None else which_fn,
        root,
    )


def _executable_lookup(project_root: Path) -> Callable[[str], str | None]:
    search_path = os.pathsep.join(
        str(project_root / entry)
        for entry in os.environ.get("PATH", os.defpath).split(os.pathsep)
    )
    return partial(shutil.which, path=search_path)


def collect_cli_request_errors(
    workflow: WorkflowPlan,
    config: Config,
    environment: Mapping[str, str] | None = None,
    working_directory: Path | None = None,
    providers: Mapping[str, CliProviderCapability] | None = None,
) -> list[str]:
    """Collect request validation errors for configured workflow providers."""

    strategy = command_strategy.command_strategy()
    errors = _unknown_provider_errors(config, providers)
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
                target.agent_config.provider_kind, providers
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
            f"Agent '{agent_key}': remove model_arg for provider_kind '{agent.provider_kind}'. "
            "The field applies only when provider_kind is 'generic'."
        )
        for agent_key, agent in sorted(config.agents.items())
        if agent.provider_kind != ProviderKind.GENERIC
        and "model_arg" in agent.model_fields_set
    ]


class CliInvokerAdapter:
    """Create the default CLI-backed agent invoker."""

    async def collect_availability_errors(
        self,
        workflow: WorkflowPlan,
        config: Config,
        project_root: Path,
        executable_lookup: Callable[[str], str | None] | None = None,
        options: JsonObject | None = None,
    ) -> tuple[str, ...]:
        """Check commands, then await configured model probes sequentially."""
        providers = verify_provider_options(options)
        lookup = (
            _executable_lookup(project_root)
            if executable_lookup is None
            else executable_lookup
        )
        errors = collect_cli_availability_errors(workflow, config, lookup, project_root)
        strategy = command_strategy.command_strategy()
        for target in request_validation_targets(workflow, config):
            request = CliInvocationRequest(
                config=target.agent_config,
                model=target.model,
                command_strategy=strategy,
                requested_reasoning=target.requested_reasoning,
                working_directory=project_root,
            )
            try:
                capability = get_cli_provider_capability(
                    request.config.provider_kind, providers
                )
                capability.validate_request(request)
                strategy.validate_request(request)
            except ValueError:
                continue  # Request diagnostics already report these failures.
            if (
                capability.check_availability is None
                or not strategy.availability(
                    request.config.cli_cmd,
                    project_root,
                    lookup,
                    request.environment if request.config.environment else None,
                ).available
            ):
                continue
            try:
                await _check_capability_availability(capability, request)
            except ValueError as exc:
                if unconfirmed_process_cleanup(exc) is not None:
                    raise
                errors.append(f"{target.location}: {exc}")
        return tuple(errors)

    def collect_request_errors(
        self,
        workflow: WorkflowPlan,
        config: Config,
        working_directory: Path | None = None,
        options: JsonObject | None = None,
    ) -> tuple[str, ...]:
        """Collect built-in CLI request diagnostics."""

        return tuple(
            collect_cli_request_errors(
                workflow,
                config,
                working_directory=working_directory,
                providers=verify_provider_options(options),
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
        return CanonicalIntegrationConfig(
            implementation=implementation,
            resolved_identity=resolved_identity,
            options=canonicalize_provider_options(options),
            option_scopes={"providers": "execution"},
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
        providers = verify_provider_options(options)
        errors = _unknown_provider_errors(config, providers)
        if errors:
            raise ValueError("; ".join(errors))
        return PlannedAgentInvoker(
            plan_builder=partial(build_cli_invocation_plan, providers=providers),
            log_presentation_builder=partial(
                build_cli_log_presentation, providers=providers
            ),
        )


def _unknown_provider_errors(
    config: Config, providers: Mapping[str, CliProviderCapability] | None
) -> list[str]:
    errors: list[str] = []
    for name, agent in sorted(config.agents.items()):
        try:
            get_cli_provider_capability(agent.provider_kind, providers)
        except ValueError as exc:
            errors.append(f"Agent '{name}': {exc}")
    return errors


async def _check_capability_availability(
    capability: CliProviderCapability, request: CliInvocationRequest
) -> None:
    callback = capability.check_availability
    assert callback is not None
    await _validate_availability_result(callback(request), capability.provider_kind)


async def _validate_availability_result(pending: object, identifier: str) -> None:
    if not inspect.isawaitable(pending):
        raise AdapterContractError(
            f"CLI provider '{identifier}': check_availability must return an awaitable"
        )
    result = await pending
    if result is not None:
        raise AdapterContractError(
            f"CLI provider '{identifier}': check_availability must return None when awaited"
        )


def _validate_config(config: Config) -> None:
    if not isinstance(config, Config):
        raise TypeError("config must be a Config instance")
