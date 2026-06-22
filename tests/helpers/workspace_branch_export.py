from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime
from pathlib import Path

from orchestrator_cli.artifacts import OutputManager
from orchestrator_cli.artifacts.run_history import RunHistoryRecord
from orchestrator_cli.artifacts.workspace.node_state import (
    build_node_workspace_descriptor,
)
from orchestrator_cli.core.execution_state import (
    RUN_STATE_SCHEMA_VERSION,
    NodeState,
    RunManifest,
)
from orchestrator_cli.core.preflight.models import (
    PreflightExecutionPlan,
    WorkspaceBranchExportRecord,
)
from orchestrator_cli.runtime.workspace.worktree.types import WorktreeSourceRef
from orchestrator_cli.version import SCHEMA_VERSION
from tests.helpers.workspace_service import run_git_text, workspace_plan


def branch_export_plan(
    repo: Path,
    tmp_path: Path,
    branch_name: str | None,
    create_branch: bool = True,
) -> PreflightExecutionPlan:
    plan = workspace_plan(
        repo,
        tmp_path / "cache",
        cleanup_on_success=False,
        kind="worktree",
    )
    node = plan.nodes[0]
    policy = node.workspace_policy
    assert policy is not None
    node = node.model_copy(
        update={
            "workspace_policy": policy.model_copy(
                update={
                    "branch_export": WorkspaceBranchExportRecord(
                        create_branch=create_branch,
                        branch_name=branch_name,
                    )
                }
            )
        }
    )
    return plan.model_copy(update={"nodes": [node]})


def write_result_bundle(
    repo: Path,
    stage_dir: Path,
    content: str,
) -> tuple[str, str, str, Path]:
    base_commit = run_git_text(repo, "rev-parse", "HEAD^{commit}")
    (repo / "README.md").write_text(content, encoding="utf-8")
    run_git_text(repo, "add", "README.md")
    result_tree = run_git_text(repo, "write-tree")
    result_commit = run_git_text(
        repo,
        "commit-tree",
        result_tree,
        "-p",
        base_commit,
        "-m",
        "workspace result",
    )
    result_ref = "refs/orchestrator-cli/tests/branch-export/result"
    run_git_text(repo, "update-ref", result_ref, result_commit)
    bundle_dir = stage_dir / "workspace-bundles"
    bundle_dir.mkdir()
    bundle_path = bundle_dir / "alpha.bundle"
    run_git_text(repo, "bundle", "create", bundle_path.as_posix(), result_ref)
    return result_commit, result_tree, result_ref, bundle_path


def write_result_bundle_from_clone(
    repo: Path,
    tmp_path: Path,
    stage_dir: Path,
    content: str,
) -> tuple[str, str, str, Path]:
    producer = tmp_path / "producer"
    subprocess.run(
        ["git", "clone", repo.as_posix(), producer.as_posix()],
        check=True,
        capture_output=True,
    )
    run_git_text(producer, "config", "user.name", "Orchestrator Test")
    run_git_text(producer, "config", "user.email", "orchestrator-test@example.invalid")
    (producer / "README.md").write_text(content, encoding="utf-8")
    run_git_text(producer, "add", "README.md")
    run_git_text(producer, "commit", "-m", "workspace result")
    result_commit = run_git_text(producer, "rev-parse", "HEAD^{commit}")
    result_tree = run_git_text(producer, "rev-parse", "HEAD^{tree}")
    result_ref = "refs/orchestrator-cli/tests/branch-export/result"
    run_git_text(producer, "update-ref", result_ref, result_commit)
    bundle_dir = stage_dir / "workspace-bundles"
    bundle_dir.mkdir()
    bundle_path = bundle_dir / "alpha.bundle"
    run_git_text(producer, "bundle", "create", bundle_path.as_posix(), result_ref)
    return result_commit, result_tree, result_ref, bundle_path


def write_tree_bundle(
    repo: Path,
    stage_dir: Path,
) -> tuple[str, str, Path]:
    result_tree = run_git_text(repo, "rev-parse", "HEAD^{tree}")
    result_ref = "refs/orchestrator-cli/tests/branch-export/tree-result"
    run_git_text(repo, "update-ref", result_ref, result_tree)
    bundle_dir = stage_dir / "workspace-bundles"
    bundle_dir.mkdir()
    bundle_path = bundle_dir / "tree.bundle"
    run_git_text(repo, "bundle", "create", bundle_path.as_posix(), result_ref)
    return result_tree, result_ref, bundle_path


def write_workspace_state(
    stages_dir: Path,
    plan: PreflightExecutionPlan,
    result_commit: str,
    result_tree: str,
    result_ref: str,
    bundle_path: Path,
    source_ref: WorktreeSourceRef | None = None,
) -> Path:
    node = plan.nodes[0]
    policy = node.workspace_policy
    assert policy is not None
    payload = {
        "version": SCHEMA_VERSION,
        "workflow_name": plan.workflow_name,
        "workflow_signature": plan.workflow_signature,
        "node_id": node.id,
        "task_id": "alpha",
        "status": "succeeded",
        "role": "executor",
        "workspace_kind": "worktree",
        "logical_worktree_name": policy.logical_worktree_name,
        "worktree_contract": policy.worktree_contract.model_dump(mode="json"),
        "workspace": {
            "materialization": "worktree_checkout",
            "lineage_producer": True,
        },
        "result": {
            "result_commit": result_commit,
            "result_tree": result_tree,
        },
        "refs": {"result": result_ref},
        "bundle": {
            "path": bundle_path.relative_to(stages_dir).as_posix(),
            "sha256": hashlib.sha256(bundle_path.read_bytes()).hexdigest(),
            "size_bytes": bundle_path.stat().st_size,
            "verified": True,
        },
    }
    if source_ref is not None:
        payload["source"] = _source_payload(stages_dir, source_ref)
        payload["invocation_source"] = _invocation_source_payload(
            stages_dir,
            source_ref,
        )
    state_path = stages_dir / "implement" / "workspace-state.json"
    state_path.write_text(json.dumps(payload), encoding="utf-8")
    return state_path


def write_node_manifest(output: OutputManager, plan: PreflightExecutionPlan) -> Path:
    node = plan.nodes[0]
    workspace = build_node_workspace_descriptor(node, plan, output)
    return output.write_node_success_state(
        NodeState(
            run_state_schema_version=RUN_STATE_SCHEMA_VERSION,
            plan_schema_version=SCHEMA_VERSION,
            workflow_identity=".orchestrator/workflows/workspace.task.md",
            workflow_name=plan.workflow_name,
            workflow_signature="a" * 64,
            run_id=output.run_id,
            run_key_name=output.run_key_name,
            node_id=node.id,
            completed_at=datetime(2026, 6, 16, 12, 0).isoformat(),
            artifacts=[],
            workspace=workspace,
        )
    )


def update_state_bundle_metadata(
    state_path: Path,
    metadata_override: dict[str, object],
) -> None:
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    bundle = payload["bundle"]
    assert isinstance(bundle, dict)
    bundle.update(metadata_override)
    state_path.write_text(json.dumps(payload), encoding="utf-8")


def _source_payload(
    stages_dir: Path,
    source_ref: WorktreeSourceRef,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "kind": source_ref.source_kind,
        "node_id": source_ref.source_node_id,
        "commit": source_ref.source_commit,
        "tree": source_ref.source_tree,
        "candidate_sequence": source_ref.candidate_sequence,
    }
    if source_ref.bundle_path is not None:
        payload["bundle_path"] = source_ref.bundle_path.relative_to(
            stages_dir
        ).as_posix()
    if source_ref.bundle_sha256 is not None:
        payload["bundle_sha256"] = source_ref.bundle_sha256
    if source_ref.bundle_size_bytes is not None:
        payload["bundle_size_bytes"] = source_ref.bundle_size_bytes
    if source_ref.bundle_ref is not None:
        payload["bundle_ref"] = source_ref.bundle_ref
    if source_ref.upstream_sources:
        payload["upstream_sources"] = [
            _source_payload(stages_dir, upstream)
            for upstream in source_ref.upstream_sources
        ]
    return payload


def _invocation_source_payload(
    stages_dir: Path,
    source_ref: WorktreeSourceRef,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "source_kind": source_ref.source_kind,
        "source_node_id": source_ref.source_node_id,
        "source_commit": source_ref.source_commit,
        "source_tree": source_ref.source_tree,
        "candidate_sequence": source_ref.candidate_sequence,
    }
    source_payload = _source_payload(stages_dir, source_ref)
    for key in ("bundle_path", "bundle_sha256", "bundle_size_bytes", "bundle_ref"):
        if key in source_payload:
            payload[f"source_{key}"] = source_payload[key]
    return payload


def history_record_for_output(output: OutputManager) -> RunHistoryRecord:
    manifest = RunManifest(
        run_state_schema_version=RUN_STATE_SCHEMA_VERSION,
        plan_schema_version=SCHEMA_VERSION,
        workflow_identity=".orchestrator/workflows/workspace.task.md",
        workflow_name="workspace",
        workflow_signature="a" * 64,
        run_id=output.run_id,
        run_key_name=output.run_key_name,
        started_at=datetime(2026, 6, 16, 12, 0).isoformat(),
        completed_at=datetime(2026, 6, 16, 12, 1).isoformat(),
        status="succeeded",
        effective_runtime_config_signature="b" * 64,
        preflight_plan_path="preflight/execution-plan.json",
        preflight_manifest_path="preflight/manifest.json",
        runtime_config_snapshot_path="preflight/runtime-config-snapshot.json",
        runtime_config_snapshot={"schema_version": SCHEMA_VERSION},
        workflow_source="workflow source",
        composed_workflow={"schema_version": SCHEMA_VERSION, "name": "workspace"},
    )
    manifest_path = output.stages_dir / "manifests" / "run.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        manifest.model_dump_json(exclude_none=True),
        encoding="utf-8",
    )
    return RunHistoryRecord(
        manifest=manifest,
        manifest_path=manifest_path,
        run_dir=output.stages_dir,
        results_dir=output.results_dir,
    )
