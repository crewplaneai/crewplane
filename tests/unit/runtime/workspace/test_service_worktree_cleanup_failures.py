from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path

import pytest

import crewplane.runtime.workspace.prepared_workspace as prepared_workspace_module
import crewplane.runtime.workspace.service.worktree_failures as worktree_failures
import crewplane.runtime.workspace.terminalization as terminalization_module
import crewplane.runtime.workspace.worktree.orchestration as workspace_worktree
from crewplane.core.preflight.models import WorkspaceSourceSnapshot
from crewplane.runtime.execution.workspace_files.generated import (
    GeneratedFileWorkspaceRegistry,
)
from crewplane.runtime.workspace import (
    prepare_invocation_workspace,
)
from crewplane.runtime.workspace.filesystem import (
    remove_workspace_path,
)
from crewplane.runtime.workspace.worktree import remove_worktree_workspace
from crewplane.runtime.workspace.worktree.types import WorktreeSourceRef
from tests.helpers.artifacts import node_artifact_request
from tests.helpers.workspace_service import (
    create_git_repo,
    read_json_object,
    workspace_invocation_context,
    workspace_invocation_request,
    workspace_output_manager,
    workspace_plan,
)
from tests.unit.runtime.workspace.service_worktree_support import (
    workspace_run_refs,
)


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
    assert workspace_run_refs(repo) == ""
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

    assert workspace_run_refs(repo) == ""
    remove_worktree_workspace(source, prepared.workspace_path)


def _exception_notes_contain(exc: BaseException, expected: str) -> bool:
    return any(expected in note for note in getattr(exc, "__notes__", ()))
