from __future__ import annotations

from crewplane.core.config import Config
from crewplane.core.token_budget import resolve_token_budget
from crewplane.core.workflow.diagnostics import WorkflowValidationDiagnostic
from crewplane.core.workflow.models import WorkflowPlan
from crewplane.core.workflow.validation.workspace import (
    collect_workspace_policy_diagnostics,
)


def validate_audit_rounds_settings(workflow: WorkflowPlan, config: Config) -> None:
    errors = collect_audit_rounds_validation_errors(workflow, config)
    if errors:
        raise ValueError("\n".join(errors))


def collect_audit_rounds_validation_errors(
    workflow: WorkflowPlan,
    config: Config,
) -> list[str]:
    max_audit_rounds = config.settings.max_audit_rounds if config.settings else 5
    return [
        (
            f"Sequential node '{node.id}' audit_rounds ({node.audit_rounds}) must be "
            f"less than or equal to settings.max_audit_rounds ({max_audit_rounds})."
        )
        for node in workflow.nodes
        if node.audit_rounds is not None and node.audit_rounds > max_audit_rounds
    ]


def collect_provider_validation_errors(
    workflow: WorkflowPlan,
    config: Config,
) -> list[str]:
    return _format_unknown_provider_errors(
        collect_missing_provider_locations(workflow, config)
    )


def collect_token_budget_validation_errors(
    workflow: WorkflowPlan,
    config: Config,
) -> list[str]:
    errors: list[str] = []
    settings_budget = (
        config.settings.token_budget if config.settings is not None else None
    )
    for node in workflow.nodes:
        if node.mode == "input":
            continue
        try:
            resolve_token_budget(settings_budget, node.token_budget)
        except ValueError as exc:
            errors.append(f"Node '{node.id}' token_budget is invalid: {exc}")
    return errors


def collect_workspace_validation_diagnostics(
    workflow: WorkflowPlan,
    config: Config,
) -> tuple[WorkflowValidationDiagnostic, ...]:
    return collect_workspace_policy_diagnostics(workflow, config)


def collect_missing_provider_locations(
    workflow: WorkflowPlan,
    config: Config,
) -> dict[str, tuple[str, ...]]:
    missing_provider_locations: dict[str, list[str]] = {}
    for node in workflow.nodes:
        for provider in node.providers:
            if provider.provider in config.agents:
                continue
            location = f"workflow '{workflow.name}' -> node '{node.id}'"
            missing_provider_locations.setdefault(provider.provider, []).append(
                location
            )
    return {
        provider_name: tuple(locations)
        for provider_name, locations in missing_provider_locations.items()
    }


def validate_provider_references(
    workflow: WorkflowPlan,
    config: Config,
) -> None:
    errors = collect_provider_validation_errors(workflow, config)
    if errors:
        raise ValueError("\n".join(errors))


def validate_token_budget_settings(workflow: WorkflowPlan, config: Config) -> None:
    errors = collect_token_budget_validation_errors(workflow, config)
    if errors:
        raise ValueError("\n".join(errors))


def _format_unknown_provider_errors(
    missing_provider_locations: dict[str, tuple[str, ...]],
) -> list[str]:
    errors: list[str] = []
    for provider_name, locations in sorted(missing_provider_locations.items()):
        unique_locations = sorted(set(locations))
        errors.append(
            "Unknown provider "
            f"'{provider_name}' referenced in: {', '.join(unique_locations)}"
        )
    return errors
