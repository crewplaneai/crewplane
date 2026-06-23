from __future__ import annotations

from crewplane.core.config import Config
from crewplane.core.workflow.models import WorkflowPlan
from crewplane.core.workflow.validation.workspace import (
    logical_workspace_selections,
)
from crewplane.core.workspace.selection import LogicalWorkspaceSelection

from .models import (
    WorkspaceBranchExportRecord,
    WorkspaceSelectionRecord,
    WorkspaceSetupCommandRecord,
    WorkspaceSetupRecord,
)


def workspace_policy_records(
    workflow: WorkflowPlan,
    config: Config,
) -> dict[str, WorkspaceSelectionRecord]:
    return {
        node_id: _record_from_selection(selection)
        for node_id, selection in logical_workspace_selections(
            workflow,
            config,
        ).items()
        if selection.enabled
    }


def _record_from_selection(
    selection: LogicalWorkspaceSelection,
) -> WorkspaceSelectionRecord:
    setup = None
    if selection.setup_profile is not None:
        setup = WorkspaceSetupRecord(
            profile_name=selection.setup_profile,
            commands=[
                WorkspaceSetupCommandRecord(
                    argv=list(command),
                    command_index=index,
                )
                for index, command in enumerate(selection.setup_commands)
            ],
        )
    return WorkspaceSelectionRecord(
        enabled=selection.enabled,
        logical_worktree_name=selection.logical_worktree_name,
        declaration_kind=selection.declaration_kind,
        source_kind=selection.source_kind,
        source_node_id=selection.source_node_id,
        clean_start=selection.clean_start,
        materialization=selection.materialization,
        worktree_contract=selection.worktree_contract,
        setup=setup,
        branch_export=WorkspaceBranchExportRecord(
            create_branch=selection.create_branch,
            branch_name=selection.branch_name,
        ),
        writable=selection.writable,
        lineage_producer=selection.lineage_producer,
    )
