from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from threading import Event, Timer

import pytest

import crewplane.runtime.execution.provider_call.generated_files as provider_invocation_generated_files_module
import crewplane.runtime.execution.provider_call.lifecycle as provider_invocation_lifecycle_module
from crewplane.architecture.contracts import (
    InvocationContext,
)
from crewplane.core.preflight.secrets import SecretContext
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.runtime.execution.provider_call import (
    ProviderCallDisplay,
    ProviderCallRequest,
    finalize_successful_workspace,
    mark_workspace_succeeded,
    run_provider_call,
)
from crewplane.runtime.execution.provider_call.cancellation import (
    WorkspaceFinalizationDeferredCancellation,
)
from crewplane.runtime.execution.runtime_context import (
    CompiledRuntimeContext,
    DeferredAsyncCleanupRegistry,
)
from crewplane.runtime.workspace import (
    WorkspaceInvocationRequest,
    prepare_invocation_workspace,
)
from crewplane.runtime.workspace.mutator_fence import workspace_mutator_is_fenced
from crewplane.runtime.workspace.prepared_workspace import PreparedWorkspace
from crewplane.runtime.workspace.worktree import remove_worktree_workspace
from tests.helpers.artifacts import node_artifact_request
from tests.helpers.workspace_service import (
    create_git_repo,
    disabled_workspace_plan,
    read_json_object,
    workspace_invocation_context,
    workspace_invocation_request,
    workspace_output_manager,
    workspace_plan,
)
from tests.unit.runtime.workspace.service_provider_invocation_support import (
    SlowSuccessfulPreparedWorkspace,
    SlowSuccessfulWorkspace,
    SuccessfulRuntimeInvoker,
    exception_notes_contain,
    wait_for_workspace_cancellation,
)


def test_workspace_success_finalization_owns_worker_when_fence_write_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(
        _run_workspace_success_finalization_owns_worker_when_fence_write_fails(
            tmp_path,
            monkeypatch,
        )
    )


def test_workspace_success_finalizer_fence_persistence_does_not_block_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(
        _run_workspace_success_finalizer_fence_persistence_does_not_block_event_loop(
            tmp_path,
            monkeypatch,
        )
    )


def test_workspace_success_finalization_preserves_cancel_on_failure() -> None:
    asyncio.run(_run_workspace_success_finalization_preserves_cancel_on_failure())


def test_lifecycle_cancellation_after_finalization_failure_records_terminal_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(
        _run_lifecycle_cancellation_after_finalization_failure_records_terminal_state(
            tmp_path,
            monkeypatch,
        )
    )


def test_worktree_finalization_failure_cleans_uncontended_cancelled_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(
        _run_worktree_finalization_failure_cleans_uncontended_cancelled_workspace(
            tmp_path,
            monkeypatch,
        )
    )


async def _run_workspace_success_finalization_owns_worker_when_fence_write_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        provider_invocation_generated_files_module,
        "WORKSPACE_THREAD_CANCELLATION_TIMEOUT_SECONDS",
        0.01,
    )
    state_path = tmp_path / "workspace-state.json"
    state_path.write_text("{}", encoding="utf-8")
    workspace = SlowSuccessfulPreparedWorkspace(
        tmp_path / "workspace",
        state_path,
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

    def fail_state_mutation(
        failed_state_path: Path,
        mutation: Callable[[dict[str, object]], None],
    ) -> dict[str, object]:
        del failed_state_path, mutation
        raise OSError("disk full")

    monkeypatch.setattr(
        "crewplane.runtime.workspace.state.mutate_workspace_state",
        fail_state_mutation,
    )
    task = asyncio.create_task(
        finalize_successful_workspace(request, workspace, True, None)
    )
    assert await asyncio.to_thread(workspace.started.wait, 2)
    task.cancel()

    try:
        with pytest.raises(WorkspaceFinalizationDeferredCancellation) as exc_info:
            await task
        assert isinstance(exc_info.value.__cause__, asyncio.CancelledError)
        assert exception_notes_contain(
            exc_info.value.__cause__,
            "Workspace success finalizer fence persistence failed: disk full",
        )
        assert len(runtime_context.deferred_workspace_cleanups.tasks) == 1
        assert len(runtime_context.deferred_workspace_cleanups.protected_tasks) == 1
        assert workspace_mutator_is_fenced(state_path)
    finally:
        workspace.release.set()

    errors = await runtime_context.deferred_workspace_cleanups.drain(1.0)

    assert errors == ()
    assert workspace.finished.is_set()
    assert not workspace_mutator_is_fenced(state_path)


async def _run_workspace_success_finalizer_fence_persistence_does_not_block_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        provider_invocation_generated_files_module,
        "WORKSPACE_THREAD_CANCELLATION_TIMEOUT_SECONDS",
        0.01,
    )
    state_path = tmp_path / "workspace-state.json"
    state_path.write_text("{}", encoding="utf-8")
    workspace = SlowSuccessfulPreparedWorkspace(tmp_path / "workspace", state_path)
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
    persistence_started = Event()
    release_persistence = Event()
    fallback_released = Event()
    event_loop_progressed = asyncio.Event()

    def block_fence_persistence(
        blocked_state_path: Path,
        mutation: Callable[[dict[str, object]], None],
    ) -> None:
        del mutation
        assert blocked_state_path == state_path
        persistence_started.set()
        assert release_persistence.wait(5)

    async def observe_persistence_start() -> None:
        assert await asyncio.to_thread(persistence_started.wait, 5)
        event_loop_progressed.set()

    def release_from_fallback_thread() -> None:
        fallback_released.set()
        release_persistence.set()

    monkeypatch.setattr(
        "crewplane.runtime.workspace.state.mutate_workspace_state",
        block_fence_persistence,
    )
    observer = asyncio.create_task(observe_persistence_start())
    fallback = Timer(2.0, release_from_fallback_thread)
    task = asyncio.create_task(
        finalize_successful_workspace(request, workspace, True, None)
    )
    assert await asyncio.to_thread(workspace.started.wait, 2)
    fallback.start()
    task.cancel()

    try:
        with pytest.raises(WorkspaceFinalizationDeferredCancellation):
            await task
        await asyncio.wait_for(event_loop_progressed.wait(), timeout=5)
        assert not fallback_released.is_set()
        assert len(runtime_context.deferred_workspace_cleanups.protected_tasks) == 2
    finally:
        release_persistence.set()
        workspace.release.set()
        fallback.cancel()
        fallback.join(timeout=5)
        await observer

    errors = await runtime_context.deferred_workspace_cleanups.drain(2.0)

    assert errors == ()
    assert workspace.finished.is_set()
    assert not workspace_mutator_is_fenced(state_path)


async def _run_workspace_success_finalization_preserves_cancel_on_failure() -> None:
    workspace = FailingSlowSuccessfulWorkspace()
    cleanup_registry = DeferredAsyncCleanupRegistry()
    task = asyncio.create_task(
        mark_workspace_succeeded(workspace, True, cleanup_registry)
    )
    assert await asyncio.to_thread(workspace.started.wait, 2)

    task.cancel()
    await wait_for_workspace_cancellation(workspace.cancel_requested_check)
    workspace.release.set()

    with pytest.raises(asyncio.CancelledError) as exc_info:
        await task
    assert workspace.finished.is_set()
    assert workspace.cancel_requested is True
    assert exception_notes_contain(
        exc_info.value,
        "Workspace success finalization after cancellation failed: "
        "workspace mark_succeeded boom",
    )
    assert cleanup_registry.tasks == set()


async def _run_lifecycle_cancellation_after_finalization_failure_records_terminal_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    plan = disabled_workspace_plan(repo)
    output = workspace_output_manager(tmp_path, repo)
    node_dir = output.create_node_dir(node_artifact_request("implement"))
    runtime_context = CompiledRuntimeContext(
        plan=plan,
        secret_context=SecretContext(),
    )
    workspace_path = tmp_path / "workspace"
    workspace_path.mkdir()
    state_path = node_dir / "workspace-state.json"
    state_path.write_text(
        json.dumps(
            {
                "status": "running",
                "workspace": {
                    "retention": "retained",
                    "retained_reason": None,
                },
                "child_process_environment": {
                    "required": False,
                    "applied": None,
                },
            }
        ),
        encoding="utf-8",
    )
    workspace = FailingSlowPreparedWorkspace(
        cwd=workspace_path,
        invocation_context=InvocationContext(
            node_id="implement",
            task_id="alpha",
            provider="alpha",
            role=ProviderRole.EXECUTOR,
            audit_round_num=None,
            round_num=1,
            findings_enabled=False,
        ),
        workspace_path=workspace_path,
        state_path=state_path,
    )

    async def fake_prepare(
        workspace_request: WorkspaceInvocationRequest,
        invocation_context: InvocationContext,
        cleanup_registry: DeferredAsyncCleanupRegistry,
    ) -> PreparedWorkspace:
        del workspace_request, invocation_context, cleanup_registry
        return workspace

    monkeypatch.setattr(
        provider_invocation_lifecycle_module,
        "prepare_workspace_with_cancellation",
        fake_prepare,
    )

    task = asyncio.create_task(
        run_provider_call(
            ProviderCallRequest(
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
                invoker=SuccessfulRuntimeInvoker(),
                telemetry=None,
            ),
            display=ProviderCallDisplay(telemetry=None),
        )
    )
    assert await asyncio.to_thread(workspace.started.wait, 2)

    task.cancel()
    await wait_for_workspace_cancellation(workspace.cancel_requested_check)
    workspace.release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    state = read_json_object(state_path)
    assert workspace.finished.is_set()
    assert workspace.terminal_cleanup_cancel_requested is False
    assert state["status"] == "cancelled"
    assert state["workspace"]["retention"] == "deleted"
    assert not workspace_path.exists()


async def _run_worktree_finalization_failure_cleans_uncontended_cancelled_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = create_git_repo(tmp_path)
    plan = workspace_plan(
        repo,
        tmp_path / "cache",
        cleanup_on_success=True,
        kind="worktree",
    )
    output = workspace_output_manager(tmp_path, repo, log_cli_output=True)
    node_dir = output.create_node_dir(node_artifact_request("implement"))
    prepared = prepare_invocation_workspace(
        workspace_invocation_request(plan, output),
        workspace_invocation_context(),
    )
    assert prepared.workspace_path is not None
    assert prepared.state_path is not None
    source = plan.workspace_source
    assert source is not None
    started = Event()
    release = Event()
    cancel_requested_check: Callable[[], bool] | None = None

    def fail_success_finalization(
        workspace: PreparedWorkspace,
        child_environment_applied: bool | None = None,
        defer_cleanup: bool = False,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> None:
        del child_environment_applied, defer_cleanup
        nonlocal cancel_requested_check
        assert workspace is prepared
        cancel_requested_check = cancel_requested
        started.set()
        assert release.wait(2)
        raise RuntimeError("workspace mark_succeeded boom")

    monkeypatch.setattr(
        PreparedWorkspace,
        "mark_succeeded",
        fail_success_finalization,
    )
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

    try:
        task = asyncio.create_task(
            finalize_successful_workspace(request, prepared, None, None)
        )
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        await wait_for_workspace_cancellation(cancel_requested_check)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

        state = read_json_object(prepared.state_path)
        assert state["status"] == "cancelled"
        assert state["workspace"]["retention"] == "deleted"
        assert not prepared.workspace_path.exists()
    finally:
        release.set()
        if prepared.workspace_path.exists():
            remove_worktree_workspace(source, prepared.workspace_path)


class FailingSlowSuccessfulWorkspace(SlowSuccessfulWorkspace):
    def mark_succeeded(
        self,
        child_environment_applied: bool | None = None,
        defer_cleanup: bool = False,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> None:
        super().mark_succeeded(
            child_environment_applied,
            defer_cleanup,
            cancel_requested,
        )
        raise RuntimeError("workspace mark_succeeded boom")


class FailingSlowPreparedWorkspace(PreparedWorkspace):
    def __init__(
        self,
        cwd: Path,
        invocation_context: InvocationContext,
        workspace_path: Path,
        state_path: Path,
    ) -> None:
        super().__init__(
            cwd=cwd,
            invocation_context=invocation_context,
            workspace_kind="snapshot",
            workspace_path=workspace_path,
            state_path=state_path,
        )
        self.started = Event()
        self.release = Event()
        self.finished = Event()
        self.cancel_requested_check: Callable[[], bool] | None = None
        self.terminal_cleanup_cancel_requested: bool | None = None

    def mark_succeeded(
        self,
        child_environment_applied: bool | None = None,
        defer_cleanup: bool = False,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> None:
        del child_environment_applied, defer_cleanup
        self.cancel_requested_check = cancel_requested
        self.started.set()
        assert self.release.wait(2)
        self.finished.set()
        raise RuntimeError("workspace mark_succeeded boom")

    def mark_cancelled(
        self,
        message: str,
        child_environment_applied: bool | None = None,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> None:
        self.terminal_cleanup_cancel_requested = (
            cancel_requested() if cancel_requested is not None else None
        )
        super().mark_cancelled(
            message,
            child_environment_applied,
            cancel_requested,
        )
