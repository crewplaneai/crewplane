from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

from orchestrator_cli.artifacts.run_history import (
    RunHistoryRecord,
    find_same_context_runs,
)
from orchestrator_cli.core.preflight.models import (
    PreflightExecutionPlan,
    ProviderRecord,
    WorkspaceSourceSnapshot,
)
from orchestrator_cli.core.preflight.workspace_observability import (
    invoker_workspace_descriptor,
)
from orchestrator_cli.core.workspace_git_policy import (
    deterministic_workspace_commit_environment,
)
from orchestrator_cli.core.workspace_policy import WorktreeContract
from orchestrator_cli.version import SCHEMA_VERSION
from tests.helpers.resume import (
    WORKFLOW_IDENTITY,
    WORKFLOW_NAME,
    WORKFLOW_SIGNATURE,
    WORKTREE_CONTRACT_PAYLOAD,
    make_run_manifest,
    write_run_manifest,
)


def source_record(tmp_path: Path, status: str = "failed") -> RunHistoryRecord:
    manifest = make_run_manifest("source", "workflow--source", status=status)
    write_run_manifest(tmp_path, manifest)
    return find_same_context_runs(
        tmp_path,
        WORKFLOW_IDENTITY,
        WORKFLOW_NAME,
        WORKFLOW_SIGNATURE,
    )[0]


def attach_git_workspace_source(
    tmp_path: Path,
    plan: PreflightExecutionPlan,
    object_format: str = "sha1",
) -> tuple[PreflightExecutionPlan, Path]:
    repo = tmp_path / "workspace-source-repo"
    repo.mkdir()
    init_args = (
        ("init", "-b", "main")
        if object_format == "sha1"
        else ("init", f"--object-format={object_format}", "-b", "main")
    )
    run_git_text(repo, *init_args)
    run_git_text(repo, "config", "user.name", "Orchestrator Test")
    run_git_text(repo, "config", "user.email", "orchestrator-test@example.invalid")
    (repo / "README.md").write_text("ready\n", encoding="utf-8")
    run_git_text(repo, "add", "README.md")
    run_git_text(repo, "commit", "-m", "initial")
    source = WorkspaceSourceSnapshot(
        worktree_contract=WorktreeContract(),
        run_base_commit=run_git_text(repo, "rev-parse", "HEAD^{commit}"),
        source_tree=run_git_text(repo, "rev-parse", "HEAD^{tree}"),
        object_format=run_git_text(repo, "rev-parse", "--show-object-format=storage"),
        repository_id=hashlib.sha256(repo.as_posix().encode("utf-8")).hexdigest(),
        git_version=run_git_text(repo, "--version"),
        git_top_level=repo.as_posix(),
        project_root_relative_path=".",
        active_git_dir=(repo / ".git").as_posix(),
        common_git_dir=(repo / ".git").as_posix(),
        clean_start="strict",
    )
    return plan.model_copy(
        update={
            "workspace_source": source,
            "runtime_config_snapshot": _runtime_config_snapshot(plan),
        }
    ), repo


def write_lineage_bundle_for_payload(
    repo: Path,
    source: RunHistoryRecord,
    payload: dict[str, object],
) -> dict[str, object]:
    source_descriptor = payload["source"]
    result_descriptor = payload["result"]
    refs = payload["refs"]
    assert isinstance(source_descriptor, dict)
    assert isinstance(result_descriptor, dict)
    assert isinstance(refs, dict)
    slug = _lineage_bundle_slug(payload)
    node_id = str(payload["node_id"])
    refs["candidate"] = _lineage_ref(
        source.manifest.run_key_name, node_id, slug, "candidate"
    )
    refs["result"] = _lineage_ref(source.manifest.run_key_name, node_id, slug, "result")
    result_tree = str(source_descriptor["tree"])
    result_commit = run_git_text_with_input(
        repo,
        ("commit-tree", result_tree, "-p", str(source_descriptor["commit"])),
        f"{node_id} {slug}\n",
        deterministic_workspace_commit_environment(),
    )
    result_descriptor["candidate_commit"] = result_commit
    result_descriptor["result_commit"] = result_commit
    result_descriptor["candidate_tree"] = result_tree
    result_descriptor["result_tree"] = result_tree
    result_ref = str(refs["result"])
    run_git_text(repo, "update-ref", result_ref, result_commit)
    bundle_path = source.run_dir / node_id / "workspace-bundles" / f"{slug}.bundle"
    bundle_path.parent.mkdir(parents=True, exist_ok=True)
    run_git_text(repo, "bundle", "create", bundle_path.as_posix(), result_ref)
    run_git_text(repo, "bundle", "verify", bundle_path.as_posix())
    bundle_bytes = bundle_path.read_bytes()
    payload["bundle"] = {
        "path": bundle_path.relative_to(source.run_dir).as_posix(),
        "sha256": hashlib.sha256(bundle_bytes).hexdigest(),
        "size_bytes": len(bundle_bytes),
        "verified": True,
    }
    return payload


def attach_source_bundle_descriptor(
    payload: dict[str, object],
    upstream_payload: dict[str, object],
) -> dict[str, object]:
    bundle = upstream_payload["bundle"]
    refs = upstream_payload["refs"]
    assert isinstance(bundle, dict)
    assert isinstance(refs, dict)
    source = payload["source"]
    invocation_source = payload["invocation_source"]
    assert isinstance(source, dict)
    assert isinstance(invocation_source, dict)
    source["bundle_path"] = bundle["path"]
    source["bundle_sha256"] = bundle["sha256"]
    source["bundle_size_bytes"] = bundle["size_bytes"]
    source["bundle_ref"] = refs["result"]
    invocation_source["source_bundle_path"] = bundle["path"]
    invocation_source["source_bundle_sha256"] = bundle["sha256"]
    invocation_source["source_bundle_size_bytes"] = bundle["size_bytes"]
    invocation_source["source_bundle_ref"] = refs["result"]
    return payload


def run_git_text(repo: Path, *args: str) -> str:
    return run_git_text_with_input(repo, args, None, None)


def run_git_text_with_input(
    repo: Path,
    args: tuple[str, ...],
    stdin: str | None,
    git_env: dict[str, str] | None,
) -> str:
    result = subprocess.run(
        ["git", "-C", repo.as_posix(), *args],
        check=True,
        capture_output=True,
        input=stdin.encode("utf-8") if stdin is not None else None,
        env=git_env,
    )
    return result.stdout.decode("utf-8").strip()


def write_stage_output_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("candidate\n", encoding="utf-8")


def write_review_status_file(stage_dir: Path, canonical_path: str) -> None:
    status_dir = stage_dir / "review-state"
    status_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "node_id": "a",
        "executed_audit_rounds": 1,
        "final_local_round_num": 2,
        "invalid_candidate_round_count": 0,
        "no_progress_round_count": 0,
        "artifact_drift_warning_count": 0,
        "consensus_reached": True,
        "continued_after_consensus_exhaustion": False,
        "canonical_executor_outputs": [
            {
                "task_id": "alpha",
                "provider": "codex",
                "role": "executor",
                "path": canonical_path,
            }
        ],
        "reviewer_outputs": [],
    }
    (status_dir / "review-loop-status.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )


def provider_workspace_state_payload(
    record,
    plan,
    source_commit: str,
    source_tree: str,
    node_id: str = "a",
    source_kind: str = "project",
    source_node_id: str | None = None,
    candidate_sequence: int | None = None,
    result_commit: str = "b" * 40,
    result_tree: str = "d" * 40,
) -> dict[str, object]:
    workspace_source = plan.workspace_source
    assert workspace_source is not None
    payload: dict[str, object] = {
        "version": SCHEMA_VERSION,
        "run_id": record.manifest.run_id,
        "run_key_name": record.manifest.run_key_name,
        "workflow_name": plan.workflow_name,
        "workflow_signature": plan.workflow_signature,
        "node_id": node_id,
        "task_id": "alpha",
        "role": "executor",
        "round_num": 1,
        "audit_round_num": None,
        "status": "succeeded",
        "workspace_kind": "worktree",
        "logical_worktree_name": "primary",
        "clean_start": "strict",
        "worktree_contract": WORKTREE_CONTRACT_PAYLOAD,
        "git": {
            "object_format": workspace_source.object_format,
            "repo_id": workspace_source.repository_id,
            "run_base_commit": workspace_source.run_base_commit,
            "source_tree": workspace_source.source_tree,
        },
        "source": {
            "kind": source_kind,
            "node_id": source_node_id,
            "commit": source_commit,
            "tree": source_tree,
            "candidate_sequence": candidate_sequence,
        },
        "invocation_source": {
            "source_kind": source_kind,
            "source_node_id": source_node_id,
            "source_commit": source_commit,
            "source_tree": source_tree,
            "candidate_sequence": candidate_sequence,
        },
        "workspace": {
            "path": None,
            "effective_cwd": None,
            "materialization": "worktree_checkout",
            "writable": True,
            "lineage_producer": True,
            "retention": "retained",
            "retained_reason": None,
            "project_root_relative_path": workspace_source.project_root_relative_path,
        },
        "result": {
            "candidate_commit": "a" * 40,
            "result_commit": result_commit,
            "candidate_tree": "c" * 40,
            "result_tree": result_tree,
            "changed_path_count": 1,
            "unreachable_provider_objects_scanned": False,
        },
        "refs": {
            "candidate": (
                "refs/orchestrator-cli/runs/"
                f"{record.manifest.run_key_name}/{node_id}/{node_id}/candidate"
            ),
            "result": (
                "refs/orchestrator-cli/runs/"
                f"{record.manifest.run_key_name}/{node_id}/{node_id}/result"
            ),
        },
        "bundle": {
            "path": f"{node_id}/workspace-bundles/{node_id}.bundle",
            "sha256": hashlib.sha256(b"bundle").hexdigest(),
            "size_bytes": len(b"bundle"),
            "verified": True,
        },
    }
    return _with_invoker(plan, payload)


def provider_record(task_id: str, role: str = "executor") -> ProviderRecord:
    return ProviderRecord(
        provider=task_id,
        role=role,
        task_id=task_id,
        agent_config_key=task_id,
        invoker_alias="mock",
        agent_config_signature=f"{task_id}-agent",
        invoker_config_signature="mock-invoker",
    )


def _lineage_bundle_slug(payload: dict[str, object]) -> str:
    audit_round_num = payload.get("audit_round_num")
    audit_part = f"audit{audit_round_num}" if audit_round_num is not None else "audit0"
    return (
        f"{payload.get('task_id')}-{payload.get('role')}-"
        f"{audit_part}-round{payload.get('round_num')}"
    )


def _lineage_ref(
    run_key_name: str,
    node_id: str,
    slug: str,
    kind: str,
) -> str:
    return f"refs/orchestrator-cli/runs/{run_key_name}/{node_id}/{slug}/{kind}"


def snapshot_workspace_state_payload(
    record,
    plan,
    task_id: str,
) -> dict[str, object]:
    workspace_source = plan.workspace_source
    assert workspace_source is not None
    payload: dict[str, object] = {
        "version": SCHEMA_VERSION,
        "run_id": record.manifest.run_id,
        "run_key_name": record.manifest.run_key_name,
        "workflow_name": plan.workflow_name,
        "workflow_signature": plan.workflow_signature,
        "node_id": "a",
        "task_id": task_id,
        "provider": task_id,
        "role": "executor",
        "round_num": 1,
        "audit_round_num": None,
        "status": "succeeded",
        "workspace_kind": "snapshot",
        "logical_worktree_name": "primary",
        "clean_start": "strict",
        "worktree_contract": WORKTREE_CONTRACT_PAYLOAD,
        "git": {
            "object_format": workspace_source.object_format,
            "repo_id": workspace_source.repository_id,
            "run_base_commit": workspace_source.run_base_commit,
            "source_tree": workspace_source.source_tree,
        },
        "source": {
            "kind": "project",
            "node_id": None,
            "commit": workspace_source.run_base_commit,
            "tree": workspace_source.source_tree,
            "candidate_sequence": None,
        },
        "invocation_source": {
            "source_kind": "project",
            "source_node_id": None,
            "source_commit": workspace_source.run_base_commit,
            "source_tree": workspace_source.source_tree,
            "candidate_sequence": None,
        },
        "workspace": {
            "path": None,
            "effective_cwd": None,
            "materialization": "snapshot_checkout",
            "writable": True,
            "lineage_producer": False,
            "retention": "retained",
            "retained_reason": None,
            "project_root_relative_path": workspace_source.project_root_relative_path,
        },
        "result": {
            "lineage_produced": False,
            "snapshot_drift_discarded": False,
            "changed_path_count": 0,
            "changed_paths": [],
            "changed_paths_truncated": False,
        },
    }
    return _with_invoker(plan, payload)


def _runtime_config_snapshot(plan: PreflightExecutionPlan) -> dict[str, object]:
    if invoker_workspace_descriptor(plan.runtime_config_snapshot) is not None:
        return plan.runtime_config_snapshot
    return {
        "schema_version": SCHEMA_VERSION,
        "invoker": {
            "implementation": "mock",
            "capabilities": {
                "workspace": {
                    "honors_cwd": True,
                    "launch_mode": "mock_no_child_process",
                    "controlled_child_environment": False,
                }
            },
        },
    }


def _with_invoker(
    plan: PreflightExecutionPlan,
    payload: dict[str, object],
) -> dict[str, object]:
    invoker = invoker_workspace_descriptor(plan.runtime_config_snapshot)
    if invoker is not None:
        payload["invoker"] = invoker
    return payload


def write_review_status_with_reviewer(stage_dir: Path) -> None:
    status_dir = stage_dir / "review-state"
    status_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "node_id": "a",
        "executed_audit_rounds": 1,
        "final_local_round_num": 1,
        "invalid_candidate_round_count": 0,
        "no_progress_round_count": 0,
        "artifact_drift_warning_count": 0,
        "consensus_reached": True,
        "continued_after_consensus_exhaustion": False,
        "canonical_executor_outputs": [
            {
                "task_id": "alpha",
                "provider": "alpha",
                "role": "executor",
                "path": "alpha_round1.md",
            }
        ],
        "reviewer_outputs": [
            {
                "task_id": "beta",
                "provider": "beta",
                "role": "reviewer",
                "path": "beta_round1.md",
            }
        ],
    }
    (status_dir / "review-loop-status.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )
