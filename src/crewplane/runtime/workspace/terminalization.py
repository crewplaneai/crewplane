from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from .mutator_fence import workspace_mutator_is_fenced
from .state import (
    WorkspaceStateRetention,
    WorkspaceStateUpdateRequest,
    read_workspace_state,
    update_workspace_retention,
    update_workspace_state,
)

TerminalWorkspaceStatus = Literal["succeeded", "failed", "cancelled"]


def workspace_mutators_are_drained(state_path: Path) -> bool:
    if workspace_mutator_is_fenced(state_path):
        return False
    payload = read_workspace_state(state_path)
    process_drain = payload.get("process_drain")
    workspace_mutator = payload.get("workspace_mutator")
    return not (
        isinstance(process_drain, dict) and process_drain.get("status") == "unresolved"
    ) and not (
        isinstance(workspace_mutator, dict)
        and workspace_mutator.get("status") == "unresolved"
    )


def publish_terminal_workspace_state(
    state_path: Path,
    status: TerminalWorkspaceStatus,
    cleanup_intended: bool,
    diagnostics: list[dict[str, str]] | None = None,
    result: Mapping[str, object] | None = None,
    refs: Mapping[str, object] | None = None,
    bundle: Mapping[str, object] | None = None,
    child_environment_applied: bool | None = None,
    retained_reason: str | None = None,
) -> None:
    drained = workspace_mutators_are_drained(state_path)
    retention = _terminal_retention(cleanup_intended, drained, retained_reason)
    update_workspace_state(
        state_path,
        WorkspaceStateUpdateRequest(
            status=status,
            diagnostics=diagnostics,
            retention=retention,
            child_environment_applied=child_environment_applied,
            result=result,
            refs=refs,
            bundle=bundle,
        ),
    )


def _terminal_retention(
    cleanup_intended: bool,
    drained: bool,
    retained_reason: str | None,
) -> WorkspaceStateRetention:
    if cleanup_intended and drained:
        return WorkspaceStateRetention("pending_cleanup", retained_reason)
    reason = retained_reason
    if reason is None:
        reason = "process_drain_unresolved" if not drained else "cleanup_not_requested"
    return WorkspaceStateRetention("retained", reason)


def publish_workspace_cleanup_result(
    state_path: Path,
    deleted: bool,
    retained_reason: str | None = None,
    diagnostic: Mapping[str, str] | None = None,
) -> None:
    update_workspace_retention(
        state_path,
        WorkspaceStateRetention(
            retention="deleted" if deleted else "retained",
            retained_reason=None if deleted else retained_reason,
        ),
        diagnostic,
    )
