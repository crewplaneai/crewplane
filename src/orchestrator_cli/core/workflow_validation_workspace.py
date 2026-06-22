from __future__ import annotations

from dataclasses import replace

from orchestrator_cli.core.config import Config, Settings
from orchestrator_cli.core.workflow_diagnostics import WorkflowValidationDiagnostic
from orchestrator_cli.core.workflow_graph import ancestor_map
from orchestrator_cli.core.workflow_models import WorkflowNode, WorkflowPlan
from orchestrator_cli.core.workflow_workspace_diagnostics import (
    workspace_policy_diagnostics,
)
from orchestrator_cli.core.workspace_policy import (
    PROJECT_ROOT_WORKTREE_SELECTOR,
    default_worktree_contract,
)
from orchestrator_cli.core.workspace_selection import LogicalWorkspaceSelection


def collect_workspace_policy_diagnostics(
    workflow: WorkflowPlan,
    config: Config,
) -> tuple[WorkflowValidationDiagnostic, ...]:
    selections = graph_safe_logical_workspace_selections(workflow, config)
    if selections is None:
        return ()
    return workspace_policy_diagnostics(workflow, config, selections)


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


def logical_workspace_selections(
    workflow: WorkflowPlan,
    config: Config,
) -> dict[str, LogicalWorkspaceSelection]:
    return _with_source_nodes(
        workflow,
        _base_logical_workspace_selections(workflow, config),
    )


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
    selector = _selected_worktree_name(workflow, node)
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
    settings = config.settings if config.settings is not None else Settings()
    selections: dict[str, LogicalWorkspaceSelection] = {}
    for node in workflow.nodes:
        if node.mode == "input":
            continue
        selections[node.id] = _selection_for_node(workflow, settings, node)
    return selections


def _selected_worktree_name(
    workflow: WorkflowPlan,
    node: WorkflowNode,
) -> str | None:
    if node.worktree == PROJECT_ROOT_WORKTREE_SELECTOR:
        return None
    if node.worktree is not None:
        return node.worktree
    if len(workflow.worktrees) == 1:
        return next(iter(workflow.worktrees))
    return None


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
    candidates = [
        ancestor
        for ancestor in ancestors.get(node.id, set())
        if _same_lineage_worktree(selection, selections.get(ancestor))
    ]
    if not candidates:
        return None
    ordered = sorted(
        candidates,
        key=lambda candidate: (
            len(ancestors.get(candidate, set())),
            node_order[candidate],
        ),
    )
    return ordered[-1]


def _same_lineage_worktree(
    selection: LogicalWorkspaceSelection,
    upstream: LogicalWorkspaceSelection | None,
) -> bool:
    return (
        upstream is not None
        and upstream.lineage_producer
        and upstream.logical_worktree_name == selection.logical_worktree_name
    )


def _is_graph_shape_error(exc: ValueError) -> bool:
    message = str(exc)
    return (
        "depends on unknown node" in message
        or message == "Workflow graph contains a cycle."
    )
