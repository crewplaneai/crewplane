from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import NoReturn

import pytest

from crewplane.core.preflight.models import WorkspaceSourceSnapshot
from crewplane.runtime.execution.deferred_cleanup import DeferredAsyncCleanupRegistry
from crewplane.runtime.execution.provider_call.workspace import (
    prepare_workspace_with_cancellation,
)
from crewplane.runtime.workspace import materialization as workspace_materialization
from crewplane.runtime.workspace import prepare_invocation_workspace
from crewplane.runtime.workspace import prepared_workspace as prepared_workspace_module
from crewplane.runtime.workspace.git import GitCommand
from crewplane.runtime.workspace.service import MaterializationLimiter
from crewplane.runtime.workspace.state import WorkspaceStateRetention
from crewplane.runtime.workspace.worktree import (
    cache as worktree_cache,
)
from crewplane.runtime.workspace.worktree import lineage as worktree_lineage
from crewplane.runtime.workspace.worktree import (
    materialization as worktree_materialization,
)
from crewplane.runtime.workspace.worktree import orchestration as worktree_orchestration
from crewplane.runtime.workspace.worktree import remove_worktree_workspace
from crewplane.runtime.workspace.worktree import reset as worktree_reset
from crewplane.runtime.workspace.worktree import reuse as worktree_reuse
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
    run_git_text,
    workspace_invocation_context,
    workspace_output_manager,
    workspace_plan,
)
from tests.helpers.workspace_worktree_reuse import (
    two_node_lineage_plan,
    workspace_request,
)


def test_same_worktree_reuses_checkout_with_incremental_reset(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    nested = repo / "packages" / "app"
    nested.mkdir(parents=True)
    (nested / ".gitignore").write_text(".cache/\n", encoding="utf-8")
    run_git_text(repo, "add", "packages/app/.gitignore")
    run_git_text(repo, "commit", "-m", "ignore caches")
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
    first_workspace_path = first.workspace_path
    (first.cwd / "result.txt").write_text("captured\n", encoding="utf-8")
    ignored_cache = first.cwd / "packages" / "app" / ".cache" / "tool"
    ignored_cache.mkdir(parents=True)
    (ignored_cache / "leftover.txt").write_text("ignored\n", encoding="utf-8")

    first.mark_succeeded(defer_cleanup=True)
    first.cleanup_after_success()
    assert first_workspace_path.exists()

    second = prepare_invocation_workspace(
        workspace_request(plan, output, "verify", reuse_cache, limiter),
        workspace_invocation_context(),
    )

    try:
        assert second.workspace_path == first_workspace_path
        assert (second.cwd / "result.txt").read_text(encoding="utf-8") == "captured\n"
        assert not (second.cwd / "packages" / "app" / ".cache").exists()
        assert run_git_text(second.cwd, "status", "--porcelain=v1") == ""
        first_state = read_json_object(
            output.create_node_dir(node_artifact_request("implement"))
            / "workspace-state.json"
        )
        second_state = read_json_object(
            output.create_node_dir(node_artifact_request("verify"))
            / "workspace-state.json"
        )
        assert first_state["workspace"]["retention"] == "pending_cleanup"
        assert second_state["reuse"]["strategy"] == "incremental_reset"
        assert second_state["reuse"]["reused"] is True
        assert (
            second_state["reuse"]["previous_workspace_state"] == "workspace-state.json"
        )
        assert second_state["git"]["worktree_lock_mode"] == "reused_incremental_reset"
    finally:
        if second.workspace_path is not None:
            second.mark_succeeded(defer_cleanup=True)
        reuse_cache.cleanup_all_best_effort()


def test_cancelled_reuse_claim_retains_without_starting_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(_cancel_reuse_claim_without_cleanup(tmp_path, monkeypatch))


async def _cancel_reuse_claim_without_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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

    reuse_started = Event()

    def wait_for_cancel(
        reused_workspace_path: Path,
        source: WorkspaceSourceSnapshot,
        source_ref: WorktreeSourceRef,
        expected_git_dir: Path,
        protected_ref_scopes: tuple[str, ...],
        state_path: Path | None,
        cancel_requested: Callable[[], bool],
    ) -> NoReturn:
        del source, source_ref, expected_git_dir, protected_ref_scopes, state_path
        assert reused_workspace_path == workspace_path
        reuse_started.set()
        while not cancel_requested():
            Event().wait(0.001)
        raise RuntimeError("reuse interrupted after durable claim")

    monkeypatch.setattr(
        worktree_materialization,
        "reuse_worktree_workspace",
        wait_for_cancel,
    )
    cleanup_started = Event()
    release_cleanup = Event()
    original_remove = worktree_cache.remove_worktree_workspace

    def stalled_cleanup(
        source: WorkspaceSourceSnapshot,
        cleanup_path: Path,
        expected_git_dir: Path,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> None:
        cleanup_started.set()
        assert release_cleanup.wait(2)
        original_remove(
            source,
            cleanup_path,
            expected_git_dir,
            cancel_requested,
        )

    monkeypatch.setattr(
        worktree_cache,
        "remove_worktree_workspace",
        stalled_cleanup,
    )
    cleanup_registry = DeferredAsyncCleanupRegistry()
    task = asyncio.create_task(
        prepare_workspace_with_cancellation(
            workspace_request(plan, output, "verify", reuse_cache, limiter),
            workspace_invocation_context(),
            cleanup_registry,
        )
    )
    assert await asyncio.to_thread(reuse_started.wait, 2)
    task.cancel()

    try:
        with pytest.raises(asyncio.CancelledError):
            await task

        state_path = (
            output.create_node_dir(node_artifact_request("verify"))
            / "workspace-state.json"
        )
        second_state = read_json_object(state_path)
        assert second_state["status"] == "cancelled"
        assert second_state["workspace"]["retention"] == "retained"
        assert workspace_path.exists()
        assert not reuse_cache.owns(workspace_path)
        assert not cleanup_started.is_set()
        assert cleanup_registry.tasks == set()
    finally:
        release_cleanup.set()

    monkeypatch.setattr(
        worktree_cache,
        "remove_worktree_workspace",
        original_remove,
    )
    cleanup = reuse_cache.cleanup_all()

    assert cleanup.errors == ()
    assert not cleanup_started.is_set()
    assert read_json_object(state_path)["workspace"]["retention"] == "retained"
    capture = first.worktree_capture
    assert capture is not None
    original_remove(capture.source, workspace_path, capture.git_dir)
    worktree_materialization.publish_workspace_cleanup_result(
        state_path,
        deleted=True,
    )
    assert not workspace_path.exists()


def test_safe_worktree_reuse_reserves_no_second_checkout_capacity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    plan = two_node_lineage_plan(repo, tmp_path / "cache")
    source = plan.workspace_source
    assert source is not None
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
    (first.cwd / "result.txt").write_text("captured\n", encoding="utf-8")
    first.mark_succeeded(defer_cleanup=True)
    first.cleanup_after_success()
    probe_calls = 0

    def record_probe(path: Path) -> SimpleNamespace:
        nonlocal probe_calls
        del path
        probe_calls += 1
        return SimpleNamespace(free=10**12)

    monkeypatch.setattr(workspace_materialization.shutil, "disk_usage", record_probe)

    second = prepare_invocation_workspace(
        workspace_request(plan, output, "verify", reuse_cache, limiter),
        workspace_invocation_context(),
    )

    try:
        assert second.workspace_path == first.workspace_path
        assert probe_calls == 0
    finally:
        second.mark_succeeded(defer_cleanup=True)
        reuse_cache.cleanup_all_best_effort()


def test_retained_successful_worktree_is_not_reused(tmp_path: Path) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    cache_root = tmp_path / "cache"
    plan = two_node_lineage_plan(repo, cache_root, cleanup_on_success=False)
    source = plan.workspace_source
    assert source is not None
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
    first_workspace_path = first.workspace_path
    (first.cwd / "result.txt").write_text("captured\n", encoding="utf-8")
    first.mark_succeeded()

    second = prepare_invocation_workspace(
        workspace_request(plan, output, "verify", reuse_cache, limiter),
        workspace_invocation_context(),
    )

    try:
        assert second.workspace_path is not None
        assert second.workspace_path != first_workspace_path
        assert first_workspace_path.exists()
        first_state = read_json_object(
            output.create_node_dir(node_artifact_request("implement"))
            / "workspace-state.json"
        )
        second_state = read_json_object(
            output.create_node_dir(node_artifact_request("verify"))
            / "workspace-state.json"
        )
        assert first_state["workspace"]["retention"] == "retained"
        assert first_state["workspace"]["retained_reason"] == "cleanup_on_success_false"
        assert second_state["reuse"]["strategy"] == "fresh_checkout"
        assert second_state["reuse"]["reused"] is False
    finally:
        if second.workspace_path is not None:
            remove_worktree_workspace(source, second.workspace_path)
        if first_workspace_path.exists():
            remove_worktree_workspace(source, first_workspace_path)


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


def test_same_worktree_reuse_failure_falls_back_to_fresh_checkout(
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
    first_workspace_path = first.workspace_path
    (first.cwd / "result.txt").write_text("captured\n", encoding="utf-8")
    first.mark_succeeded(defer_cleanup=True)
    first.cleanup_after_success()

    def fail_reuse(
        workspace_path: Path,
        source: object,
        source_ref: object,
        expected_git_dir: object,
        protected_ref_scopes: object,
        state_path: object,
    ) -> None:
        assert workspace_path.exists()
        assert source is not None
        assert source_ref is not None
        assert expected_git_dir is not None
        assert protected_ref_scopes is not None
        assert state_path is not None
        raise RuntimeError("reset verification failed")

    monkeypatch.setattr(
        worktree_materialization,
        "reuse_worktree_workspace",
        fail_reuse,
    )
    probe_calls = 0

    def record_probe(path: Path) -> SimpleNamespace:
        nonlocal probe_calls
        del path
        probe_calls += 1
        return SimpleNamespace(free=10**12)

    monkeypatch.setattr(workspace_materialization.shutil, "disk_usage", record_probe)

    second = prepare_invocation_workspace(
        workspace_request(plan, output, "verify", reuse_cache, limiter),
        workspace_invocation_context(),
    )

    try:
        assert second.workspace_path is not None
        assert second.workspace_path != first_workspace_path
        state = read_json_object(
            output.create_node_dir(node_artifact_request("verify"))
            / "workspace-state.json"
        )
        assert state["reuse"]["strategy"] == "fresh_checkout"
        assert state["reuse"]["fallback"] is True
        assert "reset verification failed" in state["reuse"]["fallback_reason"]
        assert probe_calls == 1
    finally:
        if second.workspace_path is not None:
            second.mark_succeeded(defer_cleanup=True)
        reuse_cache.cleanup_all_best_effort()


def test_failed_reuse_cleanup_retry_updates_abandoned_generation_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    plan = two_node_lineage_plan(repo, tmp_path / "cache")
    source = plan.workspace_source
    assert source is not None
    output = workspace_output_manager(tmp_path, repo)
    output.create_node_dir(node_artifact_request("implement"))
    verify_dir = output.create_node_dir(node_artifact_request("verify"))
    reuse_cache = WorktreeReuseCache()
    limiter = MaterializationLimiter.from_plan(plan)

    first = prepare_invocation_workspace(
        workspace_request(plan, output, "implement", reuse_cache, limiter),
        workspace_invocation_context(),
    )
    assert first.workspace_path is not None
    old_workspace_path = first.workspace_path
    (first.cwd / "result.txt").write_text("captured\n", encoding="utf-8")
    first.mark_succeeded(defer_cleanup=True)
    first.cleanup_after_success()

    def fail_reuse(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeError("unsafe reuse")

    removal_attempts = 0

    def fail_old_cleanup_once(
        source_arg: WorkspaceSourceSnapshot,
        workspace_path: Path,
        git_dir: Path,
    ) -> None:
        nonlocal removal_attempts
        removal_attempts += 1
        if removal_attempts == 1:
            raise RuntimeError("cleanup retry required")
        remove_worktree_workspace(source_arg, workspace_path, git_dir)

    monkeypatch.setattr(
        worktree_materialization,
        "reuse_worktree_workspace",
        fail_reuse,
    )
    monkeypatch.setattr(
        "crewplane.runtime.workspace.worktree.cache.remove_worktree_workspace",
        fail_old_cleanup_once,
    )

    second = prepare_invocation_workspace(
        workspace_request(plan, output, "verify", reuse_cache, limiter),
        workspace_invocation_context(),
    )
    assert second.workspace_path is not None
    assert second.workspace_path != old_workspace_path
    abandoned_paths = tuple(verify_dir.glob("workspace-reuse-claim-*.json"))
    assert len(abandoned_paths) == 1
    abandoned_path = abandoned_paths[0]
    assert read_json_object(abandoned_path)["workspace"]["retention"] == "retained"
    assert old_workspace_path.exists()

    cleanup = reuse_cache.cleanup_all()

    assert cleanup.errors == ()
    assert abandoned_path in cleanup.updated_state_paths
    assert read_json_object(abandoned_path)["workspace"]["retention"] == "deleted"
    assert not old_workspace_path.exists()
    remove_worktree_workspace(source, second.workspace_path)


def test_failed_fresh_fallback_preserves_new_checkout_identity_for_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    plan = two_node_lineage_plan(repo, tmp_path / "cache")
    output = workspace_output_manager(tmp_path, repo)
    output.create_node_dir(node_artifact_request("implement"))
    verify_dir = output.create_node_dir(node_artifact_request("verify"))
    reuse_cache = WorktreeReuseCache()
    limiter = MaterializationLimiter.from_plan(plan)

    first = prepare_invocation_workspace(
        workspace_request(plan, output, "implement", reuse_cache, limiter),
        workspace_invocation_context(),
    )
    assert first.workspace_path is not None
    assert first.worktree_capture is not None
    old_workspace_path = first.workspace_path
    old_git_dir = first.worktree_capture.git_dir
    (first.cwd / "result.txt").write_text("captured\n", encoding="utf-8")
    first.mark_succeeded(defer_cleanup=True)
    first.cleanup_after_success()

    def fail_reuse(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeError("unsafe reuse")

    original_remove = worktree_cache.remove_worktree_workspace

    def fail_old_cleanup(
        source: WorkspaceSourceSnapshot,
        workspace_path: Path,
        git_dir: Path,
    ) -> None:
        del source, git_dir
        assert workspace_path == old_workspace_path
        raise OSError("old cleanup failed")

    fresh_claims: list[tuple[Path, Path]] = []

    def fail_after_fresh_claim(
        git_top_level: str,
        protected_ref_scopes: tuple[str, ...] | None,
        source_ref: WorktreeSourceRef,
    ) -> NoReturn:
        del git_top_level, protected_ref_scopes, source_ref
        claim = read_json_object(verify_dir / "workspace-state.json")
        execution = claim["execution"]
        assert isinstance(execution, dict)
        workspace_path = execution["workspace_path"]
        git_dir = execution["worktree_git_dir"]
        assert isinstance(workspace_path, str)
        assert isinstance(git_dir, str)
        fresh_claims.append((Path(workspace_path), Path(git_dir)))
        raise RuntimeError("fresh post-claim failure")

    monkeypatch.setattr(
        worktree_materialization,
        "reuse_worktree_workspace",
        fail_reuse,
    )
    monkeypatch.setattr(worktree_cache, "remove_worktree_workspace", fail_old_cleanup)
    monkeypatch.setattr(
        worktree_orchestration,
        "protected_ref_snapshot_for_source",
        fail_after_fresh_claim,
    )

    with pytest.raises(RuntimeError, match="fresh post-claim failure"):
        prepare_invocation_workspace(
            workspace_request(plan, output, "verify", reuse_cache, limiter),
            workspace_invocation_context(),
        )

    assert len(fresh_claims) == 1
    fresh_workspace_path, fresh_git_dir = fresh_claims[0]
    state = read_json_object(verify_dir / "workspace-state.json")
    assert state["status"] == "failed"
    assert state["workspace"]["retention"] == "deleted"
    assert state["execution"]["worktree_git_dir"] == fresh_git_dir.as_posix()
    assert state["execution"]["worktree_git_dir"] != old_git_dir.as_posix()
    assert state["reuse"]["fallback"] is True
    assert not fresh_workspace_path.exists()
    assert old_workspace_path.exists()
    assert reuse_cache.owns(old_workspace_path)
    abandoned_paths = tuple(verify_dir.glob("workspace-reuse-claim-*.json"))
    assert len(abandoned_paths) == 1
    assert (
        read_json_object(abandoned_paths[0])["workspace"]["retained_reason"]
        == "unsafe_reuse_cleanup_failed"
    )

    monkeypatch.setattr(worktree_cache, "remove_worktree_workspace", original_remove)
    cleanup = reuse_cache.cleanup_all()

    assert cleanup.errors == ()
    assert not old_workspace_path.exists()


def test_preclaim_fallback_failure_preserves_temporary_ref_cleanup_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    plan = two_node_lineage_plan(repo, tmp_path / "cache")
    source = plan.workspace_source
    assert source is not None
    output = workspace_output_manager(tmp_path, repo)
    output.create_node_dir(node_artifact_request("implement"))
    verify_dir = output.create_node_dir(node_artifact_request("verify"))
    reuse_cache = WorktreeReuseCache()
    limiter = MaterializationLimiter.from_plan(plan)

    first = prepare_invocation_workspace(
        workspace_request(plan, output, "implement", reuse_cache, limiter),
        workspace_invocation_context(),
    )
    assert first.workspace_path is not None
    old_workspace_path = first.workspace_path
    (first.cwd / "result.txt").write_text("captured\n", encoding="utf-8")
    first.mark_succeeded(defer_cleanup=True)
    first.cleanup_after_success()

    def fail_reuse(*args: object, **kwargs: object) -> NoReturn:
        del args, kwargs
        raise RuntimeError("unsafe reuse")

    def fail_fresh_creation(*args: object, **kwargs: object) -> NoReturn:
        del args, kwargs
        raise RuntimeError("fresh creation failed")

    original_git_run = GitCommand.run

    def fail_temporary_ref_cleanup(
        command: GitCommand,
        *args: str,
    ) -> subprocess.CompletedProcess[bytes]:
        if (
            args[:3] == ("update-ref", "--no-deref", "-d")
            and len(args) > 3
            and "/imports/" in args[3]
        ):
            raise OSError("temporary ref cleanup failed")
        return original_git_run(command, *args)

    monkeypatch.setattr(
        worktree_materialization,
        "reuse_worktree_workspace",
        fail_reuse,
    )
    monkeypatch.setattr(
        worktree_materialization,
        "create_worktree_workspace",
        fail_fresh_creation,
    )
    monkeypatch.setattr(
        GitCommand,
        "run",
        fail_temporary_ref_cleanup,
    )

    with pytest.raises(RuntimeError, match="fresh creation failed"):
        prepare_invocation_workspace(
            workspace_request(plan, output, "verify", reuse_cache, limiter),
            workspace_invocation_context(),
        )

    state_path = verify_dir / "workspace-state.json"
    state = read_json_object(state_path)
    temporary_refs = state["temporary_refs"]
    assert isinstance(temporary_refs, list)
    assert len(temporary_refs) == 1
    claim = temporary_refs[0]
    assert isinstance(claim, dict)
    assert claim["phase"] == "prepared"
    assert run_git_text(repo, "rev-parse", claim["name"]) == claim["target_oid"]
    assert not old_workspace_path.exists()

    monkeypatch.setattr(GitCommand, "run", original_git_run)
    removed = worktree_lineage.reconcile_temporary_import_refs(
        state_path,
        repo,
        Path(source.common_git_dir),
        source.repository_id,
    )

    assert removed == 1
    assert read_json_object(state_path)["temporary_refs"][0]["phase"] == "removed"


def test_same_worktree_reuse_rejects_retargeted_git_file_before_reset(
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
    first_workspace_path = first.workspace_path
    assert first.worktree_capture is not None
    expected_git_dir = first.worktree_capture.git_dir
    (first.cwd / "result.txt").write_text("captured\n", encoding="utf-8")
    first.mark_succeeded(defer_cleanup=True)
    first.cleanup_after_success()
    assert first_workspace_path.exists()

    git_file = first_workspace_path / "checkout" / ".git"
    git_file.write_text(f"gitdir: {(repo / '.git').as_posix()}\n", encoding="utf-8")

    second = prepare_invocation_workspace(
        workspace_request(plan, output, "verify", reuse_cache, limiter),
        workspace_invocation_context(),
    )

    try:
        assert second.workspace_path is not None
        assert second.workspace_path != first_workspace_path
        state = read_json_object(
            output.create_node_dir(node_artifact_request("verify"))
            / "workspace-state.json"
        )
        assert state["reuse"]["strategy"] == "fresh_checkout"
        assert state["reuse"]["fallback"] is True
        assert ".git file does not match Git dir" in state["reuse"]["fallback_reason"]
        assert expected_git_dir != repo / ".git"
    finally:
        if first_workspace_path.exists():
            git_file.write_text(f"gitdir: {expected_git_dir.as_posix()}\n")
        if second.workspace_path is not None:
            second.mark_succeeded(defer_cleanup=True)
        reuse_cache.cleanup_all_best_effort()


def test_reusable_reset_rejects_symlinked_git_metadata_before_git_command(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    common_git_dir = repo_root / ".git"
    worktrees_dir = common_git_dir / "worktrees"
    checkout_root = tmp_path / "workspace" / "checkout"
    real_admin_dir = common_git_dir / "real-admin"
    checkout_root.mkdir(parents=True)
    worktrees_dir.mkdir(parents=True)
    real_admin_dir.mkdir()
    (real_admin_dir / "gitdir").write_text(
        (checkout_root / ".git").as_posix(),
        encoding="utf-8",
    )
    expected_git_dir = worktrees_dir / "checkout"
    expected_git_dir.symlink_to(real_admin_dir, target_is_directory=True)
    (checkout_root / ".git").write_text(
        f"gitdir: {expected_git_dir.as_posix()}\n",
        encoding="utf-8",
    )

    def ignore_common_git_policy_drift(repo_root: Path, common_git_dir: Path) -> None:
        del repo_root, common_git_dir

    monkeypatch.setattr(
        worktree_reset,
        "reject_common_git_policy_drift",
        ignore_common_git_policy_drift,
    )

    with pytest.raises(RuntimeError, match="metadata must not be symlinked"):
        worktree_reset.reset_reusable_worktree_checkout(
            checkout_root,
            "a" * 40,
            repo_root,
            common_git_dir,
            expected_git_dir,
        )


def test_reusable_reset_rejects_symlinked_git_info_before_unlink(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    common_git_dir = repo_root / ".git"
    expected_git_dir = common_git_dir / "worktrees" / "checkout"
    checkout_root = tmp_path / "workspace" / "checkout"
    outside_info = tmp_path / "outside-info"
    checkout_root.mkdir(parents=True)
    expected_git_dir.mkdir(parents=True)
    outside_info.mkdir()
    (expected_git_dir / "gitdir").write_text(
        (checkout_root / ".git").as_posix(),
        encoding="utf-8",
    )
    (checkout_root / ".git").write_text(
        f"gitdir: {expected_git_dir.as_posix()}\n",
        encoding="utf-8",
    )
    try:
        (expected_git_dir / "info").symlink_to(
            outside_info,
            target_is_directory=True,
        )
    except OSError:
        pytest.skip("symlink creation is unavailable")

    def ignore_common_git_policy_drift(repo_root: Path, common_git_dir: Path) -> None:
        del repo_root, common_git_dir

    def ignore_head_policy(*args: object) -> None:
        del args

    monkeypatch.setattr(
        worktree_reset,
        "reject_common_git_policy_drift",
        ignore_common_git_policy_drift,
    )
    monkeypatch.setattr(
        worktree_reset,
        "reject_attached_head_after_safe_detachment",
        ignore_head_policy,
    )
    monkeypatch.setattr(
        worktree_reset,
        "prove_detached_head",
        ignore_head_policy,
    )

    with pytest.raises(RuntimeError, match="Git info dir must be a real directory"):
        worktree_reset.reset_reusable_worktree_checkout(
            checkout_root,
            "a" * 40,
            repo_root,
            common_git_dir,
            expected_git_dir,
        )


@pytest.mark.parametrize("terminal_status", ["failed", "cancelled"])
def test_fresh_worktree_terminal_failure_removes_checkout_when_cache_exists(
    tmp_path: Path,
    terminal_status: str,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    cache_root = tmp_path / "cache"
    plan = two_node_lineage_plan(repo, cache_root)
    output = workspace_output_manager(tmp_path, repo)
    output.create_node_dir(node_artifact_request("implement"))
    reuse_cache = WorktreeReuseCache()
    limiter = MaterializationLimiter.from_plan(plan)

    workspace = prepare_invocation_workspace(
        workspace_request(plan, output, "implement", reuse_cache, limiter),
        workspace_invocation_context(),
    )
    assert workspace.workspace_path is not None
    workspace_path = workspace.workspace_path
    assert workspace_path.exists()

    if terminal_status == "failed":
        workspace.mark_failed("provider failed")
    else:
        workspace.mark_cancelled("provider cancelled")

    state = read_json_object(
        output.create_node_dir(node_artifact_request("implement"))
        / "workspace-state.json"
    )
    assert state["status"] == terminal_status
    assert state["workspace"]["retention"] == "deleted"
    assert state["workspace"]["retained_reason"] is None
    assert not workspace_path.exists()
    assert not reuse_cache.owns(workspace_path)


@pytest.mark.parametrize("terminal_status", ["failed", "cancelled"])
def test_reused_worktree_terminal_failure_removes_active_checkout(
    tmp_path: Path,
    terminal_status: str,
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
    first_workspace_path = first.workspace_path
    (first.cwd / "result.txt").write_text("captured\n", encoding="utf-8")
    first.mark_succeeded(defer_cleanup=True)
    first.cleanup_after_success()
    assert first_workspace_path.exists()

    second = prepare_invocation_workspace(
        workspace_request(plan, output, "verify", reuse_cache, limiter),
        workspace_invocation_context(),
    )
    assert second.workspace_path == first_workspace_path

    first_state_path = (
        output.create_node_dir(node_artifact_request("implement"))
        / "workspace-state.json"
    )
    if terminal_status == "failed":
        second.mark_failed("provider failed")
    else:
        second.mark_cancelled("provider cancelled")

    state = read_json_object(
        output.create_node_dir(node_artifact_request("verify")) / "workspace-state.json"
    )
    first_state = read_json_object(first_state_path)
    assert state["status"] == terminal_status
    assert state["workspace"]["retention"] == "deleted"
    assert state["workspace"]["retained_reason"] is None
    assert first_state["workspace"]["retention"] == "deleted"
    assert not first_workspace_path.exists()
    assert not reuse_cache.owns(first_workspace_path)
    cleanup = reuse_cache.cleanup_all()
    assert cleanup.errors == ()
    assert first_state_path in cleanup.updated_state_paths
    assert not first_workspace_path.exists()


def test_reused_worktree_cleanup_retry_updates_current_generation_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    plan = two_node_lineage_plan(repo, tmp_path / "cache")
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

    second = prepare_invocation_workspace(
        workspace_request(plan, output, "verify", reuse_cache, limiter),
        workspace_invocation_context(),
    )
    assert second.workspace_path == workspace_path
    assert second.state_path is not None
    current_state_path = second.state_path
    prior_state_path = first.state_path
    assert prior_state_path is not None
    original_update = worktree_cache.update_workspace_retention
    update_count = 0

    def fail_first_update(
        state_path: Path,
        retention: WorkspaceStateRetention,
    ) -> None:
        nonlocal update_count
        update_count += 1
        if update_count == 1:
            raise OSError("injected state projection failure")
        original_update(state_path, retention)

    monkeypatch.setattr(
        worktree_cache,
        "update_workspace_retention",
        fail_first_update,
    )

    second.mark_failed("provider failed")

    assert not workspace_path.exists()
    current_state = read_json_object(current_state_path)
    current_workspace = current_state["workspace"]
    assert isinstance(current_workspace, dict)
    assert current_workspace["retention"] == "retained"
    assert current_workspace["retained_reason"] == "failure_cleanup_failed"

    cleanup = reuse_cache.cleanup_all()

    assert cleanup.errors == ()
    assert cleanup.updated_state_paths == tuple(
        sorted((prior_state_path, current_state_path))
    )
    assert read_json_object(prior_state_path)["workspace"]["retention"] == "deleted"
    assert read_json_object(current_state_path)["workspace"]["retention"] == "deleted"


def test_reuse_cache_eviction_paths_do_not_rewrite_state(tmp_path: Path) -> None:
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
    checkout_root = tmp_path / "workspace" / "checkout"
    checkout_root.mkdir(parents=True)
    state_path = tmp_path / "workspace-state.json"
    state_path.write_text('{"status":"succeeded"}\n', encoding="utf-8")
    source_ref = WorktreeSourceRef(
        source_kind="project",
        source_node_id=None,
        source_commit=source.run_base_commit,
        source_tree=source.source_tree,
    )
    stale_ref = WorktreeSourceRef(
        source_kind="project",
        source_node_id=None,
        source_commit="0" * 40,
        source_tree=source.source_tree,
    )
    cache = WorktreeReuseCache()
    cache.store(
        ReusableWorktreeCheckout(
            node_id="implement",
            logical_worktree_name="implementation",
            workspace_path=checkout_root.parent,
            checkout_root=checkout_root,
            cwd=checkout_root,
            git_dir=tmp_path / "gitdir",
            source_commit=source_ref.source_commit,
            source_tree=source_ref.source_tree,
            source=source,
            state_path=state_path,
            cleanup_on_success=False,
            repository_id=source.repository_id,
            run_key_name=plan.run_key_name,
        )
    )

    assert (
        cache.take(
            "implementation",
            stale_ref,
            source.repository_id,
            plan.run_key_name,
        )
        is None
    )

    replacement_root = tmp_path / "replacement" / "checkout"
    replacement_root.mkdir(parents=True)
    cache.store(
        ReusableWorktreeCheckout(
            node_id="implement",
            logical_worktree_name="implementation",
            workspace_path=checkout_root.parent,
            checkout_root=checkout_root,
            cwd=checkout_root,
            git_dir=tmp_path / "gitdir",
            source_commit=source_ref.source_commit,
            source_tree=source_ref.source_tree,
            source=source,
            state_path=state_path,
            cleanup_on_success=False,
            repository_id=source.repository_id,
            run_key_name=plan.run_key_name,
        )
    )
    cache.store(
        ReusableWorktreeCheckout(
            node_id="verify",
            logical_worktree_name="implementation",
            workspace_path=replacement_root.parent,
            checkout_root=replacement_root,
            cwd=replacement_root,
            git_dir=tmp_path / "replacement-gitdir",
            source_commit=source_ref.source_commit,
            source_tree=source_ref.source_tree,
            source=source,
            state_path=state_path,
            cleanup_on_success=False,
            repository_id=source.repository_id,
            run_key_name=plan.run_key_name,
        )
    )

    assert state_path.read_text(encoding="utf-8") == '{"status":"succeeded"}\n'


def test_reused_worktree_rejects_source_tree_mismatch_before_reset(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
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
    wrong_tree = "f" * 40
    assert wrong_tree != source.source_tree
    reset_called = False

    def reject_reset(
        checkout_root: Path,
        source_commit: str,
        repo_root: Path,
        common_git_dir: Path,
        expected_git_dir: Path,
    ) -> None:
        nonlocal reset_called
        del checkout_root, source_commit, repo_root, common_git_dir, expected_git_dir
        reset_called = True
        raise AssertionError("reset should not run for mismatched source trees")

    monkeypatch.setattr(
        worktree_reuse,
        "reset_reusable_worktree_checkout",
        reject_reset,
    )

    with pytest.raises(
        RuntimeError, match="project source descriptor is contradictory"
    ):
        worktree_reuse.reuse_worktree_workspace(
            tmp_path / "workspace",
            source,
            WorktreeSourceRef(
                source_kind="project",
                source_node_id=None,
                source_commit=source.run_base_commit,
                source_tree=wrong_tree,
            ),
            tmp_path / "gitdir",
        )

    assert reset_called is False


def test_reused_worktree_rejects_missing_local_source_bundle_before_reset(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
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
    reset_called = False

    def reject_reset(
        checkout_root: Path,
        source_commit: str,
        repo_root: Path,
        common_git_dir: Path,
        expected_git_dir: Path,
    ) -> None:
        nonlocal reset_called
        del checkout_root, source_commit, repo_root, common_git_dir, expected_git_dir
        reset_called = True
        raise AssertionError("reset should not run for missing source bundles")

    monkeypatch.setattr(
        worktree_reuse,
        "reset_reusable_worktree_checkout",
        reject_reset,
    )

    with pytest.raises(RuntimeError, match="bundle is missing"):
        worktree_reuse.reuse_worktree_workspace(
            tmp_path / "workspace",
            source,
            WorktreeSourceRef(
                source_kind="node",
                source_node_id="upstream",
                source_commit=source.run_base_commit,
                source_tree=source.source_tree,
                candidate_sequence=1,
                bundle_path=tmp_path / "missing.bundle",
                bundle_sha256="0" * 64,
                bundle_size_bytes=1,
                bundle_ref="refs/crewplane/test/missing",
            ),
            tmp_path / "gitdir",
        )

    assert reset_called is False
