from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType

from crewplane.artifacts.atomic import atomic_write_json
from crewplane.artifacts.workspace.state.paths import workspace_reuse_claim_filename
from crewplane.core.preflight.models import (
    PreflightExecutionPlan,
    WorkspaceSourceSnapshot,
)
from crewplane.runtime.workspace.materialization import (
    MaterializationCapacityRequest,
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

type ReuseClaimRecorder = Callable[[ReusableWorktreeCheckout, int], None]
type FreshClaimRecorder = Callable[[WorktreeProvisioningClaim, int], None]


@dataclass(frozen=True, slots=True)
class WorktreeClaimCallbacks:
    record_reuse: ReuseClaimRecorder | None = None
    record_fresh: FreshClaimRecorder | None = None


@dataclass(frozen=True, slots=True)
class WorktreeMaterializationRequest:
    plan: PreflightExecutionPlan
    slug: str
    source: WorkspaceSourceSnapshot
    source_ref: WorktreeSourceRef
    protected_ref_scopes: tuple[str, ...]
    parent_slug: str | None
    logical_worktree_name: str | None
    lineage_producer: bool
    reuse_cache: WorktreeReuseCache | None
    materialization_limiter: MaterializationLimiter | None = None
    planned_workspace_path: Path | None = None
    state_path: Path | None = None
    cancel_requested: Callable[[], bool] | None = None
    claims: WorktreeClaimCallbacks = field(default_factory=WorktreeClaimCallbacks)


@dataclass(frozen=True)
class WorktreeMaterialization:
    worktree: WorktreeWorkspace
    reuse: Mapping[str, object]
    reuse_generation: int


def materialize_worktree_workspace(
    request: WorktreeMaterializationRequest,
) -> WorktreeMaterialization:
    reuse = _take_reusable_checkout(request)
    if reuse is None:
        return _fresh_worktree(request, request.claims.record_fresh)
    return _materialize_reused_worktree(request, reuse)


@dataclass(frozen=True, slots=True)
class _ReusableCheckoutLease:
    cache: WorktreeReuseCache
    checkout: ReusableWorktreeCheckout


@dataclass(slots=True)
class _FreshClaimTracker:
    recorder: FreshClaimRecorder | None
    recorded: bool = False

    @property
    def callback(self) -> FreshClaimRecorder | None:
        return self.record if self.recorder is not None else None

    def record(self, claim: WorktreeProvisioningClaim, generation: int) -> None:
        if self.recorder is None:
            return
        self.recorder(claim, generation)
        self.recorded = True


def _take_reusable_checkout(
    request: WorktreeMaterializationRequest,
) -> _ReusableCheckoutLease | None:
    cache = request.reuse_cache
    logical_name = request.logical_worktree_name
    if cache is None or logical_name is None or not request.lineage_producer:
        return None
    checkout = cache.take(
        logical_name,
        request.source_ref,
        request.source.repository_id,
        request.plan.run_key_name,
        request.cancel_requested,
    )
    if checkout is None:
        return None
    return _ReusableCheckoutLease(cache, checkout)


def _materialize_reused_worktree(
    request: WorktreeMaterializationRequest,
    reuse: _ReusableCheckoutLease,
) -> WorktreeMaterialization:
    reusable = reuse.checkout
    reuse_generation = reusable.reuse_generation + 1
    if request.claims.record_reuse is not None:
        request.claims.record_reuse(reusable, reuse_generation)
    try:
        worktree = _reuse_worktree(request, reusable)
    except Exception as exc:
        if _reuse_was_cancelled(request):
            _resolve_cancelled_reuse(request, reuse, exc)
            raise
        return _recover_failed_reuse(request, reuse, reuse_generation, exc)
    return _reused_materialization(worktree, reusable, reuse_generation)


def _reuse_worktree(
    request: WorktreeMaterializationRequest,
    reusable: ReusableWorktreeCheckout,
) -> WorktreeWorkspace:
    if request.cancel_requested is None:
        return reuse_worktree_workspace(
            reusable.workspace_path,
            request.source,
            request.source_ref,
            reusable.git_dir,
            request.protected_ref_scopes,
            request.state_path,
        )
    return reuse_worktree_workspace(
        reusable.workspace_path,
        request.source,
        request.source_ref,
        reusable.git_dir,
        request.protected_ref_scopes,
        request.state_path,
        request.cancel_requested,
    )


def _reuse_was_cancelled(request: WorktreeMaterializationRequest) -> bool:
    return request.cancel_requested is not None and request.cancel_requested()


def _resolve_cancelled_reuse(
    request: WorktreeMaterializationRequest,
    reuse: _ReusableCheckoutLease,
    failure: BaseException,
) -> None:
    state_path = request.state_path
    if state_path is None:
        raise RuntimeError(
            "Cancelled workspace reuse lacks durable state evidence."
        ) from failure
    _resolve_cancelled_reuse_claim(
        reuse.cache,
        reuse.checkout,
        state_path,
        failure,
    )


def _recover_failed_reuse(
    request: WorktreeMaterializationRequest,
    reuse: _ReusableCheckoutLease,
    reuse_generation: int,
    failure: BaseException,
) -> WorktreeMaterialization:
    state_path = _require_fallback_state_path(request, failure)
    abandoned_claim_path = _archive_failed_reuse_claim(
        state_path,
        reuse_generation,
        failure,
    )
    _cleanup_abandoned_reuse(
        request,
        reuse,
        reuse_generation,
        abandoned_claim_path,
    )
    return _materialize_fresh_fallback(
        request,
        state_path,
        abandoned_claim_path,
        failure,
    )


def _require_fallback_state_path(
    request: WorktreeMaterializationRequest,
    failure: BaseException,
) -> Path:
    if request.state_path is None:
        raise RuntimeError(
            "Unsafe workspace reuse cannot fall back without durable state evidence."
        ) from failure
    return request.state_path


def _cleanup_abandoned_reuse(
    request: WorktreeMaterializationRequest,
    reuse: _ReusableCheckoutLease,
    reuse_generation: int,
    abandoned_claim_path: Path,
) -> None:
    failed_entry = replace(
        reuse.checkout,
        state_path=abandoned_claim_path,
        reuse_generation=reuse_generation,
    )
    cleanup_error = reuse.cache.cleanup_entry_best_effort(
        failed_entry,
        request.cancel_requested,
    )
    publish_workspace_cleanup_result(
        abandoned_claim_path,
        deleted=cleanup_error is None,
        retained_reason=(
            None if cleanup_error is None else "unsafe_reuse_cleanup_failed"
        ),
    )


def _materialize_fresh_fallback(
    request: WorktreeMaterializationRequest,
    state_path: Path,
    abandoned_claim_path: Path,
    failure: BaseException,
) -> WorktreeMaterialization:
    claim_tracker = _FreshClaimTracker(request.claims.record_fresh)
    try:
        materialization = _fresh_worktree(request, claim_tracker.callback)
    except Exception:
        _record_failed_fallback(
            request,
            state_path,
            abandoned_claim_path,
            claim_tracker.recorded,
        )
        raise
    return _fallback_materialization(materialization, abandoned_claim_path, failure)


def _fallback_materialization(
    materialization: WorktreeMaterialization,
    abandoned_claim_path: Path,
    failure: BaseException,
) -> WorktreeMaterialization:
    return WorktreeMaterialization(
        worktree=materialization.worktree,
        reuse=_reuse_metadata(
            strategy="fresh_checkout",
            reused=False,
            fallback=True,
            fallback_reason=str(failure),
            abandoned_claim_artifact=abandoned_claim_path.name,
        ),
        reuse_generation=materialization.reuse_generation,
    )


def _record_failed_fallback(
    request: WorktreeMaterializationRequest,
    state_path: Path,
    abandoned_claim_path: Path,
    fresh_claim_recorded: bool,
) -> None:
    if fresh_claim_recorded:
        _record_failed_fallback_metadata(state_path, abandoned_claim_path)
        return
    _restore_failed_fallback_state(
        state_path,
        abandoned_claim_path,
        request.planned_workspace_path,
    )


def _reused_materialization(
    worktree: WorktreeWorkspace,
    reusable: ReusableWorktreeCheckout,
    reuse_generation: int,
) -> WorktreeMaterialization:
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
    request: WorktreeMaterializationRequest,
    record_fresh_claim: FreshClaimRecorder | None,
) -> WorktreeMaterialization:
    worktree = _create_fresh_worktree(request, record_fresh_claim)
    return WorktreeMaterialization(
        worktree=worktree,
        reuse=_reuse_metadata(
            strategy="fresh_checkout",
            reused=False,
            fallback=False,
        ),
        reuse_generation=1,
    )


def _create_fresh_worktree(
    request: WorktreeMaterializationRequest,
    record_fresh_claim: FreshClaimRecorder | None,
) -> WorktreeWorkspace:
    owner = (
        TemporaryRefOwner(request.state_path)
        if request.state_path is not None
        else None
    )
    with (
        ensure_source_commit_available(
            request.source,
            request.source_ref,
            owner,
            request.cancel_requested,
        ),
        workspace_materialization_slot(
            request.plan,
            request.materialization_limiter,
            _fresh_capacity_request(request),
        ),
    ):
        return _add_fresh_worktree(request, record_fresh_claim)


def _add_fresh_worktree(
    request: WorktreeMaterializationRequest,
    record_fresh_claim: FreshClaimRecorder | None,
) -> WorktreeWorkspace:
    return create_worktree_workspace(
        request.plan,
        request.slug,
        request.source,
        request.source_ref,
        request.protected_ref_scopes,
        workspace_family=(
            "workspaces" if request.lineage_producer else "review-workspaces"
        ),
        parent_slug=request.parent_slug,
        state_path=request.state_path,
        cancel_requested=request.cancel_requested,
        source_chain_verified=True,
        record_fresh_claim=record_fresh_claim,
    )


def _fresh_capacity_request(
    request: WorktreeMaterializationRequest,
) -> MaterializationCapacityRequest | None:
    if request.planned_workspace_path is None:
        return None
    return MaterializationCapacityRequest(
        request.planned_workspace_path,
        request.source,
        estimate_full_repository=True,
        source_tree=request.source_ref.source_tree,
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
        workspace_reuse_claim_filename(state_path.stem, generation)
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
