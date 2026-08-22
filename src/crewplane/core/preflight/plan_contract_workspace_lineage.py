"""Validate persisted preflight workspace lineage contracts."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import PreflightExecutionNode, WorkspaceSelectionRecord


def validate_workspace_source_lineage(
    node: PreflightExecutionNode,
    nodes_by_id: dict[str, PreflightExecutionNode],
    node_order: dict[str, int],
) -> None:
    policy = node.workspace_policy
    if policy is None:
        return
    _validate_declared_workspace_source(node, nodes_by_id, node_order)
    _validate_direct_worktree_dependencies(node, nodes_by_id)
    if not _is_lineage_worktree_policy(policy):
        return
    expected_source = _latest_same_worktree_source(node, nodes_by_id, node_order)
    if expected_source is None:
        if policy.source_kind != "project" or policy.source_node_id is not None:
            raise ValueError(
                f"Persisted node '{node.id}' must use project workspace source "
                "without a same-worktree lineage ancestor."
            )
        return
    if policy.source_kind != "node" or policy.source_node_id != expected_source:
        raise ValueError(
            f"Persisted node '{node.id}' must use latest same-worktree lineage "
            f"source '{expected_source}'."
        )


def _validate_declared_workspace_source(
    node: PreflightExecutionNode,
    nodes_by_id: dict[str, PreflightExecutionNode],
    node_order: dict[str, int],
) -> None:
    policy = node.workspace_policy
    if policy is None or policy.source_kind != "node":
        return
    source_node_id = policy.source_node_id
    if source_node_id is None or source_node_id not in nodes_by_id:
        raise ValueError(
            f"Persisted node '{node.id}' workspace policy references an unknown "
            "source node."
        )
    if source_node_id not in _ancestor_node_ids(node, nodes_by_id):
        raise ValueError(
            f"Persisted node '{node.id}' workspace source '{source_node_id}' must "
            "be an upstream dependency."
        )
    if node_order[source_node_id] >= node_order[node.id]:
        raise ValueError(
            f"Persisted node '{node.id}' workspace source '{source_node_id}' is not "
            "upstream."
        )
    source_policy = nodes_by_id[source_node_id].workspace_policy
    if (
        source_policy is None
        or not source_policy.enabled
        or not source_policy.lineage_producer
    ):
        raise ValueError(
            f"Persisted node '{node.id}' workspace source '{source_node_id}' must "
            "be an enabled lineage producer."
        )
    if source_policy.logical_worktree_name != policy.logical_worktree_name:
        raise ValueError(
            f"Persisted node '{node.id}' workspace source '{source_node_id}' must "
            "use the same logical worktree."
        )


def _validate_direct_worktree_dependencies(
    node: PreflightExecutionNode,
    nodes_by_id: dict[str, PreflightExecutionNode],
) -> None:
    policy = node.workspace_policy
    if policy is None or not _is_lineage_worktree_policy(policy):
        return
    for dependency_id in node.dependencies:
        dependency = nodes_by_id.get(dependency_id)
        dependency_policy = (
            dependency.workspace_policy if dependency is not None else None
        )
        if dependency_policy is None or not _is_lineage_worktree_policy(
            dependency_policy
        ):
            continue
        if dependency_policy.logical_worktree_name != policy.logical_worktree_name:
            raise ValueError(
                f"Persisted node '{node.id}' directly depends on lineage producer "
                f"'{dependency_id}' from a different logical worktree; dependencies "
                "between worktrees must use the same logical worktree."
            )


def _latest_same_worktree_source(
    node: PreflightExecutionNode,
    nodes_by_id: dict[str, PreflightExecutionNode],
    node_order: dict[str, int],
) -> str | None:
    policy = node.workspace_policy
    if policy is None:
        return None
    candidates: list[str] = []
    for node_id in _ancestor_node_ids(node, nodes_by_id):
        candidate_policy = nodes_by_id[node_id].workspace_policy
        if candidate_policy is None or not _is_lineage_worktree_policy(
            candidate_policy
        ):
            continue
        if candidate_policy.logical_worktree_name == policy.logical_worktree_name:
            candidates.append(node_id)
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda node_id: (
            len(_ancestor_node_ids(nodes_by_id[node_id], nodes_by_id)),
            node_order[node_id],
        ),
    )


def _ancestor_node_ids(
    node: PreflightExecutionNode,
    nodes_by_id: dict[str, PreflightExecutionNode],
) -> set[str]:
    pending = list(node.dependencies)
    ancestors: set[str] = set()
    while pending:
        dependency_id = pending.pop()
        if dependency_id in ancestors:
            continue
        ancestors.add(dependency_id)
        dependency = nodes_by_id.get(dependency_id)
        if dependency is not None:
            pending.extend(dependency.dependencies)
    return ancestors.intersection(nodes_by_id)


def _is_lineage_worktree_policy(
    policy: WorkspaceSelectionRecord | None,
) -> bool:
    return bool(
        policy is not None
        and policy.enabled
        and policy.declaration_kind == "worktree"
        and policy.lineage_producer
    )
