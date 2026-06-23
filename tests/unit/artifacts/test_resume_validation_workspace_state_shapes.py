from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from crewplane.artifacts.failure_artifacts import (
    build_invocation_failure_artifact,
)
from crewplane.artifacts.naming import build_node_state_filename
from crewplane.artifacts.resume.validation import validate_resume_frontier
from crewplane.artifacts.run_history import RunHistoryRecord
from crewplane.core.preflight.models import PreflightExecutionPlan
from crewplane.core.workflow.keywords import ProviderRole
from tests.helpers.resume import (
    attach_workspace_descriptor,
    make_node_state,
    make_plan,
    write_node_state,
    write_result,
)
from tests.helpers.resume_validation import (
    attach_git_workspace_source,
    attach_source_bundle_descriptor,
    provider_record,
    provider_workspace_state_payload,
    run_git_text,
    snapshot_workspace_state_payload,
    source_record,
    write_lineage_bundle_for_payload,
    write_review_status_with_reviewer,
    write_stage_output_file,
)
from tests.helpers.workspace_records import workspace_selection_record


def test_validate_frontier_accepts_clean_snapshot_provider_state(
    tmp_path,
) -> None:
    source = source_record(tmp_path)
    plan = make_plan()
    policy = workspace_selection_record(
        enabled=True,
        kind="snapshot",
        clean_start="strict",
        materialization="snapshot_checkout",
    )
    node = plan.nodes[0].model_copy(update={"workspace_policy": policy})
    plan = plan.model_copy(
        update={
            "nodes": [node, plan.nodes[1]],
        }
    )
    plan, _repo = attach_git_workspace_source(tmp_path, plan)
    descriptor = write_result(source.results_dir, "a-result.md", "a output")
    write_node_state(
        source.run_dir,
        make_node_state(source.manifest, "a", [descriptor]),
    )
    state_path = source.run_dir / "a" / "workspace-state.json"
    state_path.parent.mkdir(exist_ok=True)
    state_path.write_text(
        json.dumps(snapshot_workspace_state_payload(source, plan, "alpha")),
        encoding="utf-8",
    )
    attach_workspace_descriptor(source.run_dir, plan, "a")

    frontier = validate_resume_frontier(source, plan)

    assert frontier.resumed_node_ids == ("a",)


def test_validate_frontier_rejects_wrong_workspace_state_artifact_digest(
    tmp_path,
) -> None:
    source = source_record(tmp_path)
    plan = make_plan()
    policy = workspace_selection_record(
        enabled=True,
        kind="snapshot",
        clean_start="strict",
        materialization="snapshot_checkout",
    )
    node = plan.nodes[0].model_copy(update={"workspace_policy": policy})
    plan = plan.model_copy(update={"nodes": [node, plan.nodes[1]]})
    plan, _repo = attach_git_workspace_source(tmp_path, plan)
    descriptor = write_result(source.results_dir, "a-result.md", "a output")
    write_node_state(
        source.run_dir,
        make_node_state(source.manifest, "a", [descriptor]),
    )
    state_path = source.run_dir / "a" / "workspace-state.json"
    state_path.parent.mkdir(exist_ok=True)
    state_path.write_text(
        json.dumps(snapshot_workspace_state_payload(source, plan, "alpha")),
        encoding="utf-8",
    )
    attach_workspace_descriptor(source.run_dir, plan, "a")
    node_state_path = (
        source.run_dir / "manifests" / "nodes" / build_node_state_filename("a")
    )
    node_state_payload = json.loads(node_state_path.read_text(encoding="utf-8"))
    workspace_artifact = node_state_payload["workspace"]["states"][0][
        "workspace_state_artifact"
    ]
    workspace_artifact["resume_sha256"] = "f" * 64
    node_state_path.write_text(json.dumps(node_state_payload), encoding="utf-8")

    frontier = validate_resume_frontier(source, plan)

    assert frontier.resumed_node_ids == ()


def test_validate_frontier_rejects_snapshot_state_without_drift_result(
    tmp_path,
) -> None:
    source = source_record(tmp_path)
    plan = make_plan()
    policy = workspace_selection_record(
        enabled=True,
        kind="snapshot",
        clean_start="strict",
        materialization="snapshot_checkout",
    )
    node = plan.nodes[0].model_copy(update={"workspace_policy": policy})
    plan = plan.model_copy(
        update={
            "nodes": [node, plan.nodes[1]],
        }
    )
    plan, _repo = attach_git_workspace_source(tmp_path, plan)
    descriptor = write_result(source.results_dir, "a-result.md", "a output")
    write_node_state(
        source.run_dir,
        make_node_state(source.manifest, "a", [descriptor]),
    )
    payload = snapshot_workspace_state_payload(source, plan, "alpha")
    result = payload["result"]
    assert isinstance(result, dict)
    del result["lineage_produced"]
    state_path = source.run_dir / "a" / "workspace-state.json"
    state_path.parent.mkdir(exist_ok=True)
    state_path.write_text(json.dumps(payload), encoding="utf-8")
    attach_workspace_descriptor(source.run_dir, plan, "a")

    frontier = validate_resume_frontier(source, plan)

    assert frontier.resumed_node_ids == ()


def test_validate_frontier_rejects_missing_snapshot_provider_state(
    tmp_path,
) -> None:
    source = source_record(tmp_path)
    plan = make_plan()
    policy = workspace_selection_record(
        enabled=True,
        kind="snapshot",
        clean_start="strict",
        materialization="snapshot_checkout",
    )
    node = plan.nodes[0].model_copy(
        update={
            "mode": "parallel",
            "provider_records": [
                provider_record("alpha"),
                provider_record("beta"),
            ],
            "workspace_policy": policy,
        }
    )
    plan = plan.model_copy(
        update={
            "nodes": [node, plan.nodes[1]],
        }
    )
    plan, _repo = attach_git_workspace_source(tmp_path, plan)
    descriptor = write_result(source.results_dir, "a-result.md", "a output")
    write_node_state(
        source.run_dir,
        make_node_state(source.manifest, "a", [descriptor]),
    )
    state_path = source.run_dir / "a" / "workspace-state-alpha.json"
    state_path.parent.mkdir(exist_ok=True)
    state_path.write_text(
        json.dumps(snapshot_workspace_state_payload(source, plan, "alpha")),
        encoding="utf-8",
    )

    frontier = validate_resume_frontier(source, plan)

    assert frontier.resumed_node_ids == ()


def test_validate_frontier_accepts_all_failed_snapshot_workspace_invocations(
    tmp_path,
) -> None:
    source = source_record(tmp_path)
    plan = make_plan()
    policy = workspace_selection_record(
        enabled=True,
        kind="snapshot",
        clean_start="strict",
        materialization="snapshot_checkout",
    )
    node = plan.nodes[0].model_copy(
        update={
            "mode": "parallel",
            "provider_records": [
                provider_record("alpha"),
                provider_record("beta"),
            ],
            "workspace_policy": policy,
        }
    )
    plan = plan.model_copy(
        update={
            "nodes": [node, plan.nodes[1]],
        }
    )
    plan, _repo = attach_git_workspace_source(tmp_path, plan)
    descriptor = write_result(source.results_dir, "a-result.md", "a output")
    write_node_state(
        source.run_dir,
        make_node_state(source.manifest, "a", [descriptor]),
    )
    stage_dir = source.run_dir / "a"
    stage_dir.mkdir(parents=True, exist_ok=True)
    for task_id in ("alpha", "beta"):
        (stage_dir / f"{task_id}_round1.md").write_text(
            build_invocation_failure_artifact(
                provider=task_id,
                task_id=task_id,
                error="quota exhausted",
            ),
            encoding="utf-8",
        )
        payload = snapshot_workspace_state_payload(source, plan, task_id)
        payload["status"] = "failed"
        payload.pop("result")
        (stage_dir / f"workspace-state-{task_id}.json").write_text(
            json.dumps(payload),
            encoding="utf-8",
        )
    attach_workspace_descriptor(source.run_dir, plan, "a")

    frontier = validate_resume_frontier(source, plan)

    assert frontier.resumed_node_ids == ("a",)


def test_validate_frontier_rejects_parallel_lineage_worktree_with_failed_output(
    tmp_path,
) -> None:
    source = source_record(tmp_path)
    plan = make_plan()
    policy = workspace_selection_record(
        enabled=True,
        kind="worktree",
        clean_start="strict",
        materialization="worktree_checkout",
    )
    node = plan.nodes[0].model_copy(
        update={
            "mode": "parallel",
            "provider_records": [
                provider_record("alpha"),
                provider_record("beta"),
            ],
            "workspace_policy": policy,
        }
    )
    plan = plan.model_copy(
        update={
            "nodes": [node, plan.nodes[1]],
        }
    )
    plan, repo = attach_git_workspace_source(tmp_path, plan)
    assert plan.workspace_source is not None
    descriptor = write_result(source.results_dir, "a-result.md", "a output")
    write_node_state(
        source.run_dir,
        make_node_state(source.manifest, "a", [descriptor]),
    )
    stage_dir = source.run_dir / "a"
    write_stage_output_file(stage_dir / "alpha_round1.md")
    (stage_dir / "beta_round1.md").write_text(
        build_invocation_failure_artifact(
            provider="beta",
            task_id="beta",
            error="quota exhausted",
        ),
        encoding="utf-8",
    )
    payload = provider_workspace_state_payload(
        source,
        plan,
        source_commit=plan.workspace_source.run_base_commit,
        source_tree=plan.workspace_source.source_tree,
    )
    write_lineage_bundle_for_payload(repo, source, payload)
    (stage_dir / "workspace-state-alpha.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )
    failed_payload = provider_workspace_state_payload(
        source,
        plan,
        source_commit=plan.workspace_source.run_base_commit,
        source_tree=plan.workspace_source.source_tree,
    )
    failed_payload["task_id"] = "beta"
    failed_payload["provider"] = "beta"
    failed_payload["status"] = "failed"
    failed_payload.pop("result")
    failed_payload.pop("refs")
    failed_payload.pop("bundle")
    (stage_dir / "workspace-state-beta.json").write_text(
        json.dumps(failed_payload),
        encoding="utf-8",
    )
    attach_workspace_descriptor(source.run_dir, plan, "a")

    frontier = validate_resume_frontier(source, plan)

    assert frontier.resumed_node_ids == ()


def test_validate_frontier_rejects_missing_reviewer_workspace_state(
    tmp_path,
) -> None:
    source = source_record(tmp_path)
    plan = make_plan()
    policy = workspace_selection_record(
        enabled=True,
        kind="worktree",
        clean_start="strict",
        materialization="worktree_checkout",
    )
    node = plan.nodes[0].model_copy(
        update={
            "provider_records": [
                provider_record("alpha", role=ProviderRole.EXECUTOR),
                provider_record("beta", role=ProviderRole.REVIEWER),
            ],
            "workspace_policy": policy,
        }
    )
    plan = plan.model_copy(
        update={
            "nodes": [node, plan.nodes[1]],
        }
    )
    plan, repo = attach_git_workspace_source(tmp_path, plan)
    descriptor = write_result(source.results_dir, "a-result.md", "a output")
    write_node_state(
        source.run_dir,
        make_node_state(source.manifest, "a", [descriptor]),
    )
    stage_dir = source.run_dir / "a"
    write_stage_output_file(stage_dir / "alpha_round1.md")
    write_stage_output_file(stage_dir / "beta_round1.md")
    write_review_status_with_reviewer(stage_dir)
    assert plan.workspace_source is not None
    payload = provider_workspace_state_payload(
        source,
        plan,
        source_commit=plan.workspace_source.run_base_commit,
        source_tree=plan.workspace_source.source_tree,
    )
    write_lineage_bundle_for_payload(repo, source, payload)
    (stage_dir / "workspace-state-alpha.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )

    frontier = validate_resume_frontier(source, plan)

    assert frontier.resumed_node_ids == ()


def test_validate_frontier_rejects_lineage_state_missing_result_ref(
    tmp_path,
) -> None:
    source = source_record(tmp_path)
    plan = make_plan()
    policy = workspace_selection_record(
        enabled=True,
        kind="worktree",
        clean_start="strict",
        materialization="worktree_checkout",
    )
    node = plan.nodes[0].model_copy(update={"workspace_policy": policy})
    plan = plan.model_copy(
        update={
            "nodes": [node, plan.nodes[1]],
        }
    )
    plan, repo = attach_git_workspace_source(tmp_path, plan)
    assert plan.workspace_source is not None
    descriptor = write_result(source.results_dir, "a-result.md", "a output")
    write_node_state(
        source.run_dir,
        make_node_state(source.manifest, "a", [descriptor]),
    )
    payload = provider_workspace_state_payload(
        source,
        plan,
        source_commit=plan.workspace_source.run_base_commit,
        source_tree=plan.workspace_source.source_tree,
    )
    write_lineage_bundle_for_payload(repo, source, payload)
    refs = payload["refs"]
    assert isinstance(refs, dict)
    del refs["result"]
    (source.run_dir / "a" / "workspace-state.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )

    frontier = validate_resume_frontier(source, plan)

    assert frontier.resumed_node_ids == ()


def test_validate_frontier_rejects_lineage_state_boolean_changed_count(
    tmp_path,
) -> None:
    source = source_record(tmp_path)
    plan, payload = _write_valid_lineage_workspace_state(tmp_path, source)
    result = payload["result"]
    assert isinstance(result, dict)
    result["changed_path_count"] = False
    (source.run_dir / "a" / "workspace-state.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )

    frontier = validate_resume_frontier(source, plan)

    assert frontier.resumed_node_ids == ()


def test_validate_frontier_rejects_unexpected_later_lineage_state(
    tmp_path,
) -> None:
    source = source_record(tmp_path)
    plan, payload = _write_valid_lineage_workspace_state(tmp_path, source)
    assert plan.workspace_source is not None
    result = payload["result"]
    assert isinstance(result, dict)
    repo = Path(plan.workspace_source.git_top_level)
    extra_payload = provider_workspace_state_payload(
        source,
        plan,
        source_commit=str(result["result_commit"]),
        source_tree=str(result["result_tree"]),
        source_kind="candidate",
        source_node_id="a",
        candidate_sequence=1,
    )
    attach_source_bundle_descriptor(extra_payload, payload)
    extra_payload["round_num"] = 2
    write_lineage_bundle_for_payload(repo, source, extra_payload)
    (source.run_dir / "a" / "workspace-state-alpha-round2.json").write_text(
        json.dumps(extra_payload),
        encoding="utf-8",
    )
    attach_workspace_descriptor(source.run_dir, plan, "a")

    frontier = validate_resume_frontier(source, plan)

    assert frontier.resumed_node_ids == ()


def test_validate_frontier_rejects_bundle_missing_recorded_result_ref(
    tmp_path,
) -> None:
    source = source_record(tmp_path)
    plan, payload = _write_valid_lineage_workspace_state(tmp_path, source)
    refs = payload["refs"]
    assert isinstance(refs, dict)
    refs["result"] = (
        f"refs/crewplane/runs/{source.manifest.run_key_name}/a/missing/result"
    )
    (source.run_dir / "a" / "workspace-state.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )

    frontier = validate_resume_frontier(source, plan)

    assert frontier.resumed_node_ids == ()


def test_validate_frontier_rejects_bundle_result_ref_wrong_commit(
    tmp_path,
) -> None:
    source = source_record(tmp_path)
    plan, payload = _write_valid_lineage_workspace_state(tmp_path, source)
    result = payload["result"]
    assert isinstance(result, dict)
    result["result_commit"] = "f" * 40
    (source.run_dir / "a" / "workspace-state.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )

    frontier = validate_resume_frontier(source, plan)

    assert frontier.resumed_node_ids == ()


def test_validate_frontier_rejects_bundle_result_ref_not_commit(
    tmp_path,
) -> None:
    source = source_record(tmp_path)
    plan, payload = _write_valid_lineage_workspace_state(tmp_path, source)
    assert plan.workspace_source is not None
    repo = Path(plan.workspace_source.git_top_level)
    blob_path = repo / "blob-result.txt"
    blob_path.write_text("not a commit\n", encoding="utf-8")
    blob_id = run_git_text(repo, "hash-object", "-w", blob_path.as_posix())
    refs = payload["refs"]
    result = payload["result"]
    bundle = payload["bundle"]
    assert isinstance(refs, dict)
    assert isinstance(result, dict)
    assert isinstance(bundle, dict)
    result_ref = str(refs["result"])
    run_git_text(repo, "update-ref", result_ref, blob_id)
    bundle_path = source.run_dir / str(bundle["path"])
    run_git_text(repo, "bundle", "create", bundle_path.as_posix(), result_ref)
    bundle_bytes = bundle_path.read_bytes()
    result["result_commit"] = blob_id
    bundle["sha256"] = hashlib.sha256(bundle_bytes).hexdigest()
    bundle["size_bytes"] = len(bundle_bytes)
    (source.run_dir / "a" / "workspace-state.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )

    frontier = validate_resume_frontier(source, plan)

    assert frontier.resumed_node_ids == ()


def test_validate_frontier_rejects_bundle_header_repo_only_commit(
    tmp_path,
    monkeypatch,
) -> None:
    source = source_record(tmp_path)
    plan, payload = _write_valid_lineage_workspace_state(tmp_path, source)
    assert plan.workspace_source is not None
    repo = Path(plan.workspace_source.git_top_level)
    result = payload["result"]
    bundle = payload["bundle"]
    assert isinstance(result, dict)
    assert isinstance(bundle, dict)
    original_result_commit = str(result["result_commit"])
    repo_only_commit = _write_repo_only_commit(repo)
    _poison_git_template_with_repo_objects(tmp_path, repo, monkeypatch)
    bundle_path = source.run_dir / str(bundle["path"])
    original_bundle_bytes = bundle_path.read_bytes()
    assert original_result_commit.encode("ascii") in original_bundle_bytes
    tampered_bundle_bytes = original_bundle_bytes.replace(
        original_result_commit.encode("ascii"),
        repo_only_commit.encode("ascii"),
        1,
    )
    bundle_path.write_bytes(tampered_bundle_bytes)
    result["result_commit"] = repo_only_commit
    bundle["sha256"] = hashlib.sha256(tampered_bundle_bytes).hexdigest()
    bundle["size_bytes"] = len(tampered_bundle_bytes)
    (source.run_dir / "a" / "workspace-state.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )

    frontier = validate_resume_frontier(source, plan)

    assert frontier.resumed_node_ids == ()


def test_validate_frontier_rejects_corrupt_bundle_file(
    tmp_path,
) -> None:
    source = source_record(tmp_path)
    plan, payload = _write_valid_lineage_workspace_state(tmp_path, source)
    bundle = payload["bundle"]
    assert isinstance(bundle, dict)
    bundle_path = source.run_dir / str(bundle["path"])
    corrupt_bytes = b"not a git bundle"
    bundle_path.write_bytes(corrupt_bytes)
    bundle["sha256"] = hashlib.sha256(corrupt_bytes).hexdigest()
    bundle["size_bytes"] = len(corrupt_bytes)
    (source.run_dir / "a" / "workspace-state.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )

    frontier = validate_resume_frontier(source, plan)

    assert frontier.resumed_node_ids == ()


def test_validate_frontier_rejects_bool_bundle_size(
    tmp_path,
) -> None:
    source = source_record(tmp_path)
    plan, payload = _write_valid_lineage_workspace_state(tmp_path, source)
    bundle = payload["bundle"]
    assert isinstance(bundle, dict)
    bundle["size_bytes"] = True
    (source.run_dir / "a" / "workspace-state.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )

    frontier = validate_resume_frontier(source, plan)

    assert frontier.resumed_node_ids == ()


def test_validate_frontier_accepts_sha256_lineage_bundle(
    tmp_path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    source = source_record(tmp_path)
    plan = make_plan()
    policy = workspace_selection_record(
        enabled=True,
        kind="worktree",
        clean_start="strict",
        materialization="worktree_checkout",
    )
    node = plan.nodes[0].model_copy(update={"workspace_policy": policy})
    plan = plan.model_copy(update={"nodes": [node, plan.nodes[1]]})
    try:
        plan, repo = attach_git_workspace_source(tmp_path, plan, "sha256")
    except subprocess.CalledProcessError as exc:
        pytest.skip(f"git sha256 object format is unavailable: {exc}")
    assert plan.workspace_source is not None
    descriptor = write_result(source.results_dir, "a-result.md", "a output")
    write_node_state(
        source.run_dir,
        make_node_state(source.manifest, "a", [descriptor]),
    )
    payload = provider_workspace_state_payload(
        source,
        plan,
        source_commit=plan.workspace_source.run_base_commit,
        source_tree=plan.workspace_source.source_tree,
    )
    write_lineage_bundle_for_payload(repo, source, payload)
    (source.run_dir / "a" / "workspace-state.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )
    attach_workspace_descriptor(source.run_dir, plan, "a")

    frontier = validate_resume_frontier(source, plan)

    assert frontier.resumed_node_ids == ("a",)


def test_validate_frontier_rejects_corrupt_review_status_for_workspace_node(
    tmp_path,
) -> None:
    source = source_record(tmp_path)
    plan = make_plan()
    policy = workspace_selection_record(
        enabled=True,
        kind="worktree",
        clean_start="strict",
        materialization="worktree_checkout",
    )
    node = plan.nodes[0].model_copy(update={"workspace_policy": policy})
    plan = plan.model_copy(
        update={
            "nodes": [node, plan.nodes[1]],
        }
    )
    plan, repo = attach_git_workspace_source(tmp_path, plan)
    assert plan.workspace_source is not None
    descriptor = write_result(source.results_dir, "a-result.md", "a output")
    write_node_state(
        source.run_dir,
        make_node_state(source.manifest, "a", [descriptor]),
    )
    status_path = source.run_dir / "a" / "review-state" / "review-loop-status.json"
    status_path.parent.mkdir(parents=True)
    status_path.write_text("{", encoding="utf-8")
    payload = provider_workspace_state_payload(
        source,
        plan,
        source_commit=plan.workspace_source.run_base_commit,
        source_tree=plan.workspace_source.source_tree,
    )
    write_lineage_bundle_for_payload(repo, source, payload)
    (source.run_dir / "a" / "workspace-state.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )

    frontier = validate_resume_frontier(source, plan)

    assert frontier.resumed_node_ids == ()


def _write_valid_lineage_workspace_state(
    tmp_path: Path,
    source: RunHistoryRecord,
) -> tuple[PreflightExecutionPlan, dict[str, object]]:
    plan = make_plan()
    policy = workspace_selection_record(
        enabled=True,
        kind="worktree",
        clean_start="strict",
        materialization="worktree_checkout",
    )
    node = plan.nodes[0].model_copy(update={"workspace_policy": policy})
    plan = plan.model_copy(update={"nodes": [node, plan.nodes[1]]})
    plan, repo = attach_git_workspace_source(tmp_path, plan)
    assert plan.workspace_source is not None
    descriptor = write_result(source.results_dir, "a-result.md", "a output")
    write_node_state(
        source.run_dir,
        make_node_state(source.manifest, "a", [descriptor]),
    )
    payload = provider_workspace_state_payload(
        source,
        plan,
        source_commit=plan.workspace_source.run_base_commit,
        source_tree=plan.workspace_source.source_tree,
    )
    write_lineage_bundle_for_payload(repo, source, payload)
    (source.run_dir / "a" / "workspace-state.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )
    return plan, payload


def _write_repo_only_commit(repo: Path) -> str:
    repo_only_path = repo / "repo-only.txt"
    repo_only_path.write_text("repo only\n", encoding="utf-8")
    run_git_text(repo, "add", repo_only_path.as_posix())
    run_git_text(repo, "commit", "-m", "repo-only")
    return run_git_text(repo, "rev-parse", "HEAD^{commit}")


def _poison_git_template_with_repo_objects(
    tmp_path: Path, repo: Path, monkeypatch
) -> None:
    repo_objects = Path(run_git_text(repo, "rev-parse", "--git-path", "objects"))
    if not repo_objects.is_absolute():
        repo_objects = repo / repo_objects
    template_dir = tmp_path / "poison-template"
    alternates_path = template_dir / "objects" / "info" / "alternates"
    alternates_path.parent.mkdir(parents=True)
    alternates_path.write_text(repo_objects.as_posix() + "\n", encoding="utf-8")
    monkeypatch.setenv("GIT_TEMPLATE_DIR", template_dir.as_posix())
