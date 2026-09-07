from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from threading import Event
from time import monotonic, sleep

import pytest

import crewplane.runtime.agent.invocation.retry_reset as retry_reset_module
import crewplane.runtime.execution.provider_call.generated_files as provider_invocation_generated_files_module
import crewplane.runtime.execution.provider_call.workspace as provider_invocation_workspace_module
import crewplane.runtime.workspace.locks as workspace_locks_module
import crewplane.runtime.workspace.service.retry_reset as workspace_service_retry_reset
import crewplane.runtime.workspace.service.worktree as workspace_service_worktree
import crewplane.runtime.workspace.service.worktree_failures as workspace_service_worktree_failures
from crewplane.adapters.invokers.cli_invoker import build_cli_invocation_plan
from crewplane.architecture.contracts import CommandResult, InvocationContext
from crewplane.core.config import AgentConfig
from crewplane.core.preflight.models import (
    PreflightExecutionPlan,
    WorkspaceSetupCommandRecord,
    WorkspaceSetupRecord,
)
from crewplane.core.preflight.secrets import SecretContext
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.runtime.agent.invocation.retry_reset import reset_before_retry
from crewplane.runtime.agent.invoker import invoke_agent_with_runner
from crewplane.runtime.execution.provider_call import (
    ProviderCallDisplay,
    ProviderCallRequest,
    finalize_successful_workspace,
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
    PreparedWorkspace,
    WorkspaceInvocationRequest,
    prepare_invocation_workspace,
)
from crewplane.runtime.workspace.invocation import invocation_slug
from crewplane.runtime.workspace.service.types import WorktreePreparationPlan
from crewplane.runtime.workspace.setup import (
    WorkspaceSetupCancelled,
    WorkspaceSetupError,
)
from crewplane.runtime.workspace.state_evidence import record_workspace_process_drain
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


def test_provider_invocation_runs_selected_worktree_setup_before_provider(
    tmp_path: Path,
) -> None:
    asyncio.run(
        _run_provider_invocation_runs_selected_worktree_setup_before_provider(tmp_path)
    )


def test_provider_invocation_setup_failure_prevents_provider_call(
    tmp_path: Path,
) -> None:
    asyncio.run(_run_provider_invocation_setup_failure_prevents_provider_call(tmp_path))


def test_provider_invocation_setup_cancellation_terminates_setup_process_group(
    tmp_path: Path,
) -> None:
    asyncio.run(
        _run_provider_invocation_setup_cancellation_terminates_setup_process_group(
            tmp_path
        )
    )


def test_worktree_retry_reset_reruns_selected_setup(tmp_path: Path) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    cache_root = tmp_path / "cache"
    plan = _plan_with_setup(
        workspace_plan(repo, cache_root, cleanup_on_success=False, kind="worktree"),
        [
            [
                sys.executable,
                "-c",
                (
                    "from pathlib import Path; "
                    "Path('setup-marker.txt').write_text('ready')"
                ),
            ]
        ],
    )
    output = workspace_output_manager(tmp_path, repo, log_cli_output=True)
    output.create_node_dir(node_artifact_request("implement"))
    prepared = prepare_invocation_workspace(
        workspace_invocation_request(plan, output),
        workspace_invocation_context(),
    )
    assert prepared.workspace_path is not None
    assert prepared.state_path is not None
    assert prepared.invocation_context.retry_reset is not None
    source = plan.workspace_source
    assert source is not None

    try:
        marker = prepared.cwd / "setup-marker.txt"
        assert marker.read_text(encoding="utf-8") == "ready"
        marker.unlink()
        (prepared.cwd / "README.md").write_text("dirty\n", encoding="utf-8")
        prepared.invocation_context.retry_reset()

        assert marker.read_text(encoding="utf-8") == "ready"
        assert (prepared.cwd / "README.md").read_text(encoding="utf-8") == "ready\n"
        prepared.mark_succeeded()
        state = read_json_object(prepared.state_path)
        assert state["status"] == "succeeded"
        assert state["run_id"] == plan.run_id
        assert state["setup"]["status"] == "succeeded"
    finally:
        remove_worktree_workspace(source, prepared.workspace_path)


def test_worktree_retry_setup_rejects_identity_change_at_state_update(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    plan = _plan_with_setup(
        workspace_plan(
            repo,
            tmp_path / "cache",
            cleanup_on_success=False,
            kind="worktree",
        ),
        [[sys.executable, "-c", "pass"]],
    )
    output = workspace_output_manager(tmp_path, repo, log_cli_output=True)
    output.create_node_dir(node_artifact_request("implement"))
    prepared = prepare_invocation_workspace(
        workspace_invocation_request(plan, output),
        workspace_invocation_context(),
    )
    assert prepared.workspace_path is not None
    assert prepared.state_path is not None
    assert prepared.workspace_state_payload is not None
    assert prepared.invocation_context.retry_reset is not None
    source = plan.workspace_source
    assert source is not None
    original_update = workspace_service_retry_reset.update_workspace_setup

    def tamper_before_update(
        state_path: Path,
        setup_summary: Mapping[str, object],
        base_payload: Mapping[str, object] | None = None,
    ) -> None:
        payload = read_json_object(state_path)
        payload["run_id"] = "tampered-between-check-and-write"
        state_path.write_text(json.dumps(payload), encoding="utf-8")
        original_update(
            state_path,
            setup_summary,
            base_payload=base_payload,
        )

    monkeypatch.setattr(
        workspace_service_retry_reset,
        "update_workspace_setup",
        tamper_before_update,
    )

    try:
        with pytest.raises(RuntimeError, match="state identity changed"):
            prepared.invocation_context.retry_reset()
        assert prepared.workspace_state_payload["run_id"] == plan.run_id
    finally:
        remove_worktree_workspace(source, prepared.workspace_path)


def test_worktree_retry_reset_cancellation_terminates_retry_setup(
    tmp_path: Path,
) -> None:
    asyncio.run(_run_worktree_retry_reset_cancellation_terminates_retry_setup(tmp_path))


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
    holder = _start_git_metadata_lock_holder(lock_path)
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
        _stop_git_metadata_lock_holder(holder)
        remove_worktree_workspace(source, prepared.workspace_path)


def test_worktree_preparation_cancellation_bounds_asyncio_run_while_lock_is_held(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
    holder = _start_git_metadata_lock_holder(lock_path)
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
        await _wait_for_path(workspace_path)
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        cleanup_errors = await cleanup_registry.drain(0.05)
        assert not any(isinstance(error, TimeoutError) for error in cleanup_errors)
        assert all(
            str(error) == "Workspace Git metadata lock acquisition was cancelled."
            for error in cleanup_errors
        )

    try:
        started = monotonic()
        asyncio.run(cancel_preparation())
        elapsed = monotonic() - started

        assert elapsed < 0.5
        state = read_json_object(state_path)
        assert state["status"] == "cancelled"
        assert state["workspace"]["retention"] == "retained"
        assert workspace_path.exists()
    finally:
        _stop_git_metadata_lock_holder(holder)


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
    holder = _start_git_metadata_lock_holder(lock_path) if hold_git_lock else None
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
            _stop_git_metadata_lock_holder(holder)
        if prepared.workspace_path.exists():
            remove_worktree_workspace(source, prepared.workspace_path)


def test_worktree_success_finalizer_bounds_asyncio_run_while_lock_is_held(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    cache_root = tmp_path / "cache"
    plan = workspace_plan(repo, cache_root, cleanup_on_success=False, kind="worktree")
    output = workspace_output_manager(tmp_path, repo, log_cli_output=True)
    node_dir = output.create_node_dir(node_artifact_request("implement"))
    prepared = prepare_invocation_workspace(
        workspace_invocation_request(plan, output),
        workspace_invocation_context(),
    )
    assert prepared.workspace_path is not None
    source = plan.workspace_source
    assert source is not None
    lock_path = Path(source.common_git_dir) / "crewplane" / "workspace.lock"
    holder = _start_git_metadata_lock_holder(lock_path)
    monkeypatch.setattr(
        provider_invocation_generated_files_module,
        "WORKSPACE_THREAD_CANCELLATION_TIMEOUT_SECONDS",
        0.01,
    )
    lock_poll_started = Event()
    release_lock_poll = Event()

    def controlled_lock_poll(seconds: float) -> None:
        del seconds
        lock_poll_started.set()
        assert release_lock_poll.wait(2)

    monkeypatch.setattr(workspace_locks_module, "sleep", controlled_lock_poll)
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

    async def cancel_finalizer() -> tuple[Exception, ...]:
        task = asyncio.create_task(
            finalize_successful_workspace(request, prepared, None, None)
        )
        assert await asyncio.to_thread(lock_poll_started.wait, 2)
        task.cancel()
        try:
            with pytest.raises(WorkspaceFinalizationDeferredCancellation):
                await task
        finally:
            release_lock_poll.set()
        return await runtime_context.deferred_workspace_cleanups.drain(0.2)

    try:
        started = monotonic()
        cleanup_errors = asyncio.run(cancel_finalizer())
        elapsed = monotonic() - started

        assert elapsed < 0.5
        assert len(cleanup_errors) == 1
        assert str(cleanup_errors[0]) == (
            "Workspace Git metadata lock acquisition was cancelled."
        )
    finally:
        release_lock_poll.set()
        _stop_git_metadata_lock_holder(holder)
        if prepared.workspace_path.exists():
            remove_worktree_workspace(source, prepared.workspace_path)


def test_worktree_setup_failure_records_retained_when_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    cache_root = tmp_path / "cache"
    plan = _plan_with_setup(
        workspace_plan(repo, cache_root, cleanup_on_success=True, kind="worktree"),
        [[sys.executable, "-c", "import sys; sys.exit(7)"]],
    )
    output = workspace_output_manager(tmp_path, repo, log_cli_output=True)
    output.create_node_dir(node_artifact_request("implement"))

    def fail_cleanup(source: object, workspace_path: Path, git_dir: Path) -> None:
        del source, git_dir
        assert workspace_path.exists()
        raise RuntimeError("cleanup denied")

    monkeypatch.setattr(
        workspace_service_worktree_failures,
        "remove_worktree_workspace",
        fail_cleanup,
    )

    with pytest.raises(WorkspaceSetupError):
        prepare_invocation_workspace(
            workspace_invocation_request(plan, output),
            workspace_invocation_context(),
        )

    state = read_json_object(
        output.create_node_dir(node_artifact_request("implement"))
        / "workspace-state.json"
    )
    assert state["status"] == "failed"
    assert state["workspace"]["retention"] == "retained"
    assert state["workspace"]["retained_reason"] == "setup_failed_cleanup_failed"
    assert state["diagnostics"][-1] == {
        "level": "warning",
        "message": "Workspace cleanup after preparation failure failed: cleanup denied",
    }


def test_worktree_preparation_failure_records_retained_when_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    cache_root = tmp_path / "cache"
    plan = workspace_plan(repo, cache_root, cleanup_on_success=True, kind="worktree")
    output = workspace_output_manager(tmp_path, repo, log_cli_output=True)
    output.create_node_dir(node_artifact_request("implement"))

    def fail_state_refresh(payload: dict[str, object], state_path: Path) -> None:
        del payload
        assert state_path.is_file()
        raise RuntimeError("workspace state refresh failed")

    def fail_cleanup(source: object, workspace_path: Path, git_dir: Path) -> None:
        del source, git_dir
        assert workspace_path.exists()
        raise RuntimeError("cleanup denied")

    monkeypatch.setattr(
        workspace_service_worktree,
        "refresh_trusted_workspace_state_payload",
        fail_state_refresh,
    )
    monkeypatch.setattr(
        workspace_service_worktree_failures,
        "remove_worktree_workspace",
        fail_cleanup,
    )

    with pytest.raises(RuntimeError, match="workspace state refresh failed"):
        prepare_invocation_workspace(
            workspace_invocation_request(plan, output),
            workspace_invocation_context(),
        )

    state = read_json_object(
        output.create_node_dir(node_artifact_request("implement"))
        / "workspace-state.json"
    )
    assert state["status"] == "failed"
    assert state["workspace"]["retention"] == "retained"
    assert state["workspace"]["retained_reason"] == (
        "preparation_failed_cleanup_failed"
    )
    assert state["diagnostics"][-1] == {
        "level": "warning",
        "message": "Workspace cleanup after preparation failure failed: cleanup denied",
    }


def test_worktree_preparation_cancellation_persists_cleanup_diagnostic(
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
    output = workspace_output_manager(tmp_path, repo, log_cli_output=True)
    output.create_node_dir(node_artifact_request("implement"))
    prepared = prepare_invocation_workspace(
        workspace_invocation_request(plan, output),
        workspace_invocation_context(),
    )
    assert prepared.workspace_path is not None
    assert prepared.state_path is not None

    def fail_cleanup(source_arg: object, workspace_path: Path, git_dir: Path) -> None:
        del source_arg, git_dir
        assert workspace_path == prepared.workspace_path
        raise RuntimeError("cleanup denied")

    monkeypatch.setattr(
        workspace_service_worktree_failures,
        "remove_worktree_workspace",
        fail_cleanup,
    )
    failure = WorkspaceSetupCancelled(
        "Workspace setup profile was cancelled.",
        {"status": "cancelled"},
    )

    workspace_service_worktree_failures.record_cancelled_worktree_preparation(
        source,
        prepared.workspace_path,
        prepared.state_path,
        failure,
    )

    state = read_json_object(prepared.state_path)
    assert state["status"] == "cancelled"
    assert state["workspace"]["retention"] == "retained"
    assert state["workspace"]["retained_reason"] == "cancelled_cleanup_failed"
    assert state["diagnostics"][-1] == {
        "level": "warning",
        "message": (
            "Workspace cleanup after preparation cancellation failed: cleanup denied"
        ),
    }
    remove_worktree_workspace(source, prepared.workspace_path)


def test_worktree_preparation_retains_when_process_drain_is_unresolved(
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
    output = workspace_output_manager(tmp_path, repo, log_cli_output=True)
    node_dir = output.create_node_dir(node_artifact_request("implement"))
    cleanup_calls = 0

    def fail_with_unresolved_process(
        request: WorkspaceInvocationRequest,
        preparation_plan: WorktreePreparationPlan,
        cwd: Path,
        checkout_root: Path,
        trusted_state_payload: dict[str, object],
    ) -> None:
        del request, cwd, checkout_root, trusted_state_payload
        record_workspace_process_drain(
            preparation_plan.state_path,
            "unresolved",
            pid=123,
            process_group_id=123,
            reason="test process remains live",
        )
        raise RuntimeError("setup drain failed")

    def reject_cleanup(source: object, workspace_path: Path) -> None:
        del source, workspace_path
        nonlocal cleanup_calls
        cleanup_calls += 1

    monkeypatch.setattr(
        workspace_service_worktree,
        "run_worktree_setup",
        fail_with_unresolved_process,
    )
    monkeypatch.setattr(
        workspace_service_worktree_failures,
        "remove_worktree_workspace",
        reject_cleanup,
    )

    with pytest.raises(RuntimeError, match="setup drain failed"):
        prepare_invocation_workspace(
            workspace_invocation_request(plan, output),
            workspace_invocation_context(),
        )

    state = read_json_object(node_dir / "workspace-state.json")
    workspace_path = Path(str(state["execution"]["workspace_path"]))
    assert cleanup_calls == 0
    assert workspace_path.exists()
    assert state["status"] == "failed"
    assert state["workspace"]["retention"] == "retained"
    assert state["workspace"]["retained_reason"] == "process_drain_unresolved"

    source = plan.workspace_source
    assert source is not None
    remove_worktree_workspace(source, workspace_path)


def test_provider_invocation_skips_unselected_setup_profile_for_snapshot(
    tmp_path: Path,
) -> None:
    asyncio.run(
        _run_provider_invocation_skips_unselected_setup_profile_for_snapshot(tmp_path)
    )


async def _run_provider_invocation_runs_selected_worktree_setup_before_provider(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    cache_root = tmp_path / "cache"
    plan = _plan_with_setup(
        workspace_plan(
            repo,
            cache_root,
            cleanup_on_success=True,
            kind="worktree",
            launch_mode="mock_no_child_process",
            controlled_child_environment=False,
        ),
        [
            [
                sys.executable,
                "-c",
                (
                    "from pathlib import Path; "
                    "Path('setup-marker.txt').write_text('ready')"
                ),
            ]
        ],
    )
    output = workspace_output_manager(tmp_path, repo, log_cli_output=True)
    output.create_node_dir(node_artifact_request("implement"))
    runtime_context = CompiledRuntimeContext(
        plan=plan,
        secret_context=SecretContext(),
    )
    node_dir = output.get_node_dir(node_artifact_request("implement"))
    assert node_dir is not None
    invoker = SetupMarkerInvoker(expect_marker=True)

    await run_provider_call(
        ProviderCallRequest(
            runtime_context=runtime_context,
            output=output,
            node_id="implement",
            provider=plan.nodes[0].provider_records[0],
            task_id="alpha",
            audit_round_num=None,
            round_num=1,
            prompt="setup first",
            output_file=node_dir / "alpha_round1.md",
            role_label=ProviderRole.EXECUTOR,
            invoker=invoker,
            telemetry=None,
        ),
        display=ProviderCallDisplay(telemetry=None),
    )

    state = read_json_object(node_dir / "workspace-state.json")
    assert invoker.calls == 1
    assert state["status"] == "succeeded"
    assert state["setup"]["status"] == "succeeded"
    assert state["setup"]["profile_name"] == "bootstrap"
    assert (node_dir / "workspace-setup" / "setup.json").is_file()
    assert (node_dir / "workspace-setup" / "setup.log").is_file()
    runtime_context.generated_file_workspaces.cleanup_node("implement")


async def _run_worktree_retry_reset_cancellation_terminates_retry_setup(
    tmp_path: Path,
) -> None:
    if os.name != "posix":
        pytest.skip("process-group cleanup is POSIX-only")
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    cache_root = tmp_path / "cache"
    setup_counter = tmp_path / "setup-count.txt"
    retry_setup_started = tmp_path / "retry-setup-started.txt"
    leaked_child_marker = tmp_path / "retry-setup-child-survived.txt"
    child_script = (
        "import pathlib, time; "
        "time.sleep(1.0); "
        f"pathlib.Path({str(leaked_child_marker)!r}).write_text('alive')"
    )
    setup_script = (
        "import pathlib, subprocess, sys, time; "
        f"counter = pathlib.Path({str(setup_counter)!r}); "
        "count = int(counter.read_text()) if counter.exists() else 0; "
        "counter.write_text(str(count + 1)); "
        "pathlib.Path('setup-marker.txt').write_text('ready'); "
        "started = "
        f"pathlib.Path({str(retry_setup_started)!r}); "
        "subprocess.Popen([sys.executable, '-c', "
        f"{child_script!r}]) if count else None; "
        "started.write_text('started') if count else None; "
        "time.sleep(30) if count else None"
    )
    plan = _plan_with_setup(
        workspace_plan(repo, cache_root, cleanup_on_success=False, kind="worktree"),
        [[sys.executable, "-c", setup_script]],
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

    async def runner(
        cmd: list[str],  # noqa: ARG001
        stdin_data: bytes | None,  # noqa: ARG001
        log_file: Path | None,  # noqa: ARG001
        append_log: bool,  # noqa: ARG001
        log_header: bytes | None,  # noqa: ARG001
        cwd: Path,  # noqa: ARG001
        invocation_context: InvocationContext | None,  # noqa: ARG001
        idle_timeout_seconds: float | None,  # noqa: ARG001
        child_environment: object | None = None,  # noqa: ARG001
    ):
        return CommandResult(
            returncode=2,
            stdout_text="retry",
            stderr_text="",
        )

    task = asyncio.create_task(
        invoke_agent_with_runner(
            config=AgentConfig(
                cli_cmd=[sys.executable],
                default_model="test",
                max_retries=1,
                retry_delay_seconds=0,
                retry_on_exit_codes=[2],
            ),
            model="test",
            prompt="prompt",
            output_file=node_dir / "alpha_round1.md",
            cwd=prepared.cwd,
            log_file=None,
            invocation_context=prepared.invocation_context,
            command_runner=runner,
            plan_builder=build_cli_invocation_plan,
        )
    )
    try:
        await _wait_for_path(retry_setup_started)
        cancelled_at = monotonic()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        elapsed = monotonic() - cancelled_at

        await asyncio.sleep(1.2)
        state = read_json_object(prepared.state_path)
        assert elapsed < 1.5
        assert state["setup"]["status"] == "cancelled"
        assert not leaked_child_marker.exists()
    finally:
        if not task.done():
            task.cancel()
        remove_worktree_workspace(source, prepared.workspace_path)


async def _run_provider_invocation_setup_failure_prevents_provider_call(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    cache_root = tmp_path / "cache"
    plan = _plan_with_setup(
        workspace_plan(repo, cache_root, cleanup_on_success=True, kind="worktree"),
        [[sys.executable, "-c", "import sys; sys.exit(7)"]],
    )
    output = workspace_output_manager(tmp_path, repo, log_cli_output=True)
    output.create_node_dir(node_artifact_request("implement"))
    runtime_context = CompiledRuntimeContext(
        plan=plan,
        secret_context=SecretContext(),
    )
    node_dir = output.get_node_dir(node_artifact_request("implement"))
    assert node_dir is not None
    invoker = SetupMarkerInvoker(expect_marker=True)
    output_file = node_dir / "alpha_round1.md"

    with pytest.raises(WorkspaceSetupError):
        await run_provider_call(
            ProviderCallRequest(
                runtime_context=runtime_context,
                output=output,
                node_id="implement",
                provider=plan.nodes[0].provider_records[0],
                task_id="alpha",
                audit_round_num=None,
                round_num=1,
                prompt="setup failure",
                output_file=output_file,
                role_label=ProviderRole.EXECUTOR,
                invoker=invoker,
                telemetry=None,
            ),
            display=ProviderCallDisplay(telemetry=None),
        )

    state = read_json_object(node_dir / "workspace-state.json")
    assert invoker.calls == 0
    assert not output_file.exists()
    assert state["status"] == "failed"
    assert state["setup"]["status"] == "failed"
    assert state["setup"]["commands"][0]["exit_code"] == 7


async def _run_provider_invocation_setup_cancellation_terminates_setup_process_group(
    tmp_path: Path,
) -> None:
    if os.name != "posix":
        pytest.skip("process-group cleanup is POSIX-only")
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    cache_root = tmp_path / "cache"
    setup_started = tmp_path / "setup-started.txt"
    leaked_child_marker = tmp_path / "setup-child-survived.txt"
    child_script = (
        "import pathlib, time; "
        "time.sleep(1.0); "
        f"pathlib.Path({str(leaked_child_marker)!r}).write_text('alive')"
    )
    parent_script = (
        "import pathlib, subprocess, sys, time; "
        f"pathlib.Path({str(setup_started)!r}).write_text('started'); "
        f"subprocess.Popen([sys.executable, '-c', {child_script!r}]); "
        "time.sleep(30)"
    )
    plan = _plan_with_setup(
        workspace_plan(repo, cache_root, cleanup_on_success=True, kind="worktree"),
        [[sys.executable, "-c", parent_script]],
    )
    output = workspace_output_manager(tmp_path, repo, log_cli_output=True)
    output.create_node_dir(node_artifact_request("implement"))
    runtime_context = CompiledRuntimeContext(
        plan=plan,
        secret_context=SecretContext(),
    )
    node_dir = output.get_node_dir(node_artifact_request("implement"))
    assert node_dir is not None
    invoker = SetupMarkerInvoker(expect_marker=True)
    output_file = node_dir / "alpha_round1.md"

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
                prompt="cancel setup",
                output_file=output_file,
                role_label=ProviderRole.EXECUTOR,
                invoker=invoker,
                telemetry=None,
            ),
            display=ProviderCallDisplay(telemetry=None),
        )
    )
    await _wait_for_path(setup_started)

    cancelled_at = monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    elapsed = monotonic() - cancelled_at
    assert runtime_context.deferred_workspace_cleanups.tasks == set()
    errors = await runtime_context.deferred_workspace_cleanups.drain(2.0)

    await asyncio.sleep(1.2)
    state = read_json_object(node_dir / "workspace-state.json")
    assert errors == ()
    assert elapsed < 1.5
    assert invoker.calls == 0
    assert not output_file.exists()
    assert state["status"] == "cancelled"
    assert state["setup"]["status"] == "cancelled"
    assert state["workspace"]["retention"] == "deleted"
    assert not (
        cache_root
        / "workspaces"
        / "test-repo"
        / plan.run_key_name
        / invocation_slug("implement", "alpha", None, 1)
    ).exists()
    assert not leaked_child_marker.exists()


async def _run_provider_invocation_skips_unselected_setup_profile_for_snapshot(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    cache_root = tmp_path / "cache"
    plan = _plan_with_available_setup_profile(
        workspace_plan(
            repo,
            cache_root,
            cleanup_on_success=True,
            kind="snapshot",
            launch_mode="mock_no_child_process",
            controlled_child_environment=False,
        ),
        [
            [
                sys.executable,
                "-c",
                (
                    "from pathlib import Path; "
                    "Path('setup-marker.txt').write_text('unexpected')"
                ),
            ]
        ],
    )
    output = workspace_output_manager(tmp_path, repo, log_cli_output=True)
    output.create_node_dir(node_artifact_request("implement"))
    runtime_context = CompiledRuntimeContext(
        plan=plan,
        secret_context=SecretContext(),
    )
    node_dir = output.get_node_dir(node_artifact_request("implement"))
    assert node_dir is not None
    invoker = SetupMarkerInvoker(expect_marker=False)

    await run_provider_call(
        ProviderCallRequest(
            runtime_context=runtime_context,
            output=output,
            node_id="implement",
            provider=plan.nodes[0].provider_records[0],
            task_id="alpha",
            audit_round_num=None,
            round_num=1,
            prompt="snapshot setup skip",
            output_file=node_dir / "alpha_round1.md",
            role_label=ProviderRole.EXECUTOR,
            invoker=invoker,
            telemetry=None,
        ),
        display=ProviderCallDisplay(telemetry=None),
    )

    state = read_json_object(node_dir / "workspace-state.json")
    assert invoker.calls == 1
    assert "setup" not in state
    assert not (node_dir / "workspace-setup").exists()
    runtime_context.generated_file_workspaces.cleanup_node("implement")


async def _wait_for_path(path: Path, timeout_seconds: float = 2.0) -> None:
    deadline = monotonic() + timeout_seconds
    while monotonic() < deadline:
        if path.exists():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"Timed out waiting for {path.as_posix()}")


def _start_git_metadata_lock_holder(lock_path: Path) -> subprocess.Popen[str]:
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import fcntl, pathlib, sys, time; "
                "handle = pathlib.Path(sys.argv[1]).open('a+b'); "
                "fcntl.flock(handle.fileno(), fcntl.LOCK_EX); "
                "print('locked', flush=True); "
                "time.sleep(1)"
            ),
            lock_path.as_posix(),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert holder.stdout is not None
    assert holder.stdout.readline().strip() == "locked"
    return holder


def _stop_git_metadata_lock_holder(holder: subprocess.Popen[str]) -> None:
    holder.terminate()
    holder.wait(timeout=2)
    if holder.stdout is not None:
        holder.stdout.close()


class SetupMarkerInvoker:
    def __init__(self, expect_marker: bool) -> None:
        self.expect_marker = expect_marker
        self.calls = 0

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
        del config, model, prompt, log_file, invocation_context
        self.calls += 1
        marker = cwd / "setup-marker.txt"
        assert marker.exists() is self.expect_marker
        if self.expect_marker:
            assert marker.read_text(encoding="utf-8") == "ready"
        output_file.write_text("provider completed\n", encoding="utf-8")

    def log_presentation_for(self, config: AgentConfig) -> None:
        del config
        return None


def _plan_with_setup(
    plan: PreflightExecutionPlan,
    commands: list[list[str]],
) -> PreflightExecutionPlan:
    plan = _plan_with_available_setup_profile(plan, commands)
    node = plan.nodes[0]
    policy = node.workspace_policy
    assert policy is not None
    updated_policy = policy.model_copy(
        update={
            "setup": WorkspaceSetupRecord(
                profile_name="bootstrap",
                commands=[
                    WorkspaceSetupCommandRecord(argv=argv, command_index=index)
                    for index, argv in enumerate(commands)
                ],
            )
        }
    )
    return plan.model_copy(
        update={
            "nodes": [node.model_copy(update={"workspace_policy": updated_policy})],
        }
    )


def _plan_with_available_setup_profile(
    plan: PreflightExecutionPlan,
    commands: list[list[str]],
) -> PreflightExecutionPlan:
    runtime_snapshot = dict(plan.runtime_config_snapshot)
    workspace = dict(runtime_snapshot.get("workspace", {}))
    workspace["setup_profiles"] = {"bootstrap": {"run": commands}}
    runtime_snapshot["workspace"] = workspace
    return plan.model_copy(update={"runtime_config_snapshot": runtime_snapshot})
