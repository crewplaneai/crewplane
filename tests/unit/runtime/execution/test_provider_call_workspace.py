from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from threading import Event, Thread
from time import monotonic, sleep
from unittest.mock import Mock

import pytest

import crewplane.runtime.execution.provider_call.generated_files as generated_files_module
import crewplane.runtime.execution.provider_call.lifecycle as lifecycle_module
import crewplane.runtime.execution.provider_call.workspace as workspace_module
from crewplane.core.preflight.secrets import SecretContext
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.runtime.execution.common import ProviderCallDisplay, ProviderCallRequest
from crewplane.runtime.execution.runtime_context import (
    CompiledRuntimeContext,
    DeferredAsyncCleanupRegistry,
)
from crewplane.runtime.workspace import PreparedWorkspace
from crewplane.runtime.workspace.setup import WorkspaceSetupCancellation
from crewplane.runtime.workspace.snapshot import WorkspaceSnapshotCancelled
from tests.helpers.workspace_service import (
    disabled_workspace_plan,
    workspace_invocation_context,
    workspace_invocation_request,
    workspace_output_manager,
)


def test_late_workspace_preparation_result_is_marked_cancelled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(
        _run_late_workspace_preparation_result_is_marked_cancelled(
            tmp_path,
            monkeypatch,
        )
    )


async def _run_late_workspace_preparation_result_is_marked_cancelled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    invocation_context = workspace_invocation_context()
    prepared_workspace = PreparedWorkspace(repo, invocation_context)
    prepare_workspace = Mock(return_value=prepared_workspace)
    mark_cancelled = Mock()
    cancellation_checked = Event()
    allow_preparation_to_return = Event()
    original_is_cancelled = WorkspaceSetupCancellation.is_cancelled

    def pause_after_cancellation_check(
        setup_cancellation: WorkspaceSetupCancellation,
    ) -> bool:
        cancellation_requested = original_is_cancelled(setup_cancellation)
        if not cancellation_requested:
            cancellation_checked.set()
            if not allow_preparation_to_return.wait(timeout=2):
                raise TimeoutError("Test did not release workspace preparation.")
        return cancellation_requested

    monkeypatch.setattr(
        workspace_module,
        "PREPARATION_CANCELLATION_TIMEOUT_SECONDS",
        0.05,
    )
    monkeypatch.setattr(
        workspace_module,
        "prepare_invocation_workspace",
        prepare_workspace,
    )
    monkeypatch.setattr(
        WorkspaceSetupCancellation,
        "is_cancelled",
        pause_after_cancellation_check,
    )
    monkeypatch.setattr(prepared_workspace, "mark_cancelled", mark_cancelled)
    request = workspace_invocation_request(
        disabled_workspace_plan(repo),
        workspace_output_manager(tmp_path, repo),
    )
    cleanup_registry = DeferredAsyncCleanupRegistry()
    preparation_task = asyncio.create_task(
        workspace_module.prepare_workspace_with_cancellation(
            request,
            invocation_context,
            cleanup_registry,
        )
    )

    try:
        assert await asyncio.to_thread(cancellation_checked.wait, 2)
        preparation_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await preparation_task
    finally:
        allow_preparation_to_return.set()

    cleanup_errors = await cleanup_registry.drain(2.0)

    assert cleanup_errors == ()
    prepare_workspace.assert_called_once()
    mark_cancelled.assert_called_once_with(
        workspace_module.PREPARATION_CANCELLATION_MESSAGE,
        None,
    )


def test_pre_invocation_generated_file_baseline_observes_task_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(
        _run_pre_invocation_generated_file_baseline_observes_task_cancellation(
            tmp_path,
            monkeypatch,
        )
    )


def test_deferred_workspace_snapshot_cancellation_is_not_cleanup_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(
        _run_deferred_workspace_snapshot_cancellation_is_not_cleanup_failure(
            tmp_path,
            monkeypatch,
        )
    )


async def _run_deferred_workspace_snapshot_cancellation_is_not_cleanup_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    output = workspace_output_manager(tmp_path, repo)
    node_dir = output.create_stage_dir("implement")
    request = _provider_call_request(
        tmp_path,
        repo,
        node_dir / "alpha_executor_0_round1.md",
    )
    prepared_workspace = PreparedWorkspace(
        repo,
        workspace_invocation_context(),
        workspace_kind="snapshot",
        workspace_path=repo,
    )
    snapshot_started = Event()

    def slow_snapshot_cancellation(
        request: ProviderCallRequest,
        prepared_workspace: PreparedWorkspace,
        change_baseline: object,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> None:
        del request, prepared_workspace, change_baseline, cancel_requested
        snapshot_started.set()
        sleep(0.05)
        raise WorkspaceSnapshotCancelled("Snapshot worker observed cancellation.")

    monkeypatch.setattr(
        generated_files_module,
        "WORKSPACE_THREAD_CANCELLATION_TIMEOUT_SECONDS",
        0.01,
    )
    monkeypatch.setattr(
        generated_files_module,
        "snapshot_invocation_generated_files",
        slow_snapshot_cancellation,
    )

    snapshot_task = asyncio.create_task(
        generated_files_module.snapshot_invocation_generated_files_async(
            request,
            prepared_workspace,
        )
    )
    assert await asyncio.to_thread(snapshot_started.wait, 2)
    snapshot_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await snapshot_task

    assert await request.runtime_context.deferred_workspace_cleanups.drain(1.0) == ()


async def _run_pre_invocation_generated_file_baseline_observes_task_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    output = workspace_output_manager(tmp_path, repo)
    node_dir = output.create_stage_dir("implement")
    invocation_context = workspace_invocation_context()
    prepared_workspace = PreparedWorkspace(
        repo,
        invocation_context,
        workspace_kind="snapshot",
        workspace_path=repo,
    )
    request = _provider_call_request(
        tmp_path,
        repo,
        node_dir / "alpha_executor_0_round1.md",
    )
    baseline_started = Event()
    baseline_cancelled = Event()

    async def fake_prepare_workspace_with_cancellation(
        workspace_request: object,
        invocation_context: object,
        cleanup_registry: object,
    ) -> PreparedWorkspace:
        del workspace_request, invocation_context, cleanup_registry
        return prepared_workspace

    def blocking_baseline_capture(
        cls: type[generated_files_module.GeneratedFileChangeBaseline],
        invocation_root: Path,
        filesystem_fallback_enabled: bool,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> generated_files_module.GeneratedFileChangeBaseline:
        del cls, filesystem_fallback_enabled
        baseline_started.set()
        deadline = monotonic() + 1.0
        while cancel_requested is None or not cancel_requested():
            if monotonic() >= deadline:
                raise TimeoutError("Baseline capture did not observe cancellation.")
            sleep(0.01)
        del invocation_root
        baseline_cancelled.set()
        raise WorkspaceSnapshotCancelled("Baseline snapshot was cancelled.")

    monkeypatch.setattr(
        lifecycle_module,
        "prepare_workspace_with_cancellation",
        fake_prepare_workspace_with_cancellation,
    )
    monkeypatch.setattr(
        generated_files_module.GeneratedFileChangeBaseline,
        "capture",
        classmethod(blocking_baseline_capture),
    )
    loop = asyncio.get_running_loop()
    preparation_task = asyncio.create_task(
        lifecycle_module.run_provider_invocation_lifecycle(
            request,
            True,
            ProviderCallDisplay(telemetry=None, show_console_summary=False),
        )
    )

    def cancel_after_baseline_starts() -> None:
        if baseline_started.wait(timeout=2):
            loop.call_soon_threadsafe(preparation_task.cancel)

    cancellation_thread = Thread(target=cancel_after_baseline_starts, daemon=True)
    cancellation_thread.start()
    try:
        with pytest.raises(asyncio.CancelledError):
            await preparation_task
    finally:
        cancellation_thread.join(timeout=2)

    assert baseline_cancelled.wait(timeout=2)
    assert await request.runtime_context.deferred_workspace_cleanups.drain(2.0) == ()


def _provider_call_request(
    tmp_path: Path,
    repo: Path,
    output_file: Path,
) -> ProviderCallRequest:
    plan = disabled_workspace_plan(repo)
    runtime_context = CompiledRuntimeContext(plan=plan, secret_context=SecretContext())
    provider = plan.nodes[0].provider_records[0]
    return ProviderCallRequest(
        runtime_context=runtime_context,
        output=workspace_output_manager(tmp_path, repo),
        node_id="implement",
        provider=provider,
        task_id=provider.task_id,
        audit_round_num=None,
        round_num=1,
        prompt="prompt",
        output_file=output_file,
        role_label=ProviderRole.EXECUTOR,
        invoker=Mock(),
        telemetry=None,
    )
