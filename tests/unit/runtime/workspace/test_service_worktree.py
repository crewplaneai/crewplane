from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from crewplane.artifacts.generated_files.catalog import (
    snapshot_generated_file_workspace,
)
from crewplane.runtime.execution.provider_call.generated_files import (
    capture_generated_file_change_baseline,
)
from crewplane.runtime.workspace import (
    PreparedWorkspace,
    prepare_invocation_workspace,
)
from crewplane.runtime.workspace.git import GitCommand
from crewplane.runtime.workspace.worktree import remove_worktree_workspace
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
from tests.helpers.workspace_worktree_reuse import with_node_setup
from tests.unit.runtime.workspace.service_worktree_support import (
    workspace_run_refs,
)


def test_project_root_success_without_workspace_state_is_noop(tmp_path: Path) -> None:
    prepared = PreparedWorkspace(
        cwd=tmp_path,
        invocation_context=workspace_invocation_context(),
    )

    prepared.mark_succeeded()


def test_managed_workspace_success_requires_state_path(tmp_path: Path) -> None:
    prepared = PreparedWorkspace(
        cwd=tmp_path,
        invocation_context=workspace_invocation_context(),
        workspace_kind="snapshot",
        workspace_path=tmp_path / "workspace",
    )

    with pytest.raises(
        RuntimeError,
        match="Workspace success requires workspace and state paths",
    ):
        prepared.mark_succeeded()


def test_worktree_success_requires_capture_metadata(tmp_path: Path) -> None:
    workspace_path = tmp_path / "workspace"
    workspace_path.mkdir()
    prepared = PreparedWorkspace(
        cwd=tmp_path,
        invocation_context=workspace_invocation_context(),
        workspace_kind="worktree",
        workspace_path=workspace_path,
        state_path=tmp_path / "workspace-state.json",
        lineage_producer=True,
    )

    with pytest.raises(
        RuntimeError,
        match="Workspace success requires worktree capture metadata",
    ):
        prepared.mark_succeeded()


def test_worktree_workspace_captures_result_commit_and_bundle(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    cache_root = tmp_path / "cache"
    plan = workspace_plan(
        repo,
        cache_root,
        cleanup_on_success=True,
        kind="worktree",
    )
    output = workspace_output_manager(tmp_path, repo)
    output.create_node_dir(node_artifact_request("implement"))

    prepared = prepare_invocation_workspace(
        workspace_invocation_request(plan, output),
        workspace_invocation_context(),
    )
    assert prepared.workspace_path is not None
    workspace_path = prepared.workspace_path
    source = plan.workspace_source
    assert source is not None
    assert workspace_path.parent == (
        cache_root / "workspaces" / source.repository_id / plan.run_key_name
    )
    (prepared.cwd / "result.txt").write_text("captured\n", encoding="utf-8")

    prepared.mark_succeeded()

    state = read_json_object(
        output.create_node_dir(node_artifact_request("implement"))
        / "workspace-state.json"
    )
    result = state["result"]
    bundle = state["bundle"]
    execution = state["execution"]
    assert isinstance(result, dict)
    assert isinstance(bundle, dict)
    assert execution["cache_root"] == cache_root.as_posix()
    assert execution["workspace_path"] == workspace_path.as_posix()
    assert execution["checkout_root"] == (workspace_path / "checkout").as_posix()
    assert execution["checkout_size_bytes"] >= len("ready\n")
    assert execution["effective_cwd"] == (workspace_path / "checkout").as_posix()
    assert execution["provisioning_duration_seconds"] >= 0
    assert state["invoker"]["launch_mode"] == "runtime_command_runner"
    assert state["invoker"]["controlled_child_environment"] is True
    assert state["git"]["worktree_lock_mode"] in {
        "add_lock_reason",
        "lock_after_add",
    }
    assert result["changed_path_count"] == 1
    assert isinstance(result["result_commit"], str)
    assert (
        run_git_text(repo, "show", f"{result['result_commit']}:result.txt")
        == "captured"
    )
    bundle_path = output.stages_dir / str(bundle["path"])
    assert bundle_path.is_file()
    assert int(bundle["size_bytes"]) == bundle_path.stat().st_size
    assert not workspace_path.exists()


def test_worktree_setup_allows_large_ignored_files(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    (repo / ".gitignore").write_text("weights/\n", encoding="utf-8")
    run_git_text(repo, "add", ".gitignore")
    run_git_text(repo, "commit", "-m", "ignore setup weights")
    plan = with_node_setup(
        workspace_plan(repo, tmp_path / "cache", True, kind="worktree"),
        "implement",
        [
            [
                sys.executable,
                "-c",
                "from pathlib import Path\n"
                "Path('weights').mkdir()\n"
                "with Path('weights/model.bin').open('wb') as handle:\n"
                "    handle.truncate(4 * 1024**3 + 1)\n",
            ]
        ],
    )
    output = workspace_output_manager(tmp_path, repo)
    output.create_node_dir(node_artifact_request("implement"))

    prepared = prepare_invocation_workspace(
        workspace_invocation_request(plan, output),
        workspace_invocation_context(),
    )
    assert prepared.workspace_path is not None
    assert prepared.state_path is not None
    assert (prepared.cwd / "weights/model.bin").stat().st_size == 4 * 1024**3 + 1
    baseline = capture_generated_file_change_baseline(prepared)
    assert baseline is not None
    result_file = prepared.cwd / "result.txt"
    result_file.write_text("captured\n", encoding="utf-8")
    assert baseline.candidate_files() == (result_file,)

    prepared.mark_succeeded()

    state = read_json_object(prepared.state_path)
    assert state["status"] == "succeeded"
    assert state["setup"]["status"] == "succeeded"
    result_commit = state["result"]["result_commit"]
    assert run_git_text(repo, "show", f"{result_commit}:result.txt") == "captured"
    assert run_git_text(repo, "ls-tree", "-r", result_commit, "--", "weights") == ""
    assert not prepared.workspace_path.exists()


def test_worktree_result_capture_uses_temporary_index(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    cache_root = tmp_path / "cache"
    plan = workspace_plan(
        repo,
        cache_root,
        cleanup_on_success=False,
        kind="worktree",
    )
    output = workspace_output_manager(tmp_path, repo)
    output.create_node_dir(node_artifact_request("implement"))

    prepared = prepare_invocation_workspace(
        workspace_invocation_request(plan, output),
        workspace_invocation_context(),
    )
    assert prepared.workspace_path is not None
    source = plan.workspace_source
    assert source is not None
    (prepared.cwd / "result.txt").write_text("captured\n", encoding="utf-8")

    prepared.mark_succeeded()

    assert prepared.workspace_path.exists()
    assert not (prepared.workspace_path / "capture.index").exists()
    remove_worktree_workspace(source, prepared.workspace_path)


def test_worktree_capture_rejects_filesystem_race_after_private_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    plan = workspace_plan(
        repo,
        tmp_path / "cache",
        cleanup_on_success=False,
        kind="worktree",
    )
    output = workspace_output_manager(tmp_path, repo)
    output.create_node_dir(node_artifact_request("implement"))
    prepared = prepare_invocation_workspace(
        workspace_invocation_request(plan, output),
        workspace_invocation_context(),
    )
    assert prepared.workspace_path is not None
    source = plan.workspace_source
    assert source is not None
    result_path = prepared.cwd / "result.txt"
    result_path.write_text("staged\n", encoding="utf-8")
    original_run = GitCommand.run
    raced = False

    def race_after_write_tree(
        command: GitCommand,
        *args: str,
    ) -> subprocess.CompletedProcess[bytes]:
        nonlocal raced
        completed = original_run(command, *args)
        if args == ("write-tree",) and not raced:
            raced = True
            result_path.write_text("changed after staging\n", encoding="utf-8")
        return completed

    monkeypatch.setattr(GitCommand, "run", race_after_write_tree)

    with pytest.raises(subprocess.CalledProcessError):
        prepared.mark_succeeded()

    assert raced is True
    assert workspace_run_refs(repo) == ""
    remove_worktree_workspace(source, prepared.workspace_path)


def test_worktree_capture_excludes_new_ignored_file_force_added_by_provider(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    (repo / ".gitignore").write_text("ignored-output/\n", encoding="utf-8")
    run_git_text(repo, "add", ".gitignore")
    run_git_text(repo, "commit", "-m", "ignore provider output")
    plan = workspace_plan(
        repo,
        tmp_path / "cache",
        cleanup_on_success=False,
        kind="worktree",
    )
    output = workspace_output_manager(tmp_path, repo)
    output.create_node_dir(node_artifact_request("implement"))
    prepared = prepare_invocation_workspace(
        workspace_invocation_request(plan, output),
        workspace_invocation_context(),
    )
    assert prepared.workspace_path is not None
    source = plan.workspace_source
    assert source is not None
    ignored_file = prepared.cwd / "ignored-output" / "secret.txt"
    ignored_file.parent.mkdir()
    ignored_file.write_text("not accepted\n", encoding="utf-8")
    (prepared.cwd / "result.txt").write_text("accepted\n", encoding="utf-8")
    run_git_text(prepared.cwd, "add", "--force", "ignored-output/secret.txt")

    prepared.mark_succeeded()

    assert prepared.state_path is not None
    state = read_json_object(prepared.state_path)
    result = state["result"]
    refs = state["refs"]
    assert isinstance(result, dict)
    assert isinstance(refs, dict)
    tree_paths = run_git_text(
        repo,
        "ls-tree",
        "-r",
        "--name-only",
        str(result["result_tree"]),
    ).splitlines()
    assert "result.txt" in tree_paths
    assert "ignored-output/secret.txt" not in tree_paths
    assert result["changed_path_count"] == 1
    run_git_text(repo, "update-ref", "-d", str(refs["candidate"]))
    run_git_text(repo, "update-ref", "-d", str(refs["result"]))
    remove_worktree_workspace(source, prepared.workspace_path)


def test_worktree_generated_file_snapshot_uses_git_change_baseline(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    (repo / ".gitignore").write_text("ignored-output/\n", encoding="utf-8")
    run_git_text(repo, "add", ".gitignore")
    run_git_text(repo, "commit", "-m", "ignore generated output")
    plan = workspace_plan(
        repo,
        tmp_path / "cache",
        cleanup_on_success=False,
        kind="worktree",
    )
    output = workspace_output_manager(tmp_path, repo)
    stage_dir = output.create_node_dir(node_artifact_request("implement"))

    prepared = prepare_invocation_workspace(
        workspace_invocation_request(plan, output),
        workspace_invocation_context(),
    )
    assert prepared.workspace_path is not None
    source = plan.workspace_source
    assert source is not None
    baseline = capture_generated_file_change_baseline(prepared)
    assert baseline is not None
    ignored_file = prepared.cwd / "ignored-output" / "report.txt"
    ignored_file.parent.mkdir()
    ignored_file.write_text("ignored generated content\n", encoding="utf-8")
    generated_file = prepared.cwd / "report.txt"
    generated_file.write_text("generated content\n", encoding="utf-8")
    provider_output = stage_dir / "alpha_round1.md"
    provider_output.write_text(
        "Created `report.txt` and `ignored-output/report.txt`.\n",
        encoding="utf-8",
    )

    snapshot = snapshot_generated_file_workspace(
        provider_output,
        prepared.cwd,
        candidate_files=baseline.candidate_files(),
    )

    assert (snapshot / "report.txt").read_text(
        encoding="utf-8"
    ) == "generated content\n"
    assert not (snapshot / "ignored-output" / "report.txt").exists()
    remove_worktree_workspace(source, prepared.workspace_path)


def test_worktree_retry_reset_restores_attempt_baseline(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    cache_root = tmp_path / "cache"
    plan = workspace_plan(
        repo,
        cache_root,
        cleanup_on_success=False,
        kind="worktree",
    )
    output = workspace_output_manager(tmp_path, repo)
    output.create_node_dir(node_artifact_request("implement"))

    prepared = prepare_invocation_workspace(
        workspace_invocation_request(plan, output),
        workspace_invocation_context(),
    )
    assert prepared.workspace_path is not None
    assert prepared.invocation_context.retry_reset is not None
    source = plan.workspace_source
    assert source is not None
    dirty_path = prepared.cwd / "attempt.txt"
    dirty_path.write_text("dirty\n", encoding="utf-8")
    (prepared.cwd / "README.md").write_text("changed\n", encoding="utf-8")
    prepared.invocation_context.retry_reset()

    assert not dirty_path.exists()
    assert (prepared.cwd / "README.md").read_text(encoding="utf-8") == "ready\n"
    assert (
        run_git_text(prepared.cwd, "rev-parse", "HEAD^{commit}")
        == source.run_base_commit
    )
    assert run_git_text(prepared.cwd, "branch", "--show-current") == ""
    assert run_git_text(prepared.cwd, "status", "--porcelain=v1") == ""
    remove_worktree_workspace(source, prepared.workspace_path)
