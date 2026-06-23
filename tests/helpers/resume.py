from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path

from crewplane.artifacts.workspace.node_state import (
    build_node_workspace_descriptor,
)
from crewplane.core.execution_state import (
    RUN_STATE_SCHEMA_VERSION,
    ArtifactDescriptor,
    NodeState,
    RunManifest,
)
from crewplane.core.preflight.models import (
    ArtifactContract,
    DependencyEdge,
    ExecutionPolicy,
    PreflightExecutionNode,
    PreflightExecutionPlan,
    ProviderRecord,
    WorkspaceFileLocator,
    WorkspaceSelectionRecord,
    WorkspaceSourceSnapshot,
)
from crewplane.core.workspace.policy import WorktreeContract
from crewplane.version import SCHEMA_VERSION

WORKFLOW_IDENTITY = ".crewplane/workflows/workflow.task.md"
WORKFLOW_NAME = "Workflow"
WORKFLOW_SIGNATURE = hashlib.sha256(b"workflow").hexdigest()
RUNTIME_SIGNATURE = hashlib.sha256(b"runtime").hexdigest()
WORKTREE_CONTRACT = WorktreeContract()
WORKTREE_CONTRACT_PAYLOAD = WORKTREE_CONTRACT.model_dump(mode="json")


def sha256_hex(value: str | bytes) -> str:
    payload = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(payload).hexdigest()


WORKSPACE_BLOB_SHA256 = sha256_hex("workspace input")
WORKSPACE_BLOB_ID = "d" * 40
WORKSPACE_RUN_BASE_COMMIT = "a" * 40
WORKSPACE_SOURCE_TREE = "b" * 40


def iso_datetime(offset_seconds: int = 0) -> str:
    return (datetime(2026, 6, 9, 12, 0) + timedelta(seconds=offset_seconds)).isoformat()


def make_run_manifest(
    run_id: str,
    run_key_name: str,
    status: str = "running",
    started_offset: int = 0,
    workflow_identity: str = WORKFLOW_IDENTITY,
    workflow_name: str = WORKFLOW_NAME,
    workflow_signature: str = WORKFLOW_SIGNATURE,
) -> RunManifest:
    terminal = status in {"succeeded", "failed", "cancelled"}
    return RunManifest(
        run_state_schema_version=RUN_STATE_SCHEMA_VERSION,
        plan_schema_version=SCHEMA_VERSION,
        workflow_identity=workflow_identity,
        workflow_name=workflow_name,
        workflow_signature=workflow_signature,
        run_id=run_id,
        run_key_name=run_key_name,
        started_at=iso_datetime(started_offset),
        completed_at=iso_datetime(started_offset + 1) if terminal else None,
        status=status,
        effective_runtime_config_signature=RUNTIME_SIGNATURE,
        preflight_plan_path="preflight/execution-plan.json",
        preflight_manifest_path="preflight/manifest.json",
        runtime_config_snapshot_path="preflight/runtime-config-snapshot.json",
        runtime_config_snapshot={"schema_version": SCHEMA_VERSION},
        workflow_source="workflow source",
        composed_workflow={"schema_version": SCHEMA_VERSION, "name": workflow_name},
        failure_message="failed" if status == "failed" else None,
        cancel_reason="cancelled" if status == "cancelled" else None,
    )


def write_run_manifest(state_dir: Path, manifest: RunManifest) -> Path:
    path = (
        state_dir
        / "execution-stages"
        / manifest.run_key_name
        / "manifests"
        / "run.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        manifest.model_dump_json(indent=2, exclude_none=True) + "\n",
        encoding="utf-8",
    )
    return path


def make_plan(findings_edge: bool = False) -> PreflightExecutionPlan:
    a_contract = ArtifactContract(
        stage_path="a",
        output_path="a-result.md",
        findings_path="a-findings.md",
    )
    b_contract = ArtifactContract(stage_path="b", output_path="b-result.md")
    dependency_edge = DependencyEdge(
        source_node="a",
        target_node="b",
        artifact_name="findings" if findings_edge else "output",
        dependency_signature=sha256_hex("a->b"),
        artifact_key="findings" if findings_edge else "output",
    )
    return PreflightExecutionPlan(
        run_id="current-run",
        run_key_name="workflow--current-run",
        project_root=".",
        context_root=".crewplane/execution-stages/workflow--current-run",
        manifest_root=".crewplane/execution-stages/workflow--current-run/manifests",
        created_at=iso_datetime(),
        workflow_name=WORKFLOW_NAME,
        workflow_signature=WORKFLOW_SIGNATURE,
        execution_order=["a", "b"],
        nodes=[
            PreflightExecutionNode(
                id="a",
                mode="sequential",
                dependencies=[],
                execution_policy=ExecutionPolicy(),
                artifact_contract=a_contract,
                provider_records=[make_provider_record("alpha")],
            ),
            PreflightExecutionNode(
                id="b",
                mode="sequential",
                dependencies=["a"],
                execution_policy=ExecutionPolicy(),
                artifact_contract=b_contract,
                provider_records=[make_provider_record("alpha")],
            ),
        ],
        render_plans=[],
        static_resources=[],
        token_catalog=[],
        dependency_graph=[dependency_edge],
        runtime_config_snapshot={"schema_version": SCHEMA_VERSION},
        effective_runtime_config_signature=RUNTIME_SIGNATURE,
        fingerprint_metadata={"payload_version": "1"},
    )


def make_provider_record(task_id: str, role: str = "executor") -> ProviderRecord:
    return ProviderRecord(
        provider=task_id,
        role=role,
        task_id=task_id,
        agent_config_key=task_id,
        invoker_alias="mock",
        agent_config_signature=sha256_hex(f"{task_id}:agent"),
        invoker_config_signature=sha256_hex("mock:invoker"),
    )


def make_snapshot_workspace_plan() -> PreflightExecutionPlan:
    plan = make_plan()
    locator = make_workspace_file_locator()
    policy = WorkspaceSelectionRecord(
        enabled=True,
        logical_worktree_name="primary",
        declaration_kind="snapshot",
        source_kind="project",
        source_node_id=None,
        clean_start="strict",
        materialization="snapshot_checkout",
        worktree_contract=WORKTREE_CONTRACT,
        writable=True,
        lineage_producer=False,
    )
    node = plan.nodes[0].model_copy(
        update={
            "mode": "input",
            "dependencies": [],
            "provider_records": [],
            "workspace_policy": policy,
            "input_workspace_file_locator_id": locator.locator_id,
        }
    )
    return plan.model_copy(
        update={
            "execution_order": [node.id],
            "nodes": [node],
            "dependency_graph": [],
            "workspace_source": make_workspace_source_snapshot(),
            "workspace_file_locators": [locator],
        }
    )


def make_workspace_source_snapshot() -> WorkspaceSourceSnapshot:
    return WorkspaceSourceSnapshot(
        worktree_contract=WORKTREE_CONTRACT,
        run_base_commit=WORKSPACE_RUN_BASE_COMMIT,
        source_tree=WORKSPACE_SOURCE_TREE,
        object_format="sha1",
        repository_id=sha256_hex("repo"),
        git_version="2.34.1",
        git_top_level="/repo",
        project_root_relative_path=".",
        active_git_dir="/repo/.git",
        common_git_dir="/repo/.git",
        clean_start="strict",
    )


def make_workspace_file_locator() -> WorkspaceFileLocator:
    return WorkspaceFileLocator(
        locator_id="workspace-file-a",
        content_ref="workspace-files/workspace-file-a.txt",
        occurrence_id="a:input:file:docs/input.md",
        node_id="a",
        target="input_output",
        source_class="project_initial",
        raw_token="{{file:docs/input.md}}",
        raw_path="docs/input.md",
        source_root="/repo",
        source_root_relative_to_project=".",
        project_root_relative_to_git_top=".",
        git_top_relative_path="docs/input.md",
        workspace_relative_path="docs/input.md",
        git_blob=WORKSPACE_BLOB_ID,
        git_file_mode="100644",
        byte_size=len(b"workspace input"),
        canonical_blob_sha256=WORKSPACE_BLOB_SHA256,
        literal_path_verified=True,
        utf8_validated=True,
    )


def write_snapshot_workspace_state(
    run_dir: Path,
    manifest: RunManifest,
    plan: PreflightExecutionPlan,
    corrupted_injected_sha256: str | None = None,
) -> Path:
    node = plan.nodes[0]
    locator = plan.workspace_file_locators[0]
    source = plan.workspace_source
    assert source is not None
    injected_sha256 = corrupted_injected_sha256 or locator.canonical_blob_sha256
    payload = {
        "version": SCHEMA_VERSION,
        "run_id": manifest.run_id,
        "run_key_name": manifest.run_key_name,
        "workflow_name": plan.workflow_name,
        "workflow_signature": plan.workflow_signature,
        "node_id": node.id,
        "status": "succeeded",
        "workspace_kind": (
            node.workspace_policy.declaration_kind if node.workspace_policy else None
        ),
        "logical_worktree_name": (
            node.workspace_policy.logical_worktree_name
            if node.workspace_policy
            else None
        ),
        "clean_start": (
            node.workspace_policy.clean_start if node.workspace_policy else None
        ),
        "worktree_contract": WORKTREE_CONTRACT.model_dump(mode="json"),
        "git": {
            "object_format": source.object_format,
            "repo_id": source.repository_id,
            "run_base_commit": source.run_base_commit,
            "source_tree": source.source_tree,
            "worktree_config_active": False,
        },
        "source": {
            "kind": "project",
            "node_id": None,
            "commit": source.run_base_commit,
            "tree": source.source_tree,
            "candidate_sequence": None,
        },
        "workspace": {
            "path": None,
            "effective_cwd": None,
            "materialization": "snapshot_checkout",
            "writable": True,
            "lineage_producer": False,
            "retention": "not_applicable",
            "retained_reason": None,
            "project_root_relative_path": source.project_root_relative_path,
        },
        "result": {
            "lineage_produced": False,
            "snapshot_drift_discarded": False,
            "changed_path_count": 0,
            "changed_paths": [],
            "changed_paths_truncated": False,
        },
        "invocation_source": {
            "source_kind": "project",
            "source_node_id": None,
            "source_commit": source.run_base_commit,
            "source_tree": source.source_tree,
            "candidate_sequence": None,
        },
        "rendered_workspace_files": [
            {
                "occurrence_id": locator.occurrence_id,
                "invocation_id": f"{node.id}.input",
                "role": "input",
                "round_num": None,
                "source_kind": "project",
                "source_commit": source.run_base_commit,
                "source_tree": source.source_tree,
                "candidate_sequence": None,
                "workspace_relative_path": locator.workspace_relative_path,
                "git_blob": locator.git_blob,
                "git_file_mode": locator.git_file_mode,
                "byte_size": locator.byte_size,
                "canonical_blob_sha256": locator.canonical_blob_sha256,
                "injected_sha256": injected_sha256,
                "byte_source": "git_blob",
                "literal_path_verified": True,
                "utf8_validated": True,
                "target": "input_output",
            }
        ],
        "diagnostics": [],
    }
    stage_path = node.artifact_contract.stage_path
    assert stage_path is not None
    path = run_dir / stage_path / "workspace-state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    attach_workspace_descriptor(run_dir, plan, node.id)
    return path


def write_result(
    results_dir: Path, relative_path: str, content: str
) -> ArtifactDescriptor:
    path = results_dir / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return ArtifactDescriptor(
        kind="findings" if relative_path.endswith("-findings.md") else "output",
        relative_path=relative_path,
        sha256=sha256_hex(content),
        size_bytes=len(content.encode("utf-8")),
    )


def make_node_state(
    manifest: RunManifest,
    node_id: str,
    artifacts: list[ArtifactDescriptor],
) -> NodeState:
    return NodeState(
        run_state_schema_version=RUN_STATE_SCHEMA_VERSION,
        plan_schema_version=SCHEMA_VERSION,
        workflow_identity=manifest.workflow_identity,
        workflow_name=manifest.workflow_name,
        workflow_signature=manifest.workflow_signature,
        run_id=manifest.run_id,
        run_key_name=manifest.run_key_name,
        node_id=node_id,
        completed_at=iso_datetime(10),
        artifacts=artifacts,
    )


def write_node_state(run_dir: Path, node_state: NodeState) -> Path:
    from crewplane.artifacts.naming import build_node_state_filename

    path = (
        run_dir / "manifests" / "nodes" / build_node_state_filename(node_state.node_id)
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        node_state.model_dump_json(indent=2, exclude_none=True) + "\n",
        encoding="utf-8",
    )
    return path


def attach_workspace_descriptor(
    run_dir: Path,
    plan: PreflightExecutionPlan,
    node_id: str,
) -> None:
    node_state_path = _node_state_path(run_dir, node_id)
    if not node_state_path.is_file():
        return
    node_state = NodeState.model_validate_json(
        node_state_path.read_text(encoding="utf-8")
    )
    node = next(node for node in plan.nodes if node.id == node_id)
    workspace = build_node_workspace_descriptor(
        node,
        plan,
        _WorkspaceDescriptorStore(run_dir, node.artifact_contract.stage_path),
    )
    updated = node_state.model_copy(update={"workspace": workspace})
    node_state_path.write_text(
        updated.model_dump_json(indent=2, exclude_none=True) + "\n",
        encoding="utf-8",
    )


def _node_state_path(run_dir: Path, node_id: str) -> Path:
    from crewplane.artifacts.naming import build_node_state_filename

    return run_dir / "manifests" / "nodes" / build_node_state_filename(node_id)


class _WorkspaceDescriptorStore:
    def __init__(self, run_dir: Path, stage_path: str | None) -> None:
        self.run_id = "test-run"
        self.run_key_name = run_dir.name
        self.task_name = "test"
        self.stages_dir = run_dir
        self.results_dir = run_dir.parent.parent / "execution-results" / run_dir.name
        self.logs_dir = run_dir / "logs"
        self.project_root = run_dir
        self.log_cli_output = False
        self._stage_dir = run_dir / (stage_path or "")

    def get_stage_dir(self, stage_name: str) -> Path | None:
        del stage_name
        return self._stage_dir if self._stage_dir.is_dir() else None
