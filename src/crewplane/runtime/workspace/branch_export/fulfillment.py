from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from crewplane.architecture.contracts import JsonObject
from crewplane.artifacts.atomic import atomic_write_json
from crewplane.artifacts.naming import (
    build_node_state_filename,
)
from crewplane.artifacts.workspace.node_state import (
    build_node_workspace_descriptor,
)
from crewplane.core.execution_state import NodeState
from crewplane.core.preflight.models import (
    PreflightExecutionNode,
    PreflightExecutionPlan,
    WorkspaceSourceSnapshot,
)
from crewplane.runtime.workspace.branch_export.git import (
    BranchExportOperation,
    create_or_verify_branch_ref,
)


@dataclass(frozen=True)
class BranchExportCheckpoint:
    state_path: Path
    state_relative_path: str
    node_id: str
    task_id: str
    result_commit: str
    result_tree: str
    result_ref: str
    bundle_path: Path
    bundle_relative_path: str
    bundle_sha256: str
    bundle_size_bytes: int


@dataclass(frozen=True)
class _WorkspaceDescriptorStore:
    run_id: str
    run_key_name: str
    task_name: str
    stages_dir: Path
    results_dir: Path
    logs_dir: Path
    project_root: Path
    log_cli_output: bool
    stage_name: str
    stage_dir: Path

    def get_stage_dir(self, stage_name: str) -> Path | None:
        if stage_name != self.stage_name or not self.stage_dir.is_dir():
            return None
        return self.stage_dir


def create_branch_export_ref(
    source: WorkspaceSourceSnapshot,
    branch_ref: str,
    checkpoint: BranchExportCheckpoint,
) -> BranchExportOperation:
    return create_or_verify_branch_ref(
        source,
        branch_ref,
        checkpoint.result_commit,
        allow_existing=True,
    )


def record_branch_export_fulfillment(
    plan: PreflightExecutionPlan,
    node: PreflightExecutionNode,
    stages_dir: Path,
    results_dir: Path,
    checkpoint: BranchExportCheckpoint,
    record_path: Path,
    record_payload: JsonObject,
    operation: BranchExportOperation,
) -> None:
    try:
        record_relative_path = record_path.relative_to(stages_dir).as_posix()
    except ValueError:
        record_relative_path = record_path.as_posix()
    state_payload = _workspace_state_payload(checkpoint.state_path)
    state_payload["branch_export"] = {
        "status": record_payload["status"],
        "operation": operation,
        "branch_name": record_payload["branch_name"],
        "branch_ref": record_payload["branch_ref"],
        "record_artifact": record_relative_path,
        "result_commit": checkpoint.result_commit,
        "result_tree": checkpoint.result_tree,
        "completed_at": record_payload["created_at"],
    }
    if "failure_message" in record_payload:
        state_payload["branch_export"]["failure_message"] = record_payload[
            "failure_message"
        ]
    atomic_write_json(checkpoint.state_path, state_payload)
    _refresh_node_manifest_workspace_descriptor(plan, node, stages_dir, results_dir)


def record_skipped_branch_export_fulfillment(
    plan: PreflightExecutionPlan,
    node: PreflightExecutionNode,
    stages_dir: Path,
    results_dir: Path,
    record_path: Path,
    record_payload: JsonObject,
) -> None:
    state_path = _node_workspace_state_path(node, stages_dir)
    if state_path is None:
        return
    try:
        record_relative_path = record_path.relative_to(stages_dir).as_posix()
    except ValueError:
        record_relative_path = record_path.as_posix()
    state_payload = _workspace_state_payload(state_path)
    state_payload["branch_export"] = {
        "status": record_payload["status"],
        "operation": record_payload["operation"],
        "branch_name": record_payload["branch_name"],
        "branch_ref": record_payload["branch_ref"],
        "record_artifact": record_relative_path,
        "skip_reason": record_payload.get("skip_reason"),
        "completed_at": record_payload["created_at"],
    }
    atomic_write_json(state_path, state_payload)
    _refresh_node_manifest_workspace_descriptor(plan, node, stages_dir, results_dir)


def _workspace_state_payload(state_path: Path) -> dict[str, object]:
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(
            f"Workspace branch export state is invalid: {state_path.as_posix()}"
        )
    return payload


def _node_workspace_state_path(
    node: PreflightExecutionNode,
    stages_dir: Path,
) -> Path | None:
    stage_path = node.artifact_contract.stage_path
    if stage_path is None:
        return None
    state_path = stages_dir / stage_path / "workspace-state.json"
    if not state_path.is_file() or state_path.is_symlink():
        return None
    return state_path


def _refresh_node_manifest_workspace_descriptor(
    plan: PreflightExecutionPlan,
    node: PreflightExecutionNode,
    stages_dir: Path,
    results_dir: Path,
) -> None:
    node_state_path = (
        stages_dir / "manifests" / "nodes" / build_node_state_filename(node.id)
    )
    if not node_state_path.is_file() or node_state_path.is_symlink():
        return
    node_state = NodeState.model_validate_json(
        node_state_path.read_text(encoding="utf-8")
    )
    stage_path = node.artifact_contract.stage_path
    if stage_path is None:
        return
    store = _WorkspaceDescriptorStore(
        run_id=node_state.run_id,
        run_key_name=node_state.run_key_name,
        task_name=node_state.workflow_name,
        stages_dir=stages_dir,
        results_dir=results_dir,
        logs_dir=stages_dir / "logs",
        project_root=stages_dir,
        log_cli_output=False,
        stage_name=node.id,
        stage_dir=stages_dir / stage_path,
    )
    refreshed = node_state.model_copy(
        update={"workspace": build_node_workspace_descriptor(node, plan, store)}
    )
    validated = NodeState.model_validate(refreshed.model_dump(mode="json"))
    atomic_write_json(
        node_state_path,
        validated.model_dump(mode="json", exclude_none=True),
    )
