from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

import crewplane.runtime.workspace.branch_export as branch_exports
from crewplane.artifacts import OutputManager
from crewplane.artifacts.naming import build_workspace_export_filename
from crewplane.core.preflight.models import WorkspaceSourceSnapshot
from crewplane.runtime.workspace.branch_export import (
    fulfill_branch_exports,
)
from crewplane.runtime.workspace.branch_export import git as branch_export_git
from crewplane.runtime.workspace.worktree import lineage as worktree_lineage
from crewplane.runtime.workspace.worktree import (
    temporary_refs as worktree_temporary_refs,
)
from crewplane.runtime.workspace.worktree.types import WorktreeSourceRef
from tests.helpers.artifacts import node_artifact_request
from tests.helpers.workspace_branch_export import (
    branch_export_plan,
    update_state_bundle_metadata,
    write_node_manifest,
    write_result_bundle,
    write_result_bundle_from_clone,
    write_workspace_state,
)
from tests.helpers.workspace_lineage_bundles import create_prerequisite_bundle_chain
from tests.helpers.workspace_service import (
    create_git_repo,
    git_commit_exists,
    run_git_text,
)


def test_fulfill_branch_exports_creates_branch_and_audit_record(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    plan = branch_export_plan(repo, tmp_path, branch_name="feature/exported")
    output = OutputManager("workspace", base_dir=tmp_path / "artifacts")
    result_commit, result_tree, result_ref, bundle_path = write_result_bundle(
        repo,
        output.create_node_dir(node_artifact_request("implement")),
        "feature result\n",
    )
    write_workspace_state(
        output.stages_dir,
        plan,
        result_commit,
        result_tree,
        result_ref,
        bundle_path,
    )
    node_state_path = write_node_manifest(output, plan)

    records = fulfill_branch_exports(plan, output)

    assert len(records) == 1
    assert records[0].name == build_workspace_export_filename("primary")
    assert run_git_text(repo, "rev-parse", "refs/heads/feature/exported") == (
        result_commit
    )
    record = json.loads(records[0].read_text(encoding="utf-8"))
    assert record["status"] == "fulfilled"
    assert record["branch_name"] == "feature/exported"
    assert record["operation"] == "created"
    assert record["branch_exists_before"] is False
    assert record["branch_exists_after"] is True
    assert record["result_commit"] == result_commit
    assert record["worktree_contract"]["mode"] == "blob_exact"
    state = json.loads(
        (
            output.create_node_dir(node_artifact_request("implement"))
            / "workspace-state.json"
        ).read_text(encoding="utf-8")
    )
    assert state["branch_export"]["status"] == "fulfilled"
    assert state["branch_export"]["operation"] == "created"
    assert state["branch_export"]["branch_ref"] == "refs/heads/feature/exported"
    node_state = json.loads(node_state_path.read_text(encoding="utf-8"))
    manifest_state = node_state["workspace"]["states"][0]
    assert manifest_state["branch_export"]["operation"] == "created"
    artifact = manifest_state["workspace_state_artifact"]
    assert (
        artifact["sha256"]
        == hashlib.sha256(
            (
                output.create_node_dir(node_artifact_request("implement"))
                / "workspace-state.json"
            ).read_bytes()
        ).hexdigest()
    )


def test_fulfill_branch_exports_verifies_source_chain_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    plan = branch_export_plan(repo, tmp_path, branch_name="feature/exported")
    output = OutputManager("workspace", base_dir=tmp_path / "artifacts")
    result_commit, result_tree, result_ref, bundle_path = write_result_bundle(
        repo,
        output.create_node_dir(node_artifact_request("implement")),
        "feature result\n",
    )
    write_workspace_state(
        output.stages_dir,
        plan,
        result_commit,
        result_tree,
        result_ref,
        bundle_path,
    )
    original_verifier = worktree_lineage.verify_workspace_source_chain
    verified_commits: list[str] = []

    def record_verification(
        source: WorkspaceSourceSnapshot,
        source_ref: WorktreeSourceRef,
    ) -> None:
        verified_commits.append(source_ref.source_commit)
        original_verifier(source, source_ref)

    monkeypatch.setattr(
        worktree_lineage,
        "verify_workspace_source_chain",
        record_verification,
    )

    fulfill_branch_exports(plan, output)

    assert verified_commits == [result_commit]


def test_fulfill_branch_exports_keeps_import_ref_until_branch_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    plan = branch_export_plan(repo, tmp_path, branch_name="feature/exported")
    output = OutputManager("workspace", base_dir=tmp_path / "artifacts")
    stage_dir = output.create_node_dir(node_artifact_request("implement"))
    result_commit, result_tree, result_ref, bundle_path = write_result_bundle(
        repo,
        stage_dir,
        "feature result\n",
    )
    state_path = write_workspace_state(
        output.stages_dir,
        plan,
        result_commit,
        result_tree,
        result_ref,
        bundle_path,
    )
    run_git_text(repo, "update-ref", "-d", result_ref)
    run_git_text(repo, "reflog", "expire", "--expire=now", "--all")
    run_git_text(repo, "gc", "--prune=now")
    assert not git_commit_exists(repo, result_commit)
    original_create_branch = branch_exports.create_branch_export_ref

    def create_branch_after_prune(
        source: WorkspaceSourceSnapshot,
        branch_ref: str,
        checkpoint: object,
        allow_existing: bool = False,
        allow_create: bool = True,
    ) -> str:
        imported_refs = run_git_text(
            repo,
            "for-each-ref",
            "--format=%(objectname)",
            f"refs/crewplane/runs/{plan.run_key_name}/imports",
        ).splitlines()
        assert result_commit in imported_refs
        run_git_text(repo, "reflog", "expire", "--expire=now", "--all")
        run_git_text(repo, "gc", "--prune=now")
        return original_create_branch(
            source,
            branch_ref,
            checkpoint,
            allow_existing=allow_existing,
            allow_create=allow_create,
        )

    monkeypatch.setattr(
        branch_exports,
        "create_branch_export_ref",
        create_branch_after_prune,
    )

    fulfill_branch_exports(plan, output)

    assert run_git_text(repo, "rev-parse", "refs/heads/feature/exported") == (
        result_commit
    )
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert "temporary_refs" not in state
    assert not tuple(stage_dir.glob("workspace-temporary-refs-*.json"))


def test_fulfill_branch_exports_rejects_existing_exact_branch_without_record(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    plan = branch_export_plan(repo, tmp_path, branch_name="feature/exported")
    output = OutputManager("workspace", base_dir=tmp_path / "artifacts")
    result_commit, result_tree, result_ref, bundle_path = write_result_bundle(
        repo,
        output.create_node_dir(node_artifact_request("implement")),
        "feature result\n",
    )
    write_workspace_state(
        output.stages_dir,
        plan,
        result_commit,
        result_tree,
        result_ref,
        bundle_path,
    )
    run_git_text(repo, "update-ref", "refs/heads/feature/exported", result_commit)

    with pytest.raises(RuntimeError, match="refuses to overwrite"):
        fulfill_branch_exports(plan, output)

    record = json.loads(
        _export_record_path(output.stages_dir).read_text(encoding="utf-8")
    )
    assert record["status"] == "failed_verification"
    assert record["operation"] == "failed_verification"
    assert record["branch_exists_before"] is True
    assert record["branch_exists_after"] is True
    assert run_git_text(repo, "rev-parse", "refs/heads/feature/exported") == (
        result_commit
    )
    state = json.loads(
        (
            output.create_node_dir(node_artifact_request("implement"))
            / "workspace-state.json"
        ).read_text(encoding="utf-8")
    )
    assert state["branch_export"]["operation"] == "failed_verification"


def test_fulfill_branch_exports_refuses_existing_mismatched_branch(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    plan = branch_export_plan(repo, tmp_path, branch_name="feature/exported")
    output = OutputManager("workspace", base_dir=tmp_path / "artifacts")
    result_commit, result_tree, result_ref, bundle_path = write_result_bundle(
        repo,
        output.create_node_dir(node_artifact_request("implement")),
        "feature result\n",
    )
    write_workspace_state(
        output.stages_dir,
        plan,
        result_commit,
        result_tree,
        result_ref,
        bundle_path,
    )
    base_commit = run_git_text(repo, "rev-parse", "HEAD^{commit}")
    run_git_text(repo, "update-ref", "refs/heads/feature/exported", base_commit)

    with pytest.raises(RuntimeError, match="refuses to overwrite"):
        fulfill_branch_exports(plan, output)

    record_path = _export_record_path(output.stages_dir)
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record["status"] == "failed_verification"
    assert record["operation"] == "failed_verification"
    assert record["branch_exists_before"] is True
    assert record["branch_exists_after"] is True
    assert "refuses to overwrite" in record["failure_message"]
    state = json.loads(
        (
            output.create_node_dir(node_artifact_request("implement"))
            / "workspace-state.json"
        ).read_text(encoding="utf-8")
    )
    assert state["branch_export"]["status"] == "failed_verification"


def test_fulfill_branch_exports_reconciles_recorded_branch_state(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    plan = branch_export_plan(repo, tmp_path, branch_name="feature/exported")
    output = OutputManager("workspace", base_dir=tmp_path / "artifacts")
    result_commit, result_tree, result_ref, bundle_path = write_result_bundle(
        repo,
        output.create_node_dir(node_artifact_request("implement")),
        "feature result\n",
    )
    write_workspace_state(
        output.stages_dir,
        plan,
        result_commit,
        result_tree,
        result_ref,
        bundle_path,
    )
    fulfill_branch_exports(plan, output)

    records = fulfill_branch_exports(plan, output)

    assert len(records) == 1
    record = json.loads(records[0].read_text(encoding="utf-8"))
    assert record["status"] == "fulfilled"
    assert record["operation"] == "created"
    assert record["branch_exists_before"] is False
    assert record["branch_exists_after"] is True
    assert run_git_text(repo, "rev-parse", "refs/heads/feature/exported") == (
        result_commit
    )
    state = json.loads(
        (
            output.create_node_dir(node_artifact_request("implement"))
            / "workspace-state.json"
        ).read_text(encoding="utf-8")
    )
    assert state["branch_export"]["operation"] == "created"

    run_git_text(repo, "update-ref", "-d", "refs/heads/feature/exported")
    recreated_records = fulfill_branch_exports(plan, output)

    recreated = json.loads(recreated_records[0].read_text(encoding="utf-8"))
    assert recreated["status"] == "fulfilled"
    assert recreated["operation"] == "created"
    assert recreated["branch_exists_before"] is False
    assert recreated["branch_exists_after"] is True
    assert run_git_text(repo, "rev-parse", "refs/heads/feature/exported") == (
        result_commit
    )


def test_fulfill_branch_exports_records_failed_replay_after_branch_moves(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    plan = branch_export_plan(repo, tmp_path, branch_name="feature/exported")
    output = OutputManager("workspace", base_dir=tmp_path / "artifacts")
    result_commit, result_tree, result_ref, bundle_path = write_result_bundle(
        repo,
        output.create_node_dir(node_artifact_request("implement")),
        "feature result\n",
    )
    state_path = write_workspace_state(
        output.stages_dir,
        plan,
        result_commit,
        result_tree,
        result_ref,
        bundle_path,
    )
    fulfill_branch_exports(plan, output)
    base_commit = run_git_text(repo, "rev-parse", "HEAD^{commit}")
    branch_ref = "refs/heads/feature/exported"
    run_git_text(repo, "update-ref", branch_ref, base_commit, result_commit)

    with pytest.raises(RuntimeError, match="refuses to overwrite"):
        fulfill_branch_exports(plan, output)

    record = json.loads(
        _export_record_path(output.stages_dir).read_text(encoding="utf-8")
    )
    assert record["status"] == "failed_verification"
    assert record["operation"] == "failed_verification"
    assert record["branch_exists_before"] is True
    assert record["branch_exists_after"] is True
    assert run_git_text(repo, "rev-parse", branch_ref) == base_commit
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["branch_export"]["status"] == "failed_verification"


def test_fulfill_branch_exports_records_failed_replay_after_symbolic_replacement(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    plan = branch_export_plan(repo, tmp_path, branch_name="feature/exported")
    output = OutputManager("workspace", base_dir=tmp_path / "artifacts")
    result_commit, result_tree, result_ref, bundle_path = write_result_bundle(
        repo,
        output.create_node_dir(node_artifact_request("implement")),
        "feature result\n",
    )
    state_path = write_workspace_state(
        output.stages_dir,
        plan,
        result_commit,
        result_tree,
        result_ref,
        bundle_path,
    )
    fulfill_branch_exports(plan, output)
    target_ref = "refs/heads/user-work"
    target_commit = run_git_text(repo, "rev-parse", "HEAD^{commit}")
    run_git_text(repo, "update-ref", target_ref, target_commit)
    branch_ref = "refs/heads/feature/exported"
    run_git_text(repo, "symbolic-ref", branch_ref, target_ref)

    with pytest.raises(RuntimeError, match="destination is symbolic"):
        fulfill_branch_exports(plan, output)

    record = json.loads(
        _export_record_path(output.stages_dir).read_text(encoding="utf-8")
    )
    assert record["status"] == "failed_verification"
    assert record["operation"] == "failed_verification"
    assert run_git_text(repo, "symbolic-ref", branch_ref) == target_ref
    assert run_git_text(repo, "rev-parse", target_ref) == target_commit
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["branch_export"]["status"] == "failed_verification"


def test_fulfill_branch_exports_rejects_invalid_branch_name(tmp_path: Path) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    plan = branch_export_plan(
        repo,
        tmp_path,
        branch_name="refs/heads/feature/exported",
    )
    output = OutputManager("workspace", base_dir=tmp_path / "artifacts")
    result_commit, result_tree, result_ref, bundle_path = write_result_bundle(
        repo,
        output.create_node_dir(node_artifact_request("implement")),
        "feature result\n",
    )
    write_workspace_state(
        output.stages_dir,
        plan,
        result_commit,
        result_tree,
        result_ref,
        bundle_path,
    )

    with pytest.raises(RuntimeError, match="must not include refs/"):
        fulfill_branch_exports(plan, output)

    assert not run_git_text(repo, "branch", "--list", "feature/exported")
    record_path = _export_record_path(output.stages_dir)
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record["status"] == "failed_verification"
    assert record["branch_ref"] is None
    assert "must not include refs" in record["failure_message"]


def test_fulfill_branch_exports_validates_generated_branch_name(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    plan = branch_export_plan(repo, tmp_path, branch_name=None).model_copy(
        update={"workflow_name": "Workspace / Feature: thing"}
    )
    output = OutputManager("workspace", base_dir=tmp_path / "artifacts")
    result_commit, result_tree, result_ref, bundle_path = write_result_bundle(
        repo,
        output.create_node_dir(node_artifact_request("implement")),
        "feature result\n",
    )
    write_workspace_state(
        output.stages_dir,
        plan,
        result_commit,
        result_tree,
        result_ref,
        bundle_path,
    )

    records = fulfill_branch_exports(plan, output)

    record = json.loads(records[0].read_text(encoding="utf-8"))
    assert record["branch_name"].startswith(
        "crewplane/Workspace-Feature-thing/primary/"
    )
    assert run_git_text(repo, "rev-parse", record["branch_ref"]) == result_commit


@pytest.mark.parametrize(
    ("metadata_override", "message"),
    [
        ({"sha256": "0" * 64}, "digest mismatch"),
        ({"size_bytes": 1}, "size mismatch"),
        ({"size_bytes": True}, "lacks verified bundle evidence"),
    ],
)
def test_fulfill_branch_exports_rejects_bundle_metadata_mismatch(
    tmp_path: Path,
    metadata_override: dict[str, object],
    message: str,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    plan = branch_export_plan(repo, tmp_path, branch_name="feature/exported")
    output = OutputManager("workspace", base_dir=tmp_path / "artifacts")
    result_commit, result_tree, result_ref, bundle_path = write_result_bundle(
        repo,
        output.create_node_dir(node_artifact_request("implement")),
        "feature result\n",
    )
    state_path = write_workspace_state(
        output.stages_dir,
        plan,
        result_commit,
        result_tree,
        result_ref,
        bundle_path,
    )
    update_state_bundle_metadata(state_path, metadata_override)

    with pytest.raises(RuntimeError, match=message):
        fulfill_branch_exports(plan, output)

    record_path = _export_record_path(output.stages_dir)
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record["status"] == "failed_verification"
    assert message in record["failure_message"]


def test_fulfill_branch_exports_rejects_result_tree_mismatch_before_branch_ref(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    plan = branch_export_plan(repo, tmp_path, branch_name="feature/exported")
    output = OutputManager("workspace", base_dir=tmp_path / "artifacts")
    result_commit, _result_tree, result_ref, bundle_path = write_result_bundle(
        repo,
        output.create_node_dir(node_artifact_request("implement")),
        "feature result\n",
    )
    write_workspace_state(
        output.stages_dir,
        plan,
        result_commit,
        "0" * 40,
        result_ref,
        bundle_path,
    )

    with pytest.raises(RuntimeError, match="recorded commit and tree"):
        fulfill_branch_exports(plan, output)

    assert run_git_text(repo, "branch", "--list", "feature/exported") == ""
    record_path = _export_record_path(output.stages_dir)
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record["status"] == "failed_verification"
    assert "recorded commit and tree" in record["failure_message"]


@pytest.mark.parametrize(
    ("identity_field", "foreign_value"),
    (
        ("run_id", "foreign-run-id"),
        ("run_key_name", "foreign-run-key"),
        ("git.repo_id", "foreign-repository"),
    ),
)
def test_fulfill_branch_exports_rejects_foreign_checkpoint_identity(
    tmp_path: Path,
    identity_field: str,
    foreign_value: str,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    plan = branch_export_plan(repo, tmp_path, branch_name="feature/foreign")
    output = OutputManager("workspace", base_dir=tmp_path / "artifacts")
    result_commit, result_tree, result_ref, bundle_path = write_result_bundle(
        repo,
        output.create_node_dir(node_artifact_request("implement")),
        "foreign result\n",
    )
    state_path = write_workspace_state(
        output.stages_dir,
        plan,
        result_commit,
        result_tree,
        result_ref,
        bundle_path,
    )
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    publication = payload["ref_publication"]
    if identity_field == "git.repo_id":
        payload["git"]["repo_id"] = foreign_value
        publication["repository_id"] = foreign_value
    elif identity_field == "run_key_name":
        old_run_key = payload["run_key_name"]
        payload["run_key_name"] = foreign_value
        publication["run_key_name"] = foreign_value
        for label in ("candidate", "result"):
            foreign_ref = payload["refs"][label].replace(old_run_key, foreign_value)
            payload["refs"][label] = foreign_ref
            publication["destinations"][label]["name"] = foreign_ref
            run_git_text(
                repo,
                "update-ref",
                foreign_ref,
                publication["destinations"][label]["target_oid"],
            )
        bundle_path.unlink()
        run_git_text(
            repo,
            "bundle",
            "create",
            bundle_path.as_posix(),
            payload["refs"]["result"],
        )
        payload["bundle"]["sha256"] = hashlib.sha256(
            bundle_path.read_bytes()
        ).hexdigest()
        payload["bundle"]["size_bytes"] = bundle_path.stat().st_size
    else:
        payload[identity_field] = foreign_value
        publication[identity_field] = foreign_value
    state_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RuntimeError, match="state does not match"):
        fulfill_branch_exports(plan, output)

    assert run_git_text(repo, "branch", "--list", "feature/foreign") == ""


def test_fulfill_branch_exports_rejects_foreign_live_repository_identity(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    plan = branch_export_plan(repo, tmp_path, branch_name="feature/foreign-live")
    output = OutputManager("workspace", base_dir=tmp_path / "artifacts")
    result_commit, result_tree, result_ref, bundle_path = write_result_bundle(
        repo,
        output.create_node_dir(node_artifact_request("implement")),
        "foreign result\n",
    )
    state_path = write_workspace_state(
        output.stages_dir,
        plan,
        result_commit,
        result_tree,
        result_ref,
        bundle_path,
    )
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    payload["git"]["repo_id"] = "foreign-repository"
    payload["ref_publication"]["repository_id"] = "foreign-repository"
    state_path.write_text(json.dumps(payload), encoding="utf-8")
    source = plan.workspace_source
    assert source is not None
    plan = plan.model_copy(
        update={
            "workspace_source": source.model_copy(
                update={"repository_id": "foreign-repository"}
            )
        }
    )

    with pytest.raises(RuntimeError, match="repository identity changed"):
        fulfill_branch_exports(plan, output)

    assert run_git_text(repo, "branch", "--list", "feature/foreign-live") == ""


def test_fulfill_branch_exports_imports_missing_result_from_bundle(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    plan = branch_export_plan(repo, tmp_path, branch_name="feature/exported")
    output = OutputManager("workspace", base_dir=tmp_path / "artifacts")
    result_commit, result_tree, result_ref, bundle_path = (
        write_result_bundle_from_clone(
            repo,
            tmp_path,
            output.create_node_dir(node_artifact_request("implement")),
            "feature result\n",
        )
    )
    assert not git_commit_exists(repo, result_commit)
    write_workspace_state(
        output.stages_dir,
        plan,
        result_commit,
        result_tree,
        result_ref,
        bundle_path,
    )

    fulfill_branch_exports(plan, output)

    assert git_commit_exists(repo, result_commit)
    assert run_git_text(repo, "rev-parse", "refs/heads/feature/exported") == (
        result_commit
    )


def test_fulfill_branch_exports_preserves_prepared_record_when_import_ref_cleanup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    plan = branch_export_plan(repo, tmp_path, branch_name="feature/exported")
    output = OutputManager("workspace", base_dir=tmp_path / "artifacts")
    result_commit, result_tree, result_ref, bundle_path = (
        write_result_bundle_from_clone(
            repo,
            tmp_path,
            output.create_node_dir(node_artifact_request("implement")),
            "feature result\n",
        )
    )
    write_workspace_state(
        output.stages_dir,
        plan,
        result_commit,
        result_tree,
        result_ref,
        bundle_path,
    )
    original_mark_removed = worktree_temporary_refs.mark_workspace_temporary_ref_removed

    def fail_after_temporary_ref_cleanup(
        state_path: Path,
        ref_name: str,
    ) -> None:
        original_mark_removed(state_path, ref_name)
        raise RuntimeError("injected temporary ref cleanup failure")

    monkeypatch.setattr(
        worktree_temporary_refs,
        "mark_workspace_temporary_ref_removed",
        fail_after_temporary_ref_cleanup,
    )

    with pytest.raises(RuntimeError, match="temporary ref cleanup failure"):
        fulfill_branch_exports(plan, output)

    record_path = _export_record_path(output.stages_dir)
    prepared = json.loads(record_path.read_text(encoding="utf-8"))
    assert prepared["status"] == "prepared"
    assert run_git_text(repo, "rev-parse", "refs/heads/feature/exported") == (
        result_commit
    )

    monkeypatch.setattr(
        worktree_temporary_refs,
        "mark_workspace_temporary_ref_removed",
        original_mark_removed,
    )
    records = fulfill_branch_exports(plan, output)

    recovered = json.loads(records[0].read_text(encoding="utf-8"))
    assert recovered["status"] == "fulfilled"
    assert recovered["operation"] == "verified_existing"
    assert recovered["recovery_mode"] == "prepared_record"


def test_fulfill_branch_exports_preserves_prepared_record_when_branch_lock_teardown_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    plan = branch_export_plan(repo, tmp_path, branch_name="feature/exported")
    output = OutputManager("workspace", base_dir=tmp_path / "artifacts")
    result_commit, result_tree, result_ref, bundle_path = write_result_bundle(
        repo,
        output.create_node_dir(node_artifact_request("implement")),
        "feature result\n",
    )
    write_workspace_state(
        output.stages_dir,
        plan,
        result_commit,
        result_tree,
        result_ref,
        bundle_path,
    )
    original_lock = branch_export_git.git_metadata_lock

    @contextmanager
    def fail_after_branch_lock_teardown(common_git_dir: Path) -> Iterator[None]:
        with original_lock(common_git_dir):
            yield
        raise RuntimeError("injected branch lock teardown failure")

    monkeypatch.setattr(
        branch_export_git,
        "git_metadata_lock",
        fail_after_branch_lock_teardown,
    )

    with pytest.raises(RuntimeError, match="branch lock teardown failure"):
        fulfill_branch_exports(plan, output)

    record_path = _export_record_path(output.stages_dir)
    prepared = json.loads(record_path.read_text(encoding="utf-8"))
    assert prepared["status"] == "prepared"
    assert run_git_text(repo, "rev-parse", "refs/heads/feature/exported") == (
        result_commit
    )

    monkeypatch.setattr(branch_export_git, "git_metadata_lock", original_lock)
    records = fulfill_branch_exports(plan, output)

    recovered = json.loads(records[0].read_text(encoding="utf-8"))
    assert recovered["status"] == "fulfilled"
    assert recovered["operation"] == "verified_existing"
    assert recovered["recovery_mode"] == "prepared_record"


def test_fulfill_branch_exports_preserves_prepared_record_after_update_ref_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    plan = branch_export_plan(repo, tmp_path, branch_name="feature/exported")
    output = OutputManager("workspace", base_dir=tmp_path / "artifacts")
    result_commit, result_tree, result_ref, bundle_path = write_result_bundle(
        repo,
        output.create_node_dir(node_artifact_request("implement")),
        "feature result\n",
    )
    write_workspace_state(
        output.stages_dir,
        plan,
        result_commit,
        result_tree,
        result_ref,
        bundle_path,
    )
    original_run = branch_export_git.GitCommand.run
    timed_out = False

    def mutate_then_timeout(
        command: branch_export_git.GitCommand,
        *args: str,
    ) -> subprocess.CompletedProcess[bytes]:
        nonlocal timed_out
        result = original_run(command, *args)
        if (
            args[:3] == ("update-ref", "--no-deref", "refs/heads/feature/exported")
            and not timed_out
        ):
            timed_out = True
            raise subprocess.TimeoutExpired(("git", *args), command.timeout_seconds)
        return result

    monkeypatch.setattr(branch_export_git.GitCommand, "run", mutate_then_timeout)

    with pytest.raises(RuntimeError, match="timed out"):
        fulfill_branch_exports(plan, output)

    record_path = _export_record_path(output.stages_dir)
    prepared = json.loads(record_path.read_text(encoding="utf-8"))
    assert prepared["status"] == "prepared"
    assert run_git_text(repo, "rev-parse", "refs/heads/feature/exported") == (
        result_commit
    )

    monkeypatch.setattr(branch_export_git.GitCommand, "run", original_run)
    records = fulfill_branch_exports(plan, output)

    recovered = json.loads(records[0].read_text(encoding="utf-8"))
    assert recovered["status"] == "fulfilled"
    assert recovered["operation"] == "verified_existing"
    assert recovered["recovery_mode"] == "prepared_record"


@pytest.mark.parametrize(
    "race_matches_target",
    (True, False),
    ids=("exact-target", "different-target"),
)
def test_fulfill_branch_exports_terminalizes_current_run_branch_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    race_matches_target: bool,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    plan = branch_export_plan(repo, tmp_path, branch_name="feature/raced")
    output = OutputManager("workspace", base_dir=tmp_path / "artifacts")
    result_commit, result_tree, result_ref, bundle_path = write_result_bundle(
        repo,
        output.create_node_dir(node_artifact_request("implement")),
        "feature result\n",
    )
    write_workspace_state(
        output.stages_dir,
        plan,
        result_commit,
        result_tree,
        result_ref,
        bundle_path,
    )
    branch_ref = "refs/heads/feature/raced"
    raced_commit = (
        result_commit
        if race_matches_target
        else run_git_text(repo, "rev-parse", "HEAD^{commit}")
    )
    original_branch_ref_exists = branch_exports.branch_ref_exists
    raced = False

    def create_branch_after_probe(
        source: WorkspaceSourceSnapshot,
        requested_branch_ref: str,
    ) -> bool:
        nonlocal raced
        exists = original_branch_ref_exists(source, requested_branch_ref)
        if not raced:
            assert exists is False
            raced = True
            run_git_text(repo, "update-ref", requested_branch_ref, raced_commit)
        return exists

    monkeypatch.setattr(
        branch_exports,
        "branch_ref_exists",
        create_branch_after_probe,
    )

    for attempt in range(2):
        with pytest.raises(RuntimeError, match="refuses to overwrite"):
            fulfill_branch_exports(plan, output)

        record = json.loads(
            _export_record_path(output.stages_dir).read_text(encoding="utf-8")
        )
        assert record["status"] == "failed_verification", f"attempt {attempt + 1}"
        assert record["operation"] == "failed_verification"
        assert record["recovery_mode"] == "initial"

    assert run_git_text(repo, "rev-parse", branch_ref) == raced_commit


def test_fulfill_branch_exports_rejects_prerequisite_bundle_chain(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    plan = branch_export_plan(repo, tmp_path, branch_name="feature/exported")
    output = OutputManager("workspace", base_dir=tmp_path / "artifacts")
    first, second = create_prerequisite_bundle_chain(
        repo,
        output.stages_dir / "prepare" / "workspace-bundles" / "first.bundle",
        output.create_node_dir(node_artifact_request("implement"))
        / "workspace-bundles"
        / "second.bundle",
    )
    if git_commit_exists(repo, first.commit) or git_commit_exists(repo, second.commit):
        pytest.skip("git retained the test commits after pruning")
    write_workspace_state(
        output.stages_dir,
        plan,
        second.commit,
        second.tree,
        second.ref,
        second.path,
        source_ref=WorktreeSourceRef(
            source_kind="node",
            source_node_id="prepare",
            source_commit=first.commit,
            source_tree=first.tree,
            candidate_sequence=1,
            bundle_path=first.path,
            bundle_sha256=first.sha256,
            bundle_size_bytes=first.size_bytes,
            bundle_ref=first.ref,
        ),
    )

    with pytest.raises(RuntimeError, match="advertises prerequisites"):
        fulfill_branch_exports(plan, output)

    assert not git_commit_exists(repo, first.commit)
    assert not git_commit_exists(repo, second.commit)
    assert not run_git_text(repo, "branch", "--list", "feature/exported")


def test_create_branch_ref_refuses_raced_existing_branch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    plan = branch_export_plan(repo, tmp_path, branch_name="feature/raced")
    source = plan.workspace_source
    assert source is not None
    result_commit = run_git_text(repo, "rev-parse", "HEAD^{commit}")
    branch_ref = "refs/heads/feature/raced"
    branch_commit_calls = 0

    class RacedGitCommand:
        def run(self, *args: str) -> object:
            if args[:2] == ("update-ref", "--no-deref"):
                raise subprocess.CalledProcessError(
                    1,
                    ("git", *args),
                    stderr=b"cannot lock ref",
                )
            raise AssertionError(f"unexpected git command: {args!r}")

    def fake_git(cwd: Path) -> RacedGitCommand:
        assert cwd == repo
        return RacedGitCommand()

    def fake_branch_commit(command: object, ref: str) -> str | None:
        del command
        nonlocal branch_commit_calls
        assert ref == branch_ref
        branch_commit_calls += 1
        return None if branch_commit_calls == 1 else result_commit

    monkeypatch.setattr(branch_export_git, "git", fake_git)
    monkeypatch.setattr(branch_export_git, "branch_commit", fake_branch_commit)

    with pytest.raises(RuntimeError, match="refuses to overwrite existing branch"):
        branch_export_git.create_or_verify_branch_ref(
            source,
            branch_ref,
            result_commit,
        )

    assert branch_commit_calls == 2


@pytest.mark.parametrize("target_exists", (False, True), ids=("dangling", "live"))
def test_create_branch_ref_rejects_symbolic_destination_without_touching_target(
    tmp_path: Path,
    target_exists: bool,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    plan = branch_export_plan(repo, tmp_path, branch_name="feature/exported")
    source = plan.workspace_source
    assert source is not None
    result_commit = run_git_text(repo, "rev-parse", "HEAD^{commit}")
    branch_ref = "refs/heads/feature/exported"
    target_ref = "refs/heads/user-work"
    if target_exists:
        run_git_text(repo, "update-ref", target_ref, result_commit)
    run_git_text(repo, "symbolic-ref", branch_ref, target_ref)

    with pytest.raises(RuntimeError, match="symbolic"):
        branch_export_git.create_or_verify_branch_ref(
            source,
            branch_ref,
            result_commit,
            allow_existing=True,
        )

    assert run_git_text(repo, "symbolic-ref", branch_ref) == target_ref
    assert _optional_ref_oid(repo, target_ref) == (
        result_commit if target_exists else None
    )


def _optional_ref_oid(repo: Path, ref_name: str) -> str | None:
    result = subprocess.run(
        ["git", "-C", repo.as_posix(), "rev-parse", "--verify", ref_name],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def test_workspace_export_filename_preserves_colliding_logical_names() -> None:
    first = build_workspace_export_filename("primary/worktree")
    second = build_workspace_export_filename("primary worktree")

    assert first != second
    assert first.endswith(".json")
    assert second.endswith(".json")
    assert len(first) <= 180
    assert len(second) <= 180


def _export_record_path(
    stages_dir: Path, logical_worktree_name: str = "primary"
) -> Path:
    return (
        stages_dir
        / "workspace-exports"
        / build_workspace_export_filename(logical_worktree_name)
    )
