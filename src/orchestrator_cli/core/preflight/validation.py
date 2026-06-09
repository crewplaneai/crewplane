from orchestrator_cli.core.config import Config
from orchestrator_cli.core.preflight.diagnostics import PreflightDiagnostic
from orchestrator_cli.core.workflow_diagnostics import WorkflowValidationDiagnostic
from orchestrator_cli.core.workflow_models import WorkflowPlan
from orchestrator_cli.core.workflow_validation import (
    collect_missing_provider_locations,
    collect_provider_validation_diagnostics,
    collect_workflow_policy_diagnostics,
    collect_workflow_validation_diagnostics,
    validate_audit_rounds_settings,
    validate_token_budget_settings,
)


def validate_preflight_workflow_references(workflow: WorkflowPlan) -> WorkflowPlan:
    diagnostics = collect_preflight_workflow_reference_diagnostics(workflow)
    if diagnostics:
        raise ValueError("\n".join(diagnostic.message for diagnostic in diagnostics))
    return workflow


def collect_preflight_workflow_reference_diagnostics(
    workflow: WorkflowPlan,
) -> tuple[PreflightDiagnostic, ...]:
    return _to_preflight_diagnostics(collect_workflow_validation_diagnostics(workflow))


def validate_preflight_provider_references(
    workflow: WorkflowPlan,
    config: Config,
) -> None:
    diagnostics = collect_preflight_provider_reference_diagnostics(workflow, config)
    if diagnostics:
        raise ValueError("\n".join(diagnostic.message for diagnostic in diagnostics))


def collect_preflight_provider_reference_diagnostics(
    workflow: WorkflowPlan,
    config: Config,
) -> tuple[PreflightDiagnostic, ...]:
    diagnostics = _preflight_provider_diagnostics(workflow, config)
    if diagnostics:
        return diagnostics
    return _to_preflight_diagnostics(
        collect_provider_validation_diagnostics(workflow, config)
    )


def validate_preflight_audit_rounds(
    workflow: WorkflowPlan,
    config: Config,
) -> None:
    validate_audit_rounds_settings(workflow, config)


def validate_preflight_token_budget(
    workflow: WorkflowPlan,
    config: Config,
) -> None:
    validate_token_budget_settings(workflow, config)


def collect_preflight_policy_diagnostics(
    workflow: WorkflowPlan,
    config: Config,
) -> tuple[PreflightDiagnostic, ...]:
    return _to_preflight_diagnostics(
        collect_workflow_policy_diagnostics(workflow, config)
    )


def _missing_provider_locations(
    workflow: WorkflowPlan,
    config: Config,
) -> dict[str, tuple[str, ...]]:
    return collect_missing_provider_locations(workflow, config)


def _preflight_provider_diagnostics(
    workflow: WorkflowPlan,
    config: Config,
) -> tuple[PreflightDiagnostic, ...]:
    diagnostics: list[PreflightDiagnostic] = []
    for provider_name, locations in _missing_provider_locations(
        workflow, config
    ).items():
        provider_locations = ", ".join(sorted(set(locations)))
        diagnostics.append(
            PreflightDiagnostic(
                code="PREFLIGHT-VALIDATION",
                phase="provider",
                message=(
                    f"Unknown provider '{provider_name}' referenced at: "
                    f"{provider_locations}."
                ),
            )
        )
    return tuple(diagnostics)


def _to_preflight_diagnostics(
    diagnostics: tuple[WorkflowValidationDiagnostic, ...],
) -> tuple[PreflightDiagnostic, ...]:
    return tuple(
        PreflightDiagnostic(
            code="PREFLIGHT-VALIDATION",
            phase=diagnostic.phase,
            message=diagnostic.message,
            severity=diagnostic.severity,
            node_id=diagnostic.node_id,
            metadata={"workflow_code": diagnostic.code, **diagnostic.metadata},
        )
        for diagnostic in diagnostics
    )
