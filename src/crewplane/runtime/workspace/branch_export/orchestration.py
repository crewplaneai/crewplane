from __future__ import annotations

from collections.abc import Collection
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
)
from crewplane.runtime.workspace.branch_export.attempts import (
    BranchExportAttempt,
    BranchExportRecordWriter,
    BranchExportRun,
    fulfillment_payload,
    preview_payload,
)
from crewplane.runtime.workspace.branch_export.checkpoint import StageLookup
from crewplane.runtime.workspace.branch_export.fulfillment import (
    record_branch_export_fulfillment,
    record_skipped_branch_export_fulfillment,
)
from crewplane.runtime.workspace.branch_export.reconciliation import (
    BranchExportOrigin,
)
from crewplane.runtime.workspace.branch_export.records import (
    branch_export_operation,
    checkpoint_from_record,
)


@dataclass(frozen=True)
class _HistoryStageLookup:
    stage_dirs: dict[str, Path]

    def get_node_dir(self, request: NodeArtifactRequest) -> Path | None:
        stage_dir = self.stage_dirs.get(request.node_id)
        if stage_dir is None or not stage_dir.is_dir():
            return None
        return stage_dir


@dataclass(frozen=True)
class _BranchExportBatch:
    run: BranchExportRun
    write_record: BranchExportRecordWriter
    operation_origin: BranchExportOrigin
    resumed_node_ids: Collection[str]
    eligible_node_ids: Collection[str] | None = None


def fulfill_branch_exports(
    plan: PreflightExecutionPlan,
    output: ArtifactStorePort,
    resumed_node_ids: Collection[str] = (),
) -> tuple[Path, ...]:
    run = _branch_export_run(
        plan,
        output.run_id,
        output.run_key_name,
        output.stages_dir,
        output,
    )
    if run is None:
        return ()
    return _fulfill_branch_export_batch(
        _BranchExportBatch(
            run=run,
            write_record=output.write_workspace_export,
            operation_origin="current_run",
            resumed_node_ids=frozenset(resumed_node_ids),
        )
    )


def fulfill_branch_exports_from_history(
    plan: PreflightExecutionPlan,
    source: RunHistoryRecord,
    eligible_node_ids: Collection[str] | None = None,
) -> tuple[Path, ...]:
    run = _branch_export_run(
        plan,
        source.manifest.run_id,
        source.manifest.run_key_name,
        source.run_dir,
        _history_stage_lookup(plan, source),
    )
    if run is None:
        return ()

    def write_record(logical_worktree_name: str, payload: object) -> Path:
        export_dir = source.run_dir / "workspace-exports"
        export_name = build_workspace_export_filename(logical_worktree_name)
        return atomic_write_json(export_dir / export_name, payload)

    return _fulfill_branch_export_batch(
        _BranchExportBatch(
            run=run,
            write_record=write_record,
            operation_origin="verified_history",
            resumed_node_ids=(),
            eligible_node_ids=eligible_node_ids,
        )
    )


def preview_branch_exports_from_history(
    plan: PreflightExecutionPlan,
    source: RunHistoryRecord,
) -> tuple[JsonObject, ...]:
    run = _branch_export_run(
        plan,
        source.manifest.run_id,
        source.manifest.run_key_name,
        source.run_dir,
        _history_stage_lookup(plan, source),
    )
    if run is None:
        return ()
    return tuple(
        preview_payload(
            BranchExportAttempt(
                run=run,
                logical_worktree_name=logical_worktree_name,
                node=node,
                policy=node.workspace_policy,
                operation_origin="verified_history",
            )
        )
        for logical_worktree_name, node in _selected_worktree_nodes_by_worktree(
            plan
        ).items()
        if node.workspace_policy is not None
    )


def _branch_export_run(
    plan: PreflightExecutionPlan,
    run_id: str,
    run_key_name: str,
    stages_dir: Path,
    state_lookup: StageLookup,
) -> BranchExportRun | None:
    source = plan.workspace_source
    if source is None:
        return None
    return BranchExportRun(
        plan=plan,
        run_id=run_id,
        run_key_name=run_key_name,
        source=source,
        stages_dir=stages_dir,
        state_lookup=state_lookup,
    )


def _history_stage_lookup(
    plan: PreflightExecutionPlan,
    source: RunHistoryRecord,
) -> _HistoryStageLookup:
    return _HistoryStageLookup(
        {
            node.id: source.run_dir / node.artifact_contract.stage_path
            for node in plan.nodes
            if node.artifact_contract.stage_path is not None
        }
    )


def _fulfill_branch_export_batch(batch: _BranchExportBatch) -> tuple[Path, ...]:
    records: list[Path] = []
    for logical_worktree_name, node in _selected_worktree_nodes_by_worktree(
        batch.run.plan
    ).items():
        if (
            batch.eligible_node_ids is not None
            and node.id not in batch.eligible_node_ids
        ):
            continue
        policy = node.workspace_policy
        if policy is None:
            continue
        attempt = BranchExportAttempt(
            run=batch.run,
            logical_worktree_name=logical_worktree_name,
            node=node,
            policy=policy,
            operation_origin=_node_operation_origin(batch, node.id),
        )
        payload = fulfillment_payload(attempt, batch.write_record)
        record_path = batch.write_record(logical_worktree_name, payload)
        _record_state_fulfillment(
            batch.run.plan,
            node,
            batch.run.stages_dir,
            payload,
            record_path,
        )
        records.append(record_path)
        if payload["status"] == "failed_verification":
            raise RuntimeError(str(payload["failure_message"]))
    return tuple(records)


def _node_operation_origin(
    batch: _BranchExportBatch,
    node_id: str,
) -> BranchExportOrigin:
    if (
        batch.operation_origin == "verified_history"
        or node_id in batch.resumed_node_ids
    ):
        return "verified_history"
    return "current_run"


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


def _record_state_fulfillment(
    plan: PreflightExecutionPlan,
    node: PreflightExecutionNode,
    stages_dir: Path,
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
                record_path,
                record_payload,
            )
        return
    record_branch_export_fulfillment(
        plan,
        node,
        stages_dir,
        checkpoint,
        record_path,
        record_payload,
        branch_export_operation(record_payload),
    )
