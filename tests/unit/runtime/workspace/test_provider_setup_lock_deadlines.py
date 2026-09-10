from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from threading import Event
from time import monotonic, sleep

import pytest

import crewplane.runtime.agent.invocation.retry_reset as retry_reset_module
import crewplane.runtime.execution.provider_call.workspace as provider_invocation_workspace_module
import crewplane.runtime.workspace.locks as workspace_locks_module
import crewplane.runtime.workspace.service.worktree_failures as workspace_service_worktree_failures
from crewplane.architecture.contracts import InvocationContext
from crewplane.runtime.agent.invocation.retry_reset import reset_before_retry
from crewplane.runtime.execution.runtime_context import (
    DeferredAsyncCleanupRegistry,
)
from crewplane.runtime.workspace import (
    PreparedWorkspace,
    WorkspaceInvocationRequest,
    prepare_invocation_workspace,
)
from crewplane.runtime.workspace.invocation import invocation_slug
from crewplane.runtime.workspace.service.types import WorktreePreparationPlan
from crewplane.runtime.workspace.worktree import remove_worktree_workspace
from tests.helpers.artifacts import node_artifact_request
from tests.helpers.workspace_service import (
    create_git_repo,
    read_json_object,
    workspace_invocation_context,
    workspace_invocation_request,
    workspace_output_manager,
    workspace_plan,
)
from tests.unit.runtime.workspace.service_provider_setup_invocation_support import (
    start_git_metadata_lock_holder,
    stop_git_metadata_lock_holder,
)


def test_worktree_retry_reset_deadline_bounds_asyncio_run_while_lock_is_held(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    cache_root = tmp_path / "cache"
    plan = workspace_plan(repo, cache_root, cleanup_on_success=False, kind="worktree")
    output = workspace_output_manager(tmp_path, repo, log_cli_output=True)
    output.create_node_dir(node_artifact_request("implement"))
    prepared = prepare_invocation_workspace(
        workspace_invocation_request(plan, output),
        workspace_invocation_context(),
    )
    assert prepared.workspace_path is not None
    source = plan.workspace_source
    assert source is not None
    lock_path = Path(source.common_git_dir) / "crewplane" / "workspace.lock"
    holder = start_git_metadata_lock_holder(lock_path)
    monkeypatch.setattr(retry_reset_module, "RETRY_RESET_DEADLINE_SECONDS", 0.02)

    try:
        started = monotonic()
        with pytest.raises(
            RuntimeError,
            match="Workspace retry reset exceeded its internal deadline",
        ):
            asyncio.run(reset_before_retry(prepared.invocation_context))
        elapsed = monotonic() - started

        assert elapsed < 0.5
    finally:
        stop_git_metadata_lock_holder(holder)
        remove_worktree_workspace(source, prepared.workspace_path)


@pytest.mark.parametrize(
    "defer_terminal_state",
    [False, True],
    ids=["ready-terminal-state", "deferred-terminal-state"],
)
def test_worktree_preparation_cancellation_bounds_asyncio_run_while_lock_is_held(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    defer_terminal_state: bool,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    cache_root = tmp_path / "cache"
    plan = workspace_plan(repo, cache_root, cleanup_on_success=False, kind="worktree")
    output = workspace_output_manager(tmp_path, repo, log_cli_output=True)
    node_dir = output.create_node_dir(node_artifact_request("implement"))
    state_path = node_dir / "workspace-state.json"
    source = plan.workspace_source
    assert source is not None
    workspace_path = (
        cache_root
        / "workspaces"
        / source.repository_id
        / plan.run_key_name
        / invocation_slug("implement", "alpha", None, 1)
    )
    lock_path = Path(source.common_git_dir) / "crewplane" / "workspace.lock"
    lock_path.parent.mkdir(parents=True)
    holder = start_git_metadata_lock_holder(lock_path, hold_seconds=10)
    lock_wait_started = Event()
    release_terminal_state = Event()
    if not defer_terminal_state:
        release_terminal_state.set()
    file_lock_api = workspace_locks_module.fcntl
    assert file_lock_api is not None
    original_flock = file_lock_api.flock
    original_record_cancelled = workspace_service_worktree_failures.record_cancelled_unmaterialized_worktree_preparation

    def observe_contended_lock(
        file_descriptor: int,
        operation: int,
    ) -> None:
        try:
            original_flock(file_descriptor, operation)
        except BlockingIOError:
            if operation == file_lock_api.LOCK_EX | file_lock_api.LOCK_NB:
                lock_wait_started.set()
            raise

    def record_cancelled_after_release(
        preparation_plan: WorktreePreparationPlan,
        failure: Exception,
    ) -> None:
        assert release_terminal_state.wait(5), (
            "Terminal-state recording was not released"
        )
        original_record_cancelled(preparation_plan, failure)

    monkeypatch.setattr(
        file_lock_api,
        "flock",
        observe_contended_lock,
    )
    monkeypatch.setattr(
        workspace_service_worktree_failures,
        "record_cancelled_unmaterialized_worktree_preparation",
        record_cancelled_after_release,
    )
    monkeypatch.setattr(
        provider_invocation_workspace_module,
        "PREPARATION_CANCELLATION_TIMEOUT_SECONDS",
        0.02,
    )

    async def cancel_preparation() -> None:
        cleanup_registry = DeferredAsyncCleanupRegistry()
        task = asyncio.create_task(
            provider_invocation_workspace_module.prepare_workspace_with_cancellation(
                workspace_invocation_request(plan, output),
                workspace_invocation_context(),
                cleanup_registry,
            )
        )
        try:
            # Directory creation precedes Git checks; observe actual lock contention.
            assert await asyncio.to_thread(lock_wait_started.wait, 5)
            task.cancel()
            done, _ = await asyncio.wait({task}, timeout=2)
            assert task in done, "Preparation cancellation did not return while locked"
            with pytest.raises(asyncio.CancelledError):
                await task
            if defer_terminal_state:
                assert cleanup_registry.has_unfinished_protected_tasks
                assert read_json_object(state_path)["status"] == "running"
        finally:
            release_terminal_state.set()

        # Durable state writes may outlast the short preparation cancellation budget.
        cleanup_errors = await cleanup_registry.drain(2)
        assert not any(isinstance(error, TimeoutError) for error in cleanup_errors)
        assert all(
            str(error) == "Workspace Git metadata lock acquisition was cancelled."
            for error in cleanup_errors
        )

    try:
        asyncio.run(cancel_preparation())

        # Include asyncio.run's executor shutdown: the lock must still be held.
        assert holder.poll() is None
        state = read_json_object(state_path)
        assert state["status"] == "cancelled"
        assert state["workspace"]["retention"] == "retained"
        assert workspace_path.exists()
    finally:
        release_terminal_state.set()
        stop_git_metadata_lock_holder(holder)


@pytest.mark.parametrize(
    ("hold_git_lock", "delay_past_preparation_timeout"),
    [(False, False), (True, False), (False, True)],
    ids=[
        "uncontended-cleanup",
        "held-lock-is-bounded",
        "deferred-uncontended-cleanup",
    ],
)
def test_preparation_cancellation_has_bounded_terminal_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    hold_git_lock: bool,
    delay_past_preparation_timeout: bool,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    cache_root = tmp_path / "cache"
    plan = workspace_plan(repo, cache_root, cleanup_on_success=False, kind="worktree")
    output = workspace_output_manager(tmp_path, repo, log_cli_output=True)
    output.create_node_dir(node_artifact_request("implement"))
    request = workspace_invocation_request(plan, output)
    invocation_context = workspace_invocation_context()
    prepared = prepare_invocation_workspace(request, invocation_context)
    assert prepared.workspace_path is not None
    source = plan.workspace_source
    assert source is not None
    lock_path = Path(source.common_git_dir) / "crewplane" / "workspace.lock"
    holder = start_git_metadata_lock_holder(lock_path) if hold_git_lock else None
    prepare_started = Event()
    preparation_cancelled = Event()
    release_preparation = Event()

    def finish_preparation_after_cancellation(
        delayed_request: WorkspaceInvocationRequest,
        delayed_context: InvocationContext,
    ) -> PreparedWorkspace:
        del delayed_context
        cancellation = delayed_request.setup_cancellation
        assert cancellation is not None
        prepare_started.set()
        deadline = monotonic() + 2
        while not cancellation.is_cancelled():
            if monotonic() >= deadline:
                raise AssertionError("Timed out waiting for preparation cancellation.")
            sleep(0.001)
        preparation_cancelled.set()
        if delay_past_preparation_timeout:
            assert release_preparation.wait(2)
        return prepared

    monkeypatch.setattr(
        provider_invocation_workspace_module,
        "prepare_invocation_workspace",
        finish_preparation_after_cancellation,
    )
    cancellation_timeout = (
        provider_invocation_workspace_module.PREPARATION_CANCELLATION_TIMEOUT_SECONDS
    )
    if hold_git_lock or delay_past_preparation_timeout:
        monkeypatch.setattr(
            provider_invocation_workspace_module,
            "PREPARATION_CANCELLATION_TIMEOUT_SECONDS",
            0.02,
        )

    async def cancel_preparation() -> None:
        cleanup_registry = DeferredAsyncCleanupRegistry()
        task = asyncio.create_task(
            provider_invocation_workspace_module.prepare_workspace_with_cancellation(
                request,
                invocation_context,
                cleanup_registry,
            )
        )
        assert await asyncio.to_thread(prepare_started.wait, 2)
        task.cancel()
        if delay_past_preparation_timeout:
            assert await asyncio.to_thread(preparation_cancelled.wait, 2)
        with pytest.raises(asyncio.CancelledError):
            await task
        if delay_past_preparation_timeout:
            monkeypatch.setattr(
                provider_invocation_workspace_module,
                "PREPARATION_CANCELLATION_TIMEOUT_SECONDS",
                cancellation_timeout,
            )
        release_preparation.set()
        assert await cleanup_registry.drain(1) == ()

    try:
        started = monotonic()
        asyncio.run(cancel_preparation())
        elapsed = monotonic() - started

        assert elapsed < 1
        state = read_json_object(prepared.state_path)
        assert state["status"] == "cancelled"
        assert state["workspace"]["retention"] == (
            "retained" if hold_git_lock else "deleted"
        )
        assert prepared.workspace_path.exists() is hold_git_lock
    finally:
        release_preparation.set()
        if holder is not None:
            stop_git_metadata_lock_holder(holder)
        if prepared.workspace_path.exists():
            remove_worktree_workspace(source, prepared.workspace_path)
