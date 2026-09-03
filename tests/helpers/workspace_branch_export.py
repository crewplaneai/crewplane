from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from crewplane.artifacts import OutputManager
from crewplane.artifacts.run_history import RunHistoryRecord
from crewplane.artifacts.workspace.node_state import (
    build_node_workspace_descriptor,
)
from crewplane.core.execution_state import (
    RUN_STATE_SCHEMA_VERSION,
    NodeState,
    RunManifest,
)
from crewplane.core.preflight.models import (
    PreflightExecutionPlan,
    WorkspaceBranchExportRecord,
)
from crewplane.core.workspace.invocation_identity import invocation_slug
from crewplane.runtime.workspace.worktree.types import WorktreeSourceRef
from crewplane.version import SCHEMA_VERSION
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
    result_ref = _result_ref(stage_dir)
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
    run_git_text(producer, "config", "user.name", "Crewplane Test")
    run_git_text(producer, "config", "user.email", "crewplane-test@example.invalid")
    (producer / "README.md").write_text(content, encoding="utf-8")
    run_git_text(producer, "add", "README.md")
    run_git_text(producer, "commit", "-m", "workspace result")
    result_commit = run_git_text(producer, "rev-parse", "HEAD^{commit}")
    result_tree = run_git_text(producer, "rev-parse", "HEAD^{tree}")
    result_ref = _result_ref(stage_dir)
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
    result_ref = _result_ref(stage_dir)
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
    source = plan.workspace_source
    assert source is not None
    run_key_name = stages_dir.name
    _workflow_hash, separator, run_id = run_key_name.rsplit("--", 1)[-1].partition("-")
    assert separator and run_id
    project_source_ref = WorktreeSourceRef(
        source_kind="project",
        source_node_id=None,
        source_commit=source.run_base_commit,
        source_tree=source.source_tree,
        candidate_sequence=None,
    )
    invocation_source = source_ref or project_source_ref
    if source_ref is not None and not source_ref.upstream_sources:
        invocation_source = replace(
            source_ref,
            upstream_sources=(project_source_ref,),
        )
    ref_base = result_ref.removesuffix("/result")
    candidate_ref = f"{ref_base}/candidate"
    payload = {
        "version": SCHEMA_VERSION,
        "run_id": run_id,
        "run_key_name": run_key_name,
        "workflow_name": plan.workflow_name,
        "workflow_signature": plan.workflow_signature,
        "node_id": node.id,
        "task_id": "alpha",
        "provider": "alpha",
        "status": "succeeded",
        "role": "executor",
        "round_num": 1,
        "audit_round_num": None,
        "workspace_kind": "worktree",
        "logical_worktree_name": policy.logical_worktree_name,
        "clean_start": policy.clean_start,
        "worktree_contract": policy.worktree_contract.model_dump(mode="json"),
        "git": {
            "object_format": source.object_format,
            "repo_id": source.repository_id,
            "run_base_commit": source.run_base_commit,
            "source_tree": source.source_tree,
            "git_top_level": source.git_top_level,
            "active_git_dir": source.active_git_dir,
            "common_git_dir": source.common_git_dir,
        },
        "source": _source_payload(stages_dir, invocation_source),
        "workspace": {
            "path": None,
            "effective_cwd": None,
            "cache_key": "primary",
            "materialization": "worktree_checkout",
            "writable": True,
            "lineage_producer": True,
            "retention": "retained",
            "retained_reason": None,
            "project_root_relative_path": source.project_root_relative_path,
            "reuse_generation": 1,
        },
        "execution": {
            "cache_root": None,
            "workspace_path": (stages_dir / "workspace").as_posix(),
            "checkout_root": (stages_dir / "workspace").as_posix(),
            "checkout_size_bytes": 0,
            "effective_cwd": (stages_dir / "workspace").as_posix(),
            "provisioning_duration_seconds": 0.0,
            "worktree_git_dir": f"{source.common_git_dir}/worktrees/test",
        },
        "invocation_source": _invocation_source_payload(
            stages_dir,
            invocation_source,
        ),
        "child_process_environment": {"required": True, "applied": True},
        "invoker": {"implementation": "mock"},
        "rendered_workspace_files": [],
        "diagnostics": [],
        "process_drain": {"status": "confirmed"},
        "result": {
            "candidate_commit": result_commit,
            "result_commit": result_commit,
            "candidate_tree": result_tree,
            "result_tree": result_tree,
            "changed_path_count": 1,
            "unreachable_object_inclusion": False,
        },
        "refs": {"candidate": candidate_ref, "result": result_ref},
        "ref_publication": {
            "phase": "published",
            "repository_id": source.repository_id,
            "run_id": run_id,
            "run_key_name": run_key_name,
            "node_id": node.id,
            "task_id": "alpha",
            "role": "executor",
            "round_num": 1,
            "audit_round_num": None,
            "destinations": {
                "candidate": {
                    "name": candidate_ref,
                    "target_oid": result_commit,
                    "expected_old_oid": None,
                },
                "result": {
                    "name": result_ref,
                    "target_oid": result_commit,
                    "expected_old_oid": None,
                },
            },
        },
        "bundle": {
            "path": bundle_path.relative_to(stages_dir).as_posix(),
            "sha256": hashlib.sha256(bundle_path.read_bytes()).hexdigest(),
            "size_bytes": bundle_path.stat().st_size,
            "verified": True,
        },
    }
    state_path = stages_dir / "implement" / "workspace-state.json"
    state_path.write_text(json.dumps(payload), encoding="utf-8")
    return state_path


def _result_ref(stage_dir: Path) -> str:
    run_key_name = stage_dir.parent.name
    node_id = stage_dir.name
    slug = invocation_slug(node_id, "alpha", None, 1)
    return f"refs/crewplane/runs/{run_key_name}/{node_id}/{slug}/result"


def write_node_manifest(output: OutputManager, plan: PreflightExecutionPlan) -> Path:
    node = plan.nodes[0]
    workspace = build_node_workspace_descriptor(node, plan, output)
    return output.write_node_success_state(
        NodeState(
            run_state_schema_version=RUN_STATE_SCHEMA_VERSION,
            plan_schema_version=SCHEMA_VERSION,
            workflow_identity=".crewplane/workflows/workspace.task.md",
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
        workflow_identity=".crewplane/workflows/workspace.task.md",
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
