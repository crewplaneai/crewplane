from __future__ import annotations

import asyncio
import os
import shutil
import sys
from pathlib import Path
from time import monotonic

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
    plan = plan_with_setup(
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
    plan = plan_with_setup(
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


async def _wait_for_path(path: Path, timeout_seconds: float = 2.0) -> None:
    deadline = monotonic() + timeout_seconds
    while monotonic() < deadline:
        if path.exists():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"Timed out waiting for {path.as_posix()}")
