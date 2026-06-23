from __future__ import annotations

from pathlib import Path

from crewplane.core.preflight.models import WorkspaceSourceSnapshot
from crewplane.runtime.workspace.cleanup_notes import note_cleanup_failure
from crewplane.runtime.workspace.setup import WorkspaceSetupCancelled
from crewplane.runtime.workspace.state import (
    WorkspaceStateRetention,
    WorkspaceStateUpdateRequest,
    update_workspace_state,
)
from crewplane.runtime.workspace.worktree import remove_worktree_workspace

from .common import (
    record_failed_preparation_state,
    worktree_preparation_failure_state,
)
from .types import WorktreePreparationPlan


def record_failed_unmaterialized_worktree_preparation(
    plan: WorktreePreparationPlan,
    failure: Exception,
) -> None:
    record_failed_preparation_state(
        plan.state_path,
        failure,
        workspace_retention=failed_unmaterialized_workspace_retention(
            plan.planned_workspace_path,
        ),
    )


def failed_unmaterialized_workspace_retention(planned_workspace_path: Path) -> str:
    if planned_workspace_path.exists() or planned_workspace_path.is_symlink():
        return "retained"
    return "deleted"


def record_failed_worktree_preparation(
    source: WorkspaceSourceSnapshot,
    workspace_path: Path,
    state_path: Path | None,
    failure: Exception,
) -> None:
    diagnostics, retained_reason, setup = worktree_preparation_failure_state(failure)
    retention = "deleted"
    reason: str | None = None
    try:
        remove_worktree_workspace(source, workspace_path)
    except Exception as exc:
        note_cleanup_failure(
            failure,
            "Workspace cleanup after preparation failure",
            exc,
        )
        retention = "retained"
        reason = f"{retained_reason}_cleanup_failed"
        diagnostics.append(
            {
                "level": "warning",
                "message": (
                    f"Workspace cleanup after preparation failure failed: {exc}"
                ),
            }
        )
    if state_path is not None and state_path.exists():
        try:
            update_workspace_state(
                state_path,
                WorkspaceStateUpdateRequest(
                    status="failed",
                    diagnostics=diagnostics,
                    retention=WorkspaceStateRetention(
                        retention=retention,
                        retained_reason=reason,
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


def record_cancelled_worktree_preparation(
    source: WorkspaceSourceSnapshot,
    workspace_path: Path,
    state_path: Path | None,
    failure: WorkspaceSetupCancelled,
) -> None:
    diagnostics = [{"level": "warning", "message": str(failure)}]
    retention = "deleted"
    reason: str | None = None
    try:
        remove_worktree_workspace(source, workspace_path)
    except Exception as exc:
        note_cleanup_failure(
            failure,
            "Workspace cleanup after preparation cancellation",
            exc,
        )
        retention = "retained"
        reason = "cancelled_cleanup_failed"
        diagnostics.append(
            {
                "level": "warning",
                "message": (
                    f"Workspace cleanup after preparation cancellation failed: {exc}"
                ),
            }
        )
    if state_path is not None and state_path.exists():
        try:
            update_workspace_state(
                state_path,
                WorkspaceStateUpdateRequest(
                    status="cancelled",
                    diagnostics=diagnostics,
                    retention=WorkspaceStateRetention(
                        retention=retention,
                        retained_reason=reason,
                    ),
                    setup=failure.summary,
                ),
            )
        except Exception as exc:
            note_cleanup_failure(
                failure,
                "Workspace cancelled-state recording after preparation cancellation",
                exc,
            )
