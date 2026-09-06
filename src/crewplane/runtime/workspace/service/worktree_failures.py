from __future__ import annotations

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
    if state_path is not None and state_path.exists():
        drained = workspace_mutators_are_drained(state_path)
        retention = "pending_cleanup" if drained else "retained"
        terminal_reason = retained_reason if drained else "process_drain_unresolved"
        try:
            update_workspace_state(
                state_path,
                WorkspaceStateUpdateRequest(
                    status="failed",
                    diagnostics=diagnostics,
                    retention=WorkspaceStateRetention(
                        retention=retention,
                        retained_reason=terminal_reason,
                    ),
                    setup=setup,
                ),
            )
        except Exception as exc:
            note_cleanup_failure(
                failure,
                "Workspace failure-state recording after preparation failure",
                exc,
            )
            return
    else:
        return
    if not drained:
        return
    try:
        _cleanup_worktree_preparation_path(
            source,
            workspace_path,
            state_path,
            reuse_cache,
        )
    except Exception as exc:
        note_cleanup_failure(
            failure,
            "Workspace cleanup after preparation failure",
            exc,
        )
        update_workspace_retention(
            state_path,
            WorkspaceStateRetention(
                "retained",
                f"{retained_reason}_cleanup_failed",
            ),
            {
                "level": "warning",
                "message": (
                    f"Workspace cleanup after preparation failure failed: {exc}"
                ),
            },
        )
        return
    update_workspace_retention(state_path, WorkspaceStateRetention("deleted"))


def record_cancelled_worktree_preparation(
    source: WorkspaceSourceSnapshot,
    workspace_path: Path,
    state_path: Path | None,
    failure: Exception,
    reuse_cache: WorktreeReuseCache | None = None,
) -> None:
    diagnostics = [{"level": "warning", "message": str(failure)}]
    setup = failure.summary if isinstance(failure, WorkspaceSetupCancelled) else None
    if state_path is not None and state_path.exists():
        drained = workspace_mutators_are_drained(state_path)
        retention = "pending_cleanup" if drained else "retained"
        retained_reason = "cancelled" if drained else "process_drain_unresolved"
        try:
            update_workspace_state(
                state_path,
                WorkspaceStateUpdateRequest(
                    status="cancelled",
                    diagnostics=diagnostics,
                    retention=WorkspaceStateRetention(
                        retention=retention,
                        retained_reason=retained_reason,
                    ),
                    setup=setup,
                ),
            )
        except Exception as exc:
            note_cleanup_failure(
                failure,
                "Workspace cancelled-state recording after preparation cancellation",
                exc,
            )
            return
    else:
        return
    if not drained:
        return
    try:
        _cleanup_worktree_preparation_path(
            source,
            workspace_path,
            state_path,
            reuse_cache,
        )
    except Exception as exc:
        note_cleanup_failure(
            failure,
            "Workspace cleanup after preparation cancellation",
            exc,
        )
        update_workspace_retention(
            state_path,
            WorkspaceStateRetention("retained", "cancelled_cleanup_failed"),
            {
                "level": "warning",
                "message": (
                    f"Workspace cleanup after preparation cancellation failed: {exc}"
                ),
            },
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
