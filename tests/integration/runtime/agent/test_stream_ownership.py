import asyncio
import io
import os
import sys
import traceback
from threading import Event
from unittest.mock import Mock

import pytest

from crewplane.runtime.agent.process import streams
from crewplane.runtime.agent.process.drain import process_group_is_alive
from crewplane.runtime.agent.process.stream_capture import CapturedStream
from tests.helpers.platforms import requires_posix
from tests.helpers.processes import kill_process_group


async def settle(task, timeout=5):
    done, _ = await asyncio.wait((task,), timeout=timeout)
    assert done, "owned task did not settle before independent watchdog"
    return task.result()


@pytest.mark.race
@pytest.mark.parametrize("stream", ["stdout", "stderr"])
@pytest.mark.parametrize("logging", [False, True])
@requires_posix
def test_capture_write_failure_drains_live_child_without_invocation_deadline(
    monkeypatch, stream, logging
):
    paths = []

    class FaultingCapture(CapturedStream):
        def __init__(self):
            super().__init__()
            paths.append(self.path)

        def write(self, payload):
            if payload.startswith(stream.encode()):
                raise OSError(f"{stream} storage failed")
            super().write(payload)

    monkeypatch.setattr(streams, "CapturedStream", FaultingCapture)

    async def check():
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import os,sys; os.write(1,b'stdout\\n'); os.write(2,b'stderr\\n'); sys.stdin.read()",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        unhandled = []

        def collect_exception(loop, context):
            del loop
            unhandled.append(context)

        asyncio.get_running_loop().set_exception_handler(collect_exception)
        task = asyncio.create_task(
            streams.collect_process_output(
                process, io.BytesIO() if logging else None, process_group_id=process.pid
            )
        )
        try:
            with pytest.raises(OSError, match=f"{stream} storage failed"):
                await settle(task)
            assert process.returncode is not None
            with pytest.raises(ProcessLookupError):
                os.killpg(process.pid, 0)
            assert len(paths) == 2 and all(not path.exists() for path in paths)
            assert unhandled == []
        finally:
            if process.returncode is None:
                process.kill()
            await asyncio.wait_for(process.wait(), 5)
            task.cancel()
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)

    asyncio.run(check())


@pytest.mark.race
@requires_posix
def test_exited_leader_descendant_drains_before_completed_capture_log_worker(
    tmp_path, monkeypatch
):
    async def check():
        loop = asyncio.get_running_loop()
        started, captured = asyncio.Event(), asyncio.Event()
        release = Event()
        capture_streams = streams.capture_process_streams

        async def record_capture_completion(*args, **kwargs):
            await capture_streams(*args, **kwargs)
            captured.set()

        class HeldLog(io.BytesIO):
            def write(self, payload):
                loop.call_soon_threadsafe(started.set)
                assert release.wait(10)
                assert not self.closed
                return super().write(payload)

        monkeypatch.setattr(
            streams, "capture_process_streams", record_capture_completion
        )
        child_pid_path = tmp_path / "descendant.pid"
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import subprocess, sys\n"
            "from pathlib import Path\n"
            "child = subprocess.Popen(\n"
            "    [sys.executable, '-c', 'import time; time.sleep(60)'],\n"
            "    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,\n"
            ")\n"
            "Path(sys.argv[1]).write_text(str(child.pid))\n"
            "print('leader exited')\n",
            str(child_pid_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        log = HeldLog()
        task = asyncio.create_task(
            streams.collect_process_output(process, log, process_group_id=process.pid)
        )
        try:
            await asyncio.wait_for(started.wait(), 5)
            await asyncio.wait_for(captured.wait(), 5)
            await asyncio.wait_for(process.wait(), 5)
            child_pid = int(child_pid_path.read_text())
            os.kill(child_pid, 0)
            assert process.returncode == 0
            assert process.stdout.at_eof() and process.stderr.at_eof()

            task.cancel("first")
            async with asyncio.timeout(5):
                while process_group_is_alive(process.pid):
                    await asyncio.sleep(0.001)
            with pytest.raises(ProcessLookupError):
                os.kill(child_pid, 0)
            assert not release.is_set() and not task.done() and not log.closed
            task.cancel("second")
            await asyncio.sleep(0)
            assert not task.done()

            release.set()
            with pytest.raises(asyncio.CancelledError):
                await settle(task)
        finally:
            release.set()
            kill_process_group(process.pid)
            await asyncio.wait_for(process.wait(), 5)
            task.cancel()
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)
            if not task.cancelled() and task.exception() is None:
                task.result().cleanup()
            log.close()

    asyncio.run(check())


@pytest.mark.race
def test_reader_eof_does_not_complete_a_live_invocation():
    async def check():
        # The Windows venv redirector retains pipe handles after its child closes them.
        process = await asyncio.create_subprocess_exec(
            vars(sys)["_base_executable"],
            "-c",
            "import os,sys; os.close(1); os.close(2); sys.stdin.read()",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        task = asyncio.create_task(streams.collect_process_output(process, None))
        try:
            async with asyncio.timeout(5):
                while not (process.stdout.at_eof() and process.stderr.at_eof()):
                    await asyncio.sleep(0.001)
            assert not task.done() and process.returncode is None
            process.stdin.close()
            capture = await settle(task)
            capture.cleanup()
        finally:
            if process.returncode is None:
                process.kill()
            await asyncio.wait_for(process.wait(), 5)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(check())


@pytest.mark.race
@pytest.mark.parametrize("trigger", ["cancel", "reader"])
@pytest.mark.parametrize("storage_failure", [None, "write", "flush"])
def test_live_child_drains_while_log_worker_retains_ownership(
    monkeypatch, trigger, storage_failure
):
    async def check():
        loop = asyncio.get_running_loop()
        started, failed_reader = asyncio.Event(), asyncio.Event()
        release = Event()
        paths = []

        class HeldLog(io.BytesIO):
            def write(self, payload):
                loop.call_soon_threadsafe(started.set)
                assert release.wait(10)
                assert not self.closed
                if storage_failure == "write":
                    raise OSError("held log write failed")
                return super().write(payload)

            def flush(self):
                if storage_failure == "flush":
                    raise OSError("held log flush failed")
                return super().flush()

        class FaultingCapture(CapturedStream):
            def __init__(self):
                super().__init__()
                paths.append(self.path)

            def write(self, payload):
                if payload.startswith(b"stderr"):
                    failed_reader.set()
                    raise OSError("sibling reader failed")
                super().write(payload)

        monkeypatch.setattr(streams, "CapturedStream", FaultingCapture)
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import os,sys; os.write(1,b'stdout\\n'); sys.stdin.buffer.read(1); os.write(2,b'stderr\\n'); sys.stdin.read()",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        log = HeldLog()
        task = asyncio.create_task(streams.collect_process_output(process, log))
        try:
            await asyncio.wait_for(started.wait(), 5)
            if trigger == "reader":
                process.stdin.write(b"x")
                await process.stdin.drain()
                await asyncio.wait_for(failed_reader.wait(), 5)
            else:
                task.cancel("first")
            await asyncio.wait_for(process.wait(), 5)
            assert not task.done() and not log.closed
            if trigger == "cancel":
                task.cancel("second")
                await asyncio.sleep(0)
                assert not task.done()
            release.set()
            with pytest.raises(
                asyncio.CancelledError
                if trigger == "cancel"
                else (OSError, ExceptionGroup)
            ) as raised:
                await settle(task)
            if storage_failure:
                assert "held log" in "".join(traceback.format_exception(raised.value))
            assert all(not path.exists() for path in paths)
        finally:
            release.set()
            if process.returncode is None:
                process.kill()
            await asyncio.wait_for(process.wait(), 5)
            task.cancel()
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)
            log.close()

    asyncio.run(check())


@pytest.mark.race
@pytest.mark.parametrize("failure", [False, True])
@requires_posix
def test_command_exit_receipt_waits_for_retained_log_worker(
    tmp_path, monkeypatch, failure
):
    from crewplane.architecture.contracts import InvocationContext
    from crewplane.runtime.agent.invocation import command

    async def check():
        loop = asyncio.get_running_loop()
        started = asyncio.Event()
        release = Event()
        processes, receipts = [], []
        real_spawn = asyncio.create_subprocess_exec

        async def spawn(*args, **kwargs):
            process = await real_spawn(*args, **kwargs)
            processes.append(process)
            return process

        class HeldLog(io.BytesIO):
            def write(self, payload):
                loop.call_soon_threadsafe(started.set)
                assert release.wait(10)
                assert not self.closed
                if failure:
                    raise OSError("command log failed")
                return super().write(payload)

        log = HeldLog()
        monkeypatch.setattr(command.asyncio, "create_subprocess_exec", spawn)
        monkeypatch.setattr(command, "open_log_handle", Mock(return_value=log))
        context = InvocationContext(
            "node", "task", "alpha", "executor", process_event_sink=receipts.append
        )
        task = asyncio.create_task(
            command.run_command_once(
                [
                    sys.executable,
                    "-c",
                    "import os,time; os.write(1,b'ready\\n'); time.sleep(60)",
                ],
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
            await asyncio.wait_for(started.wait(), 5)
            task.cancel("first")
            await asyncio.wait_for(processes[0].wait(), 5)
            assert not task.done() and not log.closed
            assert [event.status for event in receipts] == ["started"]
            task.cancel("second")
            await asyncio.sleep(0)
            assert not task.done()
        finally:
            release.set()
            for process in processes:
                if process.returncode is None:
                    process.kill()
                await asyncio.wait_for(process.wait(), 5)
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)
        with pytest.raises(asyncio.CancelledError) as raised:
            task.result()
        assert log.closed
        assert [event.status for event in receipts] == ["started", "exited"]
        if failure:
            assert "command log failed" in "".join(
                traceback.format_exception(raised.value)
            )

    asyncio.run(check())
