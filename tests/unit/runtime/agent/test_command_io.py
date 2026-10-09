import asyncio
import io
import sys
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from crewplane.architecture.contracts import (
    ChildProcessEnvironment,
    InvocationContext,
    InvocationWorkspaceContext,
)
from crewplane.runtime.agent.invocation import command, command_lifecycle
from crewplane.runtime.agent.process import posix_session, runner, session, streams
from crewplane.runtime.agent.process.drain import (
    ProcessDrainError,
    ProcessDrainEvidence,
)
from crewplane.runtime.agent.process.stream_capture import (
    ProcessOutputCapture,
    ProcessStreamCapture,
)
from tests.helpers.process_sessions import windows_session_stub


@pytest.mark.parametrize("operation", ["write", "flush"])
@pytest.mark.parametrize("close_fails", [False, True])
def test_log_initialization_closes_on_failure_without_replacing_exception(
    tmp_path, monkeypatch, operation, close_fails
):
    failure = OSError(f"header {operation} failed")
    handle = Mock()
    getattr(handle, operation).side_effect = failure
    if close_fails:
        handle.close.side_effect = OSError("close failed")
    monkeypatch.setattr(Path, "open", Mock(return_value=handle))
    with pytest.raises(OSError) as caught:
        command.open_log_handle(tmp_path / "log", False, b"header")
    assert caught.value is failure
    handle.close.assert_called_once_with()


@pytest.mark.parametrize("stage", ["log", "confirmed", "unresolved", "cancelled"])
@pytest.mark.parametrize("cancelled", [False, True])
def test_command_io_keeps_loop_responsive_and_finishes_before_exit_reporting(
    tmp_path, monkeypatch, stage, cancelled
):
    order = []
    main_thread = threading.get_ident()
    release = threading.Event()
    log = io.BytesIO()
    process = SimpleNamespace(pid=42, returncode=0)
    stdout, stderr = tmp_path / "stdout", tmp_path / "stderr"
    stdout.write_bytes(b"answer")
    stderr.write_bytes(b"")
    capture = ProcessOutputCapture(
        ProcessStreamCapture(stdout, b"answer"), ProcessStreamCapture(stderr, b"")
    )
    drain_error = ProcessDrainError(
        ProcessDrainEvidence(42, None, True, False), "descendant remained live"
    )
    original_cancellation = asyncio.CancelledError("collection cancelled")
    monkeypatch.setattr(
        session, "sys", SimpleNamespace(platform="linux", exception=sys.exception)
    )
    monkeypatch.setattr(
        command.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)
    )
    monkeypatch.setattr(posix_session, "supports_posix_process_groups", lambda: False)
    monkeypatch.setattr(
        runner,
        "write_stdin_and_collect_output",
        AsyncMock(
            return_value=capture,
            side_effect=RuntimeError("collection failed")
            if stage == "unresolved"
            else original_cancellation
            if stage == "cancelled"
            else None,
        ),
    )
    monkeypatch.setattr(
        runner,
        "reap_failed_process",
        AsyncMock(side_effect=drain_error if stage == "unresolved" else None),
    )
    monkeypatch.setattr(streams, "drain_process_pipes", AsyncMock())

    async def check():
        started = asyncio.Event()
        loop = asyncio.get_running_loop()
        blocked = False

        def blocking_io():
            nonlocal blocked
            if blocked:
                return
            blocked = True
            loop.call_soon_threadsafe(started.set)
            assert threading.get_ident() != main_thread
            assert release.wait(5), "test did not release blocked storage"
            order.append("persisted")

        def open_log(*args, **kwargs):
            assert args or kwargs
            if stage == "log":
                blocking_io()
            return log

        def confirm(state_path, pid, group_id):
            assert state_path == tmp_path / "state" and pid == 42 and group_id is None
            if stage in {"confirmed", "cancelled"}:
                blocking_io()

        def unresolved(state_path, error):
            assert state_path == tmp_path / "state" and error is drain_error
            blocking_io()

        def receipt(event):
            assert threading.get_ident() == main_thread
            if event.status == "exited":
                assert log.closed and "persisted" in order
            order.append(event.status)

        def environment_applied():
            assert threading.get_ident() == main_thread
            order.append("environment")

        workspace = Mock(spec=InvocationWorkspaceContext)
        workspace.workspace_state_path = tmp_path / "state"
        workspace.child_environment_required = True
        context = replace(
            InvocationContext("node", "task", "provider", "executor"),
            workspace=workspace,
            process_event_sink=receipt,
            workspace_environment_applied_recorder=environment_applied,
        )
        monkeypatch.setattr(command, "open_log_handle", open_log)
        monkeypatch.setattr(command, "confirm_workspace_process_drain", confirm)
        monkeypatch.setattr(
            command, "record_unresolved_workspace_process_drain", unresolved
        )
        task = asyncio.create_task(
            command.run_command_once(
                ["provider"],
                None,
                tmp_path / "log",
                False,
                b"header",
                tmp_path,
                context,
                None,
                ChildProcessEnvironment({}, ()),
            )
        )
        try:
            await asyncio.wait_for(started.wait(), 1)
            if cancelled:
                task.cancel("first cancellation")
                await asyncio.sleep(0)
                task.cancel("second cancellation")
                await asyncio.sleep(0)
            assert not task.done()
            assert not log.closed
            assert order == ["environment", "started"]
            release.set()
            if cancelled or stage == "cancelled":
                with pytest.raises(asyncio.CancelledError) as caught:
                    await task
                if stage == "cancelled":
                    assert caught.value is original_cancellation
                else:
                    assert caught.value.args == ("first cancellation",)
                if stage == "unresolved":
                    assert caught.value.__cause__ is drain_error
            elif stage == "unresolved":
                with pytest.raises(ProcessDrainError) as caught:
                    await task
                assert caught.value is drain_error
                assert str(caught.value.__cause__) == "collection failed"
            else:
                result = await task
                assert result.returncode == 0 and result.stdout_text == "answer"
                assert result.stdout_path == stdout and stdout.exists()
                result.cleanup_stream_files()
            assert order == ["environment", "started", "persisted"] + (
                [] if stage == "unresolved" else ["exited"]
            )
            assert log.closed
            if cancelled and stage == "confirmed":
                assert not stdout.exists() and not stderr.exists()
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            capture.cleanup()
            log.close()

    asyncio.run(check())


@pytest.fixture
def command_case(tmp_path, monkeypatch):
    stdout, stderr = tmp_path / "stdout", tmp_path / "stderr"
    stdout.write_bytes(b"answer")
    stderr.write_bytes(b"diagnostic")
    capture = ProcessOutputCapture(
        ProcessStreamCapture(stdout, b"answer"),
        ProcessStreamCapture(stderr, b"diagnostic"),
    )
    log = io.BytesIO()
    order = []
    main_thread = threading.get_ident()

    def receipt(event):
        assert threading.get_ident() == main_thread
        order.append(event.status)

    context = InvocationContext(
        "node", "task", "provider", "executor", process_event_sink=receipt
    )
    process = SimpleNamespace(pid=42, returncode=0)
    monkeypatch.setattr(
        session, "sys", SimpleNamespace(platform="linux", exception=sys.exception)
    )
    monkeypatch.setattr(posix_session, "supports_posix_process_groups", lambda: False)
    monkeypatch.setattr(
        command.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)
    )
    monkeypatch.setattr(command, "open_log_handle", Mock(return_value=log))
    monkeypatch.setattr(
        runner, "write_stdin_and_collect_output", AsyncMock(return_value=capture)
    )
    monkeypatch.setattr(runner, "reap_failed_process", AsyncMock())
    monkeypatch.setattr(streams, "drain_process_pipes", AsyncMock())
    monkeypatch.setattr(command, "confirm_workspace_process_drain", Mock())
    monkeypatch.setattr(command, "record_unresolved_workspace_process_drain", Mock())
    try:
        yield SimpleNamespace(
            capture=capture, log=log, context=context, process=process, order=order
        )
    finally:
        capture.cleanup()
        log.close()


@pytest.mark.parametrize("stage", ["log", "confirmed", "failed", "original_cancel"])
@pytest.mark.parametrize("cancelled", [False, True], ids=["uncancelled", "cancelled"])
def test_threaded_io_failure_preserves_error_or_cancellation(
    tmp_path, monkeypatch, command_case, stage, cancelled
):
    case = command_case
    failure = OSError("storage failed")
    original_cancel = asyncio.CancelledError("collection cancelled")
    if stage in {"failed", "original_cancel"}:
        runner.write_stdin_and_collect_output.side_effect = (
            RuntimeError("collection failed") if stage == "failed" else original_cancel
        )

    async def check():
        started = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()
        calls = 0

        def fail_once(*args, **kwargs):
            nonlocal calls
            assert args or kwargs
            calls += 1
            if calls == 1:
                loop.call_soon_threadsafe(started.set)
                assert release.wait(5), "test did not release blocked storage"
                raise failure
            return case.log if stage == "log" else None

        monkeypatch.setattr(
            command,
            "open_log_handle" if stage == "log" else "confirm_workspace_process_drain",
            fail_once,
        )
        task = asyncio.create_task(
            command.run_command_once(
                ["provider"],
                None,
                tmp_path / "log",
                False,
                None,
                tmp_path,
                case.context,
                None,
            )
        )
        try:
            await asyncio.wait_for(started.wait(), 1)
            if cancelled:
                task.cancel("first cancellation")
                await asyncio.sleep(0)
                task.cancel("second cancellation")
                await asyncio.sleep(0)
            assert not task.done()
            release.set()
            expected = (
                asyncio.CancelledError
                if cancelled or stage == "original_cancel"
                else OSError
                if stage == "failed"
                else RuntimeError
            )
            with pytest.raises(expected) as caught:
                await task
            if stage == "original_cancel":
                assert caught.value is original_cancel
            elif cancelled:
                assert caught.value.args == ("first cancellation",)
            elif stage == "failed":
                assert caught.value is failure
            else:
                assert str(caught.value) == "Execution error: storage failed"
            if expected is not OSError:
                assert caught.value.__cause__ is failure
            assert case.order == ["started", "exited"]
            assert case.log.closed is (stage != "log")
            if stage == "confirmed":
                assert not case.capture.stdout.path.exists()
                assert not case.capture.stderr.path.exists()
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(check())


@pytest.mark.parametrize("windows", [False, True])
@pytest.mark.parametrize("cancelled", [False, True])
@pytest.mark.parametrize(
    "stage",
    [
        "log_close",
        "capture_error",
        "capture_cancel",
        "capture_unresolved",
        "capture_exit",
    ],
)
def test_filesystem_cleanup_keeps_loop_responsive_and_defers_cancellation(
    tmp_path, monkeypatch, command_case, stage, cancelled, windows
):
    case = command_case
    failure = RuntimeError("drain persistence failed")
    original_cancel = asyncio.CancelledError("persistence cancelled")
    drain_error = ProcessDrainError(
        ProcessDrainEvidence(42, None, True, False), "descendant remained live"
    )
    main_thread = threading.get_ident()
    if stage in {"capture_error", "capture_cancel", "capture_unresolved"}:
        command.confirm_workspace_process_drain.side_effect = [
            original_cancel if stage == "capture_cancel" else failure,
            None,
        ]
    if stage == "capture_unresolved":
        runner.reap_failed_process.side_effect = drain_error

    def close_job():
        assert threading.get_ident() == main_thread
        case.order.append("job-close")

    if windows:
        launch = Mock(
            process=case.process,
            assigned=True,
            start=AsyncMock(return_value=case.process),
            collect=AsyncMock(return_value=case.capture),
            drain=AsyncMock(),
            close=close_job,
            cleanup_error=Mock(return_value=drain_error),
        )
        monkeypatch.setattr(
            session, "process_session", Mock(return_value=windows_session_stub(launch))
        )
        monkeypatch.setattr(
            session, "sys", SimpleNamespace(platform="win32", exception=sys.exception)
        )

    def receipt(event):
        assert threading.get_ident() == main_thread
        if event.status == "exited":
            assert case.log.closed
        case.order.append(event.status)
        if event.status == "exited" and stage == "capture_exit":
            raise OSError("exit receipt failed")

    context = replace(case.context, process_event_sink=receipt)

    async def check():
        started = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()
        original_close = command_lifecycle.close_log_handle
        original_cleanup = ProcessOutputCapture.cleanup
        blocked = False

        def block_storage():
            nonlocal blocked
            if blocked:
                return
            blocked = True
            loop.call_soon_threadsafe(started.set)
            assert threading.get_ident() != main_thread
            assert release.wait(5), "test did not release blocked cleanup"

        def close_log(handle):
            if stage == "log_close":
                block_storage()
            original_close(handle)
            case.order.append("log-close")

        def cleanup(capture):
            if stage != "log_close":
                block_storage()
            original_cleanup(capture)
            case.order.append("capture-cleanup")

        monkeypatch.setattr(command_lifecycle, "close_log_handle", close_log)
        monkeypatch.setattr(ProcessOutputCapture, "cleanup", cleanup)
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
            await asyncio.wait_for(started.wait(), 1)
            if cancelled:
                task.cancel("first cancellation")
                await asyncio.sleep(0)
                task.cancel("second cancellation")
                await asyncio.sleep(0)
            assert not task.done()
            release.set()
            if stage == "log_close" and not cancelled:
                result = await task
                assert result.returncode == 0 and result.stdout_text == "answer"
                assert case.capture.stdout.path.exists()
                result.cleanup_stream_files()
            else:
                expected = (
                    asyncio.CancelledError
                    if cancelled or stage == "capture_cancel"
                    else ProcessDrainError
                    if stage == "capture_unresolved"
                    or (windows and stage == "capture_exit")
                    else RuntimeError
                )
                with pytest.raises(expected) as caught:
                    await task
                if stage == "capture_cancel":
                    assert caught.value is original_cancel
                elif cancelled:
                    assert caught.value.args == ("first cancellation",)
                elif stage == "capture_error":
                    assert caught.value is failure
                if stage == "capture_unresolved":
                    if cancelled:
                        assert caught.value.__cause__ is drain_error
                    else:
                        assert caught.value is drain_error
                        assert caught.value.__cause__ is failure
                if stage == "capture_exit" and (cancelled or windows):
                    if cancelled and windows:
                        assert caught.value.__cause__ is drain_error
                    else:
                        assert isinstance(caught.value.__cause__, RuntimeError)
                assert not case.capture.stdout.path.exists()
                assert not case.capture.stderr.path.exists()
            job_close = ["job-close"] if windows else []
            close_order = ["log-close", *job_close]
            expected_order = ["started"]
            if stage in {"capture_error", "capture_cancel", "capture_unresolved"}:
                expected_order.append("capture-cleanup")
            expected_order.extend(close_order)
            expected_order.append("exited")
            if stage == "capture_exit" or (stage == "log_close" and cancelled):
                expected_order.append("capture-cleanup")
            assert case.order == expected_order
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(check())
