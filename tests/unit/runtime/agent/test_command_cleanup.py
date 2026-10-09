import asyncio
import io
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from crewplane.architecture.contracts import InvocationContext
from crewplane.runtime.agent.invocation import command
from crewplane.runtime.agent.process import drain
from crewplane.runtime.agent.process.stream_capture import (
    ProcessOutputCapture,
    ProcessStreamCapture,
)


@pytest.mark.parametrize("stage", ["terminate", "pipes", "windows"])
@pytest.mark.parametrize("drain_fails", [False, True])
@pytest.mark.parametrize("original_cancel", [False, True])
@pytest.mark.parametrize("repeated_cancel", [False, True], ids=["single", "repeated"])
def test_process_cleanup_finishes_before_cancellation_and_persistence(
    tmp_path, monkeypatch, stage, drain_fails, original_cancel, repeated_cancel
):
    order = []
    log = io.BytesIO()
    failure = (
        asyncio.CancelledError("collection cancelled")
        if original_cancel
        else RuntimeError("collection failed")
    )
    process = SimpleNamespace(pid=42, returncode=None)
    drain_error = drain.ProcessDrainError(
        drain.ProcessDrainEvidence(42, None, True, False), "descendant remained live"
    )
    ready, release = asyncio.Event(), asyncio.Event()

    def terminate():
        order.append("TERM")
        if stage == "terminate":
            ready.set()

    def kill():
        order.append("KILL")
        process.returncode = -9

    process.terminate, process.kill = terminate, kill
    monkeypatch.setattr(command, "supports_posix_process_groups", lambda: False)
    monkeypatch.setattr(
        command.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)
    )
    monkeypatch.setattr(command, "open_log_handle", Mock(return_value=log))
    monkeypatch.setattr(
        command, "write_stdin_and_collect_output", AsyncMock(side_effect=failure)
    )

    def confirmed(*args):
        assert args[1:] == (42, None)
        assert process.returncode == -9
        order.append("confirmed")

    def unresolved(state_path, error):
        assert state_path is None and error is drain_error
        order.append("unresolved")

    monkeypatch.setattr(command, "confirm_workspace_process_drain", confirmed)
    monkeypatch.setattr(
        command, "record_unresolved_workspace_process_drain", unresolved
    )

    def receipt(event):
        if event.status == "exited":
            assert log.closed and process.returncode == -9
        order.append(event.status)

    context = InvocationContext(
        "node", "task", "provider", "executor", process_event_sink=receipt
    )

    async def check():
        async def pause():
            ready.set()
            await release.wait()

        async def pipes(target, diagnostics, group_id):
            assert target is process and diagnostics is None and group_id is None
            order.append("pipes")
            if stage == "pipes":
                await pause()
            if drain_fails:
                raise drain_error

        async def windows_drain():
            order.append("windows-drain")
            await pause()
            process.returncode = -9
            if drain_fails:
                raise drain_error

        monkeypatch.setattr(command, "drain_process_pipes", pipes)
        monkeypatch.setattr(
            command,
            "sys",
            SimpleNamespace(
                platform="win32" if stage == "windows" else "linux",
                exception=sys.exception,
            ),
        )
        if stage == "windows":
            launch = Mock(
                process=process,
                assigned=True,
                start=AsyncMock(return_value=process),
                collect=AsyncMock(side_effect=failure),
                drain=windows_drain,
                cleanup_error=Mock(return_value=drain_error),
                close=Mock(side_effect=lambda: order.append("job-close")),
            )
            monkeypatch.setattr(command, "WindowsLaunch", Mock(return_value=launch))
        task = asyncio.create_task(
            command.run_command_once(
                ["provider"],
                None,
                tmp_path / "log",
                False,
                None,
                tmp_path,
                context,
                None,
            )
        )
        try:
            await asyncio.wait_for(ready.wait(), 1)
            if repeated_cancel:
                task.cancel("cleanup cancelled")
                await asyncio.sleep(0)
                task.cancel("cleanup cancelled again")
                await asyncio.sleep(0)
                assert not task.done()
            release.set()
            expected = (
                asyncio.CancelledError
                if original_cancel or repeated_cancel
                else drain.ProcessDrainError
                if drain_fails
                else RuntimeError
            )
            with pytest.raises(expected) as caught:
                await task
            if original_cancel:
                assert caught.value is failure
            elif repeated_cancel:
                assert caught.value.args == ("cleanup cancelled",)
            elif drain_fails:
                assert caught.value is drain_error
                if stage != "windows":
                    assert caught.value.__cause__ is failure
            else:
                assert caught.value is failure
            if drain_fails and (original_cancel or repeated_cancel):
                assert caught.value.__cause__ is drain_error
            expected_order = ["started"]
            if stage == "windows":
                expected_order.append("windows-drain")
                if not drain_fails:
                    expected_order.append("pipes")
            else:
                expected_order.extend(["TERM", "KILL", "pipes"])
            expected_order.append("unresolved" if drain_fails else "confirmed")
            if stage == "windows":
                expected_order.append("job-close")
            if not drain_fails:
                expected_order.append("exited")
            assert order == expected_order
            assert log.closed and process.returncode == -9
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            log.close()

    asyncio.run(check())


@pytest.mark.parametrize("cancelled", [False, True])
def test_windows_drain_failure_deletes_owned_captures_before_finalization(
    tmp_path, monkeypatch, cancelled
):
    stdout, stderr = tmp_path / "stdout", tmp_path / "stderr"
    stdout.write_bytes(b"answer")
    stderr.write_bytes(b"diagnostic")
    capture = ProcessOutputCapture(
        ProcessStreamCapture(stdout, b"answer"),
        ProcessStreamCapture(stderr, b"diagnostic"),
    )
    failure = (
        asyncio.CancelledError("persistence cancelled")
        if cancelled
        else RuntimeError("persistence failed")
    )
    process = SimpleNamespace(pid=42, returncode=0)
    drain_error = drain.ProcessDrainError(
        drain.ProcessDrainEvidence(42, None, True, False), "descendant remained live"
    )
    log = io.BytesIO()
    captures_at_close = []

    def close_job():
        assert log.closed
        captures_at_close.append((stdout.exists(), stderr.exists()))

    launch = Mock(
        process=process,
        assigned=True,
        start=AsyncMock(return_value=process),
        collect=AsyncMock(return_value=capture),
        drain=AsyncMock(side_effect=drain_error),
        cleanup_error=Mock(return_value=drain_error),
        close=close_job,
    )
    monkeypatch.setattr(command, "WindowsLaunch", Mock(return_value=launch))
    monkeypatch.setattr(
        command, "sys", SimpleNamespace(platform="win32", exception=sys.exception)
    )
    monkeypatch.setattr(command, "supports_posix_process_groups", lambda: False)
    monkeypatch.setattr(command, "open_log_handle", Mock(return_value=log))
    monkeypatch.setattr(
        command, "confirm_workspace_process_drain", Mock(side_effect=failure)
    )
    unresolved = Mock()
    monkeypatch.setattr(
        command, "record_unresolved_workspace_process_drain", unresolved
    )
    reaper, pipes = AsyncMock(), AsyncMock()
    monkeypatch.setattr(command, "reap_failed_process", reaper)
    monkeypatch.setattr(command, "drain_process_pipes", pipes)

    async def check():
        with pytest.raises(
            asyncio.CancelledError if cancelled else drain.ProcessDrainError
        ) as caught:
            await command.run_command_once(
                ["provider"], None, tmp_path / "log", False, None, tmp_path, None, None
            )
        if cancelled:
            assert caught.value is failure and caught.value.__cause__ is drain_error
        else:
            assert caught.value is drain_error and caught.value.__cause__ is failure
        assert not stdout.exists() and not stderr.exists()
        assert captures_at_close == [(False, False)]
        unresolved.assert_called_once_with(None, drain_error)
        reaper.assert_not_awaited()
        pipes.assert_not_awaited()

    try:
        asyncio.run(check())
    finally:
        capture.cleanup()
        log.close()
