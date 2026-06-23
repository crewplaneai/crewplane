from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from crewplane.artifacts import OutputManager
from crewplane.artifacts.naming import build_workspace_export_filename
from crewplane.runtime.workspace.branch_export import (
    fulfill_branch_exports,
    fulfill_branch_exports_from_history,
    preview_branch_exports_from_history,
)
from crewplane.runtime.workspace.branch_export.records import (
    checkpoint_from_record,
)
from crewplane.runtime.workspace.worktree.types import WorktreeSourceRef
from tests.helpers.workspace_branch_export import (
    branch_export_plan,
    history_record_for_output,
    write_node_manifest,
    write_result_bundle,
    write_tree_bundle,
    write_workspace_state,
)
from tests.helpers.workspace_lineage_bundles import create_prerequisite_bundle_chain
from tests.helpers.workspace_service import (
    create_git_repo,
    git_commit_exists,
    run_git_text,
)


def test_preview_branch_exports_rejects_result_ref_that_is_not_a_commit(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    plan = branch_export_plan(repo, tmp_path, branch_name="feature/preview")
    output = OutputManager("workspace", base_dir=tmp_path / "artifacts")
    result_tree, result_ref, bundle_path = write_tree_bundle(
        repo,
        output.create_stage_dir("implement"),
    )
    write_workspace_state(
        output.stages_dir,
        plan,
        result_tree,
        result_tree,
        result_ref,
        bundle_path,
    )
    history = history_record_for_output(output)

    records = preview_branch_exports_from_history(plan, history)

    assert len(records) == 1
    record = records[0]
    assert record["dry_run"] is True
    assert record["status"] == "failed_verification"
    assert "recorded commit and tree" in record["failure_message"]
    assert run_git_text(repo, "branch", "--list", "feature/preview") == ""


def test_checkpoint_from_record_rejects_malformed_required_fields(
    tmp_path: Path,
) -> None:
    payload = {
        "workspace_state_artifact": "implement/workspace-state.json",
        "node_id": 123,
        "task_id": "alpha",
        "result_commit": "a" * 40,
        "result_tree": "b" * 40,
        "result_ref": "refs/crewplane/run/implement/result",
        "bundle": {
            "path": "implement/workspace-bundles/result.bundle",
            "sha256": "c" * 64,
            "size_bytes": "12",
        },
    }

    with pytest.raises(RuntimeError, match="Invalid branch export checkpoint record"):
        checkpoint_from_record(tmp_path, payload)


def test_checkpoint_from_record_accepts_valid_required_fields(
    tmp_path: Path,
) -> None:
    payload = {
        "workspace_state_artifact": "implement/workspace-state.json",
        "node_id": "implement",
        "task_id": "alpha",
        "result_commit": "a" * 40,
        "result_tree": "b" * 40,
        "result_ref": "refs/crewplane/run/implement/result",
        "bundle": {
            "path": "implement/workspace-bundles/result.bundle",
            "sha256": "c" * 64,
            "size_bytes": 12,
        },
    }

    checkpoint = checkpoint_from_record(tmp_path, payload)

    assert checkpoint is not None
    assert checkpoint.state_path == tmp_path / "implement/workspace-state.json"
    assert checkpoint.node_id == "implement"
    assert checkpoint.bundle_size_bytes == 12


def test_fulfill_branch_exports_from_history_writes_duplicate_skip_record(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    plan = branch_export_plan(repo, tmp_path, branch_name="feature/history")
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
    history = history_record_for_output(output)

    records = fulfill_branch_exports_from_history(plan, history)

    assert len(records) == 1
    assert records[0].parent == output.stages_dir / "workspace-exports"
    assert records[0].name == build_workspace_export_filename("primary")
    record = json.loads(records[0].read_text(encoding="utf-8"))
    assert record["run_id"] == history.manifest.run_id
    assert record["run_key_name"] == history.manifest.run_key_name
    assert record["branch_name"] == "feature/history"
    assert run_git_text(repo, "rev-parse", "refs/heads/feature/history") == (
        result_commit
    )


def test_fulfill_branch_exports_writes_skipped_record_when_disabled(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    plan = branch_export_plan(
        repo,
        tmp_path,
        branch_name=None,
        create_branch=False,
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
    node_state_path = write_node_manifest(output, plan)

    records = fulfill_branch_exports(plan, output)

    assert len(records) == 1
    record = json.loads(records[0].read_text(encoding="utf-8"))
    assert record["status"] == "skipped"
    assert record["operation"] == "skipped"
    assert record["skip_reason"] == "create_branch_false"
    assert record["branch_name"] is None
    state = json.loads(
        (output.create_stage_dir("implement") / "workspace-state.json").read_text(
            encoding="utf-8"
        )
    )
    assert state["branch_export"]["status"] == "skipped"
    assert state["branch_export"]["operation"] == "skipped"
    assert state["branch_export"]["record_artifact"].startswith("workspace-exports/")
    node_state = json.loads(node_state_path.read_text(encoding="utf-8"))
    manifest_state = node_state["workspace"]["states"][0]
    assert manifest_state["branch_export"]["status"] == "skipped"


def test_preview_branch_exports_verifies_without_creating_ref(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    plan = branch_export_plan(repo, tmp_path, branch_name="feature/preview")
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
    history = history_record_for_output(output)

    records = preview_branch_exports_from_history(plan, history)

    assert len(records) == 1
    record = records[0]
    assert record["dry_run"] is True
    assert record["status"] == "fulfilled"
    assert record["operation"] == "created"
    assert record["branch_exists_before"] is False
    assert record["branch_exists_after"] is False
    assert not run_git_text(repo, "branch", "--list", "feature/preview")


def test_preview_and_history_fulfillment_verify_existing_expected_branch(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    plan = branch_export_plan(repo, tmp_path, branch_name="feature/preview")
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
    run_git_text(repo, "update-ref", "refs/heads/feature/preview", result_commit)
    history = history_record_for_output(output)

    preview_records = preview_branch_exports_from_history(plan, history)
    record_paths = fulfill_branch_exports_from_history(plan, history)

    assert len(preview_records) == 1
    assert preview_records[0]["dry_run"] is True
    assert preview_records[0]["status"] == "fulfilled"
    assert preview_records[0]["operation"] == "verified_existing"
    assert len(record_paths) == 1
    record = json.loads(record_paths[0].read_text(encoding="utf-8"))
    assert record["status"] == "fulfilled"
    assert record["operation"] == "verified_existing"
    assert record["branch_exists_before"] is True
    assert record["branch_exists_after"] is True


def test_preview_branch_exports_verifies_chained_bundles_without_branch_ref(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    plan = branch_export_plan(repo, tmp_path, branch_name="feature/preview")
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
    history = history_record_for_output(output)

    records = preview_branch_exports_from_history(plan, history)

    assert len(records) == 1
    record = records[0]
    assert record["dry_run"] is True
    assert record["status"] == "fulfilled"
    assert record["operation"] == "created"
    assert not git_commit_exists(repo, first.commit)
    assert not git_commit_exists(repo, second.commit)
    assert not run_git_text(repo, "branch", "--list", "feature/preview")


def test_preview_branch_exports_verifies_sha256_chained_bundles(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    try:
        repo = create_git_repo(tmp_path, object_format="sha256")
    except subprocess.CalledProcessError as exc:
        pytest.skip(f"git sha256 object format is unavailable: {exc}")
    plan = branch_export_plan(repo, tmp_path, branch_name="feature/preview")
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
    history = history_record_for_output(output)

    records = preview_branch_exports_from_history(plan, history)

    assert len(records) == 1
    record = records[0]
    assert record["dry_run"] is True
    assert record["status"] == "fulfilled"
    assert record["operation"] == "created"
    assert not git_commit_exists(repo, first.commit)
    assert not git_commit_exists(repo, second.commit)
    assert not run_git_text(repo, "branch", "--list", "feature/preview")
