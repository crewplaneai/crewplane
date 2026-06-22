from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from orchestrator_cli.architecture.contracts import JsonObject
from orchestrator_cli.architecture.ports import ArtifactStorePort
from orchestrator_cli.artifacts.atomic import atomic_write_json
from orchestrator_cli.artifacts.naming import build_workspace_export_filename
from orchestrator_cli.artifacts.run_history import RunHistoryRecord
from orchestrator_cli.core.preflight.models import (
    PreflightExecutionNode,
    PreflightExecutionPlan,
    WorkspaceSelectionRecord,
    WorkspaceSourceSnapshot,
)
from orchestrator_cli.runtime.workspace.branch_export_checkpoint import (
    StageLookup,
    validated_checkpoint,
)
from orchestrator_cli.runtime.workspace.branch_export_fulfillment import (
    BranchExportCheckpoint,
    create_branch_export_ref,
    record_branch_export_fulfillment,
    record_skipped_branch_export_fulfillment,
)
from orchestrator_cli.runtime.workspace.branch_export_git import (
    branch_ref_exists,
    planned_branch_operation,
    validated_branch_ref,
)
from orchestrator_cli.runtime.workspace.branch_export_records import (
    branch_export_operation,
    branch_export_record,
    branch_name,
    checkpoint_from_record,
    failed_branch_export_record,
    skipped_branch_export_record,
)


@dataclass(frozen=True)
class _HistoryStageLookup:
    stage_dirs: dict[str, Path]

    def get_stage_dir(self, stage_name: str) -> Path | None:
        stage_dir = self.stage_dirs.get(stage_name)
        if stage_dir is None or not stage_dir.is_dir():
            return None
        return stage_dir


def fulfill_branch_exports(
    plan: PreflightExecutionPlan,
    output: ArtifactStorePort,
) -> tuple[Path, ...]:
    return _fulfill_branch_exports(
        plan=plan,
        run_id=output.run_id,
        run_key_name=output.run_key_name,
        stages_dir=output.stages_dir,
        results_dir=output.results_dir,
        state_lookup=output,
        write_record=output.write_workspace_export,
    )


def fulfill_branch_exports_from_history(
    plan: PreflightExecutionPlan,
    source: RunHistoryRecord,
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
) -> tuple[Path, ...]:
    source = plan.workspace_source
    if source is None:
        return ()
    records: list[Path] = []
    for logical_worktree_name, node in _selected_worktree_nodes_by_worktree(
        plan
    ).items():
        policy = node.workspace_policy
        if policy is None:
            continue
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
) -> JsonObject:
    if not policy.branch_export.create_branch:
        return skipped_branch_export_record(
            plan,
            run_id,
            run_key_name,
            logical_worktree_name,
            node.id,
        )
    checkpoint: BranchExportCheckpoint | None = None
    branch_name_value = branch_name(plan, policy, logical_worktree_name, run_key_name)
    branch_ref: str | None = None
    branch_exists_before: bool | None = None
    try:
        checkpoint = validated_checkpoint(
            plan,
            source,
            node,
            policy,
            stages_dir,
            state_lookup,
            True,
        )
        branch_ref = validated_branch_ref(source, branch_name_value)
        branch_exists_before = branch_ref_exists(source, branch_ref)
        operation = create_branch_export_ref(
            source,
            branch_ref,
            checkpoint,
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
            branch_exists_before,
            branch_ref_exists(source, branch_ref),
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
            branch_exists_before,
            str(exc),
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
    if not policy.branch_export.create_branch:
        return skipped_branch_export_record(
            plan,
            run_id,
            run_key_name,
            logical_worktree_name,
            node.id,
            dry_run=True,
        )
    branch_name_value = branch_name(plan, policy, logical_worktree_name, run_key_name)
    branch_ref: str | None = None
    checkpoint: BranchExportCheckpoint | None = None
    try:
        checkpoint = validated_checkpoint(
            plan,
            source,
            node,
            policy,
            stages_dir,
            state_lookup,
            False,
        )
        branch_ref = validated_branch_ref(source, branch_name_value)
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
        )


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
