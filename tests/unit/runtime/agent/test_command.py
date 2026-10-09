import asyncio
import io
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from crewplane.architecture.contracts import InvocationContext
from crewplane.runtime.agent.invocation import command
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


@pytest.mark.parametrize("execution_failure", [None, "error", "cancel"])
@pytest.mark.parametrize("reporting_failure", [None, "close", "exit"])
def test_windows_finalization_preserves_order_ownership_and_error_precedence(
    tmp_path, monkeypatch, execution_failure, reporting_failure
):
    order = []
    stdout, stderr = tmp_path / "stdout", tmp_path / "stderr"
    stdout.write_bytes(b"answer\xff")
    stderr.write_bytes(b"diagnostic")
    capture = ProcessOutputCapture(
        ProcessStreamCapture(stdout, b"answer\xff"),
        ProcessStreamCapture(stderr, b"diagnostic"),
    )
    process = SimpleNamespace(pid=42, returncode=0)
    failure = {
        None: None,
        "error": RuntimeError("collection failed"),
        "cancel": asyncio.CancelledError("cancelled"),
    }[execution_failure]

    class Log(io.BytesIO):
        def close(self):
            order.append("log-close")
            super().close()

    log = Log()

    def close_job():
        order.append("job-close")
        if reporting_failure == "close":
            raise OSError("job close failed")

    def cleanup_error(reason):
        return ProcessDrainError(ProcessDrainEvidence(42, None, True, False), reason)

    launch = Mock(
        process=process,
        assigned=True,
        start=AsyncMock(return_value=process),
        collect=AsyncMock(return_value=capture, side_effect=failure),
        drain=AsyncMock(),
        close=close_job,
        cleanup_error=cleanup_error,
    )

    def record_event(event):
        order.append(event.status)
        assert event.pid == 42
        if event.status == "exited":
            assert log.closed
            assert event.returncode == 0
            if reporting_failure == "exit":
                raise OSError("exit receipt failed")

    monkeypatch.setattr(
        session, "process_session", Mock(return_value=windows_session_stub(launch))
    )
    monkeypatch.setattr(
        session, "sys", SimpleNamespace(platform="win32", exception=sys.exception)
    )
    monkeypatch.setattr(posix_session, "supports_posix_process_groups", lambda: False)
    monkeypatch.setattr(command, "open_log_handle", Mock(return_value=log))
    monkeypatch.setattr(runner, "reap_failed_process", AsyncMock())
    monkeypatch.setattr(streams, "drain_process_pipes", AsyncMock())
    context = InvocationContext(
        "node", "task", "provider", "executor", process_event_sink=record_event
    )

    async def check():
        invocation = command.run_command_once(
            ["provider"], None, tmp_path / "log", False, None, tmp_path, context, None
        )
        if failure is None and reporting_failure is None:
            result = await invocation
            assert result.returncode == 0
            assert result.stdout_text == "answer\ufffd"
            assert result.stderr_text == "diagnostic"
            assert (result.stdout_path, result.stderr_path) == (stdout, stderr)
            assert stdout.exists() and stderr.exists()
            result.cleanup_stream_files()
            return
        expected = (
            asyncio.CancelledError
            if execution_failure == "cancel"
            else ProcessDrainError
            if reporting_failure
            else RuntimeError
        )
        with pytest.raises(expected) as caught:
            await invocation
        if execution_failure == "cancel":
            assert caught.value is failure
            if reporting_failure:
                assert isinstance(caught.value.__cause__, ProcessDrainError)
                assert (
                    "Provider process exit reporting failed"
                    in caught.value.__notes__[0]
                )
        elif reporting_failure:
            assert "Provider cleanup reporting failed" in str(caught.value)
            assert caught.value.__cause__ is not None
        else:
            assert caught.value is failure

    asyncio.run(check())
    assert order == ["started", "log-close", "job-close"] + (
        [] if reporting_failure == "close" else ["exited"]
    )
    if failure is None:
        assert not stdout.exists() and not stderr.exists()
    # A collector that raises retains ownership of captures it never returned.
    if failure is not None:
        capture.cleanup()


@pytest.mark.parametrize("append", [False, True])
def test_log_initialization_preserves_bytes_mode_and_caller_ownership(tmp_path, append):
    path = tmp_path / "nested" / "provider.log"
    path.parent.mkdir()
    path.write_bytes(b"old\r\n")
    handle = command.open_log_handle(path, append, b"header\x00\r\n")
    assert handle is not None and not handle.closed
    assert path.read_bytes() == (b"old\r\n" if append else b"") + b"header\x00\r\n"
    handle.write(b"output")
    handle.close()
    assert path.read_bytes().endswith(b"header\x00\r\noutput")


def test_structured_output_preparation_preserves_deletion_errors(tmp_path):
    command.prepare_structured_output_file(None)
    missing = tmp_path / "missing"
    command.prepare_structured_output_file(missing)
    missing.mkdir()
    with pytest.raises(OSError):
        command.prepare_structured_output_file(missing)


@pytest.mark.parametrize("execution_failure", [None, "error", "cancel"])
@pytest.mark.parametrize("reporting_failure", [False, True])
def test_posix_finalization_preserves_close_policy_and_error_precedence(
    tmp_path, monkeypatch, execution_failure, reporting_failure
):
    order = []
    stdout, stderr = tmp_path / "stdout", tmp_path / "stderr"
    stdout.write_bytes(b"answer")
    stderr.write_bytes(b"")
    capture = ProcessOutputCapture(
        ProcessStreamCapture(stdout, b"answer"), ProcessStreamCapture(stderr, b"")
    )
    failure = {
        None: None,
        "error": RuntimeError("collection failed"),
        "cancel": asyncio.CancelledError("cancelled"),
    }[execution_failure]

    class Log(io.BytesIO):
        def close(self):
            order.append("log-close")
            super().close()
            raise OSError("close failed")

    log = Log()

    def receipt(event):
        order.append(event.status)
        if event.status == "exited":
            assert log.closed
            if reporting_failure:
                raise OSError("exit receipt failed")

    monkeypatch.setattr(
        session, "sys", SimpleNamespace(platform="linux", exception=sys.exception)
    )
    monkeypatch.setattr(posix_session, "supports_posix_process_groups", lambda: False)
    monkeypatch.setattr(
        command.asyncio,
        "create_subprocess_exec",
        AsyncMock(return_value=SimpleNamespace(pid=42, returncode=0)),
    )
    monkeypatch.setattr(command, "open_log_handle", Mock(return_value=log))
    monkeypatch.setattr(
        runner,
        "write_stdin_and_collect_output",
        AsyncMock(return_value=capture, side_effect=failure),
    )
    monkeypatch.setattr(runner, "reap_failed_process", AsyncMock())
    monkeypatch.setattr(streams, "drain_process_pipes", AsyncMock())
    context = InvocationContext(
        "node", "task", "provider", "executor", process_event_sink=receipt
    )

    async def check():
        invocation = command.run_command_once(
            ["provider"], None, tmp_path / "log", False, None, tmp_path, context, None
        )
        if failure is None and not reporting_failure:
            result = await invocation
            assert result.returncode == 0 and result.stdout_text == "answer"
            assert stdout.exists() and stderr.exists()
            result.cleanup_stream_files()
        else:
            expected = (
                asyncio.CancelledError
                if execution_failure == "cancel"
                else RuntimeError
            )
            with pytest.raises(expected) as caught:
                await invocation
            if failure is not None:
                assert caught.value is failure
                if reporting_failure:
                    assert "exit reporting failed" in caught.value.__notes__[0]
            else:
                assert "process exited reporting failed" in str(caught.value)
                assert not stdout.exists() and not stderr.exists()

    try:
        asyncio.run(check())
        assert order == ["started", "log-close", "exited"]
    finally:
        capture.cleanup()
