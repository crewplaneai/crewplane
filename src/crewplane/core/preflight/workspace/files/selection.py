from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from crewplane.core.workflow.graph import ancestor_map
from crewplane.core.workflow.models import WorkflowNode, WorkflowPlan
from crewplane.core.workspace.policy import PROJECT_ROOT_WORKTREE_SELECTOR


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


def selected_worktree_kind(workflow: WorkflowPlan, node: WorkflowNode) -> str | None:
    selector = selected_worktree_name(workflow, node)
    if selector is None:
        return None
    declaration = workflow.worktrees.get(selector)
    return declaration.kind if declaration is not None else None


def node_selects_managed_workspace(
    workflow: WorkflowPlan,
    node: WorkflowNode,
) -> bool:
    if node.mode == "input" or not workflow.worktrees:
        return False
    return selected_worktree_name(workflow, node) is not None


def workflow_has_managed_workspace_selection(workflow: WorkflowPlan) -> bool:
    return any(
        node_selects_managed_workspace(workflow, node) for node in workflow.nodes
    )


def is_allowlisted_absolute_path(raw_path: str, allowed_paths: Iterable[Path]) -> bool:
    try:
        candidate = Path(raw_path.strip()).expanduser()
    except RuntimeError:
        return False
    if not candidate.is_absolute():
        return False
    normalized = candidate.resolve(strict=False)
    return any(
        normalized == allowed or normalized.is_relative_to(allowed)
        for allowed in allowed_paths
    )


def has_same_worktree_source_ancestor(
    workflow: WorkflowPlan,
    node: WorkflowNode,
    selector: str,
) -> bool:
    nodes_by_id = {candidate.id: candidate for candidate in workflow.nodes}
    for ancestor_id in ancestor_map(workflow).get(node.id, set()):
        upstream = nodes_by_id.get(ancestor_id)
        if upstream is None:
            continue
        if (
            selected_worktree_name(workflow, upstream) == selector
            and selected_worktree_kind(workflow, upstream) == "worktree"
        ):
            return True
    return False
