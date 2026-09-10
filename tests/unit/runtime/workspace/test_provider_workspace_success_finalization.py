from __future__ import annotations

import asyncio
import shutil
from collections.abc import Callable
from pathlib import Path
from threading import Event

import pytest

import crewplane.runtime.execution.provider_call.generated_files as provider_invocation_generated_files_module
from crewplane.core.preflight.secrets import SecretContext
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.runtime.execution.provider_call import (
    ProviderCallRequest,
    finalize_successful_workspace,
    mark_workspace_succeeded,
)
from crewplane.runtime.execution.provider_call.cancellation import (
    WorkspaceFinalizationDeferredCancellation,
)
from crewplane.runtime.execution.runtime_context import (
    CompiledRuntimeContext,
    DeferredAsyncCleanupRegistry,
)
from tests.helpers.artifacts import node_artifact_request
from tests.helpers.workspace_service import (
    create_git_repo,
    disabled_workspace_plan,
    workspace_output_manager,
)
from tests.unit.runtime.workspace.service_provider_invocation_support import (
    SlowSuccessfulPreparedWorkspace,
    SlowSuccessfulWorkspace,
    wait_for_workspace_cancellation,
)


def test_workspace_success_finalization_waits_after_cancellation() -> None:
    asyncio.run(_run_workspace_success_finalization_waits_after_cancellation())


def test_workspace_success_finalization_records_cleanup_after_cancellation(
    tmp_path: Path,
) -> None:
    asyncio.run(
        _run_workspace_success_finalization_records_cleanup_after_cancellation(tmp_path)
    )


def test_workspace_success_finalization_withholds_cleanup_while_pending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(
        _run_workspace_success_finalization_withholds_cleanup_while_pending(
            tmp_path,
            monkeypatch,
        )
    )


def test_deferred_success_finalization_preserves_cleanup_error_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(
        _run_deferred_success_finalization_preserves_cleanup_error_order(
            tmp_path,
            monkeypatch,
        )
    )


def test_workspace_success_finalization_cleans_after_drain_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(
        _run_workspace_success_finalization_cleans_after_drain_timeout(
            tmp_path,
            monkeypatch,
        )
    )


def test_workspace_success_finalization_defers_after_cancellation_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(
        _run_workspace_success_finalization_defers_after_cancellation_timeout(
            monkeypatch
        )
    )


def test_generated_file_snapshot_defers_after_cancellation_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(
        _run_generated_file_snapshot_defers_after_cancellation_timeout(
            tmp_path,
            monkeypatch,
        )
    )


async def _run_workspace_success_finalization_waits_after_cancellation() -> None:
    workspace = SlowSuccessfulWorkspace()
    cleanup_registry = DeferredAsyncCleanupRegistry()
    task = asyncio.create_task(
        mark_workspace_succeeded(workspace, True, cleanup_registry)
    )
    assert await asyncio.to_thread(workspace.started.wait, 2)

    task.cancel()
    await wait_for_workspace_cancellation(workspace.cancel_requested_check)
    workspace.release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert workspace.finished.is_set()
    assert workspace.child_environment_applied is True
    assert workspace.defer_cleanup is True
    assert workspace.cancel_requested is True
    assert cleanup_registry.tasks == set()


async def _run_workspace_success_finalization_records_cleanup_after_cancellation(
    tmp_path: Path,
) -> None:
    repo = create_git_repo(tmp_path)
    plan = disabled_workspace_plan(repo)
    output = workspace_output_manager(tmp_path, repo)
    node_dir = output.create_node_dir(node_artifact_request("implement"))
    runtime_context = CompiledRuntimeContext(
        plan=plan,
        secret_context=SecretContext(),
    )
    workspace = SlowSuccessfulPreparedWorkspace(tmp_path / "workspace")
    request = ProviderCallRequest(
        runtime_context=runtime_context,
        output=output,
        node_id="implement",
        provider=plan.nodes[0].provider_records[0],
        task_id="alpha",
        audit_round_num=None,
        round_num=1,
        prompt="done",
        output_file=node_dir / "alpha_round1.md",
        role_label=ProviderRole.EXECUTOR,
        invoker=object(),
        telemetry=None,
    )

    task = asyncio.create_task(
        finalize_successful_workspace(request, workspace, True, None)
    )
    assert await asyncio.to_thread(workspace.started.wait, 2)
    task.cancel()
    await wait_for_workspace_cancellation(workspace.cancel_requested_check)
    workspace.release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert workspace.finished.is_set()
    assert workspace.cancel_requested is True
    assert runtime_context.generated_file_workspaces.roots_for_node("implement") == {}
    callbacks = runtime_context.generated_file_workspaces.cleanup_by_node["implement"]
    assert callbacks == [workspace.cleanup_after_success]


async def _run_workspace_success_finalization_withholds_cleanup_while_pending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        provider_invocation_generated_files_module,
        "WORKSPACE_THREAD_CANCELLATION_TIMEOUT_SECONDS",
        0.01,
    )
    repo = create_git_repo(tmp_path)
    plan = disabled_workspace_plan(repo)
    output = workspace_output_manager(tmp_path, repo)
    node_dir = output.create_node_dir(node_artifact_request("implement"))
    runtime_context = CompiledRuntimeContext(
        plan=plan,
        secret_context=SecretContext(),
    )
    workspace = SlowSuccessfulPreparedWorkspace(tmp_path / "workspace")
    request = ProviderCallRequest(
        runtime_context=runtime_context,
        output=output,
        node_id="implement",
        provider=plan.nodes[0].provider_records[0],
        task_id="alpha",
        audit_round_num=None,
        round_num=1,
        prompt="done",
        output_file=node_dir / "alpha_round1.md",
        role_label=ProviderRole.EXECUTOR,
        invoker=object(),
        telemetry=None,
    )

    task = asyncio.create_task(
        finalize_successful_workspace(request, workspace, True, None)
    )
    assert await asyncio.to_thread(workspace.started.wait, 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert not workspace.finished.is_set()
    assert workspace.cleaned is False
    assert runtime_context.generated_file_workspaces.cleanup_by_node == {}
    assert len(runtime_context.deferred_workspace_cleanups.tasks) == 1

    workspace.release.set()
    errors = await runtime_context.deferred_workspace_cleanups.drain(1.0)

    assert errors == ()
    assert workspace.finished.is_set()
    assert workspace.cancel_requested is True
    assert runtime_context.generated_file_workspaces.roots_for_node("implement") == {}
    assert workspace.cleaned is True
    assert runtime_context.generated_file_workspaces.cleanup_by_node == {}


async def _run_deferred_success_finalization_preserves_cleanup_error_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        provider_invocation_generated_files_module,
        "WORKSPACE_THREAD_CANCELLATION_TIMEOUT_SECONDS",
        0.01,
    )
    repo = create_git_repo(tmp_path)
    plan = disabled_workspace_plan(repo)
    output = workspace_output_manager(tmp_path, repo)
    node_dir = output.create_node_dir(node_artifact_request("implement"))
    runtime_context = CompiledRuntimeContext(
        plan=plan,
        secret_context=SecretContext(),
    )
    output_file = node_dir / "alpha_round1.md"
    workspace = SlowSuccessfulPreparedWorkspace(tmp_path / "workspace")
    request = ProviderCallRequest(
        runtime_context=runtime_context,
        output=output,
        node_id="implement",
        provider=plan.nodes[0].provider_records[0],
        task_id="alpha",
        audit_round_num=None,
        round_num=1,
        prompt="done",
        output_file=output_file,
        role_label=ProviderRole.EXECUTOR,
        invoker=object(),
        telemetry=None,
    )
    cleanup_order: list[str] = []
    first_error = RuntimeError("first cleanup failed")
    second_error = RuntimeError("second cleanup failed")

    def fail_first_cleanup() -> None:
        cleanup_order.append("first")
        raise first_error

    def fail_second_cleanup() -> None:
        cleanup_order.append("second")
        raise second_error

    runtime_context.generated_file_workspaces.record(
        "implement",
        output_file,
        None,
        fail_first_cleanup,
    )
    runtime_context.generated_file_workspaces.record(
        "implement",
        output_file,
        None,
        fail_second_cleanup,
    )

    task = asyncio.create_task(
        finalize_successful_workspace(request, workspace, True, None)
    )
    assert await asyncio.to_thread(workspace.started.wait, 2)
    task.cancel()
    with pytest.raises(WorkspaceFinalizationDeferredCancellation):
        await task

    workspace.release.set()
    errors = await runtime_context.deferred_workspace_cleanups.drain(1.0)

    assert len(errors) == 1
    assert str(errors[0]) == (
        "Generated-file workspace cleanup after deferred finalization failed "
        "(2 error(s))."
    )
    assert errors[0].__cause__ is first_error
    assert cleanup_order == ["first", "second"]
    assert workspace.cleaned is True
    assert runtime_context.generated_file_workspaces.cleanup_by_node["implement"] == [
        fail_first_cleanup,
        fail_second_cleanup,
    ]


async def _run_workspace_success_finalization_cleans_after_drain_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        provider_invocation_generated_files_module,
        "WORKSPACE_THREAD_CANCELLATION_TIMEOUT_SECONDS",
        0.01,
    )
    repo = create_git_repo(tmp_path)
    plan = disabled_workspace_plan(repo)
    output = workspace_output_manager(tmp_path, repo)
    node_dir = output.create_node_dir(node_artifact_request("implement"))
    runtime_context = CompiledRuntimeContext(
        plan=plan,
        secret_context=SecretContext(),
    )
    workspace = SlowSuccessfulPreparedWorkspace(tmp_path / "workspace")
    request = ProviderCallRequest(
        runtime_context=runtime_context,
        output=output,
        node_id="implement",
        provider=plan.nodes[0].provider_records[0],
        task_id="alpha",
        audit_round_num=None,
        round_num=1,
        prompt="done",
        output_file=node_dir / "alpha_round1.md",
        role_label=ProviderRole.EXECUTOR,
        invoker=object(),
        telemetry=None,
    )

    task = asyncio.create_task(
        finalize_successful_workspace(request, workspace, True, None)
    )
    assert await asyncio.to_thread(workspace.started.wait, 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    errors = await runtime_context.deferred_workspace_cleanups.drain(0)
    assert any(isinstance(error, TimeoutError) for error in errors)
    assert runtime_context.deferred_workspace_cleanups.tasks == set()
    assert not workspace.finished.is_set()
    workspace.release.set()
    assert await asyncio.to_thread(workspace.finished.wait, 2)
    assert await asyncio.to_thread(workspace.cleaned_event.wait, 2)

    assert runtime_context.generated_file_workspaces.cleanup_by_node == {}
    assert workspace.finished.is_set()
    assert workspace.cleaned_event.is_set()
    assert workspace.cleaned is True
    assert workspace.cancel_requested is True
    assert runtime_context.generated_file_workspaces.cleanup_by_node == {}


async def _run_workspace_success_finalization_defers_after_cancellation_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        provider_invocation_generated_files_module,
        "WORKSPACE_THREAD_CANCELLATION_TIMEOUT_SECONDS",
        0.01,
    )
    workspace = SlowSuccessfulWorkspace()
    cleanup_registry = DeferredAsyncCleanupRegistry()
    task = asyncio.create_task(
        mark_workspace_succeeded(workspace, True, cleanup_registry)
    )
    assert await asyncio.to_thread(workspace.started.wait, 2)

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert not workspace.finished.is_set()
    assert len(cleanup_registry.tasks) == 1

    workspace.release.set()
    errors = await cleanup_registry.drain(1.0)

    assert errors == ()
    assert workspace.finished.is_set()
    assert workspace.cancel_requested is True
    assert cleanup_registry.tasks == set()


async def _run_generated_file_snapshot_defers_after_cancellation_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    monkeypatch.setattr(
        provider_invocation_generated_files_module,
        "WORKSPACE_THREAD_CANCELLATION_TIMEOUT_SECONDS",
        0.01,
    )
    started = Event()
    release = Event()

    def slow_snapshot(
        request: ProviderCallRequest,
        prepared_workspace: object,
        change_baseline: object | None = None,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> Path:
        del request, prepared_workspace, change_baseline
        started.set()
        assert release.wait(2)
        assert cancel_requested is not None
        assert cancel_requested() is True
        return tmp_path / "snapshot"

    monkeypatch.setattr(
        provider_invocation_generated_files_module,
        "snapshot_invocation_generated_files",
        slow_snapshot,
    )
    repo = create_git_repo(tmp_path)
    plan = disabled_workspace_plan(repo)
    output = workspace_output_manager(tmp_path, repo)
    node_dir = output.create_node_dir(node_artifact_request("implement"))
    runtime_context = CompiledRuntimeContext(
        plan=plan,
        secret_context=SecretContext(),
    )
    request = ProviderCallRequest(
        runtime_context=runtime_context,
        output=output,
        node_id="implement",
        provider=plan.nodes[0].provider_records[0],
        task_id="alpha",
        audit_round_num=None,
        round_num=1,
        prompt="done",
        output_file=node_dir / "alpha_round1.md",
        role_label=ProviderRole.EXECUTOR,
        invoker=object(),
        telemetry=None,
    )
    task = asyncio.create_task(
        provider_invocation_generated_files_module.snapshot_invocation_generated_files_async(
            request,
            object(),
        )
    )
    assert await asyncio.to_thread(started.wait, 2)

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(runtime_context.deferred_workspace_cleanups.tasks) == 1

    release.set()
    errors = await runtime_context.deferred_workspace_cleanups.drain(1.0)

    assert errors == ()
    assert runtime_context.deferred_workspace_cleanups.tasks == set()
