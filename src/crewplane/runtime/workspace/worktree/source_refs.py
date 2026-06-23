from __future__ import annotations

from pathlib import Path

from crewplane.architecture.ports import ArtifactStorePort
from crewplane.core.preflight.models import (
    PreflightExecutionNode,
    WorkspaceSelectionRecord,
    WorkspaceSourceSnapshot,
)
from crewplane.core.workflow.keywords import ProviderRole

from ..state_selection import (
    required_lineage_state_path,
    same_node_executor_state_path,
)
from .descriptors import load_source_ref_from_state
from .types import WorktreeSourceRef


def invocation_source_ref(
    output: ArtifactStorePort,
    node: PreflightExecutionNode,
    policy: WorkspaceSelectionRecord,
    source: WorkspaceSourceSnapshot,
    role_label: ProviderRole,
    round_num: int,
    audit_round_num: int | None,
) -> WorktreeSourceRef:
    if role_label == ProviderRole.REVIEWER:
        state_path = same_node_executor_state(output, node, round_num, audit_round_num)
        if state_path is not None:
            return _candidate_ref_from_state(state_path)
    if role_label == ProviderRole.EXECUTOR and round_num > 1:
        state_path = same_node_executor_state(
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
            required_lineage_state(output, policy.source_node_id)
        )
    return WorktreeSourceRef(
        source_kind="project",
        source_node_id=None,
        source_commit=source.run_base_commit,
        source_tree=source.source_tree,
        candidate_sequence=None,
    )


def required_lineage_state(output: ArtifactStorePort, node_id: str) -> Path:
    return required_lineage_state_path(output, node_id)


def same_node_executor_state(
    output: ArtifactStorePort,
    node: PreflightExecutionNode,
    round_num: int,
    audit_round_num: int | None,
    allow_prior_fallback: bool = False,
) -> Path | None:
    return same_node_executor_state_path(
        output,
        node,
        round_num,
        audit_round_num,
        allow_prior_fallback,
    )


def _candidate_ref_from_state(state_path: Path) -> WorktreeSourceRef:
    ref = load_source_ref_from_state(state_path)
    return WorktreeSourceRef(
        source_kind="candidate",
        source_node_id=ref.source_node_id,
        source_commit=ref.source_commit,
        source_tree=ref.source_tree,
        candidate_sequence=ref.candidate_sequence,
        bundle_path=ref.bundle_path,
        bundle_sha256=ref.bundle_sha256,
        bundle_size_bytes=ref.bundle_size_bytes,
        bundle_ref=ref.bundle_ref,
        upstream_sources=ref.upstream_sources,
    )
