from __future__ import annotations

import asyncio
import os
import signal
import sys
from contextlib import suppress
from dataclasses import replace
from pathlib import Path

import pytest

from crewplane.architecture.contracts import InvocationProcessEvent
from crewplane.runtime.agent.invocation.command import run_command_once
from crewplane.runtime.agent.process.drain import process_group_is_alive
from crewplane.runtime.agent.workspace_environment import workspace_child_environment
from crewplane.runtime.execution.provider_call.lifecycle_state import (
    ProviderInvocationLifecycleState,
)
from crewplane.runtime.workspace import prepare_invocation_workspace
from crewplane.runtime.workspace.mutator_fence import (
    release_workspace_mutator,
    workspace_mutator_is_fenced,
)
from tests.helpers.workspace_service import (
    create_git_repo,
    read_json_object,
    workspace_invocation_context,
    workspace_invocation_request,
    workspace_output_manager,
    workspace_plan,
)

_PROVIDER_SCRIPT = """
import os, pathlib, subprocess, sys, time
if sys.argv[2] == 'none':
    pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))
else:
    subprocess.Popen(
        [sys.executable, '-c', sys.argv[3], sys.argv[1]],
        start_new_session=True,
        stdout=None if sys.argv[2] == 'stdout' else subprocess.DEVNULL,
        stderr=None if sys.argv[2] == 'stderr' else subprocess.DEVNULL,
    )
time.sleep(30)
"""
_PIPE_HOLDER_SCRIPT = """
import os, pathlib, sys, time
pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))
time.sleep(30)
"""


@pytest.mark.skipif(os.name != "posix", reason="Requires POSIX process sessions")
@pytest.mark.parametrize("held_stream", ["none", "stdout", "stderr"])
@pytest.mark.parametrize("capture_logs", [False, True])
def test_cancelled_command_retains_workspace_until_pipes_close(
    tmp_path: Path,
    held_stream: str,
    capture_logs: bool,
) -> None:
    asyncio.run(_cancel_command_with_workspace(tmp_path, held_stream, capture_logs))


async def _cancel_command_with_workspace(
    tmp_path: Path,
    held_stream: str,
    capture_logs: bool,
) -> None:
    repo = create_git_repo(tmp_path)
    plan = workspace_plan(repo, tmp_path / "cache", True, kind="worktree")
    events: list[InvocationProcessEvent] = []
    workspace = prepare_invocation_workspace(
        workspace_invocation_request(plan, workspace_output_manager(tmp_path, repo)),
        replace(workspace_invocation_context(), process_event_sink=events.append),
    )
    assert workspace.state_path is not None
    pid_path = tmp_path / "ready.pid"
    task = asyncio.create_task(
        run_command_once(
            cmd=[
                sys.executable,
                "-c",
                _PROVIDER_SCRIPT,
                str(pid_path),
                held_stream,
                _PIPE_HOLDER_SCRIPT,
            ],
            stdin_data=None,
            log_file=tmp_path / "provider.log" if capture_logs else None,
            append_log=False,
            log_header=None,
            cwd=workspace.cwd,
            invocation_context=workspace.invocation_context,
            idle_timeout_seconds=None,
            child_environment=workspace_child_environment(workspace.cwd, workspace.cwd),
        )
    )
    try:
        async with asyncio.timeout(5):
            while not pid_path.exists():
                if task.done():
                    await task
                await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError) as cancelled:
            await asyncio.wait_for(task, timeout=3)

        pipes_open = held_stream != "none"
        state = read_json_object(workspace.state_path)
        assert state["process_drain"]["status"] == (
            "unresolved" if pipes_open else "confirmed"
        )
        assert workspace_mutator_is_fenced(workspace.state_path) is pipes_open
        assert [event.status for event in events] == ["started", "exited"]
        assert events[-1].returncode is not None
        assert not process_group_is_alive(events[0].process_group_id)

        await ProviderInvocationLifecycleState(
            prepared_workspace=workspace,
            child_environment_applied=True,
        ).mark_cancelled(cancelled.value)

        state = read_json_object(workspace.state_path)
        assert state["status"] == "cancelled"
        assert state["workspace"]["retention"] == (
            "retained" if pipes_open else "deleted"
        )
        assert workspace.cwd.exists() is pipes_open
        if pipes_open:
            os.kill(int(pid_path.read_text()), 0)
            assert any(
                "pipes remained open" in note
                for note in getattr(cancelled.value, "__notes__", ())
            )
        assert asyncio.all_tasks() == {asyncio.current_task()}
    finally:
        if held_stream != "none" and pid_path.exists():
            with suppress(ProcessLookupError):
                os.killpg(int(pid_path.read_text()), signal.SIGKILL)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        release_workspace_mutator(workspace.state_path)
        await asyncio.sleep(0.05)
