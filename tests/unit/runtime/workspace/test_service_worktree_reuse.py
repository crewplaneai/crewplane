from __future__ import annotations

import asyncio
import shutil
from collections.abc import Callable
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import NoReturn

import pytest

from crewplane.core.preflight.models import WorkspaceSourceSnapshot
from crewplane.core.workspace.repository_identity import workspace_repository_id
from crewplane.runtime.execution.deferred_cleanup import DeferredAsyncCleanupRegistry
from crewplane.runtime.execution.provider_call.workspace import (
    prepare_workspace_with_cancellation,
)
from crewplane.runtime.workspace import materialization as workspace_materialization
from crewplane.runtime.workspace import prepare_invocation_workspace
from crewplane.runtime.workspace.service import MaterializationLimiter
from crewplane.runtime.workspace.worktree import (
    cache as worktree_cache,
)
from crewplane.runtime.workspace.worktree import (
    materialization as worktree_materialization,
)
from crewplane.runtime.workspace.worktree import remove_worktree_workspace
from crewplane.runtime.workspace.worktree.cache import (
    WorktreeReuseCache,
)
from crewplane.runtime.workspace.worktree.types import WorktreeSourceRef
from tests.helpers.artifacts import node_artifact_request
from tests.helpers.workspace_service import (
    create_git_repo,
    read_json_object,
    run_git_text,
    workspace_invocation_context,
    workspace_output_manager,
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


def test_empty_nested_project_cwd_survives_retry_and_reuse(tmp_path: Path) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    project_root = repo / "packages" / "new-app"
    project_root.mkdir(parents=True)
    plan = two_node_lineage_plan(repo, tmp_path / "cache")
    source = plan.workspace_source
    assert source is not None
    plan = plan.model_copy(
        update={
            "project_root": project_root.as_posix(),
            "context_root": project_root.as_posix(),
            "workspace_source": source.model_copy(
                update={
                    "project_root_relative_path": "packages/new-app",
                    "repository_id": workspace_repository_id(
                        Path(source.common_git_dir), project_root, source.object_format
                    ),
                }
            ),
        }
    )
    output = workspace_output_manager(tmp_path, project_root)
    for node_id in ("implement", "verify"):
        output.create_node_dir(node_artifact_request(node_id))
    reuse_cache = WorktreeReuseCache()
    limiter = MaterializationLimiter.from_plan(plan)
    prepared = prepare_invocation_workspace(
        workspace_request(plan, output, "implement", reuse_cache, limiter),
        workspace_invocation_context(),
    )
    workspace_path = prepared.workspace_path
    assert workspace_path is not None
    try:
        assert prepared.cwd == workspace_path / "checkout" / "packages" / "new-app"
        assert prepared.cwd.is_dir()
        (prepared.cwd / "attempt.txt").write_text(
            "discard on retry\n", encoding="utf-8"
        )
        retry_reset = prepared.invocation_context.retry_reset
        assert retry_reset is not None
        retry_reset()
        assert prepared.cwd.is_dir()
        assert tuple(prepared.cwd.iterdir()) == ()
        prepared.mark_succeeded(defer_cleanup=True)

        prepared = prepare_invocation_workspace(
            workspace_request(plan, output, "verify", reuse_cache, limiter),
            workspace_invocation_context(),
        )
        assert prepared.workspace_path == workspace_path
        assert prepared.cwd.is_dir()
        assert tuple(prepared.cwd.iterdir()) == ()
        assert prepared.state_path is not None
        state = read_json_object(prepared.state_path)
        assert state["reuse"]["reused"] is True
        assert state["reuse"]["fallback"] is False
        prepared.mark_succeeded(defer_cleanup=True)
        assert reuse_cache.cleanup_all_best_effort() == ()
    finally:
        if workspace_path.exists():
            prepared.mark_failed("Test workspace cleanup")
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
