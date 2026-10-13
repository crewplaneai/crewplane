from __future__ import annotations

import asyncio
import sys
from collections.abc import Iterator
from contextlib import ExitStack
from pathlib import Path
from threading import Event

import pytest

import crewplane.runtime.execution.provider_call.generated_files as provider_invocation_generated_files_module
import crewplane.runtime.workspace.locks as workspace_locks_module
import crewplane.runtime.workspace.service.worktree as workspace_service_worktree
import crewplane.runtime.workspace.service.worktree_failures as workspace_service_worktree_failures
from crewplane.core.preflight.secrets import SecretContext
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.runtime.execution.provider_call import (
    ProviderCallRequest,
    finalize_successful_workspace,
)
from crewplane.runtime.execution.provider_call.cancellation import (
    WorkspaceFinalizationDeferredCancellation,
)
from crewplane.runtime.execution.runtime_context import (
    CompiledRuntimeContext,
)
from crewplane.runtime.workspace import (
    WorkspaceInvocationRequest,
    prepare_invocation_workspace,
)
from crewplane.runtime.workspace.service.types import WorktreePreparationPlan
from crewplane.runtime.workspace.setup import (
    WorkspaceSetupCancelled,
    WorkspaceSetupError,
)
from crewplane.runtime.workspace.state_evidence import record_workspace_process_drain
from tests.helpers.artifacts import node_artifact_request
from tests.helpers.platforms import requires_workspace_support
from tests.helpers.workspace_service import (
    create_git_repo,
    read_json_object,
    workspace_invocation_context,
    workspace_invocation_request,
    workspace_output_manager,
    workspace_plan,
)
from tests.unit.runtime.workspace.service_provider_invocation_support import (
    owned_workspace_task,
)
from tests.unit.runtime.workspace.service_provider_setup_invocation_support import (
    plan_with_setup,
    remove_remaining_worktree,
    start_git_metadata_lock_holder,
    stop_git_metadata_lock_holder,
)

pytestmark = requires_workspace_support


@pytest.fixture
def cleanup_stack(monkeypatch: pytest.MonkeyPatch) -> Iterator[ExitStack]:
    del monkeypatch  # Restore patches only after dependent resources are closed.
    with ExitStack() as stack:
        yield stack


def test_worktree_success_finalizer_bounds_asyncio_run_while_lock_is_held(
    cleanup_stack: ExitStack,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = create_git_repo(tmp_path)
    cache_root = tmp_path / "cache"
    plan = workspace_plan(repo, cache_root, cleanup_on_success=False, kind="worktree")
    output = workspace_output_manager(tmp_path, repo, log_cli_output=True)
    node_dir = output.create_node_dir(node_artifact_request("implement"))
    prepared = prepare_invocation_workspace(
        workspace_invocation_request(plan, output),
        workspace_invocation_context(),
    )
    cleanup_stack.callback(remove_remaining_worktree, plan, prepared.workspace_path)
    assert prepared.workspace_path is not None
    source = plan.workspace_source
    assert source is not None
    lock_path = Path(source.common_git_dir) / "crewplane" / "workspace.lock"
    holder = start_git_metadata_lock_holder(lock_path)
    cleanup_stack.callback(stop_git_metadata_lock_holder, holder)
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
        assert release_lock_poll.wait(10)

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
        async with owned_workspace_task(
            finalize_successful_workspace(request, prepared, None, None),
            runtime_context.deferred_workspace_cleanups,
            (release_lock_poll,),
        ) as task:
            assert await asyncio.to_thread(lock_poll_started.wait, 10)
            task.cancel()
            try:
                with pytest.raises(WorkspaceFinalizationDeferredCancellation):
                    await asyncio.wait_for(task, timeout=10)
                assert not release_lock_poll.is_set()
            finally:
                release_lock_poll.set()
            return await runtime_context.deferred_workspace_cleanups.drain(10)

    try:
        cleanup_errors = asyncio.run(cancel_finalizer())

        assert holder.poll() is None
        assert len(cleanup_errors) == 1
        assert str(cleanup_errors[0]) == (
            "Workspace Git metadata lock acquisition was cancelled."
        )
    finally:
        release_lock_poll.set()


def test_worktree_setup_failure_records_retained_when_cleanup_fails(
    cleanup_stack: ExitStack,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = create_git_repo(tmp_path)
    cache_root = tmp_path / "cache"
    plan = plan_with_setup(
        workspace_plan(repo, cache_root, cleanup_on_success=True, kind="worktree"),
        [[sys.executable, "-c", "import sys; sys.exit(int(sys.argv[1]))", "7"]],
    )
    output = workspace_output_manager(tmp_path, repo, log_cli_output=True)
    output.create_node_dir(node_artifact_request("implement"))

    def fail_cleanup(source: object, workspace_path: Path, git_dir: Path) -> None:
        del source, git_dir
        cleanup_stack.callback(remove_remaining_worktree, plan, workspace_path)
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
    cleanup_stack: ExitStack,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
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
        cleanup_stack.callback(remove_remaining_worktree, plan, workspace_path)
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
    cleanup_stack: ExitStack,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
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
    cleanup_stack.callback(remove_remaining_worktree, plan, prepared.workspace_path)
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


def test_worktree_preparation_retains_when_process_drain_is_unresolved(
    cleanup_stack: ExitStack,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
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
    cleanup_calls = 0

    def fail_with_unresolved_process(
        request: WorkspaceInvocationRequest,
        preparation_plan: WorktreePreparationPlan,
        cwd: Path,
        checkout_root: Path,
        trusted_state_payload: dict[str, object],
    ) -> None:
        del request, cwd, checkout_root, trusted_state_payload
        cleanup_stack.callback(
            remove_remaining_worktree, plan, preparation_plan.planned_workspace_path
        )
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
