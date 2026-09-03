from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from crewplane.architecture.contracts import JsonObject
from crewplane.core.preflight.models import (
    PreflightExecutionPlan,
    WorkspaceSelectionRecord,
)
from crewplane.runtime.workspace.branch_export.fulfillment import (
    BranchExportCheckpoint,
)
from crewplane.version import SCHEMA_VERSION

BranchExportOrigin = Literal["current_run", "verified_history"]
BranchExportRecoveryMode = Literal["initial", "prepared_record"]


@dataclass(frozen=True)
class BranchExportReconciliation:
    recovery_mode: BranchExportRecoveryMode
    operation_origin: BranchExportOrigin | None = None
    idempotent_payload: JsonObject | None = None


class ConflictingPreparedBranchExport(RuntimeError):
    pass


class UnsafeBranchExportRecord(RuntimeError):
    pass


def classify_branch_export_record(
    record_path: Path,
    plan: PreflightExecutionPlan,
    run_id: str,
    run_key_name: str,
    repository_id: str,
    logical_worktree_name: str,
    branch_name: str,
    branch_ref: str,
    checkpoint: BranchExportCheckpoint,
    policy: WorkspaceSelectionRecord,
    operation_origin: BranchExportOrigin,
) -> BranchExportReconciliation:
    del operation_origin
    if not record_path.exists(follow_symlinks=False):
        return BranchExportReconciliation("initial")
    payload = _load_record(record_path)
    status = payload.get("status")
    if status == "prepared":
        if not _record_matches(
            payload,
            plan,
            run_id,
            run_key_name,
            repository_id,
            logical_worktree_name,
            branch_name,
            branch_ref,
            checkpoint,
            policy,
        ):
            raise ConflictingPreparedBranchExport(
                "Workspace branch export has unresolved conflicting prepared evidence."
            )
        origin_value = payload.get("operation_origin")
        if origin_value == "current_run":
            origin: BranchExportOrigin = "current_run"
        elif origin_value == "verified_history":
            origin = "verified_history"
        else:
            raise ConflictingPreparedBranchExport(
                "Workspace branch export prepared evidence has an invalid origin."
            )
        return BranchExportReconciliation("prepared_record", origin)
    if status not in {"fulfilled", "skipped", "failed_verification"}:
        raise RuntimeError(
            "Workspace branch export record is malformed; rerun or regenerate "
            "the workspace artifacts."
        )
    _validate_terminal_identity(
        payload,
        plan,
        run_id,
        run_key_name,
        repository_id,
        logical_worktree_name,
        checkpoint,
        policy,
    )
    if (
        status == "fulfilled"
        and payload.get("branch_ref") == branch_ref
        and payload.get("result_commit") == checkpoint.result_commit
        and payload.get("result_tree") == checkpoint.result_tree
    ):
        return BranchExportReconciliation("initial", None, payload)
    return BranchExportReconciliation("initial")


def _load_record(record_path: Path) -> JsonObject:
    if not record_path.is_file() or record_path.is_symlink():
        raise UnsafeBranchExportRecord(
            "Workspace branch export record is missing or unsafe."
        )
    try:
        payload = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RuntimeError("Workspace branch export record is unreadable.") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Workspace branch export record is invalid.")
    return payload


def _record_matches(
    payload: JsonObject,
    plan: PreflightExecutionPlan,
    run_id: str,
    run_key_name: str,
    repository_id: str,
    logical_worktree_name: str,
    branch_name: str,
    branch_ref: str,
    checkpoint: BranchExportCheckpoint,
    policy: WorkspaceSelectionRecord,
) -> bool:
    return (
        payload.get("version") == SCHEMA_VERSION
        and payload.get("run_id") == run_id
        and payload.get("run_key_name") == run_key_name
        and payload.get("workflow_name") == plan.workflow_name
        and payload.get("workflow_signature") == plan.workflow_signature
        and payload.get("repository_id") == repository_id
        and payload.get("logical_worktree_name") == logical_worktree_name
        and payload.get("branch_name") == branch_name
        and payload.get("branch_ref") == branch_ref
        and payload.get("operation") == "prepared"
        and payload.get("recovery_mode") == "initial"
        and payload.get("expected_old_oid") is None
        and payload.get("target_oid") == checkpoint.result_commit
        and payload.get("result_commit") == checkpoint.result_commit
        and payload.get("result_tree") == checkpoint.result_tree
        and payload.get("workspace_state_artifact") == checkpoint.state_relative_path
        and payload.get("result_ref") == checkpoint.result_ref
        and payload.get("node_id") == checkpoint.node_id
        and payload.get("task_id") == checkpoint.task_id
        and payload.get("bundle")
        == {
            "path": checkpoint.bundle_relative_path,
            "sha256": checkpoint.bundle_sha256,
            "size_bytes": checkpoint.bundle_size_bytes,
        }
        and payload.get("worktree_contract")
        == policy.worktree_contract.model_dump(mode="json")
        and payload.get("dry_run") is False
    )


def _validate_terminal_identity(
    payload: JsonObject,
    plan: PreflightExecutionPlan,
    run_id: str,
    run_key_name: str,
    repository_id: str,
    logical_worktree_name: str,
    checkpoint: BranchExportCheckpoint,
    policy: WorkspaceSelectionRecord,
) -> None:
    identity_matches = (
        payload.get("version") == SCHEMA_VERSION
        and payload.get("run_id") == run_id
        and payload.get("run_key_name") == run_key_name
        and payload.get("workflow_name") == plan.workflow_name
        and payload.get("workflow_signature") == plan.workflow_signature
        and payload.get("logical_worktree_name") == logical_worktree_name
        and payload.get("repository_id") == repository_id
        and payload.get("operation_origin") in {"current_run", "verified_history"}
        and payload.get("node_id") == checkpoint.node_id
        and payload.get("dry_run") is False
        and payload.get("recovery_mode") in {"initial", "prepared_record"}
    )
    status = payload.get("status")
    if status == "skipped":
        if not (
            identity_matches
            and payload.get("operation") == "skipped"
            and payload.get("skip_reason") == "create_branch_false"
            and payload.get("branch_name") is None
            and payload.get("branch_ref") is None
            and payload.get("branch_exists_before") is None
            and payload.get("branch_exists_after") is None
            and payload.get("recovery_mode") == "initial"
        ):
            raise RuntimeError(
                "Workspace branch export terminal history contradicts the current run."
            )
        return
    if status == "failed_verification":
        checkpoint_matches = _terminal_checkpoint_matches(payload, checkpoint)
        if not (
            identity_matches
            and payload.get("operation") == "failed_verification"
            and isinstance(payload.get("failure_message"), str)
            and payload.get("branch_exists_after")
            == payload.get("branch_exists_before")
            and checkpoint_matches
            and (
                payload.get("recovery_mode") != "prepared_record"
                or _terminal_checkpoint_is_present(payload)
            )
        ):
            raise RuntimeError(
                "Workspace branch export terminal history contradicts the current run."
            )
        return
    if not (
        identity_matches
        and _terminal_branch_identity_is_valid(payload)
        and payload.get("task_id") == checkpoint.task_id
        and payload.get("operation") in {"created", "verified_existing"}
        and (
            payload.get("operation") == "created"
            and payload.get("branch_exists_before") is False
            or payload.get("operation") == "verified_existing"
            and payload.get("branch_exists_before") is True
        )
        and isinstance(payload.get("branch_exists_before"), bool)
        and payload.get("branch_exists_after") is True
        and _terminal_operation_tuple_is_valid(payload)
        and payload.get("workspace_state_artifact") == checkpoint.state_relative_path
        and payload.get("result_commit") == checkpoint.result_commit
        and payload.get("result_tree") == checkpoint.result_tree
        and payload.get("result_ref") == checkpoint.result_ref
        and payload.get("bundle")
        == {
            "path": checkpoint.bundle_relative_path,
            "sha256": checkpoint.bundle_sha256,
            "size_bytes": checkpoint.bundle_size_bytes,
        }
        and payload.get("worktree_contract")
        == policy.worktree_contract.model_dump(mode="json")
    ):
        raise RuntimeError(
            "Workspace branch export terminal history contradicts the current run."
        )


def _terminal_branch_identity_is_valid(payload: JsonObject) -> bool:
    branch_name = payload.get("branch_name")
    branch_ref = payload.get("branch_ref")
    return (
        isinstance(branch_name, str)
        and bool(branch_name)
        and branch_ref == f"refs/heads/{branch_name}"
    )


def _terminal_operation_tuple_is_valid(payload: JsonObject) -> bool:
    operation = payload.get("operation")
    origin = payload.get("operation_origin")
    recovery_mode = payload.get("recovery_mode")
    if recovery_mode == "initial" and operation == "verified_existing":
        return origin == "verified_history"
    return recovery_mode in {"initial", "prepared_record"}


def _terminal_checkpoint_matches(
    payload: JsonObject,
    checkpoint: BranchExportCheckpoint,
) -> bool:
    checkpoint_fields = {
        "workspace_state_artifact",
        "task_id",
        "result_commit",
        "result_tree",
        "result_ref",
        "bundle",
    }
    if not checkpoint_fields.intersection(payload):
        return True
    return (
        payload.get("workspace_state_artifact") == checkpoint.state_relative_path
        and payload.get("task_id") == checkpoint.task_id
        and payload.get("result_commit") == checkpoint.result_commit
        and payload.get("result_tree") == checkpoint.result_tree
        and payload.get("result_ref") == checkpoint.result_ref
        and payload.get("bundle")
        == {
            "path": checkpoint.bundle_relative_path,
            "sha256": checkpoint.bundle_sha256,
            "size_bytes": checkpoint.bundle_size_bytes,
        }
    )


def _terminal_checkpoint_is_present(payload: JsonObject) -> bool:
    return all(
        field in payload
        for field in (
            "workspace_state_artifact",
            "task_id",
            "result_commit",
            "result_tree",
            "result_ref",
            "bundle",
        )
    )
