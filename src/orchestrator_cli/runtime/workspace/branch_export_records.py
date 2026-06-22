from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from orchestrator_cli.architecture.contracts import JsonObject
from orchestrator_cli.core.preflight.models import (
    PreflightExecutionPlan,
    WorkspaceSelectionRecord,
)
from orchestrator_cli.core.workspace_policy import generated_branch_name
from orchestrator_cli.runtime.workspace.branch_export_fulfillment import (
    BranchExportCheckpoint,
)
from orchestrator_cli.runtime.workspace.branch_export_git import BranchExportOperation
from orchestrator_cli.version import SCHEMA_VERSION


def branch_export_record(
    plan: PreflightExecutionPlan,
    run_id: str,
    run_key_name: str,
    logical_worktree_name: str,
    branch_name: str,
    branch_ref: str,
    checkpoint: BranchExportCheckpoint,
    policy: WorkspaceSelectionRecord,
    operation: BranchExportOperation,
    branch_exists_before: bool,
    branch_exists_after: bool,
    dry_run: bool = False,
) -> JsonObject:
    payload: JsonObject = {
        "version": SCHEMA_VERSION,
        "run_id": run_id,
        "run_key_name": run_key_name,
        "workflow_name": plan.workflow_name,
        "workflow_signature": plan.workflow_signature,
        "logical_worktree_name": logical_worktree_name,
        "branch_name": branch_name,
        "branch_ref": branch_ref,
        "status": "fulfilled",
        "operation": operation,
        "branch_exists_before": branch_exists_before,
        "branch_exists_after": branch_exists_after,
        "dry_run": dry_run,
        "created_at": datetime.now(UTC).isoformat(),
        "worktree_contract": policy.worktree_contract.model_dump(mode="json"),
    }
    payload.update(checkpoint_record(checkpoint))
    return payload


def skipped_branch_export_record(
    plan: PreflightExecutionPlan,
    run_id: str,
    run_key_name: str,
    logical_worktree_name: str,
    node_id: str,
    dry_run: bool = False,
) -> JsonObject:
    return {
        "version": SCHEMA_VERSION,
        "run_id": run_id,
        "run_key_name": run_key_name,
        "workflow_name": plan.workflow_name,
        "workflow_signature": plan.workflow_signature,
        "logical_worktree_name": logical_worktree_name,
        "branch_name": None,
        "branch_ref": None,
        "status": "skipped",
        "operation": "skipped",
        "skip_reason": "create_branch_false",
        "branch_exists_before": None,
        "branch_exists_after": None,
        "dry_run": dry_run,
        "created_at": datetime.now(UTC).isoformat(),
        "node_id": node_id,
    }


def failed_branch_export_record(
    plan: PreflightExecutionPlan,
    run_id: str,
    run_key_name: str,
    logical_worktree_name: str,
    node_id: str,
    branch_name: str | None,
    branch_ref: str | None,
    checkpoint: BranchExportCheckpoint | None,
    branch_exists_before: bool | None,
    failure_message: str,
    dry_run: bool = False,
) -> JsonObject:
    payload: JsonObject = {
        "version": SCHEMA_VERSION,
        "run_id": run_id,
        "run_key_name": run_key_name,
        "workflow_name": plan.workflow_name,
        "workflow_signature": plan.workflow_signature,
        "logical_worktree_name": logical_worktree_name,
        "branch_name": branch_name,
        "branch_ref": branch_ref,
        "status": "failed_verification",
        "operation": "failed_verification",
        "failure_message": failure_message,
        "branch_exists_before": branch_exists_before,
        "branch_exists_after": branch_exists_before,
        "dry_run": dry_run,
        "created_at": datetime.now(UTC).isoformat(),
        "node_id": node_id,
    }
    if checkpoint is not None:
        payload.update(checkpoint_record(checkpoint))
    return payload


def checkpoint_record(checkpoint: BranchExportCheckpoint) -> JsonObject:
    return {
        "node_id": checkpoint.node_id,
        "task_id": checkpoint.task_id,
        "workspace_state_artifact": checkpoint.state_relative_path,
        "result_commit": checkpoint.result_commit,
        "result_tree": checkpoint.result_tree,
        "result_ref": checkpoint.result_ref,
        "bundle": {
            "path": checkpoint.bundle_relative_path,
            "sha256": checkpoint.bundle_sha256,
            "size_bytes": checkpoint.bundle_size_bytes,
        },
    }


def branch_name(
    plan: PreflightExecutionPlan,
    policy: WorkspaceSelectionRecord,
    logical_worktree_name: str,
    run_key_name: str,
) -> str:
    return policy.branch_export.branch_name or _generated_branch_name(
        plan.workflow_name,
        logical_worktree_name,
        run_key_name,
    )


def checkpoint_from_record(
    stages_dir: Path,
    payload: JsonObject,
) -> BranchExportCheckpoint | None:
    if not _checkpoint_fields_present(payload):
        return None
    bundle = _mapping(payload.get("bundle"))
    state_path = _required_string(payload.get("workspace_state_artifact"))
    node_id = _required_string(payload.get("node_id"))
    task_id = _required_string(payload.get("task_id"))
    result_commit = _required_string(payload.get("result_commit"))
    result_tree = _required_string(payload.get("result_tree"))
    result_ref = _required_string(payload.get("result_ref"))
    bundle_path = _required_string(bundle.get("path"))
    bundle_sha256 = _required_string(bundle.get("sha256"))
    bundle_size_bytes = bundle.get("size_bytes")
    if (
        state_path is None
        or node_id is None
        or task_id is None
        or result_commit is None
        or result_tree is None
        or result_ref is None
        or bundle_path is None
        or bundle_sha256 is None
        or not _valid_size_bytes(bundle_size_bytes)
    ):
        raise RuntimeError("Invalid branch export checkpoint record.")
    return BranchExportCheckpoint(
        state_path=stages_dir / state_path,
        state_relative_path=state_path,
        node_id=node_id,
        task_id=task_id,
        result_commit=result_commit,
        result_tree=result_tree,
        result_ref=result_ref,
        bundle_path=Path(bundle_path),
        bundle_relative_path=bundle_path,
        bundle_sha256=bundle_sha256,
        bundle_size_bytes=bundle_size_bytes,
    )


def branch_export_operation(payload: JsonObject) -> BranchExportOperation:
    operation = payload.get("operation")
    if operation in {
        "created",
        "verified_existing",
        "skipped",
        "failed_verification",
    }:
        return cast(BranchExportOperation, operation)
    raise RuntimeError(f"Invalid branch export operation: {operation!r}")


def _generated_branch_name(
    workflow_name: str,
    logical_worktree_name: str,
    run_key_name: str,
) -> str:
    return generated_branch_name(workflow_name, logical_worktree_name, run_key_name)


def _mapping(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _checkpoint_fields_present(payload: JsonObject) -> bool:
    return any(
        key in payload
        for key in (
            "workspace_state_artifact",
            "result_commit",
            "result_tree",
            "result_ref",
            "bundle",
        )
    )


def _required_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _valid_size_bytes(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0
