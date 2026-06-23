from __future__ import annotations

from crewplane.core.config import Config
from crewplane.core.workflow.diagnostics import (
    WorkflowValidationDiagnostic,
    format_diagnostics,
    node_id_from_message,
)
from crewplane.core.workflow.models import WorkflowPlan
from crewplane.core.workflow.validation.nodes import (
    collect_workflow_node_diagnostics,
)
from crewplane.core.workflow.validation.policies import (
    collect_audit_rounds_validation_errors,
    collect_missing_provider_locations,
    collect_provider_validation_errors,
    collect_token_budget_validation_errors,
    collect_workspace_validation_diagnostics,
    validate_audit_rounds_settings,
    validate_provider_references,
    validate_token_budget_settings,
)
from crewplane.core.workflow.validation.templates import (
    collect_prompt_template_diagnostics,
    extract_template_tokens,
)

__all__ = [
    "WorkflowValidationDiagnostic",
    "collect_missing_provider_locations",
    "collect_provider_validation_diagnostics",
    "collect_provider_validation_errors",
    "collect_token_budget_validation_errors",
    "collect_workspace_validation_diagnostics",
    "collect_workflow_policy_diagnostics",
    "collect_workflow_validation_diagnostics",
    "extract_template_tokens",
    "validate_audit_rounds_settings",
    "validate_provider_references",
    "validate_token_budget_settings",
    "validate_workflow_plan",
]


def validate_workflow_plan(workflow: WorkflowPlan) -> WorkflowPlan:
    """Validate a workflow plan for structural and template correctness."""

    diagnostics = collect_workflow_validation_diagnostics(workflow)
    if diagnostics:
        raise ValueError(format_diagnostics(diagnostics))
    return workflow


def collect_workflow_validation_diagnostics(
    workflow: WorkflowPlan,
) -> tuple[WorkflowValidationDiagnostic, ...]:
    return (
        *collect_workflow_node_diagnostics(workflow),
        *collect_workflow_topology_diagnostics(workflow),
        *collect_prompt_template_diagnostics(workflow),
    )


def collect_workflow_topology_diagnostics(
    workflow: WorkflowPlan,
) -> tuple[WorkflowValidationDiagnostic, ...]:
    node_ids = {node.id for node in workflow.nodes}
    if len(node_ids) != len(workflow.nodes):
        return ()
    dependencies = {node.id: set(node.needs) for node in workflow.nodes}
    if any(node_id in needs for node_id, needs in dependencies.items()):
        return ()
    if any(not needs <= node_ids for needs in dependencies.values()):
        return ()

    pending = dict(dependencies)
    while pending:
        ready = [node_id for node_id, needs in pending.items() if not needs]
        if not ready:
            return (
                WorkflowValidationDiagnostic(
                    code="WORKFLOW-DAG",
                    phase="reference",
                    message="Workflow graph contains a cycle.",
                ),
            )
        for node_id in ready:
            pending.pop(node_id)
            for needs in pending.values():
                needs.discard(node_id)
    return ()


def collect_provider_validation_diagnostics(
    workflow: WorkflowPlan,
    config: Config,
) -> tuple[WorkflowValidationDiagnostic, ...]:
    return tuple(
        WorkflowValidationDiagnostic(
            code="WORKFLOW-PROVIDER",
            phase="provider",
            message=message,
        )
        for message in collect_provider_validation_errors(workflow, config)
    )


def collect_workflow_policy_diagnostics(
    workflow: WorkflowPlan,
    config: Config,
) -> tuple[WorkflowValidationDiagnostic, ...]:
    audit_round_messages = collect_audit_rounds_validation_errors(workflow, config)
    token_budget_messages = collect_token_budget_validation_errors(workflow, config)
    return (
        *collect_workspace_validation_diagnostics(workflow, config),
        *_diagnostics_from_messages(
            "WORKFLOW-POLICY",
            "node_policy",
            audit_round_messages,
        ),
        *_diagnostics_from_messages(
            "WORKFLOW-TOKEN-BUDGET",
            "node_policy",
            token_budget_messages,
        ),
    )


def _diagnostics_from_messages(
    code: str,
    phase: str,
    messages: tuple[str, ...] | list[str],
) -> tuple[WorkflowValidationDiagnostic, ...]:
    return tuple(
        WorkflowValidationDiagnostic(
            code=code,
            phase=phase,
            message=message,
            node_id=node_id_from_message(message),
        )
        for message in messages
    )
