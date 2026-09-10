from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from crewplane.artifacts import OutputManager
from crewplane.artifacts.naming import build_workspace_export_filename
from crewplane.core.preflight.models import WorkspaceSourceSnapshot
from crewplane.runtime.workspace.branch_export import attempts as branch_export_attempts
from crewplane.runtime.workspace.branch_export import (
    fulfill_branch_exports,
)
from crewplane.runtime.workspace.worktree import lineage as worktree_lineage
from crewplane.runtime.workspace.worktree.types import WorktreeSourceRef
from tests.helpers.artifacts import node_artifact_request
from tests.helpers.workspace_branch_export import (
    branch_export_plan,
    write_node_manifest,
    write_result_bundle,
    write_workspace_state,
)
from tests.helpers.workspace_service import (
    create_git_repo,
    git_commit_exists,
    run_git_text,
)
from tests.unit.runtime.workspace.branch_export_support import (
    export_record_path,
)


@pytest.mark.parametrize("symlink_manifest", [False, True])
def test_fulfill_branch_exports_creates_branch_and_audit_record(
    symlink_manifest,
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

    original_manifest = node_state_path.read_bytes()
    if symlink_manifest:
        external_manifest = tmp_path / "external-node.json"
        node_state_path.rename(external_manifest)
        node_state_path.symlink_to(external_manifest)

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
    if symlink_manifest:
        assert node_state_path.is_symlink()
        assert external_manifest.read_bytes() == original_manifest
        return
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
    original_create_branch = branch_export_attempts.create_branch_export_ref

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
        branch_export_attempts,
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
        export_record_path(output.stages_dir).read_text(encoding="utf-8")
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

    record_path = export_record_path(output.stages_dir)
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
        export_record_path(output.stages_dir).read_text(encoding="utf-8")
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
        export_record_path(output.stages_dir).read_text(encoding="utf-8")
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
    record_path = export_record_path(output.stages_dir)
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


def test_workspace_export_filename_preserves_colliding_logical_names() -> None:
    first = build_workspace_export_filename("primary/worktree")
    second = build_workspace_export_filename("primary worktree")

    assert first != second
    assert first.endswith(".json")
    assert second.endswith(".json")
    assert len(first) <= 180
    assert len(second) <= 180
