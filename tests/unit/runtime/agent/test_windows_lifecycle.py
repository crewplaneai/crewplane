"""Ownership and failure-policy tests; native containment is tested separately."""

import asyncio
import sys
from functools import partial
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from crewplane.architecture.contracts import InvocationContext
from crewplane.architecture.ports.artifacts import ProviderProcessInvocation
from crewplane.artifacts import OutputManager
from crewplane.runtime.agent.invocation import command
from crewplane.runtime.agent.process import windows_launch
from crewplane.runtime.agent.process.drain import (
    ProcessDrainError,
    unconfirmed_process_cleanup,
)
from crewplane.runtime.agent.process.stream_capture import ProcessOutputCapture


@pytest.fixture
def launch(monkeypatch):
    job = Mock()
    job.active_process_count.return_value = 0
    monkeypatch.setattr(windows_launch, "WindowsJob", Mock(return_value=job))
    return windows_launch.WindowsLaunch()


def test_assignment_precedes_gate_and_preserves_environment(
    tmp_path, monkeypatch, launch
):
    process = SimpleNamespace(pid=123, stdin=Mock(), returncode=0)
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(windows_launch.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(
        windows_launch,
        "sys",
        SimpleNamespace(
            executable="venv-redirector.exe", _base_executable="python.exe"
        ),
    )

    async def check():
        with pytest.raises(RuntimeError, match="ownership"):
            launch.release()
        await launch.start(["provider.exe", "arg"], tmp_path, {"A": "B"})
        launch.job.assign.assert_called_once_with(123)
        process.stdin.write.assert_not_called()
        launch.release()
        process.stdin.write.assert_called_once_with(b"\x01")
        with pytest.raises(RuntimeError, match="ownership"):
            launch.release()
        await launch.drain()
        launch.close()

    asyncio.run(check())
    assert spawn.call_args.args[0] == "python.exe"
    assert spawn.call_args.kwargs["cwd"] == tmp_path
    assert spawn.call_args.kwargs["env"] == {"A": "B"}
    launch.job.close.assert_called_once()


def test_cancelled_spawn_retains_helper_for_cleanup(tmp_path, monkeypatch, launch):
    async def check():
        ready = asyncio.Event()
        release = asyncio.Event()
        process = SimpleNamespace(pid=42, stdin=Mock(), returncode=0)

        async def spawn(*args, **kwargs):
            assert args and kwargs
            ready.set()
            await release.wait()
            return process

        monkeypatch.setattr(windows_launch.asyncio, "create_subprocess_exec", spawn)
        task = asyncio.create_task(launch.start(["provider.exe"], tmp_path, None))
        await ready.wait()
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert launch.process is process
        assert not launch.assigned
        launch.job.assign.assert_not_called()
        await launch.drain()
        process.stdin.write.assert_not_called()

    asyncio.run(check())


@pytest.mark.parametrize("failure", ["assignment", "cancellation"])
def test_failed_startup_does_not_publish_exit_without_start(
    tmp_path, monkeypatch, launch, failure
):
    output = OutputManager("workflow", base_dir=tmp_path)
    invocation = ProviderProcessInvocation(
        "node", "task", "generic", "executor", None, 1
    )
    receipt = Mock(wraps=partial(output.write_provider_process_event, invocation))
    context = InvocationContext(
        "node", "task", "generic", "executor", process_event_sink=receipt
    )
    process = SimpleNamespace(
        pid=42, stdin=Mock(), stdout=None, stderr=None, returncode=None
    )

    def kill():
        process.returncode = 1

    process.kill = Mock(side_effect=kill)
    monkeypatch.setattr(command, "WindowsLaunch", Mock(return_value=launch))
    monkeypatch.setattr(
        command, "sys", SimpleNamespace(platform="win32", exception=sys.exception)
    )
    monkeypatch.setattr(command, "supports_posix_process_groups", lambda: False)
    if failure == "assignment":
        launch.job.assign.side_effect = OSError("assignment failed")

    async def check():
        ready, release = asyncio.Event(), asyncio.Event()

        async def spawn(*args, **kwargs):
            assert args and kwargs
            ready.set()
            if failure == "cancellation":
                await release.wait()
            return process

        monkeypatch.setattr(windows_launch.asyncio, "create_subprocess_exec", spawn)
        async with asyncio.timeout(5):
            task = asyncio.create_task(
                command.run_command_once(
                    ["provider.exe"], None, None, False, None, tmp_path, context, None
                )
            )
            await ready.wait()
            if failure == "cancellation":
                task.cancel()
                release.set()
            expected = (
                asyncio.CancelledError if failure == "cancellation" else RuntimeError
            )
            with pytest.raises(expected) as error:
                await task
        assert unconfirmed_process_cleanup(error.value) is None
        if failure == "assignment":
            assert "assignment failed" in str(error.value)

    asyncio.run(check())
    receipt.assert_not_called()
    process.kill.assert_called_once()
    process.stdin.write.assert_not_called()
    launch.job.close.assert_called_once()
    assert launch.cleanup_confirmed


@pytest.mark.parametrize("query_error", [False, True])
def test_unconfirmed_membership_is_never_success(monkeypatch, launch, query_error):
    launch.process = SimpleNamespace(pid=42, stdin=Mock(), returncode=0)
    launch.assigned = True
    if query_error:
        launch.job.active_process_count.side_effect = OSError("membership unavailable")
    else:
        launch.job.active_process_count.return_value = 1
    monkeypatch.setattr(windows_launch, "PROCESS_GROUP_TERM_GRACE_SECONDS", 0)
    monkeypatch.setattr(windows_launch, "PROCESS_GROUP_KILL_GRACE_SECONDS", 0)
    with pytest.raises(ProcessDrainError) as failure:
        asyncio.run(launch.drain())
    assert not launch.cleanup_confirmed
    assert failure.value.evidence.leader_stopped
    assert not failure.value.evidence.process_group_stopped


def test_unassigned_helper_is_killed_without_release(launch):
    process = SimpleNamespace(pid=42, stdin=Mock(), returncode=None)

    def kill():
        process.returncode = 1

    process.kill = Mock(side_effect=kill)
    launch.process = process
    asyncio.run(launch.drain())
    process.kill.assert_called_once()
    process.stdin.write.assert_not_called()
    assert launch.cleanup_confirmed


@pytest.mark.parametrize("membership_error", [False, True])
def test_collect_waits_for_membership_and_reclaims_failed_capture(
    monkeypatch, launch, membership_error
):
    launch.process = SimpleNamespace(pid=42, stdin=Mock(), returncode=0)
    launch.assigned = True
    capture = Mock(spec=ProcessOutputCapture)
    monkeypatch.setattr(windows_launch, "wait_for_process_exit", AsyncMock())
    monkeypatch.setattr(
        windows_launch,
        "write_stdin_and_collect_output",
        AsyncMock(return_value=capture),
    )
    if membership_error:
        launch.job.active_process_count.side_effect = OSError("query failed")
        with pytest.raises(ProcessDrainError, match="query failed"):
            asyncio.run(launch.collect(b"prompt", None, None, None))
        capture.cleanup.assert_called_once()
    else:
        assert asyncio.run(launch.collect(b"prompt", None, None, None)) is capture
        assert launch.cleanup_confirmed
        capture.cleanup.assert_not_called()


def test_wrapped_cancellation_keeps_cleanup_evidence(launch):
    evidence = launch.cleanup_error("not empty")
    cancelled = asyncio.CancelledError()
    cancelled.__cause__ = evidence
    outer = RuntimeError("parallel failed")
    outer.__cause__ = BaseExceptionGroup("failures", [ValueError("other"), cancelled])
    evidence.__context__ = outer
    assert unconfirmed_process_cleanup(outer) is evidence
    assert unconfirmed_process_cleanup(ValueError("normal")) is None
