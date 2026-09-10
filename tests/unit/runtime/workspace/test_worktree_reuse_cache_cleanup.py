from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from crewplane.runtime.workspace import prepare_invocation_workspace
from crewplane.runtime.workspace import prepared_workspace as prepared_workspace_module
from crewplane.runtime.workspace.service import MaterializationLimiter
from crewplane.runtime.workspace.state import WorkspaceStateRetention
from crewplane.runtime.workspace.worktree import (
    cache as worktree_cache,
)
from crewplane.runtime.workspace.worktree.cache import (
    ReusableWorktreeCheckout,
    WorktreeReuseCache,
)
from crewplane.runtime.workspace.worktree.types import WorktreeSourceRef
from tests.helpers.artifacts import node_artifact_request
from tests.helpers.resume import make_workspace_source_snapshot
from tests.helpers.workspace_service import (
    create_git_repo,
    read_json_object,
    workspace_invocation_context,
    workspace_output_manager,
)
from tests.helpers.workspace_worktree_reuse import (
    two_node_lineage_plan,
    workspace_request,
)


def test_reuse_cache_node_cleanup_updates_all_reused_state_paths(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    cache_root = tmp_path / "cache"
    plan = two_node_lineage_plan(repo, cache_root)
    output = workspace_output_manager(tmp_path, repo)
    output.create_node_dir(node_artifact_request("implement"))
    output.create_node_dir(node_artifact_request("verify"))
    reuse_cache = WorktreeReuseCache()
    limiter = MaterializationLimiter.from_plan(plan)

    first = prepare_invocation_workspace(
        workspace_request(plan, output, "implement", reuse_cache, limiter),
        workspace_invocation_context(),
    )
    assert first.workspace_path is not None
    workspace_path = first.workspace_path
    (first.cwd / "result.txt").write_text("captured\n", encoding="utf-8")
    first.mark_succeeded(defer_cleanup=True)
    first.cleanup_after_success()

    second = prepare_invocation_workspace(
        workspace_request(plan, output, "verify", reuse_cache, limiter),
        workspace_invocation_context(),
    )
    assert second.workspace_path == workspace_path
    second.mark_succeeded(defer_cleanup=True)
    second.cleanup_after_success()

    errors = reuse_cache.cleanup_node_best_effort("verify")

    assert errors == ()
    assert not workspace_path.exists()
    first_state = read_json_object(
        output.create_node_dir(node_artifact_request("implement"))
        / "workspace-state.json"
    )
    second_state = read_json_object(
        output.create_node_dir(node_artifact_request("verify")) / "workspace-state.json"
    )
    assert first_state["workspace"]["retention"] == "deleted"
    assert second_state["workspace"]["retention"] == "deleted"


def test_reuse_cache_cleanup_all_retains_lease_after_terminal_publish_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    cache_root = tmp_path / "cache"
    plan = two_node_lineage_plan(repo, cache_root)
    output = workspace_output_manager(tmp_path, repo)
    output.create_node_dir(node_artifact_request("implement"))
    output.create_node_dir(node_artifact_request("verify"))
    reuse_cache = WorktreeReuseCache()
    limiter = MaterializationLimiter.from_plan(plan)

    first = prepare_invocation_workspace(
        workspace_request(plan, output, "implement", reuse_cache, limiter),
        workspace_invocation_context(),
    )
    assert first.workspace_path is not None
    workspace_path = first.workspace_path
    (first.cwd / "result.txt").write_text("captured\n", encoding="utf-8")
    first.mark_succeeded(defer_cleanup=True)
    first.cleanup_after_success()

    second = prepare_invocation_workspace(
        workspace_request(plan, output, "verify", reuse_cache, limiter),
        workspace_invocation_context(),
    )
    assert second.workspace_path == workspace_path

    original_publish_terminal = (
        prepared_workspace_module.publish_terminal_workspace_state
    )

    def fail_terminal_publication(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise OSError("terminal publication failed")

    monkeypatch.setattr(
        prepared_workspace_module,
        "publish_terminal_workspace_state",
        fail_terminal_publication,
    )

    with pytest.raises(OSError, match="terminal publication failed"):
        second.mark_failed("provider failed")

    cleanup = reuse_cache.cleanup_all()

    assert len(cleanup.errors) == 1
    assert isinstance(cleanup.errors[0], RuntimeError)
    assert "remains leased by an unresolved invocation" in str(cleanup.errors[0])
    assert cleanup.updated_state_paths == ()
    assert workspace_path.exists()
    assert reuse_cache.owns(workspace_path)
    first_state = read_json_object(
        output.create_node_dir(node_artifact_request("implement"))
        / "workspace-state.json"
    )
    second_state = read_json_object(
        output.create_node_dir(node_artifact_request("verify")) / "workspace-state.json"
    )
    assert first_state["status"] == "succeeded"
    assert first_state["workspace"]["retention"] == "pending_cleanup"
    assert second_state["status"] == "running"
    assert second_state["workspace"]["retention"] == "pending"

    monkeypatch.setattr(
        prepared_workspace_module,
        "publish_terminal_workspace_state",
        original_publish_terminal,
    )
    second.mark_failed("test cleanup")

    assert reuse_cache.cleanup_all().errors == ()
    assert not workspace_path.exists()
    assert not reuse_cache.owns(workspace_path)


def test_reuse_cache_retries_stale_entry_cleanup_after_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    reuse_cache = WorktreeReuseCache()
    workspace_path = tmp_path / "workspace"
    checkout_root = workspace_path / "checkout"
    checkout_root.mkdir(parents=True)
    state_path = tmp_path / "workspace-state.json"
    state_path.write_text(
        json.dumps(
            {
                "status": "succeeded",
                "workspace": {
                    "retention": "pending_cleanup",
                    "retained_reason": "stage_finalization_pending",
                },
            }
        ),
        encoding="utf-8",
    )
    entry = ReusableWorktreeCheckout(
        node_id="implement",
        logical_worktree_name="primary",
        workspace_path=workspace_path,
        checkout_root=checkout_root,
        cwd=checkout_root,
        git_dir=workspace_path / "git-dir",
        source_commit="old-commit",
        source_tree="old-tree",
        source=make_workspace_source_snapshot(),
        state_path=state_path,
        cleanup_on_success=True,
        repository_id="repo-id",
        run_key_name="run-key",
    )
    reuse_cache.store(entry)
    removal_attempts = 0

    def remove_or_fail(source: object, path: Path, git_dir: Path) -> None:
        nonlocal removal_attempts
        del source, git_dir
        removal_attempts += 1
        if removal_attempts == 1:
            raise RuntimeError("cleanup failed")
        shutil.rmtree(path)

    monkeypatch.setattr(
        "crewplane.runtime.workspace.worktree.cache.remove_worktree_workspace",
        remove_or_fail,
    )

    reused = reuse_cache.take(
        "primary",
        WorktreeSourceRef(
            source_kind="project",
            source_node_id=None,
            source_commit="new-commit",
            source_tree="new-tree",
        ),
        "repo-id",
        "run-key",
    )

    assert reused is None
    assert reuse_cache.owns(workspace_path)
    assert read_json_object(state_path)["workspace"]["retention"] == "pending_cleanup"

    cleanup = reuse_cache.cleanup_all()

    assert cleanup.errors == ()
    assert cleanup.updated_state_paths == (state_path,)
    assert removal_attempts == 2
    assert not workspace_path.exists()
    assert read_json_object(state_path)["workspace"]["retention"] == "deleted"


def test_reuse_cache_retries_state_projection_after_physical_removal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    reuse_cache = WorktreeReuseCache()
    workspace_path = tmp_path / "workspace"
    checkout_root = workspace_path / "checkout"
    checkout_root.mkdir(parents=True)
    state_path = tmp_path / "workspace-state.json"
    state_path.write_text(
        json.dumps(
            {
                "status": "failed",
                "workspace": {
                    "retention": "pending_cleanup",
                    "retained_reason": None,
                },
            }
        ),
        encoding="utf-8",
    )
    entry = ReusableWorktreeCheckout(
        node_id="implement",
        logical_worktree_name="primary",
        workspace_path=workspace_path,
        checkout_root=checkout_root,
        cwd=checkout_root,
        git_dir=workspace_path / "git-dir",
        source_commit="source-commit",
        source_tree="source-tree",
        source=make_workspace_source_snapshot(),
        state_path=state_path,
        cleanup_on_success=True,
        repository_id="repo-id",
        run_key_name="run-key",
    )
    reuse_cache.store(entry)
    removal_count = 0

    def remove_workspace(source: object, path: Path, git_dir: Path) -> None:
        nonlocal removal_count
        del source, git_dir
        removal_count += 1
        shutil.rmtree(path)

    original_update = worktree_cache.update_workspace_retention
    update_count = 0

    def fail_first_update(
        state_path_arg: Path,
        retention: WorkspaceStateRetention,
    ) -> None:
        nonlocal update_count
        update_count += 1
        if update_count == 1:
            raise OSError("injected state projection failure")
        original_update(state_path_arg, retention)

    monkeypatch.setattr(worktree_cache, "remove_worktree_workspace", remove_workspace)
    monkeypatch.setattr(
        worktree_cache,
        "update_workspace_retention",
        fail_first_update,
    )

    error = reuse_cache.cleanup_entry_best_effort(entry)

    assert isinstance(error, OSError)
    assert removal_count == 1
    assert not workspace_path.exists()
    assert reuse_cache.owns(workspace_path)

    monkeypatch.setattr(
        worktree_cache,
        "update_workspace_retention",
        original_update,
    )
    cleanup = reuse_cache.cleanup_all()

    assert cleanup.errors == ()
    assert cleanup.updated_state_paths == (state_path,)
    assert removal_count == 1
    state = read_json_object(state_path)
    assert state["status"] == "failed"
    assert state["workspace"]["retention"] == "deleted"
    assert not reuse_cache.owns(workspace_path)


def test_reuse_cache_cleanup_workspace_includes_current_generation_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    reuse_cache = WorktreeReuseCache()
    workspace_path = tmp_path / "workspace"
    checkout_root = workspace_path / "checkout"
    checkout_root.mkdir(parents=True)
    prior_state_path = tmp_path / "prior-workspace-state.json"
    current_state_path = tmp_path / "current-workspace-state.json"
    for state_path in (prior_state_path, current_state_path):
        state_path.write_text(
            json.dumps(
                {
                    "status": "cancelled",
                    "workspace": {
                        "retention": "pending_cleanup",
                        "retained_reason": None,
                    },
                }
            ),
            encoding="utf-8",
        )
    entry = ReusableWorktreeCheckout(
        node_id="implement",
        logical_worktree_name="primary",
        workspace_path=workspace_path,
        checkout_root=checkout_root,
        cwd=checkout_root,
        git_dir=workspace_path / "git-dir",
        source_commit="source-commit",
        source_tree="source-tree",
        source=make_workspace_source_snapshot(),
        state_path=prior_state_path,
        cleanup_on_success=True,
        repository_id="repo-id",
        run_key_name="run-key",
    )
    reuse_cache.store(entry)

    def remove_workspace(source: object, path: Path, git_dir: Path) -> None:
        del source, git_dir
        shutil.rmtree(path)

    monkeypatch.setattr(worktree_cache, "remove_worktree_workspace", remove_workspace)

    updated_paths = reuse_cache.cleanup_workspace(
        workspace_path,
        state_path=current_state_path,
    )

    assert updated_paths == tuple(sorted((prior_state_path, current_state_path)))
    for state_path in updated_paths:
        state = read_json_object(state_path)
        assert state["status"] == "cancelled"
        assert state["workspace"]["retention"] == "deleted"
