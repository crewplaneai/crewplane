from __future__ import annotations

import asyncio
import json
import shutil
from collections.abc import Callable
from pathlib import Path
from threading import Event
from time import monotonic

import pytest

import crewplane.runtime.execution.provider_call.workspace as provider_invocation_workspace_module
from crewplane.architecture.contracts import (
    InvocationContext,
)
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.runtime.execution.provider_call.workspace import (
    prepare_workspace_with_cancellation,
)
from crewplane.runtime.execution.runtime_context import (
    DeferredAsyncCleanupRegistry,
)
from crewplane.runtime.workspace import (
    WorkspaceInvocationRequest,
)
from crewplane.runtime.workspace.prepared_workspace import PreparedWorkspace
from tests.helpers.artifacts import node_artifact_request
from tests.helpers.workspace_service import (
    create_git_repo,
    disabled_workspace_plan,
    read_json_object,
    workspace_output_manager,
    workspace_plan,
)


def test_workspace_preparation_cancellation_is_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(
        _run_workspace_preparation_cancellation_is_bounded(tmp_path, monkeypatch)
    )


def test_workspace_preparation_deferred_cleanup_is_drained(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(
        _run_workspace_preparation_deferred_cleanup_is_drained(tmp_path, monkeypatch)
    )


def test_workspace_preparation_deferred_cleanup_reports_prepare_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(
        _run_workspace_preparation_deferred_cleanup_reports_prepare_failure(
            tmp_path,
            monkeypatch,
        )
    )


def test_workspace_preparation_slow_mark_cancelled_cleanup_is_drained(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(
        _run_workspace_preparation_slow_mark_cancelled_cleanup_is_drained(
            tmp_path,
            monkeypatch,
        )
    )


def test_deferred_cleanup_registry_cancels_pending_task_after_timeout() -> None:
    asyncio.run(_run_deferred_cleanup_registry_cancels_pending_task_after_timeout())


def test_deferred_cleanup_registry_keeps_protected_pending_task_after_timeout() -> None:
    asyncio.run(
        _run_deferred_cleanup_registry_keeps_protected_pending_task_after_timeout()
    )


async def _run_workspace_preparation_cancellation_is_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    plan = disabled_workspace_plan(repo)
    output = workspace_output_manager(tmp_path, repo)
    output.create_node_dir(node_artifact_request("implement"))
    started = Event()
    release = Event()

    def fake_prepare(
        request: WorkspaceInvocationRequest,
        invocation_context: InvocationContext,
    ) -> PreparedWorkspace:
        del request
        started.set()
        release.wait(timeout=5)
        return PreparedWorkspace(cwd=repo, invocation_context=invocation_context)

    monkeypatch.setattr(
        provider_invocation_workspace_module,
        "prepare_invocation_workspace",
        fake_prepare,
    )
    request = WorkspaceInvocationRequest(
        plan=plan,
        output=output,
        node_id="implement",
        task_id="alpha",
        provider="alpha",
        role_label=ProviderRole.EXECUTOR,
        round_num=1,
        audit_round_num=None,
    )
    invocation_context = InvocationContext(
        node_id="implement",
        task_id="alpha",
        provider="alpha",
        role=ProviderRole.EXECUTOR,
        audit_round_num=None,
        round_num=1,
        findings_enabled=False,
    )

    cleanup_registry = DeferredAsyncCleanupRegistry()
    task = asyncio.create_task(
        prepare_workspace_with_cancellation(
            request,
            invocation_context,
            cleanup_registry,
        )
    )
    assert await asyncio.to_thread(started.wait, 2)
    started_at = monotonic()
    task.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await task
        elapsed = monotonic() - started_at
    finally:
        release.set()
    await cleanup_registry.drain(2.0)

    assert elapsed < 1.5


async def _run_workspace_preparation_deferred_cleanup_is_drained(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    cache_root = tmp_path / "cache"
    plan = workspace_plan(repo, cache_root, cleanup_on_success=True)
    output = workspace_output_manager(tmp_path, repo, log_cli_output=True)
    output.create_node_dir(node_artifact_request("implement"))
    state_path = (
        output.create_node_dir(node_artifact_request("implement"))
        / "workspace-state.json"
    )
    workspace_path = cache_root / "snapshots" / "test-repo" / "prep-cancel-deferred"
    workspace_path.mkdir(parents=True)
    started = Event()
    release = Event()
    cancelled_marked = Event()

    class ObservablePreparedWorkspace(PreparedWorkspace):
        def mark_cancelled(
            self,
            message: str,
            child_environment_applied: bool | None = None,
            cancel_requested: Callable[[], bool] | None = None,
        ) -> None:
            super().mark_cancelled(
                message,
                child_environment_applied,
                cancel_requested,
            )
            cancelled_marked.set()

    def fake_prepare(
        request: WorkspaceInvocationRequest,
        invocation_context: InvocationContext,
    ) -> PreparedWorkspace:
        del request
        started.set()
        release.wait(timeout=5)
        state_path.write_text(
            json.dumps(
                {
                    "status": "running",
                    "workspace": {
                        "retention": "retained",
                        "retained_reason": None,
                    },
                    "child_process_environment": {
                        "required": True,
                        "applied": False,
                    },
                }
            ),
            encoding="utf-8",
        )
        return ObservablePreparedWorkspace(
            cwd=repo,
            invocation_context=invocation_context,
            workspace_kind="snapshot",
            workspace_path=workspace_path,
            state_path=state_path,
        )

    monkeypatch.setattr(
        provider_invocation_workspace_module,
        "PREPARATION_CANCELLATION_TIMEOUT_SECONDS",
        0.05,
    )
    monkeypatch.setattr(
        provider_invocation_workspace_module,
        "prepare_invocation_workspace",
        fake_prepare,
    )
    cleanup_registry = DeferredAsyncCleanupRegistry()
    request = WorkspaceInvocationRequest(
        plan=plan,
        output=output,
        node_id="implement",
        task_id="alpha",
        provider="alpha",
        role_label=ProviderRole.EXECUTOR,
        round_num=1,
        audit_round_num=None,
    )
    invocation_context = InvocationContext(
        node_id="implement",
        task_id="alpha",
        provider="alpha",
        role=ProviderRole.EXECUTOR,
        audit_round_num=None,
        round_num=1,
        findings_enabled=False,
    )

    task = asyncio.create_task(
        prepare_workspace_with_cancellation(
            request,
            invocation_context,
            cleanup_registry,
        )
    )
    assert await asyncio.to_thread(started.wait, 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert state_path.exists() is False

    timeout_errors = await cleanup_registry.drain(0)
    assert state_path.exists() is False
    assert workspace_path.exists()
    assert len(timeout_errors) == 1
    assert isinstance(timeout_errors[0], TimeoutError)
    assert cleanup_registry.tasks == set()

    release.set()
    assert await asyncio.to_thread(cancelled_marked.wait, 2)

    state = read_json_object(state_path)
    assert state["status"] == "cancelled"
    assert state["child_process_environment"]["applied"] is False
    assert state["workspace"]["retention"] == "deleted"
    assert not workspace_path.exists()


async def _run_workspace_preparation_deferred_cleanup_reports_prepare_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    plan = disabled_workspace_plan(repo)
    output = workspace_output_manager(tmp_path, repo)
    output.create_node_dir(node_artifact_request("implement"))
    started = Event()
    release = Event()

    def fake_prepare(
        request: WorkspaceInvocationRequest,
        invocation_context: InvocationContext,
    ) -> PreparedWorkspace:
        del request, invocation_context
        started.set()
        release.wait(timeout=5)
        raise RuntimeError("workspace prepare boom")

    monkeypatch.setattr(
        provider_invocation_workspace_module,
        "PREPARATION_CANCELLATION_TIMEOUT_SECONDS",
        0.05,
    )
    monkeypatch.setattr(
        provider_invocation_workspace_module,
        "prepare_invocation_workspace",
        fake_prepare,
    )
    cleanup_registry = DeferredAsyncCleanupRegistry()
    request = WorkspaceInvocationRequest(
        plan=plan,
        output=output,
        node_id="implement",
        task_id="alpha",
        provider="alpha",
        role_label=ProviderRole.EXECUTOR,
        round_num=1,
        audit_round_num=None,
    )
    invocation_context = InvocationContext(
        node_id="implement",
        task_id="alpha",
        provider="alpha",
        role=ProviderRole.EXECUTOR,
        audit_round_num=None,
        round_num=1,
        findings_enabled=False,
    )

    task = asyncio.create_task(
        prepare_workspace_with_cancellation(
            request,
            invocation_context,
            cleanup_registry,
        )
    )
    assert await asyncio.to_thread(started.wait, 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    release.set()
    errors = await cleanup_registry.drain(2.0)

    assert len(errors) == 1
    assert isinstance(errors[0], RuntimeError)
    assert str(errors[0]) == "workspace prepare boom"


async def _run_workspace_preparation_slow_mark_cancelled_cleanup_is_drained(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    plan = disabled_workspace_plan(repo)
    output = workspace_output_manager(tmp_path, repo)
    output.create_node_dir(node_artifact_request("implement"))
    state_path = (
        output.create_node_dir(node_artifact_request("implement"))
        / "workspace-state.json"
    )
    workspace_path = tmp_path / "workspace"
    workspace_path.mkdir()
    prepare_started = Event()
    release_prepare = Event()
    mark_started = Event()
    release_mark = Event()

    def fake_prepare(
        request: WorkspaceInvocationRequest,
        invocation_context: InvocationContext,
    ) -> PreparedWorkspace:
        del request
        prepare_started.set()
        release_prepare.wait(timeout=5)
        state_path.write_text(
            json.dumps(
                {
                    "status": "running",
                    "workspace": {
                        "retention": "retained",
                        "retained_reason": None,
                    },
                    "child_process_environment": {
                        "required": True,
                        "applied": False,
                    },
                }
            ),
            encoding="utf-8",
        )
        return SlowMarkCancelledWorkspace(
            cwd=repo,
            invocation_context=invocation_context,
            workspace_path=workspace_path,
            state_path=state_path,
            mark_started=mark_started,
            release_mark=release_mark,
        )

    monkeypatch.setattr(
        provider_invocation_workspace_module,
        "PREPARATION_CANCELLATION_TIMEOUT_SECONDS",
        0.05,
    )
    monkeypatch.setattr(
        provider_invocation_workspace_module,
        "prepare_invocation_workspace",
        fake_prepare,
    )
    cleanup_registry = DeferredAsyncCleanupRegistry()
    request = WorkspaceInvocationRequest(
        plan=plan,
        output=output,
        node_id="implement",
        task_id="alpha",
        provider="alpha",
        role_label=ProviderRole.EXECUTOR,
        round_num=1,
        audit_round_num=None,
    )
    invocation_context = InvocationContext(
        node_id="implement",
        task_id="alpha",
        provider="alpha",
        role=ProviderRole.EXECUTOR,
        audit_round_num=None,
        round_num=1,
        findings_enabled=False,
    )

    task = asyncio.create_task(
        prepare_workspace_with_cancellation(
            request,
            invocation_context,
            cleanup_registry,
        )
    )
    assert await asyncio.to_thread(prepare_started.wait, 2)
    task.cancel()
    release_prepare.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await asyncio.to_thread(mark_started.wait, 2)
    assert cleanup_registry.tasks
    assert workspace_path.exists()

    release_mark.set()
    errors = await cleanup_registry.drain(2.0)

    assert errors == ()
    state = read_json_object(state_path)
    assert state["status"] == "cancelled"
    assert state["child_process_environment"]["applied"] is False
    assert state["workspace"]["retention"] == "deleted"
    assert not workspace_path.exists()


async def _run_deferred_cleanup_registry_cancels_pending_task_after_timeout() -> None:
    cleanup_registry = DeferredAsyncCleanupRegistry()
    release = asyncio.Event()
    cleaned = False
    cancelled = False

    async def cleanup() -> None:
        nonlocal cancelled, cleaned
        try:
            await release.wait()
            cleaned = True
        except asyncio.CancelledError:
            cancelled = True
            raise

    cleanup_registry.register(cleanup())

    timeout_errors = await cleanup_registry.drain(0.01)
    await asyncio.sleep(0)

    assert len(timeout_errors) == 1
    assert isinstance(timeout_errors[0], TimeoutError)
    assert cleanup_registry.tasks == set()
    release.set()
    errors = await cleanup_registry.drain(1.0)

    assert errors == ()
    assert cleanup_registry.tasks == set()
    assert cleaned is False
    assert cancelled is True


async def _run_deferred_cleanup_registry_keeps_protected_pending_task_after_timeout() -> (
    None
):
    cleanup_registry = DeferredAsyncCleanupRegistry()
    release = asyncio.Event()
    finished = asyncio.Event()
    cleaned = False
    cancelled = False

    async def cleanup() -> None:
        nonlocal cancelled, cleaned
        try:
            await release.wait()
            cleaned = True
            finished.set()
        except asyncio.CancelledError:
            cancelled = True
            raise

    cleanup_registry.register(cleanup(), cancel_on_timeout=False)

    timeout_errors = await cleanup_registry.drain(0)

    assert len(timeout_errors) == 1
    assert isinstance(timeout_errors[0], TimeoutError)
    assert cleanup_registry.tasks == set()
    release.set()
    await asyncio.wait_for(finished.wait(), 1.0)

    assert cleaned is True
    assert cancelled is False


class SlowMarkCancelledWorkspace(PreparedWorkspace):
    def __init__(
        self,
        cwd: Path,
        invocation_context: InvocationContext,
        workspace_path: Path,
        state_path: Path,
        mark_started: Event,
        release_mark: Event,
    ) -> None:
        super().__init__(
            cwd=cwd,
            invocation_context=invocation_context,
            workspace_kind="snapshot",
            workspace_path=workspace_path,
            state_path=state_path,
        )
        self.mark_started = mark_started
        self.release_mark = release_mark

    def mark_cancelled(
        self,
        message: str,
        child_environment_applied: bool | None = None,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> None:
        self.mark_started.set()
        assert self.release_mark.wait(2)
        super().mark_cancelled(
            message,
            child_environment_applied,
            cancel_requested,
        )
