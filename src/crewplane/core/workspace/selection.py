from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace

from crewplane.core.config import Config, Settings
from crewplane.core.workflow.graph import ancestor_map
from crewplane.core.workflow.models import WorkflowNode, WorkflowPlan

from .policy import (
    PROJECT_ROOT_WORKTREE_SELECTOR,
    WorkspaceCleanStart,
    WorkspaceMaterialization,
    WorkspaceSourceKind,
    WorktreeContract,
    WorktreeKind,
    default_worktree_contract,
)


@dataclass(frozen=True)
class LogicalWorkspaceSelection:
    node_id: str
    enabled: bool
    logical_worktree_name: str | None
    declaration_kind: WorktreeKind | None
    materialization: WorkspaceMaterialization
    source_kind: WorkspaceSourceKind
    source_node_id: str | None
    clean_start: WorkspaceCleanStart
    worktree_contract: WorktreeContract
    setup_profile: str | None
    setup_commands: tuple[tuple[str, ...], ...]
    create_branch: bool
    branch_name: str | None
    writable: bool
    lineage_producer: bool


def logical_workspace_selections(
    workflow: WorkflowPlan,
    config: Config,
) -> dict[str, LogicalWorkspaceSelection]:
    return _with_source_nodes(
        workflow,
        _base_logical_workspace_selections(workflow, config),
    )


def selected_worktree_name(workflow: WorkflowPlan, node: WorkflowNode) -> str | None:
    if node.mode == "input":
        return None
    if node.worktree == PROJECT_ROOT_WORKTREE_SELECTOR:
        return None
    if node.worktree is not None:
        return node.worktree
    if len(workflow.worktrees) == 1:
        return next(iter(workflow.worktrees))
    return None


def latest_lineage_source(
    candidate_ancestor_counts: Mapping[str, int], node_order: Mapping[str, int]
) -> str | None:
    """Rank eligible lineage sources by ancestor count, then caller-owned order."""
    return max(
        candidate_ancestor_counts,
        key=lambda node_id: (candidate_ancestor_counts[node_id], node_order[node_id]),
        default=None,
    )


def _selection_for_node(
    workflow: WorkflowPlan,
    settings: Settings,
    node: WorkflowNode,
) -> LogicalWorkspaceSelection:
    clean_start = settings.workspace.clean_start
    contract = default_worktree_contract().model_copy(
        update={
            "mode": settings.workspace.worktree_contract,
        }
    )
    selector = selected_worktree_name(workflow, node)
    if selector is None:
        return LogicalWorkspaceSelection(
            node_id=node.id,
            enabled=False,
            logical_worktree_name=None,
            declaration_kind=None,
            materialization="project_root",
            source_kind="project",
            source_node_id=None,
            clean_start=clean_start,
            worktree_contract=contract,
            setup_profile=None,
            setup_commands=(),
            create_branch=False,
            branch_name=None,
            writable=False,
            lineage_producer=False,
        )

    declaration = workflow.worktrees[selector]
    setup_profile = (
        declaration.setup_profile if declaration.kind == "worktree" else None
    )
    setup_commands = _setup_commands(settings, setup_profile)
    return LogicalWorkspaceSelection(
        node_id=node.id,
        enabled=True,
        logical_worktree_name=selector,
        declaration_kind=declaration.kind,
        materialization=(
            "worktree_checkout"
            if declaration.kind == "worktree"
            else "snapshot_checkout"
        ),
        source_kind="project",
        source_node_id=None,
        clean_start=clean_start,
        worktree_contract=contract,
        setup_profile=setup_profile,
        setup_commands=setup_commands,
        create_branch=declaration.create_branch,
        branch_name=declaration.branch_name,
        writable=True,
        lineage_producer=declaration.kind == "worktree",
    )


def _base_logical_workspace_selections(
    workflow: WorkflowPlan,
    config: Config,
) -> dict[str, LogicalWorkspaceSelection]:
    settings = config.settings
    selections: dict[str, LogicalWorkspaceSelection] = {}
    for node in workflow.nodes:
        if node.mode == "input":
            continue
        selections[node.id] = _selection_for_node(workflow, settings, node)
    return selections


def _setup_commands(
    settings: Settings,
    setup_profile: str | None,
) -> tuple[tuple[str, ...], ...]:
    if setup_profile is None:
        return ()
    profile = settings.workspace.setup_profiles.get(setup_profile)
    if profile is None:
        return ()
    return tuple(tuple(command) for command in profile.run)


def _with_source_nodes(
    workflow: WorkflowPlan,
    selections: dict[str, LogicalWorkspaceSelection],
) -> dict[str, LogicalWorkspaceSelection]:
    ancestors = ancestor_map(workflow)
    node_order = {node.id: index for index, node in enumerate(workflow.nodes)}
    updated = dict(selections)
    for node in workflow.nodes:
        selection = updated.get(node.id)
        if selection is None or selection.declaration_kind != "worktree":
            continue
        source_node_id = _latest_same_worktree_dependency(
            node,
            selection,
            updated,
            ancestors,
            node_order,
        )
        if source_node_id is not None:
            updated[node.id] = replace(
                selection,
                source_kind="node",
                source_node_id=source_node_id,
            )
    return updated


def _latest_same_worktree_dependency(
    node: WorkflowNode,
    selection: LogicalWorkspaceSelection,
    selections: dict[str, LogicalWorkspaceSelection],
    ancestors: dict[str, set[str]],
    node_order: dict[str, int],
) -> str | None:
    candidate_ancestor_counts = {
        ancestor: len(ancestors.get(ancestor, set()))
        for ancestor in ancestors.get(node.id, set())
        if _same_lineage_worktree(selection, selections.get(ancestor))
    }
    return latest_lineage_source(candidate_ancestor_counts, node_order)


def _same_lineage_worktree(
    selection: LogicalWorkspaceSelection,
    upstream: LogicalWorkspaceSelection | None,
) -> bool:
    return (
        upstream is not None
        and upstream.lineage_producer
        and upstream.logical_worktree_name == selection.logical_worktree_name
    )
