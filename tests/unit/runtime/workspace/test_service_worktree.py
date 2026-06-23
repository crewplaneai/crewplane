from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

import crewplane.runtime.workspace.prepared_workspace as prepared_workspace_module
import crewplane.runtime.workspace.worktree as workspace_worktree
from crewplane.artifacts.generated_files.catalog import (
    snapshot_generated_file_workspace,
)
from crewplane.runtime.execution.provider_call.generated_files import (
    changed_generated_file_paths,
)
from crewplane.runtime.workspace import (
    PreparedWorkspace,
    prepare_invocation_workspace,
)
from crewplane.runtime.workspace.snapshot import remove_workspace_path
from crewplane.runtime.workspace.worktree import remove_worktree_workspace
from crewplane.runtime.workspace.worktree.types import WorktreeSourceRef
from tests.helpers.workspace_service import (
    create_git_repo,
    read_json_object,
    run_git_text,
    workspace_invocation_context,
    workspace_invocation_request,
    workspace_output_manager,
    workspace_plan,
)


def test_changed_generated_file_paths_without_workspace_context_returns_empty(
    tmp_path: Path,
) -> None:
    prepared = PreparedWorkspace(
        cwd=tmp_path,
        invocation_context=workspace_invocation_context(),
    )

    assert changed_generated_file_paths(prepared, tmp_path) == set()


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
    output.create_stage_dir("implement")

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
        output.create_stage_dir("implement") / "workspace-state.json"
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


def test_worktree_workspace_provisioning_cleanup_failure_notes_primary_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
    source = plan.workspace_source
    assert source is not None
    source_ref = WorktreeSourceRef(
        source_kind="project",
        source_node_id=None,
        source_commit="f" * 40,
        source_tree=source.source_tree,
    )

    def fail_remove_worktree_workspace(
        source_arg: object, workspace_path: Path
    ) -> None:
        del source_arg, workspace_path
        raise RuntimeError("cleanup boom")

    monkeypatch.setattr(
        workspace_worktree,
        "remove_worktree_workspace",
        fail_remove_worktree_workspace,
    )

    with pytest.raises(RuntimeError, match="source commit is unavailable") as exc_info:
        workspace_worktree.create_worktree_workspace(
            plan,
            "bad-source",
            source,
            source_ref,
        )

    assert _exception_notes_contain(
        exc_info.value,
        "Workspace cleanup after worktree provisioning failure failed: cleanup boom",
    )
    shutil.rmtree(cache_root, ignore_errors=True)


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
    output.create_stage_dir("implement")

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


def test_worktree_capture_cleans_result_refs_when_bundle_export_fails(
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
    output.create_stage_dir("implement")
    prepared = prepare_invocation_workspace(
        workspace_invocation_request(plan, output),
        workspace_invocation_context(),
    )
    assert prepared.workspace_path is not None
    source = plan.workspace_source
    assert source is not None
    (prepared.cwd / "result.txt").write_text("captured\n", encoding="utf-8")

    def fail_export_bundle(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeError("bundle boom")

    monkeypatch.setattr(workspace_worktree, "export_bundle", fail_export_bundle)

    with pytest.raises(RuntimeError, match="bundle boom"):
        prepared.mark_succeeded()

    assert _workspace_run_refs(repo) == ""
    remove_worktree_workspace(source, prepared.workspace_path)


def test_worktree_capture_cleans_result_refs_when_state_recording_fails(
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
    output.create_stage_dir("implement")
    prepared = prepare_invocation_workspace(
        workspace_invocation_request(plan, output),
        workspace_invocation_context(),
    )
    assert prepared.workspace_path is not None
    source = plan.workspace_source
    assert source is not None
    (prepared.cwd / "result.txt").write_text("captured\n", encoding="utf-8")

    def fail_update_workspace_state(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeError("state write boom")

    monkeypatch.setattr(
        prepared_workspace_module,
        "update_workspace_state",
        fail_update_workspace_state,
    )

    with pytest.raises(RuntimeError, match="state write boom"):
        prepared.mark_succeeded()

    assert _workspace_run_refs(repo) == ""
    remove_worktree_workspace(source, prepared.workspace_path)


def test_worktree_generated_file_snapshot_includes_created_ignored_file(
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
    stage_dir = output.create_stage_dir("implement")

    prepared = prepare_invocation_workspace(
        workspace_invocation_request(plan, output),
        workspace_invocation_context(),
    )
    assert prepared.workspace_path is not None
    source = plan.workspace_source
    assert source is not None
    ignored_file = prepared.cwd / "ignored-output" / "report.txt"
    ignored_file.parent.mkdir()
    ignored_file.write_text("ignored generated content\n", encoding="utf-8")
    provider_output = stage_dir / "alpha_round1.md"
    provider_output.write_text(
        "Created `ignored-output/report.txt`.\n",
        encoding="utf-8",
    )

    snapshot = snapshot_generated_file_workspace(
        provider_output,
        prepared.cwd,
        changed_generated_file_paths(prepared, prepared.cwd),
    )

    assert (snapshot / "ignored-output" / "report.txt").read_text(
        encoding="utf-8"
    ) == "ignored generated content\n"
    remove_worktree_workspace(source, prepared.workspace_path)


def test_worktree_capture_rejects_replaced_workspace_root_symlink(
    tmp_path: Path,
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
    output.create_stage_dir("implement")
    prepared = prepare_invocation_workspace(
        workspace_invocation_request(plan, output),
        workspace_invocation_context(),
    )
    assert prepared.workspace_path is not None
    source = plan.workspace_source
    assert source is not None
    workspace_path = prepared.workspace_path
    outside = tmp_path / "outside"
    outside.mkdir()
    external_checkout = outside / "checkout"
    run_git_text(
        repo,
        "worktree",
        "add",
        "--detach",
        external_checkout.as_posix(),
        source.run_base_commit,
    )
    remove_worktree_workspace(source, workspace_path)
    try:
        workspace_path.symlink_to(outside, target_is_directory=True)
    except OSError:
        _remove_git_worktree_best_effort(repo, external_checkout)
        pytest.skip("symlink creation is unavailable")

    try:
        with pytest.raises(RuntimeError, match="Workspace capture root"):
            prepared.mark_succeeded()
    finally:
        remove_workspace_path(workspace_path)
        _remove_git_worktree_best_effort(repo, external_checkout)


def test_worktree_capture_rejects_replaced_checkout_symlink(
    tmp_path: Path,
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
    output.create_stage_dir("implement")
    prepared = prepare_invocation_workspace(
        workspace_invocation_request(plan, output),
        workspace_invocation_context(),
    )
    assert prepared.workspace_path is not None
    source = plan.workspace_source
    assert source is not None
    workspace_path = prepared.workspace_path
    external_checkout = tmp_path / "external-checkout"
    run_git_text(
        repo,
        "worktree",
        "add",
        "--detach",
        external_checkout.as_posix(),
        source.run_base_commit,
    )
    remove_worktree_workspace(source, workspace_path)
    workspace_path.mkdir(parents=True)
    try:
        (workspace_path / "checkout").symlink_to(
            external_checkout,
            target_is_directory=True,
        )
    except OSError:
        remove_workspace_path(workspace_path)
        _remove_git_worktree_best_effort(repo, external_checkout)
        pytest.skip("symlink creation is unavailable")

    try:
        with pytest.raises(RuntimeError, match="Workspace capture checkout"):
            prepared.mark_succeeded()
    finally:
        remove_workspace_path(workspace_path)
        _remove_git_worktree_best_effort(repo, external_checkout)


def test_worktree_capture_rejects_checkout_gitdir_for_external_worktree(
    tmp_path: Path,
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
    output.create_stage_dir("implement")
    prepared = prepare_invocation_workspace(
        workspace_invocation_request(plan, output),
        workspace_invocation_context(),
    )
    assert prepared.workspace_path is not None
    source = plan.workspace_source
    assert source is not None
    workspace_path = prepared.workspace_path
    checkout_root = workspace_path / "checkout"
    external_checkout = tmp_path / "external-checkout"
    run_git_text(
        repo,
        "worktree",
        "add",
        "--detach",
        external_checkout.as_posix(),
        source.run_base_commit,
    )
    remove_worktree_workspace(source, workspace_path)
    checkout_root.mkdir(parents=True)
    (checkout_root / ".git").write_text(
        (external_checkout / ".git").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    try:
        with pytest.raises(RuntimeError, match="does not belong to checkout"):
            prepared.mark_succeeded()
    finally:
        remove_workspace_path(workspace_path)
        _remove_git_worktree_best_effort(repo, external_checkout)


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
    output.create_stage_dir("implement")

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
    run_git_text(prepared.cwd, "checkout", "-b", "provider-branch")

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


def _remove_git_worktree_best_effort(repo: Path, checkout: Path) -> None:
    subprocess.run(
        [
            "git",
            "-C",
            repo.as_posix(),
            "worktree",
            "remove",
            "--force",
            "--force",
            checkout.as_posix(),
        ],
        check=False,
        capture_output=True,
    )


def _workspace_run_refs(repo: Path) -> str:
    return run_git_text(
        repo,
        "for-each-ref",
        "--format=%(refname)",
        "refs/crewplane/runs/workspace-run-001",
    )


def _exception_notes_contain(exc: BaseException, expected: str) -> bool:
    return any(expected in note for note in getattr(exc, "__notes__", ()))
