from __future__ import annotations

from pathlib import Path

import pytest

from crewplane.runtime.workspace import prepare_invocation_workspace
from crewplane.runtime.workspace.worktree.inspection import changed_paths
from tests.helpers import isolated_git as isolated_git_support
from tests.helpers.artifacts import node_artifact_request
from tests.helpers.workspace_service import (
    create_git_repo,
    read_json_object,
    run_git_text,
    workspace_invocation_context,
    workspace_invocation_request,
    workspace_output_manager,
    workspace_plan,
)

isolated_git = isolated_git_support.isolated_git
pytestmark = pytest.mark.usefixtures("isolated_git")


@pytest.mark.parametrize("target_kind", ["file", "directory", "dangling"])
@pytest.mark.parametrize("retarget", [False, True], ids=["unchanged", "retargeted"])
def test_snapshot_symlinks_preserve_targets_and_publish_terminal_state(
    tmp_path: Path,
    target_kind: str,
    retarget: bool,
) -> None:
    repo = create_git_repo(tmp_path)
    target = tmp_path / "external-target"
    if target_kind == "directory":
        target.mkdir()
        (target / "private.txt").write_text("private", encoding="utf-8")
    elif target_kind == "file":
        target.write_text("private", encoding="utf-8")
    (repo / "link").symlink_to(target, target_is_directory=target_kind == "directory")
    run_git_text(repo, "add", "link")
    run_git_text(repo, "commit", "-m", "add source symlink")
    plan = workspace_plan(repo, tmp_path / "cache", cleanup_on_success=True)
    output = workspace_output_manager(tmp_path, repo)
    output.create_node_dir(node_artifact_request("implement"))

    prepared = prepare_invocation_workspace(
        workspace_invocation_request(plan, output),
        workspace_invocation_context(),
    )
    assert prepared.workspace_path is not None
    assert prepared.state_path is not None
    assert prepared.initial_snapshot_entries is not None
    assert set(prepared.initial_snapshot_entries) == {"README.md", "link"}
    link = prepared.cwd / "link"
    assert link.is_symlink()
    assert link.readlink() == target
    if target_kind == "file":
        target.write_text("outside changed", encoding="utf-8")
    elif target_kind == "directory":
        (target / "private.txt").write_text("outside changed", encoding="utf-8")
    if retarget:
        link.unlink()
        link.symlink_to(tmp_path / "new-target")

    prepared.mark_succeeded()

    state = read_json_object(prepared.state_path)
    workspace = state["workspace"]
    assert isinstance(workspace, dict)
    assert state["status"] == "succeeded"
    assert workspace["retention"] == "deleted"
    assert state["result"] == {
        "lineage_produced": False,
        "drift_scan_complete": True,
        "snapshot_drift_discarded": retarget,
        "changed_path_count": int(retarget),
        "changed_paths": ["link"] if retarget else [],
        "changed_paths_truncated": False,
    }
    assert not prepared.workspace_path.exists()
    assert (repo / "link").readlink() == target
    if target_kind == "file":
        assert target.read_text(encoding="utf-8") == "outside changed"
    elif target_kind == "directory":
        assert (target / "private.txt").read_text(encoding="utf-8") == "outside changed"
    else:
        assert not target.exists()


def test_staged_rename_captures_both_paths_and_persists_result_tree(
    tmp_path: Path,
) -> None:
    repo = create_git_repo(tmp_path)
    plan = workspace_plan(
        repo, tmp_path / "cache", cleanup_on_success=True, kind="worktree"
    )
    output = workspace_output_manager(tmp_path, repo)
    output.create_node_dir(node_artifact_request("implement"))
    prepared = prepare_invocation_workspace(
        workspace_invocation_request(plan, output),
        workspace_invocation_context(),
    )
    assert prepared.state_path is not None
    assert prepared.workspace_path is not None
    run_git_text(prepared.cwd, "mv", "README.md", "renamed guide.md")

    assert changed_paths(prepared.cwd) == ("renamed guide.md", "README.md")
    prepared.mark_succeeded()

    state = read_json_object(prepared.state_path)
    workspace = state["workspace"]
    result = state["result"]
    bundle = state["bundle"]
    assert isinstance(workspace, dict)
    assert isinstance(result, dict)
    assert isinstance(bundle, dict)
    assert state["status"] == "succeeded"
    assert workspace["retention"] == "deleted"
    assert result["changed_path_count"] == 2
    commit = result["result_commit"]
    assert run_git_text(repo, "show", f"{commit}:renamed guide.md") == "ready"
    assert run_git_text(repo, "ls-tree", "--name-only", commit) == "renamed guide.md"
    assert (
        run_git_text(repo, "rev-parse", f"{commit}^{{tree}}") == result["result_tree"]
    )
    assert (output.stages_dir / bundle["path"]).is_file()
    assert not prepared.workspace_path.exists()
    assert (repo / "README.md").read_text(encoding="utf-8") == "ready\n"
    assert not (repo / "renamed guide.md").exists()
