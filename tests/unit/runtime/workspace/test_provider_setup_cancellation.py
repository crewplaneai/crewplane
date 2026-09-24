from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from contextlib import suppress
from pathlib import Path

import pytest

from crewplane.adapters.invokers.cli_invoker import build_cli_invocation_plan
from crewplane.architecture.contracts import CommandResult, InvocationContext
from crewplane.core.config import AgentConfig
from crewplane.core.preflight.secrets import SecretContext
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.runtime.agent.invoker import invoke_agent_with_runner
from crewplane.runtime.execution.provider_call import (
    ProviderCallDisplay,
    ProviderCallRequest,
    run_provider_call,
)
from crewplane.runtime.execution.runtime_context import (
    CompiledRuntimeContext,
)
from crewplane.runtime.workspace import (
    prepare_invocation_workspace,
)
from crewplane.runtime.workspace.invocation import invocation_slug
from crewplane.runtime.workspace.worktree import remove_worktree_workspace
from tests.helpers.artifacts import node_artifact_request
from tests.helpers.processes import kill_process_group
from tests.helpers.workspace_service import (
    create_git_repo,
    read_json_object,
    workspace_invocation_context,
    workspace_invocation_request,
    workspace_output_manager,
    workspace_plan,
)
from tests.unit.runtime.workspace.service_provider_setup_invocation_support import (
    SetupMarkerInvoker,
    plan_with_setup,
)


def test_provider_invocation_setup_cancellation_terminates_setup_process_group(
    tmp_path: Path,
) -> None:
    asyncio.run(
        _run_provider_invocation_setup_cancellation_terminates_setup_process_group(
            tmp_path
        )
    )


def test_worktree_retry_reset_cancellation_terminates_retry_setup(
    tmp_path: Path,
) -> None:
    asyncio.run(_run_worktree_retry_reset_cancellation_terminates_retry_setup(tmp_path))


async def _run_worktree_retry_reset_cancellation_terminates_retry_setup(
    tmp_path: Path,
) -> None:
    if os.name != "posix":
        pytest.skip("process-group cleanup is POSIX-only")
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    cache_root = tmp_path / "cache"
    plan = plan_with_setup(
        workspace_plan(repo, cache_root, cleanup_on_success=False, kind="worktree"),
        [_setup_command(tmp_path, "retry")],
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
        child = await _wait_for_child(tmp_path, task)
        task.cancel()
        await _assert_cancelled(task)
        await _assert_child_stopped(child)
        state = read_json_object(prepared.state_path)
        assert state["setup"]["status"] == "cancelled"
    finally:
        try:
            await _cleanup_setup(tmp_path, task)
        finally:
            remove_worktree_workspace(source, prepared.workspace_path)


async def _run_provider_invocation_setup_cancellation_terminates_setup_process_group(
    tmp_path: Path,
) -> None:
    if os.name != "posix":
        pytest.skip("process-group cleanup is POSIX-only")
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    cache_root = tmp_path / "cache"
    plan = plan_with_setup(
        workspace_plan(repo, cache_root, cleanup_on_success=True, kind="worktree"),
        [_setup_command(tmp_path, "setup")],
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
    try:
        child = await _wait_for_child(tmp_path, task)
        task.cancel()
        await _assert_cancelled(task)
        await _assert_child_stopped(child)
        assert runtime_context.deferred_workspace_cleanups.tasks == set()
        errors = await runtime_context.deferred_workspace_cleanups.drain(10.0)

        state = read_json_object(node_dir / "workspace-state.json")
        assert errors == ()
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
    finally:
        try:
            await _cleanup_setup(tmp_path, task)
        finally:
            await runtime_context.deferred_workspace_cleanups.drain(10.0)


def _setup_command(root: Path, mode: str) -> list[str]:
    script = Path(__file__).resolve().parents[3] / "fixtures/processes/setup_tree.py"
    return [sys.executable, str(script), mode, str(root)]


async def _wait_for_child(root: Path, task: asyncio.Task[None]) -> dict[str, int]:
    ready = root / "child-ready.json"
    async with asyncio.timeout(30):
        while not ready.exists():
            if task.done():
                await task
                pytest.fail("Setup completed before the descendant became ready")
            await asyncio.sleep(0.01)
    child = json.loads(ready.read_text(encoding="utf-8"))
    os.kill(child["pid"], 0)
    return child


async def _assert_cancelled(task: asyncio.Task[None]) -> None:
    done, _ = await asyncio.wait({task}, timeout=10)
    assert task in done, "Setup cancellation did not finish"
    with pytest.raises(asyncio.CancelledError):
        await task


async def _assert_child_stopped(child: dict[str, int]) -> None:
    async with asyncio.timeout(10):
        while True:
            try:
                os.killpg(child["process_group_id"], 0)
            except ProcessLookupError:
                break
            await asyncio.sleep(0.01)
    with pytest.raises(ProcessLookupError):
        os.kill(child["pid"], 0)


async def _cleanup_setup(root: Path, task: asyncio.Task[None]) -> None:
    # Read the parent's PID even if the descendant never announced readiness.
    pid_path = root / "setup.pid"
    if pid_path.exists():
        kill_process_group(int(pid_path.read_text(encoding="utf-8")))
    if not task.done():
        task.cancel()
    try:
        done, _ = await asyncio.wait({task}, timeout=10)
        assert task in done, "Setup task did not stop during test cleanup"
        with suppress(asyncio.CancelledError):
            await task
    finally:
        # Cancellation can race with the setup worker recording its PID.
        if pid_path.exists():
            kill_process_group(int(pid_path.read_text(encoding="utf-8")))
