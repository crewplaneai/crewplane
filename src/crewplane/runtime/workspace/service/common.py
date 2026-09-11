from __future__ import annotations

import json
from pathlib import Path

from crewplane.architecture.contracts import JsonObject
from crewplane.core.preflight.models import (
    PreflightExecutionPlan,
    WorkspaceSourceSnapshot,
)
from crewplane.core.preflight.runtime_config.workspace import (
    invoker_workspace_descriptor,
)
from crewplane.core.workspace.naming import safe_file_component
from crewplane.runtime.workspace.cleanup_notes import note_cleanup_failure
from crewplane.runtime.workspace.setup import WorkspaceSetupError
from crewplane.runtime.workspace.snapshot import (
    remove_workspace_path,
    workspace_run_hierarchy,
)
from crewplane.runtime.workspace.state import (
    WorkspaceStateRetention,
    WorkspaceStateUpdateRequest,
    WorkspaceStateWriteRequest,
    update_workspace_state,
)

from .types import WorkspaceInvocationRequest


def workspace_state_request(
    request: WorkspaceInvocationRequest,
) -> WorkspaceStateWriteRequest:
    invoker = invoker_workspace_descriptor(request.plan.runtime_config_snapshot)
    if invoker is None:
        raise RuntimeError(
            "Workspace invocation requires selected invoker workspace capability "
            "metadata."
        )
    return WorkspaceStateWriteRequest(
        run_id=request.plan.run_id,
        run_key_name=request.plan.run_key_name,
        workflow_name=request.plan.workflow_name,
        workflow_signature=request.plan.workflow_signature,
        task_id=request.task_id,
        provider=request.provider,
        role_label=request.role_label,
        round_num=request.round_num,
        audit_round_num=request.audit_round_num,
        invoker=invoker,
        rendered_workspace_files=request.rendered_workspace_files,
    )


def project_root_cwd(plan: PreflightExecutionPlan) -> Path:
    return Path(plan.project_root).resolve(strict=False)


def remove_workspace_after_failure(
    workspace_path: Path,
    failure: BaseException,
) -> bool:
    try:
        remove_workspace_path(workspace_path)
        return True
    except Exception as cleanup_error:
        note_cleanup_failure(
            failure,
            "Workspace cleanup after preparation failure",
            cleanup_error,
        )
        return False


def unmaterialized_workspace_retention(planned_workspace_path: Path) -> str:
    if planned_workspace_path.exists() or planned_workspace_path.is_symlink():
        return "retained"
    return "deleted"


def record_failed_unmaterialized_preparation(
    state_path: Path,
    planned_workspace_path: Path,
    failure: Exception,
) -> None:
    record_failed_preparation_state(
        state_path,
        failure,
        workspace_retention=unmaterialized_workspace_retention(planned_workspace_path),
    )


def record_failed_preparation_state(
    state_path: Path,
    failure: Exception,
    workspace_retention: str = "retained",
    retained_reason: str | None = None,
) -> bool:
    diagnostics, default_retained_reason, setup = worktree_preparation_failure_state(
        failure
    )
    terminal_retained_reason = retained_reason or default_retained_reason
    try:
        update_workspace_state(
            state_path,
            WorkspaceStateUpdateRequest(
                status="failed",
                diagnostics=diagnostics,
                retention=WorkspaceStateRetention(
                    retention=workspace_retention,
                    retained_reason=None
                    if workspace_retention == "deleted"
                    else terminal_retained_reason,
                ),
                setup=setup,
            ),
        )
        return True
    except Exception as exc:
        note_cleanup_failure(
            failure,
            "Workspace failure-state recording after preparation failure",
            exc,
        )
        return False


def worktree_preparation_failure_state(
    failure: Exception,
) -> tuple[list[dict[str, str]], str, JsonObject | None]:
    if isinstance(failure, WorkspaceSetupError):
        return (
            [{"level": "error", "message": str(failure)}],
            "setup_failed",
            failure.summary,
        )
    return (
        [
            {
                "level": "error",
                "message": "Workspace preparation failed before invocation.",
            }
        ],
        "preparation_failed",
        None,
    )


def trusted_workspace_state_payload(state_path: Path) -> dict[str, object]:
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"Invalid workspace state: {state_path.as_posix()}")
    return payload


def refresh_trusted_workspace_state_payload(
    payload: dict[str, object],
    state_path: Path,
) -> None:
    payload.clear()
    payload.update(trusted_workspace_state_payload(state_path))


def planned_workspace_path(
    plan: PreflightExecutionPlan,
    source: WorkspaceSourceSnapshot,
    family: str,
    slug: str,
    parent_slug: str | None = None,
) -> Path:
    run_root = workspace_run_hierarchy(plan, source, family)[-1]
    if parent_slug is not None:
        run_root = run_root / safe_file_component(parent_slug)
    return run_root / slug


def workspace_cwd(checkout_root: Path, source: WorkspaceSourceSnapshot) -> Path:
    if source.project_root_relative_path == ".":
        return checkout_root
    return checkout_root / source.project_root_relative_path
