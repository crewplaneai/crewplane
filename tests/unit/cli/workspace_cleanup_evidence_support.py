from __future__ import annotations

import json
from pathlib import Path

from crewplane.cli.workspace_cleanup_evidence import WorkspaceCleanupEvidence
from crewplane.core.preflight.models import PreflightExecutionPlan
from crewplane.version import SCHEMA_VERSION
from tests.helpers.resume import make_run_manifest
from tests.helpers.workspace_records import (
    WORKTREE_CONTRACT_PAYLOAD,
    workspace_selection_record,
)
from tests.helpers.workspace_service import disabled_workspace_plan

REPOSITORY_ID = "repo"


RUN_KEY = "run-key"


OID_A = "a" * 40


OID_B = "b" * 40


def collect_cleanup_evidence(
    stage_root: Path, cache_root: Path
) -> WorkspaceCleanupEvidence:
    return WorkspaceCleanupEvidence(
        stage_root,
        cache_root,
        REPOSITORY_ID,
        Path("/repo/.git"),
    )


def snapshot_cache_path(
    cache_root: Path,
    run_key: str = RUN_KEY,
    cache_key: str = "snapshot",
) -> Path:
    return cache_root / "snapshots" / REPOSITORY_ID / run_key / cache_key


def snapshot_claim_payload(
    workspace_path: Path,
    run_key: str = RUN_KEY,
    cache_root: Path | None = None,
) -> dict[str, object]:
    return base_claim_payload(
        workspace_path,
        run_key,
        workspace_kind="snapshot",
        materialization="snapshot_checkout",
        generation=None,
        cache_root=cache_root,
    )


def worktree_claim_payload(
    workspace_path: Path,
    run_key: str = RUN_KEY,
    generation: int | None = 1,
) -> dict[str, object]:
    return base_claim_payload(
        workspace_path,
        run_key,
        workspace_kind="worktree",
        materialization="worktree_checkout",
        generation=generation,
    )


def base_claim_payload(
    workspace_path: Path,
    run_key: str,
    workspace_kind: str,
    materialization: str,
    generation: int | None,
    cache_root: Path | None = None,
) -> dict[str, object]:
    workspace: dict[str, object] = {
        "path": workspace_path.as_posix(),
        "effective_cwd": None,
        "cache_key": workspace_path.name,
        "materialization": materialization,
        "writable": True,
        "lineage_producer": False,
        "retention": "pending_cleanup",
        "retained_reason": None,
        "project_root_relative_path": ".",
    }
    if generation is not None:
        workspace["reuse_generation"] = generation
    result = (
        {
            "drift_scan_complete": True,
            "snapshot_drift_discarded": False,
            "changed_path_count": 0,
            "changed_paths": [],
            "changed_paths_truncated": False,
        }
        if workspace_kind == "snapshot"
        else {"lineage_produced": False}
    )
    return {
        "version": SCHEMA_VERSION,
        "run_id": f"{run_key}-id",
        "run_key_name": run_key,
        "workflow_name": "workflow",
        "workflow_signature": "e" * 64,
        "node_id": "build",
        "task_id": "alpha",
        "provider": "mock",
        "role": "reviewer" if workspace_kind == "worktree" else "executor",
        "round_num": 1,
        "audit_round_num": None,
        "status": "succeeded",
        "workspace_kind": workspace_kind,
        "logical_worktree_name": "primary",
        "clean_start": "strict",
        "worktree_contract": WORKTREE_CONTRACT_PAYLOAD,
        "git": {
            "object_format": "sha1",
            "repo_id": REPOSITORY_ID,
            "run_base_commit": OID_A,
            "source_tree": OID_B,
            "git_top_level": "/repo",
            "active_git_dir": "/repo/.git",
            "common_git_dir": "/repo/.git",
        },
        "source": {
            "kind": "project",
            "node_id": None,
            "commit": OID_A,
            "tree": OID_B,
            "candidate_sequence": None,
        },
        "invocation_source": {
            "source_kind": "project",
            "source_node_id": None,
            "source_commit": OID_A,
            "source_tree": OID_B,
            "candidate_sequence": None,
        },
        "workspace": workspace,
        "execution": {
            "cache_root": (cache_root or claim_cache_root(workspace_path)).as_posix(),
            "workspace_path": workspace_path.as_posix(),
            "checkout_root": (workspace_path / "checkout").as_posix(),
            "effective_cwd": None,
            "worktree_git_dir": (
                "/repo/.git/worktrees/workspace"
                if workspace_kind == "worktree"
                else None
            ),
        },
        "process_drain": {"status": "confirmed"},
        "result": result,
    }


def claim_cache_root(workspace_path: Path) -> Path:
    for parent in workspace_path.parents:
        if parent.name in {"snapshots", "workspaces", "review-workspaces"}:
            return parent.parent
    return workspace_path.parents[3]


def write_cleanup_claim(
    stage_root: Path,
    run_key: str,
    payload: object,
    filename: str = "workspace-state.json",
) -> Path:
    workspace_kind = (
        payload.get("workspace_kind") if isinstance(payload, dict) else None
    )
    write_cleanup_evidence_plan(
        stage_root,
        run_key,
        workspace_kind=(
            workspace_kind if workspace_kind in {"snapshot", "worktree"} else "snapshot"
        ),
    )
    path = stage_root / run_key / "node" / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def write_cleanup_evidence_plan(
    stage_root: Path,
    run_key: str,
    node_id: str = "build",
    stage_path: str = "node",
    workspace_kind: str = "snapshot",
) -> None:
    run_dir = stage_root / run_key
    manifest = make_run_manifest(
        run_id=f"{run_key}-id",
        run_key_name=run_key,
        status="succeeded",
        workflow_name="workflow",
        workflow_signature="e" * 64,
    )
    payload = disabled_workspace_plan(stage_root.parent).model_dump(mode="json")
    payload.update(
        {
            "run_id": manifest.run_id,
            "run_key_name": run_key,
            "project_root": stage_root.parent.as_posix(),
            "context_root": run_dir.as_posix(),
            "manifest_root": (run_dir / "manifests").as_posix(),
            "workflow_name": manifest.workflow_name,
            "workflow_signature": manifest.workflow_signature,
            "effective_runtime_config_signature": (
                manifest.effective_runtime_config_signature
            ),
            "execution_order": [node_id],
        }
    )
    node = payload["nodes"][0]
    assert isinstance(node, dict)
    node["id"] = node_id
    node["render_plan_id"] = node_id
    node["workspace_policy"] = workspace_selection_record(
        kind=workspace_kind,
        lineage_producer=workspace_kind == "worktree",
    ).model_dump(mode="json")
    contract = node["artifact_contract"]
    assert isinstance(contract, dict)
    contract["stage_path"] = stage_path
    contract["output_path"] = f"{stage_path}/output.md"
    contract["log_path"] = f"{stage_path}/logs"
    contract["result_path"] = f"{stage_path}/output.md"
    render_plan = payload["render_plans"][0]
    assert isinstance(render_plan, dict)
    render_plan["render_plan_id"] = node_id
    render_plan["node_id"] = node_id
    plan = PreflightExecutionPlan.model_validate(payload)
    path = run_dir / "preflight" / "execution-plan.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(plan.model_dump_json(), encoding="utf-8")
    manifest_path = run_dir / "manifests" / "run.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        manifest.model_dump_json(exclude_none=True),
        encoding="utf-8",
    )
