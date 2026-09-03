from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType

from crewplane.artifacts.atomic import atomic_write_json
from crewplane.core.preflight.models import (
    PreflightExecutionPlan,
    WorkspaceSourceSnapshot,
)
from crewplane.runtime.workspace.materialization import (
    MaterializationLimiter,
    workspace_materialization_slot,
)
from crewplane.runtime.workspace.state import (
    mutate_workspace_state,
    read_workspace_state,
)
from crewplane.runtime.workspace.terminalization import (
    publish_terminal_workspace_state,
    publish_workspace_cleanup_result,
)

from . import create_worktree_workspace
from .cache import ReusableWorktreeCheckout, WorktreeReuseCache
from .lineage import ensure_source_commit_available
from .reuse import reuse_worktree_workspace
from .temporary_refs import TemporaryRefOwner
from .types import (
    WorktreeProvisioningClaim,
    WorktreeSourceRef,
    WorktreeWorkspace,
)


@dataclass(frozen=True)
class WorktreeMaterialization:
    worktree: WorktreeWorkspace
    reuse: Mapping[str, object]
    reuse_generation: int


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
    record_reuse_claim: Callable[[ReusableWorktreeCheckout, int], None] | None = None,
    record_fresh_claim: Callable[[WorktreeProvisioningClaim, int], None] | None = None,
    materialization_limiter: MaterializationLimiter | None = None,
    planned_workspace_path: Path | None = None,
    state_path: Path | None = None,
    cancel_requested: Callable[[], bool] | None = None,
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
            materialization_limiter,
            planned_workspace_path,
            state_path,
            record_fresh_claim,
            cancel_requested,
        )
    reusable = reuse_cache.take(
        logical_worktree_name,
        source_ref,
        source.repository_id,
        plan.run_key_name,
        cancel_requested,
    )
    if reusable is None:
        return _fresh_worktree(
            plan,
            slug,
            source,
            source_ref,
            protected_ref_scopes,
            parent_slug,
            lineage_producer,
            materialization_limiter,
            planned_workspace_path,
            state_path,
            record_fresh_claim,
            cancel_requested,
        )
    reuse_generation = reusable.reuse_generation + 1
    if record_reuse_claim is not None:
        record_reuse_claim(reusable, reuse_generation)
    try:
        if cancel_requested is None:
            worktree = reuse_worktree_workspace(
                reusable.workspace_path,
                source,
                source_ref,
                reusable.git_dir,
                protected_ref_scopes,
                state_path,
            )
        else:
            worktree = reuse_worktree_workspace(
                reusable.workspace_path,
                source,
                source_ref,
                reusable.git_dir,
                protected_ref_scopes,
                state_path,
                cancel_requested,
            )
    except Exception as exc:
        if cancel_requested is not None and cancel_requested():
            if state_path is None:
                raise RuntimeError(
                    "Cancelled workspace reuse lacks durable state evidence."
                ) from exc
            _resolve_cancelled_reuse_claim(
                reuse_cache,
                reusable,
                state_path,
                exc,
            )
            raise
        if state_path is None:
            raise RuntimeError(
                "Unsafe workspace reuse cannot fall back without durable state evidence."
            ) from exc
        abandoned_claim_path = _archive_failed_reuse_claim(
            state_path,
            reuse_generation,
            exc,
        )
        failed_entry = replace(
            reusable,
            state_path=abandoned_claim_path,
            reuse_generation=reuse_generation,
        )
        cleanup_error = reuse_cache.cleanup_entry_best_effort(
            failed_entry,
            cancel_requested,
        )
        publish_workspace_cleanup_result(
            abandoned_claim_path,
            deleted=cleanup_error is None,
            retained_reason=(
                None if cleanup_error is None else "unsafe_reuse_cleanup_failed"
            ),
        )
        fresh_fallback_claim_recorded = False

        def record_fallback_fresh_claim(
            worktree: WorktreeProvisioningClaim,
            generation: int,
        ) -> None:
            nonlocal fresh_fallback_claim_recorded
            if record_fresh_claim is None:
                return
            record_fresh_claim(worktree, generation)
            fresh_fallback_claim_recorded = True

        try:
            materialization = _fresh_worktree(
                plan,
                slug,
                source,
                source_ref,
                protected_ref_scopes,
                parent_slug,
                lineage_producer,
                materialization_limiter,
                planned_workspace_path,
                state_path,
                (
                    record_fallback_fresh_claim
                    if record_fresh_claim is not None
                    else None
                ),
                cancel_requested,
            )
        except Exception:
            if fresh_fallback_claim_recorded:
                _record_failed_fallback_metadata(state_path, abandoned_claim_path)
            else:
                _restore_failed_fallback_state(
                    state_path,
                    abandoned_claim_path,
                    planned_workspace_path,
                )
            raise
        return WorktreeMaterialization(
            worktree=materialization.worktree,
            reuse=_reuse_metadata(
                strategy="fresh_checkout",
                reused=False,
                fallback=True,
                fallback_reason=str(exc),
                abandoned_claim_artifact=abandoned_claim_path.name,
            ),
            reuse_generation=materialization.reuse_generation,
        )
    return WorktreeMaterialization(
        worktree=worktree,
        reuse=_reuse_metadata(
            strategy="incremental_reset",
            reused=True,
            fallback=False,
            previous_workspace_state=reusable.state_path.name,
        ),
        reuse_generation=reuse_generation,
    )


def _resolve_cancelled_reuse_claim(
    reuse_cache: WorktreeReuseCache,
    reusable: ReusableWorktreeCheckout,
    state_path: Path,
    failure: BaseException,
) -> None:
    try:
        publish_terminal_workspace_state(
            state_path,
            "cancelled",
            cleanup_intended=True,
            diagnostics=[
                {
                    "level": "warning",
                    "message": f"Workspace reuse was cancelled safely: {failure}",
                }
            ],
            retained_reason="cancelled",
        )
    finally:
        reuse_cache.discard_workspace(reusable.workspace_path)
    publish_workspace_cleanup_result(
        state_path,
        deleted=False,
        retained_reason="cancelled_reuse_cleanup_deferred",
    )


def _fresh_worktree(
    plan: PreflightExecutionPlan,
    slug: str,
    source: WorkspaceSourceSnapshot,
    source_ref: WorktreeSourceRef,
    protected_ref_scopes: tuple[str, ...],
    parent_slug: str | None,
    lineage_producer: bool,
    materialization_limiter: MaterializationLimiter | None,
    planned_workspace_path: Path | None,
    state_path: Path | None,
    record_fresh_claim: Callable[[WorktreeProvisioningClaim, int], None] | None,
    cancel_requested: Callable[[], bool] | None,
) -> WorktreeMaterialization:
    owner = TemporaryRefOwner(state_path) if state_path is not None else None
    with (
        ensure_source_commit_available(
            source,
            source_ref,
            owner,
            cancel_requested,
        ),
        workspace_materialization_slot(
            plan,
            materialization_limiter,
            planned_workspace_path,
            source,
            True,
            source_ref.source_tree,
        ),
    ):
        worktree = create_worktree_workspace(
            plan,
            slug,
            source,
            source_ref,
            protected_ref_scopes,
            workspace_family="workspaces" if lineage_producer else "review-workspaces",
            parent_slug=parent_slug,
            state_path=state_path,
            cancel_requested=cancel_requested,
            source_chain_verified=True,
            record_fresh_claim=record_fresh_claim,
        )
    return WorktreeMaterialization(
        worktree=worktree,
        reuse=_reuse_metadata(
            strategy="fresh_checkout",
            reused=False,
            fallback=False,
        ),
        reuse_generation=1,
    )


def _reuse_metadata(
    strategy: str,
    reused: bool,
    fallback: bool,
    fallback_reason: str | None = None,
    previous_workspace_state: str | None = None,
    abandoned_claim_artifact: str | None = None,
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
    if abandoned_claim_artifact is not None:
        metadata["abandoned_claim_artifact"] = abandoned_claim_artifact
    return MappingProxyType(metadata)


def _archive_failed_reuse_claim(
    state_path: Path,
    generation: int,
    failure: BaseException,
) -> Path:
    archive_path = state_path.with_name(
        f"workspace-reuse-claim-{state_path.stem}-generation-{generation}.json"
    )
    if archive_path.exists() or archive_path.is_symlink():
        raise RuntimeError(
            f"Workspace reuse claim archive already exists: {archive_path}."
        )
    atomic_write_json(archive_path, read_workspace_state(state_path))
    publish_terminal_workspace_state(
        archive_path,
        "failed",
        cleanup_intended=True,
        diagnostics=[
            {
                "level": "error",
                "message": f"Workspace reuse was abandoned safely: {failure}",
            }
        ],
        retained_reason="unsafe_reuse",
    )
    return archive_path


def _restore_failed_fallback_state(
    state_path: Path,
    abandoned_claim_path: Path,
    planned_workspace_path: Path | None,
) -> None:
    current_payload = read_workspace_state(state_path)
    payload = read_workspace_state(abandoned_claim_path)
    workspace = payload.get("workspace")
    execution = payload.get("execution")
    if not isinstance(workspace, dict) or not isinstance(execution, dict):
        raise RuntimeError("Workspace fallback state evidence is invalid.")
    workspace.pop("reuse_generation", None)
    if planned_workspace_path is not None:
        workspace["cache_key"] = planned_workspace_path.name
        execution["workspace_path"] = planned_workspace_path.as_posix()
        execution["checkout_root"] = (planned_workspace_path / "checkout").as_posix()
    execution["effective_cwd"] = None
    execution["checkout_size_bytes"] = None
    execution["provisioning_duration_seconds"] = None
    workspace["retention"] = "retained"
    workspace["retained_reason"] = "fresh_fallback_failed"
    payload["reuse"] = _failed_fallback_metadata(abandoned_claim_path)
    if "temporary_refs" in current_payload:
        payload["temporary_refs"] = current_payload["temporary_refs"]

    atomic_write_json(state_path, payload)


def _record_failed_fallback_metadata(
    state_path: Path,
    abandoned_claim_path: Path,
) -> None:
    def apply_metadata(payload: dict[str, object]) -> None:
        payload["reuse"] = _failed_fallback_metadata(abandoned_claim_path)

    mutate_workspace_state(state_path, apply_metadata)


def _failed_fallback_metadata(abandoned_claim_path: Path) -> dict[str, object]:
    return {
        "strategy": "fresh_checkout",
        "reused": False,
        "fallback": True,
        "fallback_reason": "fresh fallback materialization failed",
        "abandoned_claim_artifact": abandoned_claim_path.name,
    }
