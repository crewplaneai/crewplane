from __future__ import annotations

import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

import crewplane.runtime.workspace.prepared_workspace as prepared_workspace_module
import crewplane.runtime.workspace.service.worktree_failures as worktree_failures
import crewplane.runtime.workspace.terminalization as terminalization_module
import crewplane.runtime.workspace.worktree.orchestration as workspace_worktree
from crewplane.artifacts.generated_files.catalog import (
    snapshot_generated_file_workspace,
)
from crewplane.core.preflight.models import WorkspaceSourceSnapshot
from crewplane.runtime.execution.provider_call.generated_files import (
    capture_generated_file_change_baseline,
)
from crewplane.runtime.execution.workspace_files.generated import (
    GeneratedFileWorkspaceRegistry,
)
from crewplane.runtime.workspace import (
    PreparedWorkspace,
    prepare_invocation_workspace,
)
from crewplane.runtime.workspace.git import GitCommand
from crewplane.runtime.workspace.snapshot import remove_workspace_path
from crewplane.runtime.workspace.worktree import remove_worktree_workspace
from crewplane.runtime.workspace.worktree.types import WorktreeSourceRef
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


def test_terminal_cleanup_failure_persists_cleanup_diagnostic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    plan = workspace_plan(repo, tmp_path / "cache", cleanup_on_success=False)
    output = workspace_output_manager(tmp_path, repo)
    output.create_node_dir(node_artifact_request("implement"))
    prepared = prepare_invocation_workspace(
        workspace_invocation_request(plan, output),
        workspace_invocation_context(),
    )
    assert prepared.workspace_path is not None
    assert prepared.state_path is not None

    def fail_cleanup(path: Path) -> None:
        assert path == prepared.workspace_path
        raise RuntimeError("cleanup denied")

    monkeypatch.setattr(
        prepared_workspace_module,
        "remove_workspace_path",
        fail_cleanup,
    )

    prepared.mark_failed("provider failed")

    state = read_json_object(prepared.state_path)
    assert state["workspace"]["retention"] == "retained"
    assert state["workspace"]["retained_reason"] == "failure_cleanup_failed"
    assert state["diagnostics"] == [
        {"level": "error", "message": "provider failed"},
        {
            "level": "warning",
            "message": (
                "Workspace cleanup after terminal invocation state failed: "
                "cleanup denied"
            ),
        },
    ]
    remove_workspace_path(prepared.workspace_path)


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


def test_worktree_success_cleanup_retries_state_after_physical_removal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    plan = workspace_plan(
        repo,
        tmp_path / "cache",
        cleanup_on_success=True,
        kind="worktree",
    )
    output = workspace_output_manager(tmp_path, repo)
    node_dir = output.create_node_dir(node_artifact_request("implement"))
    prepared = prepare_invocation_workspace(
        workspace_invocation_request(plan, output),
        workspace_invocation_context(),
    )
    assert prepared.workspace_path is not None
    assert prepared.state_path is not None
    workspace_path = prepared.workspace_path
    state_path = prepared.state_path
    prepared.mark_succeeded(defer_cleanup=True)
    registry = GeneratedFileWorkspaceRegistry()
    registry.record(
        "implement",
        node_dir / "alpha_round1.md",
        None,
        prepared.cleanup_after_success,
    )
    original_publish = prepared_workspace_module.publish_workspace_cleanup_result
    publish_count = 0

    def fail_first_publish(
        state_path_arg: Path,
        deleted: bool,
        retained_reason: str | None = None,
    ) -> None:
        nonlocal publish_count
        publish_count += 1
        if publish_count == 1:
            raise OSError("injected retention write failure")
        original_publish(state_path_arg, deleted, retained_reason)

    monkeypatch.setattr(
        prepared_workspace_module,
        "publish_workspace_cleanup_result",
        fail_first_publish,
    )

    first_errors = registry.cleanup_node_best_effort("implement")

    assert [str(error) for error in first_errors] == [
        "injected retention write failure"
    ]
    assert not workspace_path.exists()
    assert read_json_object(state_path)["workspace"]["retention"] == "pending_cleanup"

    workspace_path.mkdir()
    retry_errors = registry.cleanup_node_best_effort("implement")

    assert [str(error) for error in retry_errors] == [
        "Workspace path reappeared after successful cleanup; cleanup state was "
        "retained."
    ]
    assert publish_count == 1
    remove_workspace_path(workspace_path)

    assert registry.cleanup_node_best_effort("implement") == ()
    assert publish_count == 2
    assert registry.cleanup_by_node == {}
    assert read_json_object(state_path)["workspace"]["retention"] == "deleted"


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

    with pytest.raises(
        RuntimeError,
        match="project source descriptor is contradictory",
    ) as exc_info:
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


def test_partial_worktree_failure_publishes_terminal_state_before_removal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    plan = workspace_plan(
        repo,
        tmp_path / "cache",
        cleanup_on_success=True,
        kind="worktree",
    )
    output = workspace_output_manager(tmp_path, repo)
    node_dir = output.create_node_dir(node_artifact_request("implement"))
    state_path = node_dir / "workspace-state.json"

    def fail_protected_ref_snapshot(
        git_top_level: str,
        protected_ref_scopes: tuple[str, ...] | None,
        source_ref: WorktreeSourceRef,
    ) -> None:
        del git_top_level, protected_ref_scopes, source_ref
        raise RuntimeError("injected post-add readiness failure")

    removal_states: list[tuple[str, str, bool]] = []
    original_remove = workspace_worktree.remove_worktree_workspace

    def observe_removal(
        source: WorkspaceSourceSnapshot,
        workspace_path: Path,
        expected_git_dir: Path | None = None,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> None:
        state = read_json_object(state_path)
        execution = state["execution"]
        removal_states.append(
            (
                str(state["status"]),
                str(state["workspace"]["retention"]),
                execution["worktree_git_dir"] == expected_git_dir.as_posix()
                if expected_git_dir is not None
                else False,
            )
        )
        original_remove(
            source,
            workspace_path,
            expected_git_dir,
            cancel_requested,
        )

    monkeypatch.setattr(
        workspace_worktree,
        "protected_ref_snapshot_for_source",
        fail_protected_ref_snapshot,
    )
    monkeypatch.setattr(
        workspace_worktree,
        "remove_worktree_workspace",
        observe_removal,
    )
    monkeypatch.setattr(
        worktree_failures,
        "remove_worktree_workspace",
        observe_removal,
    )

    with pytest.raises(RuntimeError, match="post-add readiness failure"):
        prepare_invocation_workspace(
            workspace_invocation_request(plan, output),
            workspace_invocation_context(),
        )

    assert removal_states == [("failed", "pending_cleanup", True)]
    state = read_json_object(state_path)
    assert state["status"] == "failed"
    assert state["workspace"]["retention"] == "deleted"


def test_partial_worktree_failure_before_exact_claim_is_retained(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    plan = workspace_plan(
        repo,
        tmp_path / "cache",
        cleanup_on_success=True,
        kind="worktree",
    )
    source = plan.workspace_source
    assert source is not None
    output = workspace_output_manager(tmp_path, repo)
    node_dir = output.create_node_dir(node_artifact_request("implement"))
    state_path = node_dir / "workspace-state.json"
    original_active_git_dir = workspace_worktree.active_git_dir

    def fail_exact_git_dir(checkout_root: Path) -> Path:
        del checkout_root
        raise RuntimeError("injected Git directory discovery failure")

    monkeypatch.setattr(
        workspace_worktree,
        "active_git_dir",
        fail_exact_git_dir,
    )

    with pytest.raises(RuntimeError, match="Git directory discovery failure"):
        prepare_invocation_workspace(
            workspace_invocation_request(plan, output),
            workspace_invocation_context(),
        )

    state = read_json_object(state_path)
    workspace_path = Path(str(state["execution"]["workspace_path"]))
    assert state["status"] == "failed"
    assert state["workspace"]["retention"] == "retained"
    assert state["execution"]["worktree_git_dir"] is None
    assert workspace_path.exists()

    monkeypatch.setattr(
        workspace_worktree,
        "active_git_dir",
        original_active_git_dir,
    )
    remove_worktree_workspace(source, workspace_path)


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
    assert _workspace_run_refs(repo) == ""
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
    output.create_node_dir(node_artifact_request("implement"))
    prepared = prepare_invocation_workspace(
        workspace_invocation_request(plan, output),
        workspace_invocation_context(),
    )
    assert prepared.workspace_path is not None
    assert prepared.state_path is not None
    (prepared.cwd / "result.txt").write_text("captured\n", encoding="utf-8")

    def fail_export_bundle(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeError("bundle boom")

    monkeypatch.setattr(workspace_worktree, "export_bundle", fail_export_bundle)

    with pytest.raises(RuntimeError, match="bundle boom"):
        prepared.mark_succeeded()

    prepared.mark_failed("bundle boom")

    state = read_json_object(prepared.state_path)
    assert _workspace_run_refs(repo) == ""
    assert state["status"] == "failed"
    assert state["ref_publication"]["phase"] == "removed"
    assert state["workspace"]["retention"] == "deleted"
    assert not prepared.workspace_path.exists()


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
    output.create_node_dir(node_artifact_request("implement"))
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
        terminalization_module,
        "update_workspace_state",
        fail_update_workspace_state,
    )

    with pytest.raises(RuntimeError, match="state write boom"):
        prepared.mark_succeeded()

    assert _workspace_run_refs(repo) == ""
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
    output.create_node_dir(node_artifact_request("implement"))
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


def test_worktree_capture_reports_unsafe_root_before_checkout_mismatch(
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
    output.create_node_dir(node_artifact_request("implement"))
    prepared = prepare_invocation_workspace(
        workspace_invocation_request(plan, output),
        workspace_invocation_context(),
    )
    assert prepared.workspace_path is not None
    assert prepared.worktree_capture is not None
    source = plan.workspace_source
    assert source is not None
    invalid_request = replace(
        prepared.worktree_capture,
        workspace_path=tmp_path / "missing-workspace",
        checkout_root=tmp_path / "unrelated-checkout",
    )

    with pytest.raises(RuntimeError, match="Workspace capture root is missing"):
        workspace_worktree.capture_worktree_result(invalid_request)

    remove_worktree_workspace(source, prepared.workspace_path)


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
    output.create_node_dir(node_artifact_request("implement"))
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
    output.create_node_dir(node_artifact_request("implement"))
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
        with pytest.raises(RuntimeError, match="Git dir changed after materialization"):
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
