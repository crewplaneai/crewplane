from __future__ import annotations

from crewplane.core.config import Config
from crewplane.core.workflow.diagnostics import WorkflowValidationDiagnostic
from crewplane.core.workflow.models import WorkflowPlan
from crewplane.core.workflow.validation.workspace_diagnostics import (
    platform_workspace_diagnostics,
    repeat_force_run_workspace_diagnostics,
    workspace_policy_diagnostics,
)
from crewplane.core.workspace.selection import (
    LogicalWorkspaceSelection,
    logical_workspace_selections,
)


def collect_workspace_policy_diagnostics(
    workflow: WorkflowPlan,
    config: Config,
) -> tuple[WorkflowValidationDiagnostic, ...]:
    repetition_diagnostics = platform_workspace_diagnostics(config) + (
        repeat_force_run_workspace_diagnostics(workflow, config)
        if workflow.repeat_force_run_count is not None
        else ()
    )
    selections = graph_safe_logical_workspace_selections(workflow, config)
    if selections is None:
        return repetition_diagnostics
    return repetition_diagnostics + workspace_policy_diagnostics(
        workflow, config, selections
    )


def validate_repeat_force_run_workspace(workflow: WorkflowPlan, config: Config) -> None:
    diagnostics = repeat_force_run_workspace_diagnostics(workflow, config)
    if diagnostics:
        raise ValueError(diagnostics[0].message)


def graph_safe_logical_workspace_selections(
    workflow: WorkflowPlan,
    config: Config,
) -> dict[str, LogicalWorkspaceSelection] | None:
    try:
        return logical_workspace_selections(workflow, config)
    except ValueError as exc:
        if not _is_graph_shape_error(exc):
            raise
        return None


def workflow_uses_managed_workspaces(workflow: WorkflowPlan) -> bool:
    return bool(workflow.worktrees)


def workflow_has_selected_managed_workspaces(
    workflow: WorkflowPlan,
    config: Config,
) -> bool:
    selections = graph_safe_logical_workspace_selections(workflow, config)
    if selections is None:
        return False
    return any(selection.enabled for selection in selections.values())


def _is_graph_shape_error(exc: ValueError) -> bool:
    message = str(exc)
    return (
        "depends on unknown node" in message
        or message == "Workflow graph contains a cycle."
    )
