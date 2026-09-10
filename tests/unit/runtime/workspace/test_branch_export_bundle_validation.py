from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from crewplane.artifacts import OutputManager
from crewplane.runtime.workspace.branch_export import (
    fulfill_branch_exports,
)
from crewplane.runtime.workspace.worktree.types import WorktreeSourceRef
from tests.helpers.artifacts import node_artifact_request
from tests.helpers.workspace_branch_export import (
    branch_export_plan,
    update_state_bundle_metadata,
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
from tests.unit.runtime.workspace.branch_export_support import (
    export_record_path,
)


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

    record_path = export_record_path(output.stages_dir)
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
    record_path = export_record_path(output.stages_dir)
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record["status"] == "failed_verification"
    assert "recorded commit and tree" in record["failure_message"]


@pytest.mark.parametrize(
    ("identity_field", "foreign_value"),
    [
        ("run_id", "foreign-run-id"),
        ("run_key_name", "foreign-run-key"),
        ("git.repo_id", "foreign-repository"),
    ],
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
