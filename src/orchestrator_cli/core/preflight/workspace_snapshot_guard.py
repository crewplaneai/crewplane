from __future__ import annotations

from orchestrator_cli.core.config import Config
from orchestrator_cli.core.workflow_models import WorkflowPlan
from orchestrator_cli.core.workflow_validation_workspace import (
    workflow_has_selected_managed_workspaces,
)

from .compile_state import (
    CompileState,
    PreflightCompileOptions,
    append_diagnostic,
)
from .diagnostics import PreflightDiagnosticCode, PreflightDiagnosticPhase


def append_missing_workspace_snapshot_diagnostic(
    workflow: WorkflowPlan,
    config: Config,
    options: PreflightCompileOptions,
    state: CompileState,
) -> None:
    if not _workspace_enabled(config):
        return
    if not workflow_has_selected_managed_workspaces(workflow, config):
        return
    if options.workspace_source_snapshot is not None:
        return
    if any(diagnostic.severity == "error" for diagnostic in state.diagnostics):
        return
    append_diagnostic(
        state,
        code=PreflightDiagnosticCode.WORKSPACE_GIT_CONTRACT,
        phase=PreflightDiagnosticPhase.WORKTREE_CONTRACT,
        message=(
            "Workspace-enabled preflight requires a trusted workspace source "
            "snapshot from the CLI source gate before compiling workspace "
            "locators or execution identity."
        ),
    )


def _workspace_enabled(config: Config) -> bool:
    return config.settings is not None and config.settings.workspace.enabled
