from __future__ import annotations

from crewplane.core.preflight.models import WorkspaceSelectionRecord
from crewplane.core.workspace.policy import WorktreeContract

WORKTREE_CONTRACT = WorktreeContract()
WORKTREE_CONTRACT_PAYLOAD = WORKTREE_CONTRACT.model_dump(mode="json")


def workspace_selection_record(
    enabled: bool = True,
    kind: str = "worktree",
    logical_name: str = "primary",
    source_kind: str = "project",
    source_node_id: str | None = None,
    clean_start: str = "strict",
    materialization: str | None = None,
    writable: bool = True,
    lineage_producer: bool | None = None,
) -> WorkspaceSelectionRecord:
    resolved_lineage = (
        kind == "worktree" if lineage_producer is None else lineage_producer
    )
    return WorkspaceSelectionRecord(
        enabled=enabled,
        logical_worktree_name=logical_name,
        declaration_kind=kind,
        source_kind=source_kind,
        source_node_id=source_node_id,
        clean_start=clean_start,
        materialization=_normalized_materialization(materialization, kind),
        worktree_contract=WORKTREE_CONTRACT,
        writable=writable,
        lineage_producer=resolved_lineage,
    )


def _materialization(kind: str) -> str:
    return "worktree_checkout" if kind == "worktree" else "snapshot_checkout"


def _normalized_materialization(value: str | None, kind: str) -> str:
    if value in {None, ""}:
        return _materialization(kind)
    return value
