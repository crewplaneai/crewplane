from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from crewplane.core.preflight.models import WorkspaceSourceSnapshot
from crewplane.runtime.workspace.cleanup_notes import note_cleanup_failure
from crewplane.runtime.workspace.setup import WorkspaceSetupCancelled
from crewplane.runtime.workspace.state import (
    WorkspaceStateRetention,
    WorkspaceStateUpdateRequest,
    read_workspace_state,
    update_workspace_retention,
    update_workspace_state,
)
from crewplane.runtime.workspace.terminalization import workspace_mutators_are_drained
from crewplane.runtime.workspace.worktree import remove_worktree_workspace
from crewplane.runtime.workspace.worktree.cache import WorktreeReuseCache

from .common import (
    record_failed_preparation_state,
    unmaterialized_workspace_retention,
    worktree_preparation_failure_state,
)
from .types import WorkspaceInvocationRequest, WorktreePreparationPlan


def record_failed_unmaterialized_worktree_preparation(
    plan: WorktreePreparationPlan,
    failure: Exception,
) -> None:
    record_failed_preparation_state(
        plan.state_path,
        failure,
        workspace_retention=unmaterialized_workspace_retention(
            plan.planned_workspace_path,
        ),
    )


def record_cancelled_unmaterialized_worktree_preparation(
    plan: WorktreePreparationPlan,
    failure: Exception,
) -> None:
    retention = unmaterialized_workspace_retention(plan.planned_workspace_path)
    try:
        update_workspace_state(
            plan.state_path,
            WorkspaceStateUpdateRequest(
                status="cancelled",
                diagnostics=[{"level": "warning", "message": str(failure)}],
                retention=WorkspaceStateRetention(
                    retention,
                    None if retention == "deleted" else "cancelled",
                ),
            ),
        )
    except Exception as exc:
        note_cleanup_failure(
            failure,
            "Workspace cancelled-state recording after preparation cancellation",
            exc,
        )


def record_worktree_materialization_failure(
    request: WorkspaceInvocationRequest,
    plan: WorktreePreparationPlan,
    fresh_claim_recorded: bool,
    failure: Exception,
) -> None:
    cancellation = request.setup_cancellation
    if cancellation is not None and cancellation.is_cancelled():
        _record_cancelled_materialization(
            request,
            plan,
            fresh_claim_recorded,
            failure,
        )
        return
    if fresh_claim_recorded:
        record_failed_worktree_preparation(
            plan.source,
            plan.planned_workspace_path,
            plan.state_path,
            failure,
            request.worktree_reuse_cache,
        )
        return
    record_failed_unmaterialized_worktree_preparation(plan, failure)


def _record_cancelled_materialization(
    request: WorkspaceInvocationRequest,
    plan: WorktreePreparationPlan,
    fresh_claim_recorded: bool,
    failure: Exception,
) -> None:
    if read_workspace_state(plan.state_path).get("status") == "cancelled":
        return
    if fresh_claim_recorded:
        record_cancelled_worktree_preparation(
            plan.source,
            plan.planned_workspace_path,
            plan.state_path,
            failure,
            request.worktree_reuse_cache,
        )
        return
    record_cancelled_unmaterialized_worktree_preparation(plan, failure)


def record_failed_worktree_preparation(
    source: WorkspaceSourceSnapshot,
    workspace_path: Path,
    state_path: Path | None,
    failure: Exception,
    reuse_cache: WorktreeReuseCache | None = None,
) -> None:
    diagnostics, retained_reason, setup = worktree_preparation_failure_state(failure)
    _finish_worktree_preparation(
        source,
        workspace_path,
        state_path,
        failure,
        reuse_cache,
        WorkspaceStateUpdateRequest(
            status="failed",
            diagnostics=diagnostics,
            retention=WorkspaceStateRetention("pending_cleanup", retained_reason),
            setup=setup,
        ),
    )


def record_cancelled_worktree_preparation(
    source: WorkspaceSourceSnapshot,
    workspace_path: Path,
    state_path: Path | None,
    failure: Exception,
    reuse_cache: WorktreeReuseCache | None = None,
) -> None:
    diagnostics = [{"level": "warning", "message": str(failure)}]
    setup = failure.summary if isinstance(failure, WorkspaceSetupCancelled) else None
    _finish_worktree_preparation(
        source,
        workspace_path,
        state_path,
        failure,
        reuse_cache,
        WorkspaceStateUpdateRequest(
            status="cancelled",
            diagnostics=diagnostics,
            retention=WorkspaceStateRetention("pending_cleanup", "cancelled"),
            setup=setup,
        ),
    )


def _finish_worktree_preparation(
    source: WorkspaceSourceSnapshot,
    workspace_path: Path,
    state_path: Path | None,
    failure: Exception,
    reuse_cache: WorktreeReuseCache | None,
    update: WorkspaceStateUpdateRequest,
) -> None:
    if state_path is None or not state_path.exists():
        return
    drained = workspace_mutators_are_drained(state_path)
    retained_reason = update.retention.retained_reason
    if not drained:
        update = replace(
            update,
            retention=WorkspaceStateRetention("retained", "process_drain_unresolved"),
        )
    preparation_outcome = "failure" if update.status == "failed" else "cancellation"
    state_label = "failure-state" if update.status == "failed" else "cancelled-state"
    try:
        update_workspace_state(state_path, update)
    except Exception as exc:
        note_cleanup_failure(
            failure,
            f"Workspace {state_label} recording after preparation {preparation_outcome}",
            exc,
        )
        return
    if not drained:
        return
    cleanup_operation = f"Workspace cleanup after preparation {preparation_outcome}"
    try:
        _cleanup_worktree_preparation_path(
            source,
            workspace_path,
            state_path,
            reuse_cache,
        )
    except Exception as exc:
        note_cleanup_failure(failure, cleanup_operation, exc)
        update_workspace_retention(
            state_path,
            WorkspaceStateRetention("retained", f"{retained_reason}_cleanup_failed"),
            {"level": "warning", "message": f"{cleanup_operation} failed: {exc}"},
        )
        return
    update_workspace_retention(state_path, WorkspaceStateRetention("deleted"))


def _cleanup_worktree_preparation_path(
    source: WorkspaceSourceSnapshot,
    workspace_path: Path,
    state_path: Path,
    reuse_cache: WorktreeReuseCache | None,
) -> None:
    if reuse_cache is not None and reuse_cache.owns(workspace_path):
        reuse_cache.cleanup_workspace(workspace_path, state_path=state_path)
        return
    remove_worktree_workspace(
        source,
        workspace_path,
        _persisted_worktree_git_dir(state_path),
    )


def _persisted_worktree_git_dir(state_path: Path) -> Path:
    execution = read_workspace_state(state_path).get("execution")
    value = execution.get("worktree_git_dir") if isinstance(execution, dict) else None
    if not isinstance(value, str) or not value:
        raise RuntimeError("Workspace state lacks exact worktree Git directory.")
    return Path(value)
