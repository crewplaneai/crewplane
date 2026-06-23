from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from crewplane.core.preflight.models import (
    PreflightExecutionPlan,
    WorkspaceSourceSnapshot,
)

from . import create_worktree_workspace
from .cache import WorktreeReuseCache
from .reuse import reuse_worktree_workspace
from .types import WorktreeSourceRef, WorktreeWorkspace


@dataclass(frozen=True)
class WorktreeMaterialization:
    worktree: WorktreeWorkspace
    reuse: Mapping[str, object]


def materialize_worktree_workspace(
    plan: PreflightExecutionPlan,
    slug: str,
    source: WorkspaceSourceSnapshot,
    source_ref: WorktreeSourceRef,
    protected_ref_scopes: tuple[str, ...],
    parent_slug: str | None,
    logical_worktree_name: str | None,
    lineage_producer: bool,
    reuse_cache: WorktreeReuseCache | None,
) -> WorktreeMaterialization:
    if reuse_cache is None or logical_worktree_name is None or not lineage_producer:
        return _fresh_worktree(
            plan,
            slug,
            source,
            source_ref,
            protected_ref_scopes,
            parent_slug,
            lineage_producer,
        )
    reusable = reuse_cache.take(logical_worktree_name, source_ref)
    if reusable is None:
        return _fresh_worktree(
            plan,
            slug,
            source,
            source_ref,
            protected_ref_scopes,
            parent_slug,
            lineage_producer,
        )
    try:
        worktree = reuse_worktree_workspace(
            reusable.workspace_path,
            source,
            source_ref,
            reusable.git_dir,
            protected_ref_scopes,
        )
    except Exception as exc:
        reuse_cache.cleanup_entry_best_effort(reusable)
        materialization = _fresh_worktree(
            plan,
            slug,
            source,
            source_ref,
            protected_ref_scopes,
            parent_slug,
            lineage_producer,
        )
        return WorktreeMaterialization(
            worktree=materialization.worktree,
            reuse=_reuse_metadata(
                strategy="fresh_checkout",
                reused=False,
                fallback=True,
                fallback_reason=str(exc),
            ),
        )
    return WorktreeMaterialization(
        worktree=worktree,
        reuse=_reuse_metadata(
            strategy="incremental_reset",
            reused=True,
            fallback=False,
            previous_workspace_state=reusable.state_path.name,
        ),
    )


def _fresh_worktree(
    plan: PreflightExecutionPlan,
    slug: str,
    source: WorkspaceSourceSnapshot,
    source_ref: WorktreeSourceRef,
    protected_ref_scopes: tuple[str, ...],
    parent_slug: str | None,
    lineage_producer: bool,
) -> WorktreeMaterialization:
    worktree = create_worktree_workspace(
        plan,
        slug,
        source,
        source_ref,
        protected_ref_scopes,
        workspace_family="workspaces" if lineage_producer else "review-workspaces",
        parent_slug=parent_slug,
    )
    return WorktreeMaterialization(
        worktree=worktree,
        reuse=_reuse_metadata(
            strategy="fresh_checkout",
            reused=False,
            fallback=False,
        ),
    )


def _reuse_metadata(
    strategy: str,
    reused: bool,
    fallback: bool,
    fallback_reason: str | None = None,
    previous_workspace_state: str | None = None,
) -> Mapping[str, object]:
    metadata: dict[str, object] = {
        "strategy": strategy,
        "reused": reused,
        "fallback": fallback,
    }
    if fallback_reason is not None:
        metadata["fallback_reason"] = fallback_reason
    if previous_workspace_state is not None:
        metadata["previous_workspace_state"] = previous_workspace_state
    return MappingProxyType(metadata)
