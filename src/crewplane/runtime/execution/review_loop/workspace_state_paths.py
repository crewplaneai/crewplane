from __future__ import annotations

from pathlib import Path

from crewplane.architecture.ports import ArtifactStorePort
from crewplane.core.preflight.models import PreflightExecutionNode
from crewplane.runtime.workspace.invocation import (
    invocation_slug,
    workspace_state_path,
)
from crewplane.runtime.workspace.setup import workspace_setup_artifacts
from crewplane.runtime.workspace.state import discard_workspace_lineage
from crewplane.runtime.workspace.worktree.refs import safe_file_component


def workspace_artifact_allowed_paths(
    output: ArtifactStorePort,
    node: PreflightExecutionNode,
    task_id: str,
    role_label: str,
    audit_round_num: int | None,
    round_num: int,
) -> set[Path]:
    if not _uses_managed_workspace(node):
        return set()
    slug = invocation_slug(node.id, task_id, audit_round_num, round_num)
    state_path = workspace_state_path(output, node, slug, audit_round_num, round_num)
    # Runtime writes this during provider calls; terminal updates rebase from a
    # trusted pre-provider payload so provider edits are not preserved.
    allowed_paths = {state_path}
    if _can_write_setup_artifacts(node):
        setup_artifacts = workspace_setup_artifacts(state_path)
        allowed_paths.update(
            {
                setup_artifacts.metadata_path,
                setup_artifacts.log_path,
            }
        )
    if _can_write_lineage_bundle(node, role_label):
        allowed_paths.add(
            state_path.parent
            / "workspace-bundles"
            / f"{safe_file_component(slug)}.bundle"
        )
    return allowed_paths


def discard_executor_workspace_lineage(
    output: ArtifactStorePort,
    node: PreflightExecutionNode,
    task_ids: set[str],
    audit_round_num: int | None,
    round_num: int,
    reason: str,
) -> None:
    if not _can_write_lineage_bundle(node, "executor"):
        return
    for task_id in sorted(task_ids):
        slug = invocation_slug(node.id, task_id, audit_round_num, round_num)
        state_path = workspace_state_path(
            output, node, slug, audit_round_num, round_num
        )
        discard_workspace_lineage(state_path, reason)


def _uses_managed_workspace(node: PreflightExecutionNode) -> bool:
    policy = node.workspace_policy
    return (
        policy is not None
        and policy.enabled
        and policy.materialization != "project_root"
    )


def _can_write_setup_artifacts(node: PreflightExecutionNode) -> bool:
    policy = node.workspace_policy
    return (
        _uses_managed_workspace(node)
        and policy is not None
        and policy.setup is not None
    )


def _can_write_lineage_bundle(
    node: PreflightExecutionNode,
    role_label: str,
) -> bool:
    policy = node.workspace_policy
    return (
        role_label == "executor"
        and policy is not None
        and policy.enabled
        and policy.lineage_producer
    )
