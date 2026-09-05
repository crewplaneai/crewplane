from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from crewplane.architecture.contracts import JsonObject
from crewplane.artifacts.naming import build_workspace_export_filename
from crewplane.core.preflight.models import (
    PreflightExecutionNode,
    PreflightExecutionPlan,
    WorkspaceSelectionRecord,
    WorkspaceSourceSnapshot,
)
from crewplane.runtime.workspace.branch_export.checkpoint import (
    StageLookup,
    validated_checkpoint,
)
from crewplane.runtime.workspace.branch_export.fulfillment import (
    BranchExportCheckpoint,
    create_branch_export_ref,
)
from crewplane.runtime.workspace.branch_export.git import (
    BranchExportOperation,
    BranchExportOperationCompletedError,
    branch_ref_exists,
    planned_branch_operation,
    validated_branch_ref,
)
from crewplane.runtime.workspace.branch_export.reconciliation import (
    BranchExportOrigin,
    BranchExportReconciliation,
    BranchExportRecordExpectation,
    classify_branch_export_record,
)
from crewplane.runtime.workspace.branch_export.records import (
    branch_export_record,
    branch_name,
    failed_branch_export_record,
    prepared_branch_export_record,
    skipped_branch_export_record,
)
from crewplane.runtime.workspace.worktree.descriptors import (
    load_source_ref_from_state,
)
from crewplane.runtime.workspace.worktree.lineage import ensure_source_commit_available
from crewplane.runtime.workspace.worktree.temporary_refs import TemporaryRefOwner

BranchExportRecordWriter = Callable[[str, object], Path]


@dataclass(frozen=True)
class BranchExportRun:
    plan: PreflightExecutionPlan
    run_id: str
    run_key_name: str
    source: WorkspaceSourceSnapshot
    stages_dir: Path
    state_lookup: StageLookup


@dataclass(frozen=True)
class BranchExportAttempt:
    run: BranchExportRun
    logical_worktree_name: str
    node: PreflightExecutionNode
    policy: WorkspaceSelectionRecord
    operation_origin: BranchExportOrigin

    @property
    def record_path(self) -> Path:
        return (
            self.run.stages_dir
            / "workspace-exports"
            / build_workspace_export_filename(self.logical_worktree_name)
        )


@dataclass(frozen=True)
class _ValidatedBranchExportAttempt:
    request: BranchExportAttempt
    branch_name: str
    branch_ref: str
    checkpoint: BranchExportCheckpoint
    reconciliation: BranchExportReconciliation

    @property
    def operation_origin(self) -> BranchExportOrigin:
        return self.reconciliation.operation_origin or self.request.operation_origin


def fulfillment_payload(
    request: BranchExportAttempt,
    write_record: BranchExportRecordWriter,
) -> JsonObject:
    record_exists_at_entry = request.record_path.exists(follow_symlinks=False)
    if not request.policy.branch_export.create_branch and not record_exists_at_entry:
        return _skipped_record(request, request.operation_origin)

    branch_name_value = _branch_name(request)
    checkpoint: BranchExportCheckpoint | None = None
    branch_ref: str | None = None
    try:
        checkpoint = _validated_checkpoint(request)
        branch_ref = validated_branch_ref(request.run.source, branch_name_value)
    except Exception as exc:
        if record_exists_at_entry or request.record_path.exists(follow_symlinks=False):
            raise
        return _failed_record(
            request,
            branch_name_value,
            branch_ref,
            checkpoint,
            None,
            str(exc),
            request.operation_origin,
            "initial",
        )

    validated_attempt = _reconcile_attempt(
        request,
        branch_name_value,
        branch_ref,
        checkpoint,
    )
    return _fulfill_validated_attempt(validated_attempt, write_record)


def preview_payload(request: BranchExportAttempt) -> JsonObject:
    if (
        not request.policy.branch_export.create_branch
        and not request.record_path.exists(follow_symlinks=False)
    ):
        return _skipped_record(request, "verified_history", dry_run=True)

    branch_name_value = _branch_name(request)
    checkpoint: BranchExportCheckpoint | None = None
    branch_ref: str | None = None
    validated_attempt: _ValidatedBranchExportAttempt | None = None
    try:
        checkpoint = _validated_checkpoint(request)
        branch_ref = validated_branch_ref(request.run.source, branch_name_value)
        validated_attempt = _reconcile_attempt(
            request,
            branch_name_value,
            branch_ref,
            checkpoint,
        )
        return _preview_validated_attempt(validated_attempt)
    except Exception as exc:
        operation_origin = (
            validated_attempt.operation_origin
            if validated_attempt is not None
            else request.operation_origin
        )
        recovery_mode = (
            validated_attempt.reconciliation.recovery_mode
            if validated_attempt is not None
            else "initial"
        )
        return _failed_record(
            request,
            branch_name_value,
            branch_ref,
            checkpoint,
            None,
            str(exc),
            operation_origin,
            recovery_mode,
            dry_run=True,
        )


def _branch_name(request: BranchExportAttempt) -> str:
    return branch_name(
        request.run.plan,
        request.policy,
        request.logical_worktree_name,
        request.run.run_key_name,
    )


def _validated_checkpoint(request: BranchExportAttempt) -> BranchExportCheckpoint:
    return validated_checkpoint(
        request.run.plan,
        request.run.source,
        request.node,
        request.policy,
        request.run.stages_dir,
        request.run.state_lookup,
        request.run.run_id,
        request.run.run_key_name,
    )


def _reconcile_attempt(
    request: BranchExportAttempt,
    branch_name_value: str,
    branch_ref: str,
    checkpoint: BranchExportCheckpoint,
) -> _ValidatedBranchExportAttempt:
    run = request.run
    reconciliation = classify_branch_export_record(
        request.record_path,
        BranchExportRecordExpectation(
            plan=run.plan,
            run_id=run.run_id,
            run_key_name=run.run_key_name,
            repository_id=run.source.repository_id,
            logical_worktree_name=request.logical_worktree_name,
            branch_name=branch_name_value,
            branch_ref=branch_ref,
            checkpoint=checkpoint,
            policy=request.policy,
        ),
    )
    return _ValidatedBranchExportAttempt(
        request=request,
        branch_name=branch_name_value,
        branch_ref=branch_ref,
        checkpoint=checkpoint,
        reconciliation=reconciliation,
    )


def _fulfill_validated_attempt(
    attempt: _ValidatedBranchExportAttempt,
    write_record: BranchExportRecordWriter,
) -> JsonObject:
    branch_exists_before: bool | None = None
    try:
        if _should_skip_reconciled_attempt(attempt):
            return _skipped_record(
                attempt.request,
                attempt.request.operation_origin,
            )
        branch_exists_before = branch_ref_exists(
            attempt.request.run.source,
            attempt.branch_ref,
        )
        return _fulfill_after_branch_probe(
            attempt,
            branch_exists_before,
            write_record,
        )
    except BranchExportOperationCompletedError:
        raise
    except Exception as exc:
        failure_message = _reconciled_failure_message(attempt, exc)
        if failure_message is None:
            raise
        return _failed_record(
            attempt.request,
            attempt.branch_name,
            attempt.branch_ref,
            attempt.checkpoint,
            branch_exists_before,
            failure_message,
            attempt.operation_origin,
            attempt.reconciliation.recovery_mode,
        )


def _should_skip_reconciled_attempt(
    attempt: _ValidatedBranchExportAttempt,
) -> bool:
    return (
        not attempt.request.policy.branch_export.create_branch
        and attempt.reconciliation.recovery_mode != "prepared_record"
    )


def _fulfill_after_branch_probe(
    attempt: _ValidatedBranchExportAttempt,
    branch_exists_before: bool,
    write_record: BranchExportRecordWriter,
) -> JsonObject:
    if attempt.reconciliation.idempotent_payload is not None and branch_exists_before:
        _create_branch_export_ref_with_source(
            attempt,
            allow_existing=True,
            allow_create=False,
        )
        return attempt.reconciliation.idempotent_payload

    allow_create = (
        attempt.reconciliation.recovery_mode == "prepared_record"
        or not branch_exists_before
    )
    if allow_create and attempt.reconciliation.recovery_mode == "initial":
        write_record(
            attempt.request.logical_worktree_name,
            _prepared_record(attempt),
        )
    operation = _create_branch_export_ref_with_source(
        attempt,
        allow_existing=(
            attempt.operation_origin == "verified_history"
            or attempt.reconciliation.recovery_mode == "prepared_record"
        ),
        allow_create=allow_create,
    )
    return _fulfilled_record(
        attempt,
        operation,
        branch_exists_before=operation == "verified_existing",
        dry_run=False,
    )


def _preview_validated_attempt(
    attempt: _ValidatedBranchExportAttempt,
) -> JsonObject:
    if _should_skip_reconciled_attempt(attempt):
        return _skipped_record(
            attempt.request,
            attempt.operation_origin,
            dry_run=True,
        )
    operation, failure_message = planned_branch_operation(
        attempt.request.run.source,
        attempt.branch_ref,
        attempt.checkpoint.result_commit,
    )
    if failure_message is not None:
        return _failed_record(
            attempt.request,
            attempt.branch_name,
            attempt.branch_ref,
            attempt.checkpoint,
            True,
            failure_message,
            attempt.operation_origin,
            attempt.reconciliation.recovery_mode,
            dry_run=True,
        )
    branch_exists = operation == "verified_existing"
    return _fulfilled_record(
        attempt,
        operation,
        branch_exists_before=branch_exists,
        dry_run=True,
    )


def _reconciled_failure_message(
    attempt: _ValidatedBranchExportAttempt,
    exc: Exception,
) -> str | None:
    if attempt.reconciliation.idempotent_payload is None:
        return str(exc)
    _, collision_message = planned_branch_operation(
        attempt.request.run.source,
        attempt.branch_ref,
        attempt.checkpoint.result_commit,
    )
    return collision_message


def _prepared_record(attempt: _ValidatedBranchExportAttempt) -> JsonObject:
    request = attempt.request
    run = request.run
    return prepared_branch_export_record(
        run.plan,
        run.run_id,
        run.run_key_name,
        run.source.repository_id,
        request.logical_worktree_name,
        attempt.branch_name,
        attempt.branch_ref,
        attempt.checkpoint,
        request.policy,
        attempt.operation_origin,
        attempt.reconciliation.recovery_mode,
    )


def _fulfilled_record(
    attempt: _ValidatedBranchExportAttempt,
    operation: BranchExportOperation,
    branch_exists_before: bool,
    dry_run: bool,
) -> JsonObject:
    request = attempt.request
    run = request.run
    return branch_export_record(
        run.plan,
        run.run_id,
        run.run_key_name,
        request.logical_worktree_name,
        attempt.branch_name,
        attempt.branch_ref,
        attempt.checkpoint,
        request.policy,
        operation,
        branch_exists_before,
        branch_exists_after=(branch_exists_before if dry_run else True),
        operation_origin=attempt.operation_origin,
        recovery_mode=attempt.reconciliation.recovery_mode,
        repository_id=run.source.repository_id,
        dry_run=dry_run,
    )


def _skipped_record(
    request: BranchExportAttempt,
    operation_origin: BranchExportOrigin,
    dry_run: bool = False,
) -> JsonObject:
    run = request.run
    return skipped_branch_export_record(
        run.plan,
        run.run_id,
        run.run_key_name,
        request.logical_worktree_name,
        request.node.id,
        dry_run=dry_run,
        operation_origin=operation_origin,
        repository_id=run.source.repository_id,
    )


def _failed_record(
    request: BranchExportAttempt,
    branch_name_value: str | None,
    branch_ref: str | None,
    checkpoint: BranchExportCheckpoint | None,
    branch_exists_before: bool | None,
    failure_message: str,
    operation_origin: BranchExportOrigin,
    recovery_mode: str,
    dry_run: bool = False,
) -> JsonObject:
    run = request.run
    return failed_branch_export_record(
        run.plan,
        run.run_id,
        run.run_key_name,
        request.logical_worktree_name,
        request.node.id,
        branch_name_value,
        branch_ref,
        checkpoint,
        branch_exists_before,
        failure_message,
        dry_run=dry_run,
        operation_origin=operation_origin,
        recovery_mode=recovery_mode,
        repository_id=run.source.repository_id,
    )


def _create_branch_export_ref_with_source(
    attempt: _ValidatedBranchExportAttempt,
    allow_existing: bool,
    allow_create: bool,
) -> BranchExportOperation:
    request = attempt.request
    owner = TemporaryRefOwner.dedicated(
        request.run.plan,
        request.run.source,
        attempt.checkpoint.state_path.parent,
        request.node.id,
        f"branch-export-{request.logical_worktree_name}",
    )
    source_ref = load_source_ref_from_state(attempt.checkpoint.state_path)
    completed_operation: BranchExportOperation | None = None
    operation_completed_before_error = False
    try:
        with ensure_source_commit_available(
            request.run.source,
            source_ref,
            owner,
            source_chain_verified=True,
        ):
            try:
                completed_operation = create_branch_export_ref(
                    request.run.source,
                    attempt.branch_ref,
                    attempt.checkpoint,
                    allow_existing=allow_existing,
                    allow_create=allow_create,
                )
            except BranchExportOperationCompletedError:
                operation_completed_before_error = True
                raise
    except BranchExportOperationCompletedError:
        raise
    except Exception as exc:
        if completed_operation is not None or operation_completed_before_error:
            raise BranchExportOperationCompletedError(str(exc)) from exc
        raise
    if completed_operation is None:
        raise RuntimeError("Workspace branch export operation did not complete.")
    return completed_operation
