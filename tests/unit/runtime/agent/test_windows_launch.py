"""Characterize gated startup, collection ordering, and drain ownership."""

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from crewplane.runtime.agent.process import streams, windows_launch
from crewplane.runtime.agent.process.drain import (
    ProcessDrainError,
    unconfirmed_process_cleanup,
)
from crewplane.runtime.agent.process.stream_capture import (
    ProcessOutputCapture,
    ProcessStreamCapture,
)


@pytest.fixture
def launch(monkeypatch):
    job = Mock()
    job.active_process_count.return_value = 0
    monkeypatch.setattr(windows_launch, "WindowsJob", Mock(return_value=job))
    launch = windows_launch.WindowsLaunch()
    launch.job = job
    return launch


@pytest.fixture
def capture(tmp_path):
    streams = []
    for name in ("stdout", "stderr"):
        path = tmp_path / name
        path.write_bytes(name.encode())
        streams.append(ProcessStreamCapture(path, name.encode()))
    return ProcessOutputCapture(*streams)


@pytest.mark.parametrize("spawn_failure", [False, True])
def test_repeated_start_cancellation_retains_spawn_outcome(
    tmp_path, monkeypatch, launch, spawn_failure
):
    process = SimpleNamespace(pid=42, stdin=Mock(), returncode=0)
    failure = OSError("spawn failed")

    async def check():
        started, finish = asyncio.Event(), asyncio.Event()

        async def spawn(*args, **kwargs):
            assert args and kwargs
            started.set()
            await finish.wait()
            if spawn_failure:
                raise failure
            return process

        monkeypatch.setattr(windows_launch.asyncio, "create_subprocess_exec", spawn)
        task = asyncio.create_task(launch.start(["provider.exe"], tmp_path, None))
        await started.wait()
        task.cancel("first")
        await asyncio.sleep(0)
        task.cancel("second")
        await asyncio.sleep(0)
        assert not task.done()
        finish.set()
        expected = OSError if spawn_failure else asyncio.CancelledError
        with pytest.raises(expected) as error:
            await task
        if spawn_failure:
            assert error.value is failure
            assert launch.process is None
        else:
            assert error.value.args == ("first",)
            assert launch.process is process

    asyncio.run(check())
    launch.job.assign.assert_not_called()
    assert not launch.assigned
    process.stdin.write.assert_not_called()


@pytest.mark.parametrize("first", ["monitor", "output"])
@pytest.mark.parametrize("stdin_data", [None, b"prompt"])
def test_collect_waits_for_both_tasks_and_transfers_capture(
    monkeypatch, launch, first, stdin_data
):
    launch.process = SimpleNamespace(pid=42, stdin=Mock(), returncode=0)
    capture = Mock(spec=ProcessOutputCapture)
    log_handle, diagnostics = Mock(), Mock()

    async def check():
        ready = {name: asyncio.Event() for name in ("monitor", "output")}
        finish = {name: asyncio.Event() for name in ready}
        finished = {name: asyncio.Event() for name in ready}

        async def monitor():
            ready["monitor"].set()
            await finish["monitor"].wait()
            finished["monitor"].set()

        async def collect(*args):
            assert args == (
                launch.process,
                stdin_data or b"",
                log_handle,
                diagnostics,
                None,
                1.25,
            )
            ready["output"].set()
            await finish["output"].wait()
            finished["output"].set()
            return capture

        monkeypatch.setattr(launch, "_drain_after_leader_exit", monitor)
        monkeypatch.setattr(windows_launch, "write_stdin_and_collect_output", collect)
        task = asyncio.create_task(
            launch.collect(stdin_data, log_handle, diagnostics, 1.25)
        )
        await asyncio.gather(*(event.wait() for event in ready.values()))
        finish[first].set()
        await finished[first].wait()
        await asyncio.sleep(0)
        assert not task.done()
        assert not launch.cleanup_confirmed
        finish["output" if first == "monitor" else "monitor"].set()
        assert await task is capture

    asyncio.run(check())
    assert launch.cleanup_confirmed
    capture.cleanup.assert_not_called()


def test_collect_simultaneous_failures_prioritize_monitor(monkeypatch, launch):
    launch.process = SimpleNamespace(pid=42, stdin=Mock(), returncode=0)
    monitor_error = launch.cleanup_error("membership query failed")
    monkeypatch.setattr(
        launch, "_drain_after_leader_exit", AsyncMock(side_effect=monitor_error)
    )
    monkeypatch.setattr(
        windows_launch,
        "write_stdin_and_collect_output",
        AsyncMock(side_effect=ValueError("capture failed")),
    )
    with pytest.raises(ProcessDrainError) as error:
        asyncio.run(launch.collect(None, None, None, None))
    assert error.value is monitor_error
    assert not launch.cleanup_confirmed


@pytest.mark.parametrize("failed_task", ["monitor", "output"])
def test_collect_failure_settles_pending_task(monkeypatch, launch, failed_task):
    launch.process = SimpleNamespace(pid=42, stdin=Mock(), returncode=0)
    failure = ValueError("collection failed")

    async def check():
        ready, settled = asyncio.Event(), asyncio.Event()

        async def fail(*args):
            assert args or failed_task == "monitor"
            await ready.wait()
            raise failure

        async def pending(*args):
            assert args or failed_task == "output"
            ready.set()
            try:
                await asyncio.Event().wait()
            finally:
                settled.set()

        monkeypatch.setattr(
            launch,
            "_drain_after_leader_exit",
            fail if failed_task == "monitor" else pending,
        )
        monkeypatch.setattr(
            windows_launch,
            "write_stdin_and_collect_output",
            fail if failed_task == "output" else pending,
        )
        with pytest.raises(ValueError) as error:
            await launch.collect(None, None, None, None)
        assert error.value is failure
        assert settled.is_set()

    asyncio.run(check())
    assert not launch.cleanup_confirmed


def test_collect_without_start_fails_explicitly(launch):
    with pytest.raises(RuntimeError, match="Windows provider helper has not started"):
        asyncio.run(launch.collect(None, None, None, None))


def test_successful_collect_in_exception_handler_transfers_capture(
    monkeypatch, launch, capture
):
    launch.process = SimpleNamespace(pid=42, stdin=Mock(), returncode=0)
    monkeypatch.setattr(launch, "_drain_after_leader_exit", AsyncMock())
    monkeypatch.setattr(
        windows_launch,
        "write_stdin_and_collect_output",
        AsyncMock(return_value=capture),
    )

    async def check():
        try:
            raise ValueError("previous invocation failed")
        except ValueError:
            assert await launch.collect(None, None, None, None) is capture
        assert capture.stdout.path.exists()
        assert capture.stderr.path.exists()
        capture.cleanup()

    asyncio.run(check())


def test_drain_and_close_have_distinct_obligations(launch):
    asyncio.run(launch.drain())
    assert launch.cleanup_confirmed
    launch.job.active_process_count.assert_not_called()
    launch.job.close.assert_not_called()
    launch.cleanup_confirmed = False
    launch.close()
    launch.job.close.assert_called_once()
    assert not launch.cleanup_confirmed


def test_drain_preserves_phase_and_poll_order(monkeypatch, launch):
    events = []
    process = SimpleNamespace(pid=42, returncode=None)
    process.stdin = Mock()
    process.stdin.close.side_effect = lambda: events.append("close")
    process.kill = Mock(side_effect=lambda: events.append("kill"))
    launch.process = process
    counts = iter((1, 1, 1, 0))

    def membership():
        events.append("query")
        return next(counts)

    async def poll(delay):
        assert delay == 0.01
        events.append("sleep")
        if events.count("sleep") == 2:
            process.returncode = 1

    times = iter((0, 0, 1, 1, 1, 1, 1))
    monkeypatch.setattr(windows_launch, "PROCESS_GROUP_TERM_GRACE_SECONDS", 0.5)
    monkeypatch.setattr(windows_launch, "PROCESS_GROUP_KILL_GRACE_SECONDS", 0.5)
    monkeypatch.setattr(windows_launch.asyncio, "sleep", poll)
    launch.job.active_process_count.side_effect = membership
    launch.job.terminate.side_effect = lambda: events.append("terminate")

    async def check():
        monkeypatch.setattr(asyncio.get_running_loop(), "time", times.__next__)
        try:
            await launch.drain()
        finally:
            monkeypatch.undo()

    asyncio.run(check())
    assert launch.cleanup_confirmed
    assert events == [
        "close",
        "query",
        "terminate",
        "kill",
        "sleep",
        "query",
        "terminate",
        "kill",
        "sleep",
        "query",
        "sleep",
        "query",
    ]


def test_drain_stdin_close_error_is_not_wrapped(launch):
    failure = OSError("stdin close failed")
    launch.process = SimpleNamespace(pid=42, stdin=Mock(), returncode=0)
    launch.process.stdin.close.side_effect = failure
    with pytest.raises(OSError) as error:
        asyncio.run(launch.drain())
    assert error.value is failure
    assert not launch.cleanup_confirmed
    launch.job.active_process_count.assert_not_called()


@pytest.mark.parametrize("completed", [False, True])
def test_repeated_collect_cancellation_reclaims_capture_after_settlement(
    monkeypatch, launch, capture, completed
):
    launch.process = SimpleNamespace(pid=42, stdin=Mock(), returncode=0)
    settle_tasks = windows_launch.cancel_pending_stream_tasks

    async def check():
        output_ready, settling, finish = (
            asyncio.Event(),
            asyncio.Event(),
            asyncio.Event(),
        )

        async def monitor():
            if not completed:
                await asyncio.Event().wait()

        async def collect(*args):
            assert args
            output_ready.set()
            return capture

        async def settle(*tasks):
            settling.set()
            await finish.wait()
            await settle_tasks(*tasks)

        monkeypatch.setattr(launch, "_drain_after_leader_exit", monitor)
        monkeypatch.setattr(windows_launch, "write_stdin_and_collect_output", collect)
        monkeypatch.setattr(windows_launch, "cancel_pending_stream_tasks", settle)
        task = asyncio.create_task(launch.collect(None, None, None, None))
        try:
            await output_ready.wait()
            if not completed:
                task.cancel("first")
            await settling.wait()
            task.cancel("first" if completed else "second")
            await asyncio.sleep(0)
            if completed:
                task.cancel("second")
                await asyncio.sleep(0)
            assert not task.done()
        finally:
            finish.set()
            outcome = (await asyncio.gather(task, return_exceptions=True))[0]
        assert isinstance(outcome, asyncio.CancelledError)
        with pytest.raises(asyncio.CancelledError) as error:
            task.result()
        assert error.value.args == ("first",)

    asyncio.run(check())
    assert not capture.stdout.path.exists()
    assert not capture.stderr.path.exists()
    assert launch.cleanup_confirmed is completed


@pytest.mark.parametrize("settlement_failure", [False, True])
def test_capture_deletion_runs_off_loop_and_finishes_before_cancellation(
    monkeypatch, launch, capture, settlement_failure
):
    launch.process = SimpleNamespace(pid=42, stdin=Mock(), returncode=0)
    failure = launch.cleanup_error("membership query failed")
    monkeypatch.setattr(
        launch, "_drain_after_leader_exit", AsyncMock(side_effect=failure)
    )
    monkeypatch.setattr(
        windows_launch,
        "write_stdin_and_collect_output",
        AsyncMock(return_value=capture),
    )
    if settlement_failure:
        failure = launch.cleanup_error("stream settlement deadline expired")
        monkeypatch.setattr(
            windows_launch,
            "cancel_pending_stream_tasks",
            AsyncMock(side_effect=failure),
        )
    cleanup_capture = capture.cleanup
    finish = threading.Event()

    async def check():
        loop = asyncio.get_running_loop()
        event_loop_thread = threading.get_ident()
        started, cleaned = asyncio.Event(), asyncio.Event()

        def cleanup(owned_capture):
            assert owned_capture is capture
            assert threading.get_ident() != event_loop_thread
            loop.call_soon_threadsafe(started.set)
            assert finish.wait(5)
            cleanup_capture()
            loop.call_soon_threadsafe(cleaned.set)

        monkeypatch.setattr(ProcessOutputCapture, "cleanup", cleanup)
        task = asyncio.create_task(launch.collect(None, None, None, None))
        try:
            async with asyncio.timeout(5):
                await started.wait()
            task.cancel("first")
            await asyncio.sleep(0)
            task.cancel("second")
            await asyncio.sleep(0)
            assert not task.done()
            assert capture.stdout.path.exists()
        finally:
            finish.set()
            outcome = (await asyncio.gather(task, return_exceptions=True))[0]
        assert isinstance(outcome, asyncio.CancelledError)
        with pytest.raises(asyncio.CancelledError) as error:
            task.result()
        assert error.value.args == ("first",)
        assert unconfirmed_process_cleanup(error.value) is failure
        assert cleaned.is_set()

    asyncio.run(check())
    assert not capture.stdout.path.exists()
    assert not capture.stderr.path.exists()


@pytest.mark.parametrize("cancelled", [False, True])
def test_settlement_failure_reclaims_capture_and_keeps_error_precedence(
    monkeypatch, launch, capture, cancelled
):
    launch.process = SimpleNamespace(pid=42, stdin=Mock(), returncode=0)
    failure = launch.cleanup_error("stream settlement deadline expired")
    monkeypatch.setattr(
        launch,
        "_drain_after_leader_exit",
        AsyncMock(side_effect=launch.cleanup_error("membership query failed")),
    )
    monkeypatch.setattr(
        windows_launch,
        "write_stdin_and_collect_output",
        AsyncMock(return_value=capture),
    )

    async def check():
        settling, finish = asyncio.Event(), asyncio.Event()

        async def settle(*tasks):
            assert all(task.done() for task in tasks)
            settling.set()
            await finish.wait()
            raise failure

        monkeypatch.setattr(windows_launch, "cancel_pending_stream_tasks", settle)
        task = asyncio.create_task(launch.collect(None, None, None, None))
        try:
            await settling.wait()
            if cancelled:
                task.cancel("first")
                await asyncio.sleep(0)
                task.cancel("second")
                await asyncio.sleep(0)
                assert not task.done()
        finally:
            finish.set()
            await asyncio.gather(task, return_exceptions=True)
        expected = asyncio.CancelledError if cancelled else ProcessDrainError
        with pytest.raises(expected) as error:
            task.result()
        if cancelled:
            assert task.cancelled()
            assert error.value.args == ("first",)
            assert error.value.__cause__ is failure
            assert unconfirmed_process_cleanup(error.value) is failure
        else:
            assert error.value is failure

    asyncio.run(check())
    assert not capture.stdout.path.exists()
    assert not capture.stderr.path.exists()


def test_collect_cancellation_keeps_bounded_settlement_failure(monkeypatch, launch):
    launch.process = SimpleNamespace(pid=42, stdin=Mock(), returncode=0)
    monkeypatch.setattr(streams, "PROCESS_PIPE_DRAIN_GRACE_SECONDS", 0.1)

    async def check():
        ready, settling, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()
        monitor_task = None

        async def monitor():
            nonlocal monitor_task
            monitor_task = asyncio.current_task()
            ready.set()
            while not finish.is_set():
                try:
                    await finish.wait()
                except asyncio.CancelledError:
                    settling.set()

        async def collect(*args):
            assert args
            await ready.wait()
            raise ValueError("capture failed")

        monkeypatch.setattr(launch, "_drain_after_leader_exit", monitor)
        monkeypatch.setattr(windows_launch, "write_stdin_and_collect_output", collect)
        task = asyncio.create_task(launch.collect(None, None, None, None))
        try:
            async with asyncio.timeout(5):
                await settling.wait()
                task.cancel("first")
                await asyncio.sleep(0)
                task.cancel("second")
                with pytest.raises(asyncio.CancelledError) as error:
                    await task
            assert task.cancelled()
            assert error.value.args == ("first",)
            failure = unconfirmed_process_cleanup(error.value)
            assert error.value.__cause__ is failure
            assert isinstance(failure, ProcessDrainError)
            assert "finite drain deadline" in str(failure)
            assert not failure.evidence.leader_stopped
            assert not failure.evidence.process_group_stopped
        finally:
            finish.set()
            assert monitor_task is not None
            await asyncio.gather(task, monitor_task, return_exceptions=True)

    asyncio.run(check())
    assert not launch.cleanup_confirmed
