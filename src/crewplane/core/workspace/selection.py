from __future__ import annotations

from dataclasses import dataclass

from .policy import (
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
    clean_start: str
    worktree_contract: WorktreeContract
    setup_profile: str | None
    setup_commands: tuple[tuple[str, ...], ...]
    create_branch: bool
    branch_name: str | None
    writable: bool
    lineage_producer: bool
