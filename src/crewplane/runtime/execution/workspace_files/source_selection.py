from __future__ import annotations

from pathlib import Path

from crewplane.architecture.contracts import NodeArtifactRequest
from crewplane.architecture.ports import ArtifactStorePort
from crewplane.core.preflight.models import (
    PreflightExecutionNode,
    PreflightExecutionPlan,
    WorkspaceFileLocator,
)
from crewplane.runtime.workspace.plan_nodes import workspace_plan_node
from crewplane.runtime.workspace.state_selection import (
    latest_executor_lineage_state_path,
    required_lineage_state_path,
    review_loop_canonical_lineage_state_path,
)
from crewplane.runtime.workspace.worktree import WorktreeSourceRef
from crewplane.runtime.workspace.worktree.descriptors import load_source_ref_from_state

from .source_resolution import (
    WorkspaceCandidateSourceContext,
    candidate_source_ref_from_state,
    contextual_candidate_source_state_path,
    initial_pre_review_source,
    uses_candidate_source,
)


def dynamic_locator_source(
    plan: PreflightExecutionPlan,
    output: ArtifactStorePort,
    locator: WorkspaceFileLocator,
    workspace_candidate_source: bool = False,
    workspace_candidate_context: WorkspaceCandidateSourceContext | None = None,
) -> WorktreeSourceRef:
    node = _locator_node(plan, locator)
    pre_review_source = initial_pre_review_source(
        plan,
        output,
        locator,
        node,
        workspace_candidate_context,
    )
    if pre_review_source is not None:
        return pre_review_source
    state_path = dynamic_locator_source_state_path(
        plan,
        output,
        locator,
        workspace_candidate_source,
        workspace_candidate_context,
    )
    if uses_candidate_source(
        locator,
        workspace_candidate_source,
        workspace_candidate_context,
    ):
        return candidate_source_ref_from_state(state_path)
    return load_source_ref_from_state(state_path)


def dynamic_locator_source_state_path(
    plan: PreflightExecutionPlan,
    output: ArtifactStorePort,
    locator: WorkspaceFileLocator,
    workspace_candidate_source: bool = False,
    workspace_candidate_context: WorkspaceCandidateSourceContext | None = None,
) -> Path:
    node = _locator_node(plan, locator)
    policy = node.workspace_policy
    if policy is None:
        raise RuntimeError(
            "Runtime-dynamic workspace file locator has no workspace policy: "
            f"{locator.locator_id}."
        )
    if uses_candidate_source(
        locator,
        workspace_candidate_source,
        workspace_candidate_context,
    ):
        return _candidate_source_state_path(
            output,
            node,
            locator,
            workspace_candidate_context,
        )
    if policy.source_kind == "node" and policy.source_node_id is not None:
        return _upstream_source_state_path(
            plan,
            output,
            policy.source_node_id,
        )
    raise RuntimeError(
        "Runtime-dynamic workspace file locator has no available candidate "
        f"source: {locator.locator_id}."
    )


def _locator_node(
    plan: PreflightExecutionPlan,
    locator: WorkspaceFileLocator,
) -> PreflightExecutionNode:
    node = next((item for item in plan.nodes if item.id == locator.node_id), None)
    if node is None:
        raise RuntimeError(
            f"Workspace file locator references an unknown node: {locator.locator_id}."
        )
    return node


def _candidate_source_state_path(
    output: ArtifactStorePort,
    node: PreflightExecutionNode,
    locator: WorkspaceFileLocator,
    context: WorkspaceCandidateSourceContext | None,
) -> Path:
    contextual_state_path = contextual_candidate_source_state_path(
        output,
        node,
        context,
    )
    if contextual_state_path is not None:
        return contextual_state_path
    if context is not None:
        raise RuntimeError(
            "Workspace source node has no matching executor state for "
            f"{context.role_label} round {context.round_num}: {locator.node_id}."
        )
    stage_dir = output.get_node_dir(
        NodeArtifactRequest(node.id, node.artifact_contract)
    )
    if stage_dir is None:
        raise RuntimeError(
            f"Workspace source node has no stage directory: {locator.node_id}."
        )
    state_path = review_loop_canonical_lineage_state_path(
        stage_dir,
        node,
    ) or latest_executor_lineage_state_path(stage_dir)
    if state_path is None:
        raise RuntimeError(
            f"Workspace source node has no succeeded executor state: {locator.node_id}."
        )
    return state_path


def _upstream_source_state_path(
    plan: PreflightExecutionPlan,
    output: ArtifactStorePort,
    source_node_id: str,
) -> Path:
    return required_lineage_state_path(
        output,
        workspace_plan_node(plan, source_node_id),
    )
