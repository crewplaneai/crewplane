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
from crewplane.runtime.agent.process.drain import (
    ProcessDrainError,
    ProcessDrainEvidence,
)
from crewplane.runtime.execution.common import ProviderCallDisplay, ProviderCallRequest
from crewplane.runtime.execution.runtime_context import (
    CompiledRuntimeContext,
    DeferredAsyncCleanupRegistry,
)
from crewplane.runtime.workspace import PreparedWorkspace, prepare_invocation_workspace
from crewplane.runtime.workspace.setup import WorkspaceSetupCancellation
from crewplane.runtime.workspace.snapshot import WorkspaceSnapshotCancelled
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
    mark_cancelled = Mock()
    preparation_started = Event()
    allow_preparation_to_return = Event()

    def prepare_workspace_after_cancellation(*args: object) -> PreparedWorkspace:
        del args
        preparation_started.set()
        if not allow_preparation_to_return.wait(timeout=2):
            raise TimeoutError("Test did not release workspace preparation.")
        return prepared_workspace

    monkeypatch.setattr(
        workspace_module,
        "PREPARATION_CANCELLATION_TIMEOUT_SECONDS",
        0.05,
    )
    monkeypatch.setattr(
        workspace_module,
        "prepare_invocation_workspace",
        prepare_workspace_after_cancellation,
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
        assert await asyncio.to_thread(preparation_started.wait, 2)
        preparation_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await preparation_task
    finally:
        allow_preparation_to_return.set()

    cleanup_errors = await cleanup_registry.drain(2.0)

    assert cleanup_errors == ()
    mark_cancelled.assert_called_once()
    message, child_environment_applied, cancel_requested = mark_cancelled.call_args.args
    assert message == workspace_module.PREPARATION_CANCELLATION_MESSAGE
    assert child_environment_applied is None
    assert not cancel_requested()


@pytest.mark.parametrize(
    "cancellation_error",
    (
        ProcessDrainError(
            ProcessDrainEvidence(
                pid=123,
                process_group_id=123,
                leader_stopped=True,
                process_group_stopped=False,
            ),
            "setup process group remained live",
        ),
        OSError("setup drain evidence write failed"),
    ),
    ids=("unresolved-drain", "evidence-write"),
)
def test_setup_cancellation_failure_preserves_cancellation_and_preparation_ownership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cancellation_error: Exception,
) -> None:
    asyncio.run(
        _run_setup_cancellation_failure_preserves_ownership(
            tmp_path,
            monkeypatch,
            cancellation_error,
        )
    )


async def _run_setup_cancellation_failure_preserves_ownership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cancellation_error: Exception,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    invocation_context = workspace_invocation_context()
    prepared_workspace = PreparedWorkspace(repo, invocation_context)
    preparation_started = Event()
    release_preparation = Event()
    mark_cancelled = Mock()

    def prepare_workspace_after_cancellation(*args: object) -> PreparedWorkspace:
        del args
        preparation_started.set()
        if not release_preparation.wait(timeout=2):
            raise TimeoutError("Test did not release workspace preparation.")
        return prepared_workspace

    def fail_setup_process_drain(self: object) -> None:
        del self
        raise cancellation_error

    monkeypatch.setattr(
        workspace_module,
        "PREPARATION_CANCELLATION_TIMEOUT_SECONDS",
        0.01,
    )
    monkeypatch.setattr(
        workspace_module,
        "prepare_invocation_workspace",
        prepare_workspace_after_cancellation,
    )
    monkeypatch.setattr(
        WorkspaceSetupCancellation,
        "cancel",
        fail_setup_process_drain,
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

    assert await asyncio.to_thread(preparation_started.wait, 2)
    preparation_task.cancel()
    try:
        with pytest.raises(asyncio.CancelledError) as exc_info:
            await preparation_task
        assert any(
            str(cancellation_error) in note
            for note in getattr(exc_info.value, "__notes__", ())
        )
        assert len(cleanup_registry.protected_tasks) == 1
    finally:
        release_preparation.set()

    assert await cleanup_registry.drain(2.0) == ()
    mark_cancelled.assert_called_once()


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


def test_cancelled_generated_file_baseline_fences_workspace_cleanup_until_drain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(
        _run_cancelled_generated_file_baseline_fences_workspace_cleanup_until_drain(
            tmp_path,
            monkeypatch,
        )
    )


async def _run_cancelled_generated_file_baseline_fences_workspace_cleanup_until_drain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = create_git_repo(tmp_path)
    plan = workspace_plan(repo, tmp_path / "cache", cleanup_on_success=True)
    output = workspace_output_manager(tmp_path, repo)
    output.create_node_dir(node_artifact_request("implement"))
    prepared_workspace = prepare_invocation_workspace(
        workspace_invocation_request(plan, output),
        workspace_invocation_context(),
    )
    assert prepared_workspace.workspace_path is not None
    assert prepared_workspace.state_path is not None
    cleanup_registry = DeferredAsyncCleanupRegistry()
    baseline_started = Event()
    release_baseline = Event()

    def block_generated_file_baseline(
        cls: type[generated_files_module.GeneratedFileChangeBaseline],
        invocation_root: Path,
        filesystem_fallback_enabled: bool,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> generated_files_module.GeneratedFileChangeBaseline:
        del cls, invocation_root, filesystem_fallback_enabled
        baseline_started.set()
        if not release_baseline.wait(timeout=2):
            raise TimeoutError("Test did not release generated-file baseline.")
        assert cancel_requested is not None
        assert cancel_requested()
        raise WorkspaceSnapshotCancelled("Baseline worker observed cancellation.")

    monkeypatch.setattr(
        generated_files_module,
        "WORKSPACE_THREAD_CANCELLATION_TIMEOUT_SECONDS",
        0.01,
    )
    monkeypatch.setattr(
        generated_files_module.GeneratedFileChangeBaseline,
        "capture",
        classmethod(block_generated_file_baseline),
    )
    baseline_task = asyncio.create_task(
        generated_files_module.capture_generated_file_change_baseline_async(
            prepared_workspace,
            cleanup_registry,
        )
    )

    try:
        assert await asyncio.to_thread(baseline_started.wait, 2)
        baseline_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await baseline_task

        prepared_workspace.mark_cancelled(
            "Cancel while baseline worker remains active."
        )
        state = read_json_object(prepared_workspace.state_path)
        assert state["status"] == "cancelled"
        assert state["workspace"]["retention"] == "retained"
        assert state["workspace_mutator"] == {
            "status": "unresolved",
            "operation": "generated_file_baseline",
        }
        assert prepared_workspace.workspace_path.is_dir()
    finally:
        release_baseline.set()

    assert await cleanup_registry.drain(2.0) == ()
    state = read_json_object(prepared_workspace.state_path)
    assert state["workspace_mutator"] == {
        "status": "confirmed",
        "operation": "generated_file_baseline",
        "outcome": "finished",
    }

    prepared_workspace.mark_cancelled("Retry cleanup after worker drain.")

    state = read_json_object(prepared_workspace.state_path)
    assert state["workspace"]["retention"] == "deleted"
    assert not prepared_workspace.workspace_path.exists()


def test_cancelled_generated_file_snapshot_fences_workspace_cleanup_until_drain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(
        _run_cancelled_generated_file_snapshot_fences_workspace_cleanup_until_drain(
            tmp_path,
            monkeypatch,
        )
    )


async def _run_cancelled_generated_file_snapshot_fences_workspace_cleanup_until_drain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = create_git_repo(tmp_path)
    plan = workspace_plan(repo, tmp_path / "cache", cleanup_on_success=True)
    output = workspace_output_manager(tmp_path, repo)
    node_dir = output.create_node_dir(node_artifact_request("implement"))
    prepared_workspace = prepare_invocation_workspace(
        workspace_invocation_request(plan, output),
        workspace_invocation_context(),
    )
    assert prepared_workspace.workspace_path is not None
    assert prepared_workspace.state_path is not None
    request = ProviderCallRequest(
        runtime_context=CompiledRuntimeContext(
            plan=plan,
            secret_context=SecretContext(),
        ),
        output=output,
        node_id="implement",
        provider=plan.nodes[0].provider_records[0],
        task_id="alpha",
        audit_round_num=None,
        round_num=1,
        prompt="done",
        output_file=node_dir / "alpha_executor_0_round1.md",
        role_label=ProviderRole.EXECUTOR,
        invoker=Mock(),
        telemetry=None,
    )
    snapshot_started = Event()
    release_snapshot = Event()

    async def use_prepared_workspace(*args: object) -> PreparedWorkspace:
        del args
        return prepared_workspace

    async def skip_generated_file_baseline(*args: object) -> None:
        del args

    async def write_provider_output(*args: object, **kwargs: object) -> None:
        del args
        output_file = kwargs["output_file"]
        assert isinstance(output_file, Path)
        output_file.write_text("done\n", encoding="utf-8")

    def block_generated_file_snapshot(
        request: ProviderCallRequest,
        workspace: PreparedWorkspace,
        change_baseline: object,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> None:
        del request, workspace, change_baseline
        snapshot_started.set()
        if not release_snapshot.wait(timeout=2):
            raise TimeoutError("Test did not release generated-file snapshot.")
        assert cancel_requested is not None
        assert cancel_requested()

    monkeypatch.setattr(
        generated_files_module,
        "WORKSPACE_THREAD_CANCELLATION_TIMEOUT_SECONDS",
        0.01,
    )
    monkeypatch.setattr(
        lifecycle_module,
        "prepare_workspace_with_cancellation",
        use_prepared_workspace,
    )
    monkeypatch.setattr(
        lifecycle_module,
        "capture_generated_file_change_baseline_async",
        skip_generated_file_baseline,
    )
    monkeypatch.setattr(
        lifecycle_module,
        "invoke_with_display",
        write_provider_output,
    )
    monkeypatch.setattr(
        generated_files_module,
        "snapshot_invocation_generated_files",
        block_generated_file_snapshot,
    )
    lifecycle_task = asyncio.create_task(
        lifecycle_module.run_provider_invocation_lifecycle(
            request,
            False,
            ProviderCallDisplay(telemetry=None, show_console_summary=False),
        )
    )

    try:
        assert await asyncio.to_thread(snapshot_started.wait, 2)
        lifecycle_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await lifecycle_task

        state = read_json_object(prepared_workspace.state_path)
        assert state["status"] == "cancelled"
        assert state["workspace"]["retention"] == "retained"
        assert state["workspace_mutator"] == {
            "status": "unresolved",
            "operation": "generated_file_snapshot",
        }
        assert prepared_workspace.workspace_path.is_dir()
    finally:
        release_snapshot.set()

    assert await request.runtime_context.deferred_workspace_cleanups.drain(2.0) == ()
    state = read_json_object(prepared_workspace.state_path)
    assert state["workspace_mutator"] == {
        "status": "confirmed",
        "operation": "generated_file_snapshot",
        "outcome": "finished",
    }

    prepared_workspace.mark_cancelled("Retry cleanup after worker drain.")

    state = read_json_object(prepared_workspace.state_path)
    assert state["workspace"]["retention"] == "deleted"
    assert not prepared_workspace.workspace_path.exists()


async def _run_deferred_workspace_snapshot_cancellation_is_not_cleanup_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    output = workspace_output_manager(tmp_path, repo)
    node_dir = output.create_node_dir(node_artifact_request("implement"))
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
    release_snapshot = Event()

    def slow_snapshot_cancellation(
        request: ProviderCallRequest,
        prepared_workspace: PreparedWorkspace,
        change_baseline: object,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> None:
        del request, prepared_workspace, change_baseline, cancel_requested
        snapshot_started.set()
        if not release_snapshot.wait(timeout=2.0):
            raise TimeoutError("Snapshot worker was not released.")
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
    try:
        with pytest.raises(asyncio.CancelledError):
            await snapshot_task
    finally:
        release_snapshot.set()

    assert await request.runtime_context.deferred_workspace_cleanups.drain(1.0) == ()


async def _run_pre_invocation_generated_file_baseline_observes_task_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    output = workspace_output_manager(tmp_path, repo)
    node_dir = output.create_node_dir(node_artifact_request("implement"))
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
        if cancel_requested is None:
            raise AssertionError("Baseline capture requires a cancellation callback.")
        deadline = monotonic() + 1.0
        while not cancel_requested():
            if monotonic() >= deadline:
                raise TimeoutError("Baseline capture did not observe cancellation.")
            sleep(0.001)
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
        assert not cancellation_thread.is_alive(), (
            f"Cancellation thread {cancellation_thread.name!r} remained alive "
            f"after join; baseline_started={baseline_started.is_set()}, "
            f"baseline_cancelled={baseline_cancelled.is_set()}."
        )

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
