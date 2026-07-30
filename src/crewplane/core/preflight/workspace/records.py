from __future__ import annotations

from crewplane.architecture.contracts import JsonObject
from crewplane.core.config import Config
from crewplane.core.preflight.runtime_config import RuntimeConfigSnapshot
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
    runtime_snapshot: RuntimeConfigSnapshot,
) -> dict[str, WorkspaceSelectionRecord]:
    return {
        node_id: _record_from_selection(
            selection,
            runtime_snapshot.workspace.setup_profiles,
        )
        for node_id, selection in logical_workspace_selections(
            workflow,
            config,
        ).items()
        if selection.enabled
    }


def _record_from_selection(
    selection: LogicalWorkspaceSelection,
    setup_profiles: JsonObject,
) -> WorkspaceSelectionRecord:
    setup = None
    if selection.setup_profile is not None:
        commands = _persisted_setup_commands(selection, setup_profiles)
        setup = WorkspaceSetupRecord(
            profile_name=selection.setup_profile,
            commands=[
                WorkspaceSetupCommandRecord(
                    argv=list(command),
                    command_index=index,
                )
                for index, command in enumerate(commands)
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


def _persisted_setup_commands(
    selection: LogicalWorkspaceSelection,
    setup_profiles: JsonObject,
) -> list[list[str | JsonObject]]:
    profile_name = selection.setup_profile
    if profile_name is None:
        return []
    profile = setup_profiles.get(profile_name)
    if not isinstance(profile, dict):
        raise ValueError(
            f"Compiled workspace setup profile '{profile_name}' is missing."
        )
    commands = profile.get("run")
    if not isinstance(commands, list) or len(commands) != len(selection.setup_commands):
        raise ValueError(
            f"Compiled workspace setup profile '{profile_name}' has invalid commands."
        )
    persisted_commands: list[list[str | JsonObject]] = []
    for command in commands:
        if not isinstance(command, list):
            raise ValueError(
                f"Compiled workspace setup profile '{profile_name}' has invalid argv."
            )
        persisted_command: list[str | JsonObject] = []
        for token in command:
            if isinstance(token, str):
                persisted_command.append(token)
            elif isinstance(token, dict):
                persisted_command.append(dict(token))
            else:
                raise ValueError(
                    f"Compiled workspace setup profile '{profile_name}' "
                    "has a non-string argv token."
                )
        persisted_commands.append(persisted_command)
    return persisted_commands
