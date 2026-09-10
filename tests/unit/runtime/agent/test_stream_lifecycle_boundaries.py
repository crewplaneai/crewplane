from __future__ import annotations

import asyncio
import io
import sys
from collections.abc import Callable
from dataclasses import replace
from threading import Event, Thread
from unittest.mock import patch

import pytest

from crewplane.runtime.agent.invocation import retry_reset
from crewplane.runtime.agent.process.stream_capture import CapturedStream
from crewplane.runtime.agent.process.streams import (
    ProcessActivity,
    capture_process_streams,
    close_log_handle,
    collect_process_output,
    drain_process_pipes,
    finish_stream_tasks_after_process_exit,
    pipe_stream,
    signal_log_queue_complete,
    watch_log_writer_status,
    watch_process_idle_timeout,
)
from tests.helpers.workspace_service import workspace_invocation_context


@pytest.mark.parametrize("capture_directly", [False, True])
def test_capture_rejects_process_without_redirected_streams(
    capture_directly: bool,
) -> None:
    async def scenario() -> None:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "pass",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await process.wait()
        stdout = CapturedStream()
        stderr = CapturedStream()
        try:
            capture = (
                capture_process_streams(process, None, stdout, stderr)
                if capture_directly
                else collect_process_output(process, None)
            )
            with pytest.raises(RuntimeError, match="Failed to capture process streams"):
                await capture
            await drain_process_pipes(process, None, None)
        finally:
            stdout.cleanup()
            stderr.cleanup()

    asyncio.run(scenario())


def test_stream_capture_flushes_incomplete_utf8_at_eof() -> None:
    async def scenario() -> None:
        reader = asyncio.StreamReader()
        reader.feed_data(b"\xe2")
        reader.feed_eof()
        queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        captured = CapturedStream()
        try:
            await pipe_stream(reader, queue, b"", captured)
            assert queue.get_nowait() == "�".encode()
            assert queue.empty()
            captured.close()
            assert captured.path.read_bytes() == b"\xe2"
        finally:
            captured.cleanup()

    asyncio.run(scenario())


def test_full_log_queue_is_released_when_writer_fails() -> None:
    async def scenario() -> None:
        queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=1)
        queue.put_nowait(b"pending")
        loop = asyncio.get_running_loop()
        status: asyncio.Future[Exception | None] = loop.create_future()
        completion = asyncio.create_task(signal_log_queue_complete(queue, status))
        loop.call_soon(status.set_result, OSError("log failed"))
        await asyncio.wait_for(completion, 1)
        assert queue.get_nowait() == b"pending"
        assert queue.empty()

    asyncio.run(scenario())


def test_completed_process_preserves_writer_error_and_drains_remaining_bytes() -> None:
    async def scenario() -> None:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "print('remaining output')",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await process.wait()
        await drain_process_pipes(process, None, None)
        await watch_process_idle_timeout(process, ProcessActivity(), 1, None, None)
        status: asyncio.Future[Exception | None] = (
            asyncio.get_running_loop().create_future()
        )
        error = OSError("log failed")
        status.set_result(error)
        with pytest.raises(OSError, match="log failed") as raised:
            await watch_log_writer_status(process, status)
        assert raised.value is error

    asyncio.run(scenario())


def test_finished_stream_failure_propagates_after_both_tasks_settle() -> None:
    async def scenario() -> None:
        failed = OSError("pipe read failed")

        async def broken_reader() -> None:
            raise failed

        async def finished_reader() -> None:
            return

        stdout = asyncio.create_task(broken_reader())
        stderr = asyncio.create_task(finished_reader())
        with pytest.raises(OSError, match="pipe read failed") as raised:
            await finish_stream_tasks_after_process_exit(stdout, stderr, None, None)
        assert raised.value is failed
        assert stdout.done()
        assert stderr.done()

    asyncio.run(scenario())


class FailingClose(io.BytesIO):
    def close(self) -> None:
        super().close()
        raise OSError("log close failed")


def test_log_close_failure_does_not_mask_invocation_failure() -> None:
    handle = FailingClose()
    close_log_handle(handle)
    assert handle.closed


@pytest.mark.parametrize("canceller_fails", [False, True], ids=["stuck", "failed"])
def test_retry_reset_cancellation_reports_stuck_or_failed_canceller(
    monkeypatch: pytest.MonkeyPatch, canceller_fails: bool
) -> None:
    workers: list[Thread] = []

    def create_worker(target: Callable[[], None], name: str, daemon: bool) -> Thread:
        worker = Thread(target=target, name=name, daemon=daemon)
        workers.append(worker)
        return worker

    monkeypatch.setattr(retry_reset, "Thread", create_worker)

    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        started = asyncio.Event()
        release = Event()

        def reset() -> None:
            loop.call_soon_threadsafe(started.set)
            release.wait()

        def cancel() -> None:
            if canceller_fails:
                release.set()
                raise OSError("cancel hook failed")
            release.wait()

        context = replace(
            workspace_invocation_context(),
            retry_reset=reset,
            retry_reset_canceller=cancel,
        )
        task = asyncio.create_task(retry_reset.reset_before_retry(context))
        try:
            await asyncio.wait_for(started.wait(), 5)
            deadline = 5 if canceller_fails else 0.01
            with patch.object(retry_reset, "RETRY_RESET_DEADLINE_SECONDS", deadline):
                task.cancel()
                with pytest.raises(asyncio.CancelledError) as raised:
                    await asyncio.wait_for(task, 10)
            notes = raised.value.__notes__
            if canceller_fails:
                assert any(
                    "cancellation failed: cancel hook failed" in note for note in notes
                )
            else:
                assert any("canceller did not stop" in note for note in notes)
                assert any("reset did not stop" in note for note in notes)
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)
            for worker in workers:
                await asyncio.to_thread(worker.join, 5)
            assert all(not worker.is_alive() for worker in workers)

    asyncio.run(scenario())
