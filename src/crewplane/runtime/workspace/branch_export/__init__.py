from __future__ import annotations

from collections.abc import Callable, Collection
from dataclasses import dataclass
from pathlib import Path

from crewplane.architecture.contracts import JsonObject, NodeArtifactRequest
from crewplane.architecture.ports import ArtifactStorePort
from crewplane.artifacts.atomic import atomic_write_json
from crewplane.artifacts.naming import build_workspace_export_filename
from crewplane.artifacts.run_history import RunHistoryRecord
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
    record_branch_export_fulfillment,
    record_skipped_branch_export_fulfillment,
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
    classify_branch_export_record,
)
from crewplane.runtime.workspace.branch_export.records import (
    branch_export_operation,
    branch_export_record,
    branch_name,
    checkpoint_from_record,
    failed_branch_export_record,
    prepared_branch_export_record,
    skipped_branch_export_record,
)
from crewplane.runtime.workspace.worktree.descriptors import (
    load_source_ref_from_state,
)
from crewplane.runtime.workspace.worktree.lineage import ensure_source_commit_available
from crewplane.runtime.workspace.worktree.temporary_refs import TemporaryRefOwner


@dataclass(frozen=True)
class _HistoryStageLookup:
    stage_dirs: dict[str, Path]

    def get_node_dir(self, request: NodeArtifactRequest) -> Path | None:
        stage_dir = self.stage_dirs.get(request.node_id)
        if stage_dir is None or not stage_dir.is_dir():
            return None
        return stage_dir


def fulfill_branch_exports(
    plan: PreflightExecutionPlan,
    output: ArtifactStorePort,
    resumed_node_ids: Collection[str] = (),
) -> tuple[Path, ...]:
    return _fulfill_branch_exports(
        plan=plan,
        run_id=output.run_id,
        run_key_name=output.run_key_name,
        stages_dir=output.stages_dir,
        results_dir=output.results_dir,
        state_lookup=output,
        write_record=output.write_workspace_export,
        operation_origin="current_run",
        resumed_node_ids=frozenset(resumed_node_ids),
    )


def fulfill_branch_exports_from_history(
    plan: PreflightExecutionPlan,
    source: RunHistoryRecord,
    eligible_node_ids: Collection[str] | None = None,
) -> tuple[Path, ...]:
    stage_dirs = {
        node.id: source.run_dir / node.artifact_contract.stage_path
        for node in plan.nodes
        if node.artifact_contract.stage_path is not None
    }

    def write_record(logical_worktree_name: str, payload: object) -> Path:
        export_dir = source.run_dir / "workspace-exports"
        export_name = build_workspace_export_filename(logical_worktree_name)
        return atomic_write_json(export_dir / export_name, payload)

    return _fulfill_branch_exports(
        plan=plan,
        run_id=source.manifest.run_id,
        run_key_name=source.manifest.run_key_name,
        stages_dir=source.run_dir,
        results_dir=source.results_dir,
        state_lookup=_HistoryStageLookup(stage_dirs),
        write_record=write_record,
        operation_origin="verified_history",
        resumed_node_ids=(),
        eligible_node_ids=eligible_node_ids,
    )


def preview_branch_exports_from_history(
    plan: PreflightExecutionPlan,
    source: RunHistoryRecord,
) -> tuple[JsonObject, ...]:
    stage_dirs = {
        node.id: source.run_dir / node.artifact_contract.stage_path
        for node in plan.nodes
        if node.artifact_contract.stage_path is not None
    }
    return _preview_branch_exports(
        plan,
        source.manifest.run_id,
        source.manifest.run_key_name,
        source.run_dir,
        _HistoryStageLookup(stage_dirs),
    )


def _fulfill_branch_exports(
    plan: PreflightExecutionPlan,
    run_id: str,
    run_key_name: str,
    stages_dir: Path,
    results_dir: Path,
    state_lookup: StageLookup,
    write_record: Callable[[str, object], Path],
    operation_origin: BranchExportOrigin,
    resumed_node_ids: Collection[str],
    eligible_node_ids: Collection[str] | None = None,
) -> tuple[Path, ...]:
    source = plan.workspace_source
    if source is None:
        return ()
    records: list[Path] = []
    for logical_worktree_name, node in _selected_worktree_nodes_by_worktree(
        plan
    ).items():
        if eligible_node_ids is not None and node.id not in eligible_node_ids:
            continue
        policy = node.workspace_policy
        if policy is None:
            continue
        node_operation_origin: BranchExportOrigin = (
            "verified_history"
            if operation_origin == "verified_history" or node.id in resumed_node_ids
            else "current_run"
        )
        record_payload = _fulfillment_payload(
            plan,
            run_id,
            run_key_name,
            logical_worktree_name,
            node,
            policy,
            source,
            stages_dir,
            state_lookup,
            node_operation_origin,
            stages_dir
            / "workspace-exports"
            / build_workspace_export_filename(logical_worktree_name),
            write_record,
        )
        record_path = write_record(logical_worktree_name, record_payload)
        _record_state_fulfillment(
            plan,
            node,
            stages_dir,
            results_dir,
            record_payload,
            record_path,
        )
        records.append(record_path)
        if record_payload["status"] == "failed_verification":
            raise RuntimeError(str(record_payload["failure_message"]))
    return tuple(records)


def _preview_branch_exports(
    plan: PreflightExecutionPlan,
    run_id: str,
    run_key_name: str,
    stages_dir: Path,
    state_lookup: StageLookup,
) -> tuple[JsonObject, ...]:
    source = plan.workspace_source
    if source is None:
        return ()
    return tuple(
        _preview_payload(
            plan,
            run_id,
            run_key_name,
            logical_worktree_name,
            node,
            source,
            stages_dir,
            state_lookup,
        )
        for logical_worktree_name, node in _selected_worktree_nodes_by_worktree(
            plan
        ).items()
        if node.workspace_policy is not None
    )


def _selected_worktree_nodes_by_worktree(
    plan: PreflightExecutionPlan,
) -> dict[str, PreflightExecutionNode]:
    nodes_by_id = {node.id: node for node in plan.nodes}
    selected: dict[str, PreflightExecutionNode] = {}
    for node_id in plan.execution_order:
        node = nodes_by_id.get(node_id)
        if node is None:
            continue
        policy = node.workspace_policy
        if (
            policy is None
            or not policy.enabled
            or policy.declaration_kind != "worktree"
            or policy.logical_worktree_name is None
        ):
            continue
        selected[policy.logical_worktree_name] = node
    return selected


def _fulfillment_payload(
    plan: PreflightExecutionPlan,
    run_id: str,
    run_key_name: str,
    logical_worktree_name: str,
    node: PreflightExecutionNode,
    policy: WorkspaceSelectionRecord,
    source: WorkspaceSourceSnapshot,
    stages_dir: Path,
    state_lookup: StageLookup,
    operation_origin: BranchExportOrigin,
    record_path: Path,
    write_record: Callable[[str, object], Path],
) -> JsonObject:
    record_exists_at_entry = record_path.exists(follow_symlinks=False)
    if not policy.branch_export.create_branch and not record_exists_at_entry:
        return skipped_branch_export_record(
            plan,
            run_id,
            run_key_name,
            logical_worktree_name,
            node.id,
            operation_origin=operation_origin,
            repository_id=source.repository_id,
        )
    checkpoint: BranchExportCheckpoint | None = None
    branch_name_value = branch_name(plan, policy, logical_worktree_name, run_key_name)
    branch_ref: str | None = None
    branch_exists_before: bool | None = None
    recovery_mode = "initial"
    attempt_origin = operation_origin
    try:
        checkpoint = validated_checkpoint(
            plan,
            source,
            node,
            policy,
            stages_dir,
            state_lookup,
            run_id,
            run_key_name,
        )
        branch_ref = validated_branch_ref(source, branch_name_value)
    except Exception as exc:
        if record_exists_at_entry or record_path.exists(follow_symlinks=False):
            raise
        return failed_branch_export_record(
            plan,
            run_id,
            run_key_name,
            logical_worktree_name,
            node.id,
            branch_name_value,
            branch_ref,
            checkpoint,
            branch_exists_before,
            str(exc),
            operation_origin=attempt_origin,
            recovery_mode=recovery_mode,
            repository_id=source.repository_id,
        )

    reconciliation = classify_branch_export_record(
        record_path,
        plan,
        run_id,
        run_key_name,
        source.repository_id,
        logical_worktree_name,
        branch_name_value,
        branch_ref,
        checkpoint,
        policy,
        operation_origin,
    )
    recovery_mode = reconciliation.recovery_mode
    attempt_origin = reconciliation.operation_origin or operation_origin
    try:
        if (
            not policy.branch_export.create_branch
            and recovery_mode != "prepared_record"
        ):
            return skipped_branch_export_record(
                plan,
                run_id,
                run_key_name,
                logical_worktree_name,
                node.id,
                operation_origin=operation_origin,
                repository_id=source.repository_id,
            )
        branch_exists_before = branch_ref_exists(source, branch_ref)
        if reconciliation.idempotent_payload is not None and branch_exists_before:
            _create_branch_export_ref_with_source(
                plan,
                source,
                node.id,
                logical_worktree_name,
                branch_ref,
                checkpoint,
                allow_existing=True,
                allow_create=False,
            )
            return reconciliation.idempotent_payload
        allow_create = recovery_mode == "prepared_record" or not branch_exists_before
        if allow_create and recovery_mode == "initial":
            prepared = prepared_branch_export_record(
                plan,
                run_id,
                run_key_name,
                source.repository_id,
                logical_worktree_name,
                branch_name_value,
                branch_ref,
                checkpoint,
                policy,
                attempt_origin,
                recovery_mode,
            )
            write_record(logical_worktree_name, prepared)
        operation = _create_branch_export_ref_with_source(
            plan,
            source,
            node.id,
            logical_worktree_name,
            branch_ref,
            checkpoint,
            allow_existing=(
                attempt_origin == "verified_history"
                or recovery_mode == "prepared_record"
            ),
            allow_create=allow_create,
        )
        branch_exists_before = operation == "verified_existing"
        return branch_export_record(
            plan,
            run_id,
            run_key_name,
            logical_worktree_name,
            branch_name_value,
            branch_ref,
            checkpoint,
            policy,
            operation,
            branch_exists_before,
            True,
            operation_origin=attempt_origin,
            recovery_mode=recovery_mode,
            repository_id=source.repository_id,
        )
    except BranchExportOperationCompletedError:
        raise
    except Exception as exc:
        failure_message = str(exc)
        if reconciliation.idempotent_payload is not None:
            _, collision_message = planned_branch_operation(
                source,
                branch_ref,
                checkpoint.result_commit,
            )
            if collision_message is None:
                raise
            failure_message = collision_message
        return failed_branch_export_record(
            plan,
            run_id,
            run_key_name,
            logical_worktree_name,
            node.id,
            branch_name_value,
            branch_ref,
            checkpoint,
            branch_exists_before,
            failure_message,
            operation_origin=attempt_origin,
            recovery_mode=recovery_mode,
            repository_id=source.repository_id,
        )


def _preview_payload(
    plan: PreflightExecutionPlan,
    run_id: str,
    run_key_name: str,
    logical_worktree_name: str,
    node: PreflightExecutionNode,
    source: WorkspaceSourceSnapshot,
    stages_dir: Path,
    state_lookup: StageLookup,
) -> JsonObject:
    policy = node.workspace_policy
    if policy is None:
        raise RuntimeError(f"Node '{node.id}' has no workspace policy.")
    record_path = (
        stages_dir
        / "workspace-exports"
        / build_workspace_export_filename(logical_worktree_name)
    )
    if not policy.branch_export.create_branch and not record_path.exists(
        follow_symlinks=False
    ):
        return skipped_branch_export_record(
            plan,
            run_id,
            run_key_name,
            logical_worktree_name,
            node.id,
            dry_run=True,
            operation_origin="verified_history",
            repository_id=source.repository_id,
        )
    branch_name_value = branch_name(plan, policy, logical_worktree_name, run_key_name)
    branch_ref: str | None = None
    checkpoint: BranchExportCheckpoint | None = None
    recovery_mode = "initial"
    attempt_origin: BranchExportOrigin = "verified_history"
    try:
        checkpoint = validated_checkpoint(
            plan,
            source,
            node,
            policy,
            stages_dir,
            state_lookup,
            run_id,
            run_key_name,
        )
        branch_ref = validated_branch_ref(source, branch_name_value)
        reconciliation = classify_branch_export_record(
            record_path,
            plan,
            run_id,
            run_key_name,
            source.repository_id,
            logical_worktree_name,
            branch_name_value,
            branch_ref,
            checkpoint,
            policy,
            attempt_origin,
        )
        recovery_mode = reconciliation.recovery_mode
        attempt_origin = reconciliation.operation_origin or attempt_origin
        if (
            not policy.branch_export.create_branch
            and recovery_mode != "prepared_record"
        ):
            return skipped_branch_export_record(
                plan,
                run_id,
                run_key_name,
                logical_worktree_name,
                node.id,
                dry_run=True,
                operation_origin=attempt_origin,
                repository_id=source.repository_id,
            )
        operation, failure_message = planned_branch_operation(
            source,
            branch_ref,
            checkpoint.result_commit,
        )
        if failure_message is not None:
            return failed_branch_export_record(
                plan,
                run_id,
                run_key_name,
                logical_worktree_name,
                node.id,
                branch_name_value,
                branch_ref,
                checkpoint,
                True,
                failure_message,
                dry_run=True,
                operation_origin=attempt_origin,
                recovery_mode=recovery_mode,
                repository_id=source.repository_id,
            )
        return branch_export_record(
            plan,
            run_id,
            run_key_name,
            logical_worktree_name,
            branch_name_value,
            branch_ref,
            checkpoint,
            policy,
            operation,
            branch_exists_before=operation == "verified_existing",
            branch_exists_after=operation == "verified_existing",
            operation_origin=attempt_origin,
            recovery_mode=recovery_mode,
            repository_id=source.repository_id,
            dry_run=True,
        )
    except Exception as exc:
        return failed_branch_export_record(
            plan,
            run_id,
            run_key_name,
            logical_worktree_name,
            node.id,
            branch_name_value,
            branch_ref,
            checkpoint,
            None,
            str(exc),
            dry_run=True,
            operation_origin=attempt_origin,
            recovery_mode=recovery_mode,
            repository_id=source.repository_id,
        )


def _create_branch_export_ref_with_source(
    plan: PreflightExecutionPlan,
    source: WorkspaceSourceSnapshot,
    node_id: str,
    logical_worktree_name: str,
    branch_ref: str,
    checkpoint: BranchExportCheckpoint,
    allow_existing: bool,
    allow_create: bool,
) -> BranchExportOperation:
    owner = TemporaryRefOwner.dedicated(
        plan,
        source,
        checkpoint.state_path.parent,
        node_id,
        f"branch-export-{logical_worktree_name}",
    )
    source_ref = load_source_ref_from_state(checkpoint.state_path)
    completed_operation: BranchExportOperation | None = None
    operation_completed_before_error = False
    try:
        with ensure_source_commit_available(
            source,
            source_ref,
            owner,
            source_chain_verified=True,
        ):
            try:
                completed_operation = create_branch_export_ref(
                    source,
                    branch_ref,
                    checkpoint,
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


def _record_state_fulfillment(
    plan: PreflightExecutionPlan,
    node: PreflightExecutionNode,
    stages_dir: Path,
    results_dir: Path,
    record_payload: JsonObject,
    record_path: Path,
) -> None:
    checkpoint = checkpoint_from_record(stages_dir, record_payload)
    if checkpoint is None:
        if record_payload.get("status") == "skipped":
            record_skipped_branch_export_fulfillment(
                plan,
                node,
                stages_dir,
                results_dir,
                record_path,
                record_payload,
            )
        return
    record_branch_export_fulfillment(
        plan,
        node,
        stages_dir,
        results_dir,
        checkpoint,
        record_path,
        record_payload,
        branch_export_operation(record_payload),
    )
