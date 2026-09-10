from __future__ import annotations

from pathlib import Path

from crewplane.runtime.workspace.cleanup_notes import note_cleanup_failure
from crewplane.runtime.workspace.state import (
    WorkspaceStateRetention,
    update_workspace_retention,
)

from .common import (
    record_failed_preparation_state,
    record_failed_unmaterialized_preparation,
    remove_workspace_after_failure,
    trusted_workspace_state_payload,
)
from .types import SnapshotPreparationPlan


def record_failed_materialized_snapshot_preparation(
    plan: SnapshotPreparationPlan,
    workspace_path: Path,
    failure: Exception,
) -> None:
    state_published = record_failed_preparation_state(
        plan.state_path,
        failure,
        workspace_retention="pending_cleanup",
    )
    if not state_published:
        return
    removed = remove_workspace_after_failure(workspace_path, failure)
    update_workspace_retention(
        plan.state_path,
        WorkspaceStateRetention(
            "deleted" if removed else "retained",
            None if removed else "preparation_failed_cleanup_failed",
        ),
    )


def terminalize_unhandled_snapshot_materialization_failure(
    plan: SnapshotPreparationPlan,
    failure: Exception,
) -> None:
    try:
        payload = trusted_workspace_state_payload(plan.state_path)
    except Exception as state_error:
        note_cleanup_failure(
            failure,
            "Workspace state inspection after snapshot materialization failure",
            state_error,
        )
        return
    if payload.get("status") != "running":
        return
    record_failed_unmaterialized_preparation(
        plan.state_path, plan.planned_workspace_path, failure
    )
