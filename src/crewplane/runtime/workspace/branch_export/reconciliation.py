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
_CHECKPOINT_FIELDS = frozenset(
    {
        "workspace_state_artifact",
        "task_id",
        "result_commit",
        "result_tree",
        "result_ref",
        "bundle",
    }
)


@dataclass(frozen=True)
class BranchExportReconciliation:
    """Describe how a persisted branch-export record should be reconciled."""

    recovery_mode: BranchExportRecoveryMode
    operation_origin: BranchExportOrigin | None = None
    idempotent_payload: JsonObject | None = None


@dataclass(frozen=True, slots=True)
class BranchExportRecordExpectation:
    """Collect the evidence expected from a branch-export record."""

    plan: PreflightExecutionPlan
    run_id: str
    run_key_name: str
    repository_id: str
    logical_worktree_name: str
    branch_name: str
    branch_ref: str
    checkpoint: BranchExportCheckpoint
    policy: WorkspaceSelectionRecord


class ConflictingPreparedBranchExport(RuntimeError):
    """Raised when prepared branch-export evidence conflicts with the request."""


class UnsafeBranchExportRecord(RuntimeError):
    """Raised when a branch-export record path is missing or unsafe."""


def classify_branch_export_record(
    record_path: Path,
    expectation: BranchExportRecordExpectation,
) -> BranchExportReconciliation:
    """Classify persisted branch-export evidence for safe reconciliation."""

    if not record_path.exists(follow_symlinks=False):
        return BranchExportReconciliation(recovery_mode="initial")
    payload = _load_record(record_path)
    match payload.get("status"):
        case "prepared":
            return _reconcile_prepared_record(payload, expectation)
        case "fulfilled" | "skipped" | "failed_verification":
            return _reconcile_terminal_record(payload, expectation)
        case _:
            raise RuntimeError(
                "Workspace branch export record is malformed; rerun or regenerate "
                "the workspace artifacts."
            )


def _reconcile_prepared_record(
    payload: JsonObject,
    expectation: BranchExportRecordExpectation,
) -> BranchExportReconciliation:
    if not _prepared_record_matches(payload, expectation):
        raise ConflictingPreparedBranchExport(
            "Workspace branch export has unresolved conflicting prepared evidence."
        )
    return BranchExportReconciliation(
        recovery_mode="prepared_record",
        operation_origin=_prepared_operation_origin(payload),
    )


def _prepared_operation_origin(payload: JsonObject) -> BranchExportOrigin:
    origin = payload.get("operation_origin")
    if origin == "current_run":
        return "current_run"
    if origin == "verified_history":
        return "verified_history"
    raise ConflictingPreparedBranchExport(
        "Workspace branch export prepared evidence has an invalid origin."
    )


def _reconcile_terminal_record(
    payload: JsonObject,
    expectation: BranchExportRecordExpectation,
) -> BranchExportReconciliation:
    _validate_terminal_record(payload, expectation)
    if (
        payload.get("status") == "fulfilled"
        and payload.get("branch_ref") == expectation.branch_ref
        and payload.get("result_commit") == expectation.checkpoint.result_commit
        and payload.get("result_tree") == expectation.checkpoint.result_tree
    ):
        return BranchExportReconciliation(
            recovery_mode="initial",
            idempotent_payload=payload,
        )
    return BranchExportReconciliation(recovery_mode="initial")


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


def _prepared_record_matches(
    payload: JsonObject,
    expectation: BranchExportRecordExpectation,
) -> bool:
    return (
        _record_identity_matches(payload, expectation)
        and payload.get("branch_name") == expectation.branch_name
        and payload.get("branch_ref") == expectation.branch_ref
        and payload.get("operation") == "prepared"
        and payload.get("recovery_mode") == "initial"
        and payload.get("expected_old_oid") is None
        and payload.get("target_oid") == expectation.checkpoint.result_commit
        and _checkpoint_matches(payload, expectation.checkpoint)
        and _worktree_contract_matches(payload, expectation.policy)
    )


def _record_identity_matches(
    payload: JsonObject,
    expectation: BranchExportRecordExpectation,
) -> bool:
    return (
        payload.get("version") == SCHEMA_VERSION
        and payload.get("run_id") == expectation.run_id
        and payload.get("run_key_name") == expectation.run_key_name
        and payload.get("workflow_name") == expectation.plan.workflow_name
        and payload.get("workflow_signature") == expectation.plan.workflow_signature
        and payload.get("logical_worktree_name") == expectation.logical_worktree_name
        and payload.get("repository_id") == expectation.repository_id
        and payload.get("node_id") == expectation.checkpoint.node_id
        and payload.get("dry_run") is False
    )


def _validate_terminal_record(
    payload: JsonObject,
    expectation: BranchExportRecordExpectation,
) -> None:
    if _terminal_record_matches(payload, expectation):
        return
    raise RuntimeError(
        "Workspace branch export terminal history contradicts the current run."
    )


def _terminal_record_matches(
    payload: JsonObject,
    expectation: BranchExportRecordExpectation,
) -> bool:
    match payload.get("status"):
        case "skipped":
            return _skipped_terminal_record_matches(payload, expectation)
        case "failed_verification":
            return _failed_terminal_record_matches(payload, expectation)
        case "fulfilled":
            return _fulfilled_terminal_record_matches(payload, expectation)
        case _:
            return False


def _terminal_identity_matches(
    payload: JsonObject,
    expectation: BranchExportRecordExpectation,
) -> bool:
    return (
        _record_identity_matches(payload, expectation)
        and payload.get("operation_origin") in {"current_run", "verified_history"}
        and payload.get("recovery_mode") in {"initial", "prepared_record"}
    )


def _skipped_terminal_record_matches(
    payload: JsonObject,
    expectation: BranchExportRecordExpectation,
) -> bool:
    return (
        _terminal_identity_matches(payload, expectation)
        and payload.get("operation") == "skipped"
        and payload.get("skip_reason") == "create_branch_false"
        and payload.get("branch_name") is None
        and payload.get("branch_ref") is None
        and payload.get("branch_exists_before") is None
        and payload.get("branch_exists_after") is None
        and payload.get("recovery_mode") == "initial"
    )


def _failed_terminal_record_matches(
    payload: JsonObject,
    expectation: BranchExportRecordExpectation,
) -> bool:
    checkpoint = expectation.checkpoint
    return (
        _terminal_identity_matches(payload, expectation)
        and payload.get("operation") == "failed_verification"
        and isinstance(payload.get("failure_message"), str)
        and payload.get("branch_exists_after") == payload.get("branch_exists_before")
        and _terminal_checkpoint_matches(payload, checkpoint)
        and (
            payload.get("recovery_mode") != "prepared_record"
            or _terminal_checkpoint_is_present(payload)
        )
    )


def _fulfilled_terminal_record_matches(
    payload: JsonObject,
    expectation: BranchExportRecordExpectation,
) -> bool:
    return (
        _terminal_identity_matches(payload, expectation)
        and _terminal_branch_identity_is_valid(payload)
        and _fulfilled_operation_matches(payload)
        and payload.get("branch_exists_after") is True
        and _terminal_operation_tuple_is_valid(payload)
        and _checkpoint_matches(payload, expectation.checkpoint)
        and _worktree_contract_matches(payload, expectation.policy)
    )


def _fulfilled_operation_matches(payload: JsonObject) -> bool:
    operation = payload.get("operation")
    branch_exists_before = payload.get("branch_exists_before")
    return (
        operation == "created"
        and branch_exists_before is False
        or operation == "verified_existing"
        and branch_exists_before is True
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


def _checkpoint_matches(
    payload: JsonObject,
    checkpoint: BranchExportCheckpoint,
) -> bool:
    return (
        payload.get("workspace_state_artifact") == checkpoint.state_relative_path
        and payload.get("task_id") == checkpoint.task_id
        and payload.get("result_commit") == checkpoint.result_commit
        and payload.get("result_tree") == checkpoint.result_tree
        and payload.get("result_ref") == checkpoint.result_ref
        and payload.get("bundle") == _expected_bundle(checkpoint)
    )


def _expected_bundle(checkpoint: BranchExportCheckpoint) -> JsonObject:
    return {
        "path": checkpoint.bundle_relative_path,
        "sha256": checkpoint.bundle_sha256,
        "size_bytes": checkpoint.bundle_size_bytes,
    }


def _worktree_contract_matches(
    payload: JsonObject,
    policy: WorkspaceSelectionRecord,
) -> bool:
    return payload.get("worktree_contract") == policy.worktree_contract.model_dump(
        mode="json"
    )


def _terminal_checkpoint_matches(
    payload: JsonObject,
    checkpoint: BranchExportCheckpoint,
) -> bool:
    if not _CHECKPOINT_FIELDS.intersection(payload):
        return True
    return _checkpoint_matches(payload, checkpoint)


def _terminal_checkpoint_is_present(payload: JsonObject) -> bool:
    return _CHECKPOINT_FIELDS.issubset(payload)
