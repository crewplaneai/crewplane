from __future__ import annotations

from dataclasses import dataclass

from crewplane.core.workflow.models import WorkflowNode, WorkflowPlan

from .policy import (
    PROJECT_ROOT_WORKTREE_SELECTOR,
    WorkspaceCleanStart,
    WorkspaceMaterialization,
    WorkspaceSourceKind,
    WorktreeContract,
    WorktreeKind,
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
