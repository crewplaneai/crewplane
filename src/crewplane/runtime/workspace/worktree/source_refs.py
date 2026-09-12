from __future__ import annotations

from pathlib import Path

from crewplane.architecture.ports import ArtifactStorePort
from crewplane.core.preflight.models import (
    PreflightExecutionNode,
    PreflightExecutionPlan,
    WorkspaceSelectionRecord,
    WorkspaceSourceSnapshot,
)
from crewplane.core.workflow.keywords import ProviderRole

from ..plan_nodes import workspace_plan_node
from ..state_selection import (
    required_lineage_state_path,
    same_node_executor_state_path,
)
from .descriptors import load_source_ref_from_state
from .types import WorktreeSourceRef, candidate_source_ref


def invocation_source_ref(
    output: ArtifactStorePort,
    plan: PreflightExecutionPlan,
    node: PreflightExecutionNode,
    policy: WorkspaceSelectionRecord,
    source: WorkspaceSourceSnapshot,
    role_label: ProviderRole,
    round_num: int,
    audit_round_num: int | None,
) -> WorktreeSourceRef:
    if role_label == ProviderRole.REVIEWER:
        state_path = same_node_executor_state_path(
            output, node, round_num, audit_round_num
        )
        if state_path is not None:
            return _candidate_ref_from_state(state_path)
    if role_label == ProviderRole.EXECUTOR and round_num > 1:
        state_path = same_node_executor_state_path(
            output,
            node,
            round_num - 1,
            audit_round_num,
            allow_prior_fallback=True,
        )
        if state_path is not None:
            return _candidate_ref_from_state(state_path)
    if policy.source_kind == "node" and policy.source_node_id is not None:
        return load_source_ref_from_state(
            required_lineage_state_path(
                output,
                workspace_plan_node(plan, policy.source_node_id),
            )
        )
    return WorktreeSourceRef(
        source_kind="project",
        source_node_id=None,
        source_commit=source.run_base_commit,
        source_tree=source.source_tree,
        candidate_sequence=None,
    )


def _candidate_ref_from_state(state_path: Path) -> WorktreeSourceRef:
    return candidate_source_ref(load_source_ref_from_state(state_path))
