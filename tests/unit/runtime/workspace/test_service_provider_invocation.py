from __future__ import annotations

import asyncio
import json
import shutil
from contextlib import suppress
from pathlib import Path
from threading import Event, Timer
from time import monotonic

import pytest

import crewplane.runtime.execution.provider_call.generated_files as provider_invocation_generated_files_module
import crewplane.runtime.execution.provider_call.lifecycle as provider_invocation_lifecycle_module
import crewplane.runtime.execution.provider_call.workspace as provider_invocation_workspace_module
from crewplane.adapters.invokers.cli_invoker import build_cli_invocation_plan
from crewplane.adapters.invokers.mock import MockInvokerAdapter
from crewplane.architecture.contracts import (
    ChildProcessEnvironment,
    CommandResult,
    InvocationContext,
)
from crewplane.core.config import AgentConfig, Config
from crewplane.core.preflight.secrets import SecretContext
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.runtime.agent.failures import InvocationFailureError
from crewplane.runtime.agent.invoker import invoke_agent_with_runner
from crewplane.runtime.execution.activity.telemetry import ExecutionTelemetry
from crewplane.runtime.execution.provider_call import (
    ProviderCallDisplay,
    ProviderCallRequest,
    finalize_successful_workspace,
    mark_workspace_succeeded,
    record_generated_file_workspace,
    run_provider_call,
)
from crewplane.runtime.execution.provider_call.workspace import (
    prepare_workspace_with_cancellation,
)
from crewplane.runtime.execution.runtime_context import (
    CompiledRuntimeContext,
    DeferredAsyncCleanupRegistry,
)
from crewplane.runtime.workspace import WorkspaceInvocationRequest
from crewplane.runtime.workspace.prepared_workspace import PreparedWorkspace
from crewplane.version import SCHEMA_VERSION
from tests.helpers.workspace_service import (
    create_git_repo,
    disabled_workspace_plan,
    read_json_object,
    workspace_output_manager,
    workspace_plan,
)


def test_provider_invocation_uses_snapshot_workspace_cwd(
    tmp_path: Path,
) -> None:
    asyncio.run(_run_provider_invocation_uses_snapshot_workspace_cwd(tmp_path))


def test_provider_invocation_cancellation_marks_workspace_state(
    tmp_path: Path,
) -> None:
    asyncio.run(_run_provider_invocation_cancellation_marks_workspace_state(tmp_path))


def test_provider_invocation_cancellation_preserves_cancel_when_mark_cancelled_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(
        _run_provider_invocation_cancellation_preserves_cancel_when_mark_cancelled_fails(
            tmp_path,
            monkeypatch,
        )
    )


def test_workspace_preparation_cancellation_marks_workspace_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(
        _run_workspace_preparation_cancellation_marks_workspace_state(
            tmp_path,
            monkeypatch,
        )
    )


def test_workspace_preparation_cancellation_preserves_cancel_when_mark_cancelled_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(
        _run_workspace_preparation_cancellation_preserves_cancel_when_mark_cancelled_fails(
            tmp_path,
            monkeypatch,
        )
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


def test_failed_provider_invocation_preserves_applied_child_environment(
    tmp_path: Path,
) -> None:
    asyncio.run(
        _run_failed_provider_invocation_preserves_applied_child_environment(tmp_path)
    )


def test_failed_provider_invocation_preserves_provider_error_when_mark_failed_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(
        _run_failed_provider_invocation_preserves_provider_error_when_mark_failed_fails(
            tmp_path,
            monkeypatch,
        )
    )


def test_generated_file_workspace_cleanup_registered_after_cwd_deleted(
    tmp_path: Path,
) -> None:
    plan = disabled_workspace_plan(tmp_path)
    output = workspace_output_manager(tmp_path, tmp_path)
    node_dir = output.create_stage_dir("implement")
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
                "status": "succeeded",
                "workspace": {
                    "retention": "pending_cleanup",
                    "retained_reason": "stage_finalization_pending",
                },
            }
        ),
        encoding="utf-8",
    )

    record_generated_file_workspace(
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
            invoker=object(),
            telemetry=None,
        ),
        PreparedWorkspace(
            cwd=workspace_path / "checkout",
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
            cleanup_on_success=True,
        ),
        None,
    )

    assert runtime_context.generated_file_workspaces.roots_for_node("implement") == {}
    errors = runtime_context.generated_file_workspaces.cleanup_all_best_effort()
    assert errors == ()
    assert not workspace_path.exists()
    assert read_json_object(state_path)["workspace"]["retention"] == "deleted"


def test_provider_invocation_generated_file_snapshot_does_not_block_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(
        _run_provider_invocation_generated_file_snapshot_does_not_block_event_loop(
            tmp_path,
            monkeypatch,
        )
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


async def _run_provider_invocation_uses_snapshot_workspace_cwd(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    cache_root = tmp_path / "cache"
    plan = workspace_plan(
        repo,
        cache_root,
        cleanup_on_success=True,
        launch_mode="mock_no_child_process",
        controlled_child_environment=False,
    )
    output = workspace_output_manager(tmp_path, repo, log_cli_output=True)
    output.create_stage_dir("implement")
    runtime_context = CompiledRuntimeContext(
        plan=plan,
        secret_context=SecretContext(),
    )
    invoker = MockInvokerAdapter().create_invoker(
        Config(
            version=SCHEMA_VERSION,
            agents={"alpha": AgentConfig(cli_cmd=["mock"])},
        ),
        options={"output_mode": "echo"},
    )
    node_dir = output.get_stage_dir("implement")
    assert node_dir is not None
    events = []
    telemetry = ExecutionTelemetry(
        workflow_name=plan.workflow_name,
        run_id=plan.run_id,
        event_sink=events.append,
        suppress_console_output=True,
    )

    await run_provider_call(
        ProviderCallRequest(
            runtime_context=runtime_context,
            output=output,
            node_id="implement",
            provider=plan.nodes[0].provider_records[0],
            task_id="alpha",
            audit_round_num=None,
            round_num=1,
            prompt="hello workspace",
            output_file=node_dir / "alpha_round1.md",
            role_label=ProviderRole.EXECUTOR,
            invoker=invoker,
            telemetry=telemetry,
        ),
        display=ProviderCallDisplay(telemetry=telemetry),
    )

    state_path = node_dir / "workspace-state.json"
    state = read_json_object(state_path)
    assert state["status"] == "succeeded"
    assert state["invoker"]["launch_mode"] == "mock_no_child_process"
    assert state["invoker"]["controlled_child_environment"] is False
    assert state["child_process_environment"]["required"] is False
    assert state["workspace"]["retention"] == "pending_cleanup"
    runtime_context.generated_file_workspaces.cleanup_node("implement")
    state = read_json_object(state_path)
    assert state["workspace"]["retention"] == "deleted"

    log_path = output.get_log_file("implement", "alpha", "alpha", None, 1)
    assert log_path is not None
    log_record = json.loads(log_path.read_text(encoding="utf-8").splitlines()[0])
    assert log_record["cwd"].startswith(cache_root.as_posix())
    assert log_record["workspace"]["workspace_kind"] == "snapshot"
    assert log_record["workspace"]["materialization"] == "snapshot_checkout"
    assert log_record["workspace"]["child_environment_required"] is False
    started_events = [
        event for event in events if event.event_type == "invocation_started"
    ]
    assert len(started_events) == 1
    workspace_events = [
        event for event in events if event.event_type == "workspace_context_recorded"
    ]
    assert len(workspace_events) == 2
    workspace_payload = workspace_events[0].payload
    assert workspace_payload.status == "running"
    assert workspace_payload.workspace_kind == "snapshot"
    assert workspace_payload.workspace_materialization == "snapshot_checkout"
    assert workspace_payload.workspace_source_kind == "project"
    assert workspace_payload.worktree_contract_mode == "blob_exact"
    assert workspace_payload.workspace_child_environment_required is False


async def _run_provider_invocation_generated_file_snapshot_does_not_block_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    plan = disabled_workspace_plan(repo)
    output = workspace_output_manager(tmp_path, repo)
    node_dir = output.create_stage_dir("implement")
    runtime_context = CompiledRuntimeContext(
        plan=plan,
        secret_context=SecretContext(),
    )
    started = Event()
    release = Event()

    def blocking_snapshot(
        request: ProviderCallRequest,
        prepared_workspace: PreparedWorkspace,
        change_baseline: object | None = None,
    ) -> None:
        del request, prepared_workspace, change_baseline
        started.set()
        assert release.wait(2)

    monkeypatch.setattr(
        provider_invocation_generated_files_module,
        "snapshot_invocation_generated_files",
        blocking_snapshot,
    )

    fallback_release = Timer(1.0, release.set)
    fallback_release.start()
    started_at = monotonic()
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
    try:
        assert await asyncio.to_thread(started.wait, 2)
        await asyncio.wait_for(asyncio.sleep(0.01), timeout=0.2)
        assert monotonic() - started_at < 0.5
        release.set()
        await task
    finally:
        release.set()
        fallback_release.cancel()
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


async def _run_failed_provider_invocation_preserves_applied_child_environment(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    cache_root = tmp_path / "cache"
    plan = workspace_plan(repo, cache_root, cleanup_on_success=True)
    output = workspace_output_manager(tmp_path, repo, log_cli_output=True)
    output.create_stage_dir("implement")
    runtime_context = CompiledRuntimeContext(
        plan=plan,
        secret_context=SecretContext(),
    )
    node_dir = output.get_stage_dir("implement")
    assert node_dir is not None
    invoker = FailingCommandRunnerInvoker()
    events = []
    telemetry = ExecutionTelemetry(
        workflow_name=plan.workflow_name,
        run_id=plan.run_id,
        event_sink=events.append,
        suppress_console_output=True,
    )

    with pytest.raises(InvocationFailureError):
        await run_provider_call(
            ProviderCallRequest(
                runtime_context=runtime_context,
                output=output,
                node_id="implement",
                provider=plan.nodes[0].provider_records[0],
                task_id="alpha",
                audit_round_num=None,
                round_num=1,
                prompt="fail after launch",
                output_file=node_dir / "alpha_round1.md",
                role_label=ProviderRole.EXECUTOR,
                invoker=invoker,
                telemetry=telemetry,
            ),
            display=ProviderCallDisplay(telemetry=telemetry),
        )

    state = read_json_object(node_dir / "workspace-state.json")
    assert state["status"] == "failed"
    assert state["child_process_environment"]["required"] is True
    assert state["child_process_environment"]["applied"] is True
    assert invoker.child_environment is not None
    assert invoker.invocation_context is not None
    assert invoker.invocation_context.workspace is not None
    assert invoker.invocation_context.workspace.child_environment_applied is False
    failed_events = [
        event for event in events if event.event_type == "invocation_failed"
    ]
    assert len(failed_events) == 1
    workspace_failed_events = [
        event
        for event in events
        if event.event_type == "workspace_context_recorded"
        and event.payload.status == "failed"
    ]
    assert len(workspace_failed_events) == 1
    assert (
        workspace_failed_events[0].payload.workspace_child_environment_applied is True
    )


async def _run_failed_provider_invocation_preserves_provider_error_when_mark_failed_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    plan = disabled_workspace_plan(repo)
    output = workspace_output_manager(tmp_path, repo)
    node_dir = output.create_stage_dir("implement")
    runtime_context = CompiledRuntimeContext(
        plan=plan,
        secret_context=SecretContext(),
    )

    async def fake_prepare(
        workspace_request: WorkspaceInvocationRequest,
        invocation_context: InvocationContext,
        cleanup_registry: DeferredAsyncCleanupRegistry,
    ) -> PreparedWorkspace:
        del workspace_request, cleanup_registry
        return FailingMarkFailedWorkspace(
            cwd=repo,
            invocation_context=invocation_context,
        )

    monkeypatch.setattr(
        provider_invocation_lifecycle_module,
        "prepare_workspace_with_cancellation",
        fake_prepare,
    )

    with pytest.raises(RuntimeError, match="provider boom") as exc_info:
        await run_provider_call(
            ProviderCallRequest(
                runtime_context=runtime_context,
                output=output,
                node_id="implement",
                provider=plan.nodes[0].provider_records[0],
                task_id="alpha",
                audit_round_num=None,
                round_num=1,
                prompt="fail",
                output_file=node_dir / "alpha_round1.md",
                role_label=ProviderRole.EXECUTOR,
                invoker=FailingRuntimeInvoker(),
                telemetry=None,
            ),
            display=ProviderCallDisplay(telemetry=None),
        )

    assert _exception_notes_contain(
        exc_info.value,
        "Workspace failure handling failed: workspace mark_failed boom",
    )


async def _run_provider_invocation_cancellation_preserves_cancel_when_mark_cancelled_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    plan = disabled_workspace_plan(repo)
    output = workspace_output_manager(tmp_path, repo)
    node_dir = output.create_stage_dir("implement")
    runtime_context = CompiledRuntimeContext(
        plan=plan,
        secret_context=SecretContext(),
    )

    async def fake_prepare(
        workspace_request: WorkspaceInvocationRequest,
        invocation_context: InvocationContext,
        cleanup_registry: DeferredAsyncCleanupRegistry,
    ) -> PreparedWorkspace:
        del workspace_request, cleanup_registry
        return FailingMarkCancelledWorkspace(
            cwd=repo,
            invocation_context=invocation_context,
        )

    monkeypatch.setattr(
        provider_invocation_lifecycle_module,
        "prepare_workspace_with_cancellation",
        fake_prepare,
    )

    with pytest.raises(asyncio.CancelledError) as exc_info:
        await run_provider_call(
            ProviderCallRequest(
                runtime_context=runtime_context,
                output=output,
                node_id="implement",
                provider=plan.nodes[0].provider_records[0],
                task_id="alpha",
                audit_round_num=None,
                round_num=1,
                prompt="cancel",
                output_file=node_dir / "alpha_round1.md",
                role_label=ProviderRole.EXECUTOR,
                invoker=CancelledInvoker(),
                telemetry=None,
            ),
            display=ProviderCallDisplay(telemetry=None),
        )

    assert _exception_notes_contain(
        exc_info.value,
        "Workspace cancellation handling failed: workspace mark_cancelled boom",
    )


async def _run_provider_invocation_cancellation_marks_workspace_state(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    cache_root = tmp_path / "cache"
    plan = workspace_plan(repo, cache_root, cleanup_on_success=True)
    output = workspace_output_manager(tmp_path, repo, log_cli_output=True)
    output.create_stage_dir("implement")
    runtime_context = CompiledRuntimeContext(
        plan=plan,
        secret_context=SecretContext(),
    )
    node_dir = output.get_stage_dir("implement")
    assert node_dir is not None

    with pytest.raises(asyncio.CancelledError):
        await run_provider_call(
            ProviderCallRequest(
                runtime_context=runtime_context,
                output=output,
                node_id="implement",
                provider=plan.nodes[0].provider_records[0],
                task_id="alpha",
                audit_round_num=None,
                round_num=1,
                prompt="cancel",
                output_file=node_dir / "alpha_round1.md",
                role_label=ProviderRole.EXECUTOR,
                invoker=CancelledInvoker(),
                telemetry=None,
            ),
            display=ProviderCallDisplay(telemetry=None),
        )

    state = read_json_object(node_dir / "workspace-state.json")
    assert state["status"] == "cancelled"
    assert state["child_process_environment"]["required"] is True
    assert state["child_process_environment"]["applied"] is False
    assert state["workspace"]["retention"] == "deleted"
    assert state["workspace"]["retained_reason"] is None
    assert not (
        cache_root
        / "snapshots"
        / "test-repo"
        / plan.run_key_name
        / "implement-alpha-round1"
    ).exists()


async def _run_workspace_success_finalization_waits_after_cancellation() -> None:
    workspace = SlowSuccessfulWorkspace()
    cleanup_registry = DeferredAsyncCleanupRegistry()
    task = asyncio.create_task(
        mark_workspace_succeeded(workspace, True, cleanup_registry)
    )
    assert await asyncio.to_thread(workspace.started.wait, 2)

    task.cancel()
    workspace.release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert workspace.finished.is_set()
    assert workspace.child_environment_applied is True
    assert workspace.defer_cleanup is True
    assert cleanup_registry.tasks == set()


async def _run_workspace_success_finalization_records_cleanup_after_cancellation(
    tmp_path: Path,
) -> None:
    repo = create_git_repo(tmp_path)
    plan = disabled_workspace_plan(repo)
    output = workspace_output_manager(tmp_path, repo)
    node_dir = output.create_stage_dir("implement")
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
    workspace.release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert workspace.finished.is_set()
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
    node_dir = output.create_stage_dir("implement")
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
    assert runtime_context.generated_file_workspaces.roots_for_node("implement") == {}
    assert workspace.cleaned is True
    assert runtime_context.generated_file_workspaces.cleanup_by_node == {}


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
    node_dir = output.create_stage_dir("implement")
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

    errors = await runtime_context.deferred_workspace_cleanups.drain(0.01)

    assert any(isinstance(error, TimeoutError) for error in errors)
    assert runtime_context.deferred_workspace_cleanups.tasks == set()
    assert runtime_context.generated_file_workspaces.cleanup_by_node == {}

    workspace.release.set()
    assert await asyncio.to_thread(workspace.finished.wait, 2)
    assert await asyncio.to_thread(workspace.cleaned_event.wait, 2)

    assert workspace.cleaned is True
    assert runtime_context.generated_file_workspaces.cleanup_by_node == {}


async def _run_workspace_success_finalization_preserves_cancel_on_failure() -> None:
    workspace = FailingSlowSuccessfulWorkspace()
    cleanup_registry = DeferredAsyncCleanupRegistry()
    task = asyncio.create_task(
        mark_workspace_succeeded(workspace, True, cleanup_registry)
    )
    assert await asyncio.to_thread(workspace.started.wait, 2)

    task.cancel()
    workspace.release.set()

    with pytest.raises(asyncio.CancelledError) as exc_info:
        await task
    assert workspace.finished.is_set()
    assert _exception_notes_contain(
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
    node_dir = output.create_stage_dir("implement")
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
    workspace.release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    state = read_json_object(state_path)
    assert workspace.finished.is_set()
    assert state["status"] == "cancelled"
    assert state["workspace"]["retention"] == "deleted"
    assert not workspace_path.exists()


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
    ) -> Path:
        del request, prepared_workspace, change_baseline
        started.set()
        assert release.wait(2)
        return tmp_path / "snapshot"

    monkeypatch.setattr(
        provider_invocation_generated_files_module,
        "snapshot_invocation_generated_files",
        slow_snapshot,
    )
    repo = create_git_repo(tmp_path)
    plan = disabled_workspace_plan(repo)
    output = workspace_output_manager(tmp_path, repo)
    node_dir = output.create_stage_dir("implement")
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


async def _run_workspace_preparation_cancellation_marks_workspace_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    cache_root = tmp_path / "cache"
    plan = workspace_plan(repo, cache_root, cleanup_on_success=True)
    output = workspace_output_manager(tmp_path, repo, log_cli_output=True)
    output.create_stage_dir("implement")
    state_path = output.create_stage_dir("implement") / "workspace-state.json"
    workspace_path = cache_root / "snapshots" / "test-repo" / "prep-cancel"
    workspace_path.mkdir(parents=True)
    started = Event()
    release = Event()

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
        return PreparedWorkspace(
            cwd=repo,
            invocation_context=invocation_context,
            workspace_kind="snapshot",
            workspace_path=workspace_path,
            state_path=state_path,
        )

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
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    state = read_json_object(state_path)
    assert state["status"] == "cancelled"
    assert state["child_process_environment"]["applied"] is False
    assert state["workspace"]["retention"] == "deleted"
    assert not workspace_path.exists()


async def _run_workspace_preparation_cancellation_preserves_cancel_when_mark_cancelled_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    plan = disabled_workspace_plan(repo)
    output = workspace_output_manager(tmp_path, repo)
    output.create_stage_dir("implement")
    started = Event()
    release = Event()

    def fake_prepare(
        request: WorkspaceInvocationRequest,
        invocation_context: InvocationContext,
    ) -> PreparedWorkspace:
        del request
        started.set()
        release.wait(timeout=5)
        return FailingMarkCancelledWorkspace(
            cwd=repo,
            invocation_context=invocation_context,
        )

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
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError) as exc_info:
        await task

    assert _exception_notes_contain(
        exc_info.value,
        "Workspace preparation cancellation handling failed: "
        "workspace mark_cancelled boom",
    )


async def _run_workspace_preparation_cancellation_is_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    plan = disabled_workspace_plan(repo)
    output = workspace_output_manager(tmp_path, repo)
    output.create_stage_dir("implement")
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
    output.create_stage_dir("implement")
    state_path = output.create_stage_dir("implement") / "workspace-state.json"
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
        ) -> None:
            super().mark_cancelled(message, child_environment_applied)
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

    timeout_errors = await cleanup_registry.drain(0.01)

    assert len(timeout_errors) == 1
    assert isinstance(timeout_errors[0], TimeoutError)
    assert cleanup_registry.tasks == set()
    assert state_path.exists() is False
    assert workspace_path.exists()

    release.set()
    assert await asyncio.to_thread(cancelled_marked.wait, 2)
    await asyncio.sleep(0)

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
    output.create_stage_dir("implement")
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
    output.create_stage_dir("implement")
    state_path = output.create_stage_dir("implement") / "workspace-state.json"
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

    timeout_errors = await cleanup_registry.drain(0.01)

    assert len(timeout_errors) == 1
    assert isinstance(timeout_errors[0], TimeoutError)
    assert cleanup_registry.tasks == set()
    release.set()
    await asyncio.wait_for(finished.wait(), 1.0)

    assert cleanup_registry.tasks == set()
    assert cleaned is True
    assert cancelled is False


class CancelledInvoker:
    async def invoke(
        self,
        config: AgentConfig,
        model: str | None,
        prompt: str,
        output_file: Path,
        cwd: Path,
        log_file: Path | None = None,
        invocation_context: InvocationContext | None = None,
    ) -> None:
        del config, model, prompt, output_file, cwd, log_file, invocation_context
        raise asyncio.CancelledError()

    def log_presentation_for(self, config: AgentConfig) -> None:
        del config
        return None


class SlowSuccessfulWorkspace:
    def __init__(self) -> None:
        self.started = Event()
        self.release = Event()
        self.finished = Event()
        self.child_environment_applied: bool | None = None
        self.defer_cleanup: bool | None = None

    def mark_succeeded(
        self,
        child_environment_applied: bool | None = None,
        defer_cleanup: bool = False,
    ) -> None:
        self.child_environment_applied = child_environment_applied
        self.defer_cleanup = defer_cleanup
        self.started.set()
        assert self.release.wait(2)
        self.finished.set()


class SlowSuccessfulPreparedWorkspace(SlowSuccessfulWorkspace):
    def __init__(self, workspace_path: Path) -> None:
        super().__init__()
        self.workspace_path = workspace_path
        self.workspace_path.mkdir()
        self.cleanup_on_success = True
        self.cleaned = False
        self.cleaned_event = Event()

    def cleanup_after_success(self) -> None:
        self.cleaned = True
        self.cleaned_event.set()


class FailingSlowSuccessfulWorkspace(SlowSuccessfulWorkspace):
    def mark_succeeded(
        self,
        child_environment_applied: bool | None = None,
        defer_cleanup: bool = False,
    ) -> None:
        super().mark_succeeded(child_environment_applied, defer_cleanup)
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

    def mark_succeeded(
        self,
        child_environment_applied: bool | None = None,
        defer_cleanup: bool = False,
    ) -> None:
        del child_environment_applied, defer_cleanup
        self.started.set()
        assert self.release.wait(2)
        self.finished.set()
        raise RuntimeError("workspace mark_succeeded boom")


class FailingRuntimeInvoker:
    async def invoke(
        self,
        config: AgentConfig,
        model: str | None,
        prompt: str,
        output_file: Path,
        cwd: Path,
        log_file: Path | None = None,
        invocation_context: InvocationContext | None = None,
    ) -> None:
        del config, model, prompt, output_file, cwd, log_file, invocation_context
        raise RuntimeError("provider boom")

    def log_presentation_for(self, config: AgentConfig) -> None:
        del config
        return None


class SuccessfulRuntimeInvoker:
    async def invoke(
        self,
        config: AgentConfig,
        model: str | None,
        prompt: str,
        output_file: Path,
        cwd: Path,
        log_file: Path | None = None,
        invocation_context: InvocationContext | None = None,
    ) -> None:
        del config, model, prompt, cwd, log_file, invocation_context
        output_file.write_text("done\n", encoding="utf-8")

    def log_presentation_for(self, config: AgentConfig) -> None:
        del config
        return None


class FailingMarkFailedWorkspace(PreparedWorkspace):
    def mark_failed(
        self,
        message: str,
        child_environment_applied: bool | None = None,
    ) -> None:
        del message, child_environment_applied
        raise RuntimeError("workspace mark_failed boom")


class FailingMarkCancelledWorkspace(PreparedWorkspace):
    def mark_cancelled(
        self,
        message: str,
        child_environment_applied: bool | None = None,
    ) -> None:
        del message, child_environment_applied
        raise RuntimeError("workspace mark_cancelled boom")


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
    ) -> None:
        self.mark_started.set()
        assert self.release_mark.wait(2)
        super().mark_cancelled(message, child_environment_applied)


class FailingCommandRunnerInvoker:
    def __init__(self) -> None:
        self.child_environment: ChildProcessEnvironment | None = None
        self.invocation_context: InvocationContext | None = None

    async def invoke(
        self,
        config: AgentConfig,
        model: str | None,
        prompt: str,
        output_file: Path,
        cwd: Path,
        log_file: Path | None = None,
        invocation_context: InvocationContext | None = None,
    ) -> None:
        async def command_runner(
            cmd: list[str],
            stdin_data: bytes | None,
            log_file: Path | None,
            append_log: bool,
            log_header: bytes | None,
            cwd: Path,
            invocation_context: InvocationContext | None,
            idle_timeout_seconds: float | None,
            child_environment: ChildProcessEnvironment | None = None,
        ) -> CommandResult:
            del (
                cmd,
                stdin_data,
                log_file,
                append_log,
                log_header,
                cwd,
                idle_timeout_seconds,
            )
            self.child_environment = child_environment
            self.invocation_context = invocation_context
            if (
                invocation_context is not None
                and invocation_context.workspace_environment_applied_recorder
                is not None
            ):
                invocation_context.workspace_environment_applied_recorder()
            return CommandResult(
                returncode=2,
                stdout_text="",
                stderr_text="provider failed",
            )

        await invoke_agent_with_runner(
            config=config,
            model=model,
            prompt=prompt,
            output_file=output_file,
            cwd=cwd,
            log_file=log_file,
            invocation_context=invocation_context,
            command_runner=command_runner,
            plan_builder=build_cli_invocation_plan,
        )

    def log_presentation_for(self, config: AgentConfig) -> None:
        del config
        return None


def _exception_notes_contain(exc: BaseException, expected: str) -> bool:
    return any(expected in note for note in getattr(exc, "__notes__", ()))
