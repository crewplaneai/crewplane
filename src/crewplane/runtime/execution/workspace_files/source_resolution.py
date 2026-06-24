from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from crewplane.architecture.ports import ArtifactStorePort
from crewplane.core.preflight.models import (
    PreflightExecutionNode,
    PreflightExecutionPlan,
    WorkspaceFileLocator,
    WorkspaceFileSourceClass,
    WorkspaceFileTarget,
)
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.runtime.workspace.state_selection import (
    required_lineage_state_path,
    same_node_executor_state_path,
)
from crewplane.runtime.workspace.worktree import WorktreeSourceRef
from crewplane.runtime.workspace.worktree.descriptors import load_source_ref_from_state

WorkspaceResolutionPhase = Literal["candidate_review", "initial_pre_review"]


@dataclass(frozen=True)
class WorkspaceCandidateSourceContext:
    role_label: ProviderRole
    round_num: int
    audit_round_num: int | None
    phase: WorkspaceResolutionPhase = "candidate_review"


def contextual_candidate_source_state_path(
    output: ArtifactStorePort,
    node: PreflightExecutionNode,
    context: WorkspaceCandidateSourceContext | None,
) -> Path | None:
    if context is None:
        return None
    if context.role_label == ProviderRole.REVIEWER:
        return same_node_executor_state_path(
            output,
            node,
            context.round_num,
            context.audit_round_num,
            allow_prior_fallback=reviewer_allows_prior_fallback(context),
        )
    if context.role_label == ProviderRole.EXECUTOR and context.round_num > 1:
        return same_node_executor_state_path(
            output,
            node,
            context.round_num - 1,
            context.audit_round_num,
            allow_prior_fallback=True,
        )
    return None


def reviewer_allows_prior_fallback(context: WorkspaceCandidateSourceContext) -> bool:
    return context.audit_round_num is not None and context.audit_round_num > 1


def uses_candidate_source(
    locator: WorkspaceFileLocator,
    workspace_candidate_source: bool,
    context: WorkspaceCandidateSourceContext | None = None,
) -> bool:
    if locator.target == WorkspaceFileTarget.REVIEWER_PROMPT:
        return (
            locator.source_class == WorkspaceFileSourceClass.RUNTIME_DYNAMIC
            and not is_initial_pre_review_context(context)
        )
    return workspace_candidate_source and locator.runtime_dynamic_after_candidate


def initial_pre_review_source(
    plan: PreflightExecutionPlan,
    output: ArtifactStorePort,
    locator: WorkspaceFileLocator,
    node: PreflightExecutionNode,
    context: WorkspaceCandidateSourceContext | None,
) -> WorktreeSourceRef | None:
    if not is_initial_pre_review_context(context):
        return None
    if locator.target != WorkspaceFileTarget.REVIEWER_PROMPT:
        return None

    candidate_state_path = same_node_executor_state_path(
        output,
        node,
        max(context.round_num, 1),
        context.audit_round_num,
        allow_prior_fallback=reviewer_allows_prior_fallback(context),
    )
    if candidate_state_path is not None:
        return candidate_source_ref_from_state(candidate_state_path)

    if (
        node.workspace_policy is not None
        and node.workspace_policy.source_kind == "node"
        and node.workspace_policy.source_node_id is not None
    ):
        return load_source_ref_from_state(
            required_lineage_state_path(
                output,
                node.workspace_policy.source_node_id,
            )
        )

    source_ref = project_source_ref(plan)
    if source_ref is None:
        raise RuntimeError(
            "Initial reviewer workspace file locator has no project source: "
            f"{locator.locator_id}."
        )
    return source_ref


def is_initial_pre_review_context(
    context: WorkspaceCandidateSourceContext | None,
) -> bool:
    return (
        context is not None
        and context.role_label == ProviderRole.REVIEWER
        and context.phase == "initial_pre_review"
    )


def project_source_ref(plan: PreflightExecutionPlan) -> WorktreeSourceRef | None:
    if plan.workspace_source is None:
        return None
    return WorktreeSourceRef(
        source_kind="project",
        source_node_id=None,
        source_commit=plan.workspace_source.run_base_commit,
        source_tree=plan.workspace_source.source_tree,
        candidate_sequence=None,
    )


def candidate_source_ref_from_state(path: Path) -> WorktreeSourceRef:
    source_ref = load_source_ref_from_state(path)
    return WorktreeSourceRef(
        source_kind="candidate",
        source_node_id=source_ref.source_node_id,
        source_commit=source_ref.source_commit,
        source_tree=source_ref.source_tree,
        candidate_sequence=source_ref.candidate_sequence,
        bundle_path=source_ref.bundle_path,
        bundle_sha256=source_ref.bundle_sha256,
        bundle_size_bytes=source_ref.bundle_size_bytes,
        bundle_ref=source_ref.bundle_ref,
        upstream_sources=source_ref.upstream_sources,
    )
