from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from crewplane.artifacts import OutputManager
from crewplane.artifacts.naming import build_workspace_export_filename
from crewplane.runtime.workspace.branch_export import (
    fulfill_branch_exports,
)
from crewplane.runtime.workspace.branch_export import git as branch_export_git
from crewplane.runtime.workspace.worktree.types import WorktreeSourceRef
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
        output.create_stage_dir("implement"),
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
        (output.create_stage_dir("implement") / "workspace-state.json").read_text(
            encoding="utf-8"
        )
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
            (output.create_stage_dir("implement") / "workspace-state.json").read_bytes()
        ).hexdigest()
    )


def test_fulfill_branch_exports_verifies_existing_expected_branch_without_record(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    plan = branch_export_plan(repo, tmp_path, branch_name="feature/exported")
    output = OutputManager("workspace", base_dir=tmp_path / "artifacts")
    result_commit, result_tree, result_ref, bundle_path = write_result_bundle(
        repo,
        output.create_stage_dir("implement"),
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

    records = fulfill_branch_exports(plan, output)

    assert len(records) == 1
    record = json.loads(records[0].read_text(encoding="utf-8"))
    assert record["status"] == "fulfilled"
    assert record["operation"] == "verified_existing"
    assert record["branch_exists_before"] is True
    assert record["branch_exists_after"] is True
    assert run_git_text(repo, "rev-parse", "refs/heads/feature/exported") == (
        result_commit
    )
    state = json.loads(
        (output.create_stage_dir("implement") / "workspace-state.json").read_text(
            encoding="utf-8"
        )
    )
    assert state["branch_export"]["operation"] == "verified_existing"


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
        output.create_stage_dir("implement"),
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
        (output.create_stage_dir("implement") / "workspace-state.json").read_text(
            encoding="utf-8"
        )
    )
    assert state["branch_export"]["status"] == "failed_verification"


def test_fulfill_branch_exports_verifies_existing_recorded_branch(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    plan = branch_export_plan(repo, tmp_path, branch_name="feature/exported")
    output = OutputManager("workspace", base_dir=tmp_path / "artifacts")
    result_commit, result_tree, result_ref, bundle_path = write_result_bundle(
        repo,
        output.create_stage_dir("implement"),
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
    assert record["operation"] == "verified_existing"
    assert record["branch_exists_before"] is True
    assert record["branch_exists_after"] is True
    assert run_git_text(repo, "rev-parse", "refs/heads/feature/exported") == (
        result_commit
    )
    state = json.loads(
        (output.create_stage_dir("implement") / "workspace-state.json").read_text(
            encoding="utf-8"
        )
    )
    assert state["branch_export"]["operation"] == "verified_existing"


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
        output.create_stage_dir("implement"),
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
        output.create_stage_dir("implement"),
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
        ({"size_bytes": True}, "lacks a bundle"),
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
        output.create_stage_dir("implement"),
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
        output.create_stage_dir("implement"),
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
            output.create_stage_dir("implement"),
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


def test_fulfill_branch_exports_imports_upstream_bundles_in_dependency_order(
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
        output.create_stage_dir("implement") / "workspace-bundles" / "second.bundle",
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

    fulfill_branch_exports(plan, output)

    assert git_commit_exists(repo, first.commit)
    assert git_commit_exists(repo, second.commit)
    assert run_git_text(repo, "rev-parse", "refs/heads/feature/exported") == (
        second.commit
    )


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
            if args[:1] == ("update-ref",):
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
