from __future__ import annotations

import json
from pathlib import Path

from orchestrator_cli.core.preflight.models import (
    PreflightExecutionPlan,
    WorkspaceSourceSnapshot,
)
from orchestrator_cli.core.preflight.workspace_observability import (
    invoker_workspace_descriptor,
)
from orchestrator_cli.core.workspace_cache import workspace_cache_root
from orchestrator_cli.runtime.workspace.cleanup_notes import note_cleanup_failure
from orchestrator_cli.runtime.workspace.setup import WorkspaceSetupError
from orchestrator_cli.runtime.workspace.snapshot import (
    remove_workspace_path,
    runtime_workspace_cache_root,
)
from orchestrator_cli.runtime.workspace.state import (
    WorkspaceStateRetention,
    WorkspaceStateUpdateRequest,
    WorkspaceStateWriteRequest,
    update_workspace_state,
)
from orchestrator_cli.runtime.workspace.worktree_refs import safe_file_component

from .service_types import WorkspaceInvocationRequest


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


def record_failed_preparation_state(
    state_path: Path,
    failure: Exception,
    workspace_retention: str = "retained",
    retained_reason: str | None = None,
) -> None:
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
    except Exception as exc:
        note_cleanup_failure(
            failure,
            "Workspace failure-state recording after preparation failure",
            exc,
        )


def worktree_preparation_failure_state(
    failure: Exception,
) -> tuple[list[dict[str, str]], str, dict[str, object] | None]:
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
    run_root = (
        workspace_cache_root(runtime_workspace_cache_root(plan))
        / family
        / source.repository_id
        / plan.run_key_name
    )
    if parent_slug is not None:
        run_root = run_root / safe_file_component(parent_slug)
    return run_root / slug


def workspace_cwd(checkout_root: Path, source: WorkspaceSourceSnapshot) -> Path:
    if source.project_root_relative_path == ".":
        return checkout_root
    return checkout_root / source.project_root_relative_path
