from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from crewplane.architecture.contracts import JsonObject
from crewplane.architecture.errors import AdapterContractError
from crewplane.architecture.loader import (
    instantiate_adapter,
    is_builtin_implementation,
    resolve_implementation_path,
)
from crewplane.architecture.ports import InvokerAdapterPort
from crewplane.core.config import Config
from crewplane.core.preflight.diagnostics import (
    PreflightDiagnostic,
    PreflightDiagnosticCode,
    PreflightDiagnosticPhase,
)
from crewplane.core.workflow.models import WorkflowPlan
from crewplane.runtime.agent.process.drain import unconfirmed_process_cleanup


@dataclass(frozen=True)
class InvokerDiagnostics:
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


def collect_invoker_diagnostics(
    workflow: WorkflowPlan,
    config: Config,
    project_root: Path,
    options: JsonObject,
) -> InvokerDiagnostics:
    diagnostics, _ = _request_diagnostics(workflow, config, project_root, options)
    return diagnostics


async def collect_invoker_availability_diagnostics(
    workflow: WorkflowPlan,
    config: Config,
    project_root: Path,
    options: JsonObject,
    executable_lookup: Callable[[str], str | None] | None = None,
) -> InvokerDiagnostics:
    diagnostics, adapter = _request_diagnostics(workflow, config, project_root, options)
    if adapter is None:
        return diagnostics
    errors = await run_availability_errors(
        workflow, config, adapter, project_root, executable_lookup, options
    )
    return InvokerDiagnostics(diagnostics.errors + errors, diagnostics.warnings)


def _request_diagnostics(
    workflow: WorkflowPlan,
    config: Config,
    project_root: Path,
    options: JsonObject,
) -> tuple[InvokerDiagnostics, InvokerAdapterPort | None]:
    implementation = config.settings.integrations.invoker.implementation
    identity = resolve_implementation_path("invoker", implementation)
    if is_builtin_implementation("invoker", identity, "mock"):
        return InvokerDiagnostics(), None
    if not is_builtin_implementation("invoker", identity, "cli"):
        return InvokerDiagnostics(
            _reasoning_ineligibility_errors(_reasoning_requested_locations(workflow))
        ), None
    adapter = instantiate_adapter("invoker", implementation)
    errors = run_request_validation_errors(
        workflow, config, adapter, identity, project_root, options
    )
    return InvokerDiagnostics(errors, run_model_arg_warnings(config, adapter)), adapter


async def run_availability_errors(
    workflow: WorkflowPlan,
    config: Config,
    adapter: InvokerAdapterPort,
    project_root: Path,
    executable_lookup: Callable[[str], str | None] | None,
    options: JsonObject,
) -> tuple[str, ...]:
    collector = getattr(adapter, "collect_availability_errors", None)
    if not callable(collector):
        return ()
    try:
        errors = await collector(
            workflow,
            config,
            project_root,
            executable_lookup=executable_lookup,
            options=options,
        )
    except Exception as exc:
        if unconfirmed_process_cleanup(exc) is not None:
            raise
        raise AdapterContractError(
            f"Invoker adapter collect_availability_errors() failed: {exc}"
        ) from exc
    return validated_adapter_messages(errors, "collect_availability_errors")


def run_request_validation_errors(
    workflow: WorkflowPlan,
    config: Config,
    adapter: InvokerAdapterPort,
    resolved_identity: str,
    working_directory: Path | None = None,
    options: JsonObject | None = None,
) -> tuple[str, ...]:
    if is_builtin_implementation("invoker", resolved_identity, "mock"):
        return ()
    requested_locations = _reasoning_requested_locations(workflow)
    if not is_builtin_implementation("invoker", resolved_identity, "cli"):
        return _reasoning_ineligibility_errors(requested_locations)
    collector = getattr(adapter, "collect_request_errors", None)
    if not callable(collector):
        raise AdapterContractError(
            "Built-in CLI invoker adapter must define collect_request_errors()."
        )
    try:
        errors = collector(
            workflow,
            config,
            working_directory=working_directory,
            options=options,
        )
    except Exception as exc:
        raise AdapterContractError(
            f"Built-in CLI invoker collect_request_errors() failed: {exc}"
        ) from exc
    return validated_adapter_messages(errors, "collect_request_errors")


def run_model_arg_warnings(
    config: Config,
    adapter: InvokerAdapterPort,
) -> tuple[str, ...]:
    collector = getattr(adapter, "collect_model_arg_warnings", None)
    if not callable(collector):
        raise AdapterContractError(
            "Built-in CLI invoker adapter must define collect_model_arg_warnings()."
        )
    try:
        warnings = collector(config)
    except Exception as exc:
        raise AdapterContractError(
            f"Built-in CLI invoker collect_model_arg_warnings() failed: {exc}"
        ) from exc
    return validated_adapter_messages(warnings, "collect_model_arg_warnings")


def invoker_preflight_warning_diagnostics(
    warnings: tuple[str, ...],
) -> tuple[PreflightDiagnostic, ...]:
    return tuple(
        PreflightDiagnostic(
            code=PreflightDiagnosticCode.PROVIDER_CONFIG,
            phase=PreflightDiagnosticPhase.PROVIDER,
            message=message,
            severity="warning",
        )
        for message in warnings
    )


def _reasoning_requested_locations(workflow: WorkflowPlan) -> tuple[str, ...]:
    return tuple(
        f"workflow '{workflow.name}' -> node '{node.id}' -> provider "
        f"'{provider.provider}'"
        for node in workflow.nodes
        for provider in node.providers
        if provider.reasoning is not None
    )


def _reasoning_ineligibility_errors(
    requested_locations: tuple[str, ...],
) -> tuple[str, ...]:
    return tuple(
        f"{location}: first-class reasoning requires the built-in CLI invoker."
        for location in requested_locations
    )


def validated_adapter_messages(value: object, method_name: str) -> tuple[str, ...]:
    if not isinstance(value, tuple) or not all(isinstance(item, str) for item in value):
        raise AdapterContractError(
            f"Invoker adapter {method_name}() must return tuple[str, ...]."
        )
    return value
