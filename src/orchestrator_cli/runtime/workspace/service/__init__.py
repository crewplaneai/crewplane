from __future__ import annotations

from orchestrator_cli.architecture.contracts import InvocationContext
from orchestrator_cli.runtime.workspace.invocation import node_by_id
from orchestrator_cli.runtime.workspace.materialization import MaterializationLimiter
from orchestrator_cli.runtime.workspace.prepared_workspace import PreparedWorkspace

from .common import project_root_cwd, workspace_state_request
from .snapshot import prepare_snapshot_invocation_workspace
from .types import (
    MaterializedSnapshotWorkspace,
    MaterializedWorktreeWorkspace,
    SnapshotPreparationPlan,
    WorkspaceInvocationRequest,
    WorktreePreparationPlan,
)
from .worktree import prepare_worktree_invocation_workspace

__all__ = [
    "MaterializationLimiter",
    "MaterializedSnapshotWorkspace",
    "MaterializedWorktreeWorkspace",
    "SnapshotPreparationPlan",
    "WorkspaceInvocationRequest",
    "WorktreePreparationPlan",
    "prepare_invocation_workspace",
    "prepare_worktree_invocation_workspace",
    "project_root_cwd",
    "workspace_state_request",
]


def prepare_invocation_workspace(
    request: WorkspaceInvocationRequest,
    invocation_context: InvocationContext,
) -> PreparedWorkspace:
    source = request.plan.workspace_source
    if source is None:
        return PreparedWorkspace(
            cwd=project_root_cwd(request.plan),
            invocation_context=invocation_context,
        )
    node = node_by_id(request.plan, request.node_id)
    policy = node.workspace_policy
    if policy is None or not policy.enabled or policy.materialization == "project_root":
        return PreparedWorkspace(
            cwd=project_root_cwd(request.plan),
            invocation_context=invocation_context,
        )
    if policy.declaration_kind == "worktree":
        return prepare_worktree_invocation_workspace(
            request,
            invocation_context,
            node,
            policy,
            source,
        )
    return prepare_snapshot_invocation_workspace(
        request,
        invocation_context,
        node,
        policy,
        source,
    )
