import asyncio
import time
import unittest

from orchestrator_cli.runtime.agent.process.runner import (
    collect_process_output,
    reap_failed_process,
)


class _SlowLogHandle:
    def __init__(self, delay_seconds: float) -> None:
        self.delay_seconds = delay_seconds
        self.writes: list[bytes] = []

    def write(self, payload: bytes) -> int:
        time.sleep(self.delay_seconds)
        self.writes.append(payload)
        return len(payload)

    def flush(self) -> None:
        time.sleep(self.delay_seconds)


class _FailingLogHandle:
    def write(self, payload: bytes) -> int:  # noqa: ARG002 - Required by test double or callback signature.
        raise OSError("disk full")

    def flush(self) -> None:
        return None


class _ProcessDouble:
    def __init__(self) -> None:
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.returncode: int | None = None
        self.kill_calls = 0
        self.terminate_calls = 0
        self._waiter = asyncio.Event()

    async def wait(self) -> int:
        await self._waiter.wait()
        return self.returncode or 0

    def complete(self, returncode: int = 0) -> None:
        self.returncode = returncode
        self.stdout.feed_eof()
        self.stderr.feed_eof()
        self._waiter.set()

    def exit_without_stream_eof(self, returncode: int = 0) -> None:
        self.returncode = returncode
        self._waiter.set()

    def kill(self) -> None:
        self.kill_calls += 1
        self.complete(returncode=-9)

    def terminate(self) -> None:
        self.terminate_calls += 1
        self.complete(returncode=-15)


class _AlreadyExitedOnTerminateProcessDouble:
    returncode: int | None = None

    def terminate(self) -> None:
        raise ProcessLookupError


class ProcessRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_collect_process_output_keeps_event_loop_live_with_slow_log_sink(
        self,
    ) -> None:
        process = _ProcessDouble()
        process.stdout.feed_data(b"".join(f"out {i}\n".encode() for i in range(20)))
        process.stderr.feed_data(b"".join(f"err {i}\n".encode() for i in range(20)))
        process.complete()
        log_handle = _SlowLogHandle(delay_seconds=0.05)
        ticks = 0

        async def heartbeat() -> None:
            nonlocal ticks
            while not collector_task.done():
                ticks += 1
                await asyncio.sleep(0.01)

        collector_task = asyncio.create_task(
            collect_process_output(process, log_handle)
        )
        heartbeat_task = asyncio.create_task(heartbeat())
        stdout_bytes, stderr_bytes = await asyncio.wait_for(collector_task, timeout=5.0)
        await heartbeat_task

        self.assertIn(b"out 0", stdout_bytes)
        self.assertIn(b"out 19", stdout_bytes)
        self.assertIn(b"err 0", stderr_bytes)
        self.assertIn(b"err 19", stderr_bytes)
        self.assertGreaterEqual(ticks, 5)
        self.assertGreaterEqual(len(log_handle.writes), 2)

    async def test_collect_process_output_returns_when_exited_process_leaves_pipes_open(
        self,
    ) -> None:
        process = _ProcessDouble()
        diagnostics = []
        process.stdout.feed_data(b"final stdout\n")
        process.stderr.feed_data(b"final stderr\n")
        process.exit_without_stream_eof()

        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            collect_process_output(process, None, diagnostics.append),
            timeout=1.0,
        )

        self.assertEqual(stdout_bytes, b"final stdout\n")
        self.assertEqual(stderr_bytes, b"final stderr\n")
        self.assertEqual(len(diagnostics), 1)
        self.assertEqual(diagnostics[0].operation, "process_pipe_drain_timeout")
        self.assertTrue(diagnostics[0].attributes["stdout_pending"])
        self.assertTrue(diagnostics[0].attributes["stderr_pending"])

    async def test_collect_process_output_logs_partial_lines_when_pipe_drain_times_out(
        self,
    ) -> None:
        process = _ProcessDouble()
        log_handle = _SlowLogHandle(delay_seconds=0)
        process.stdout.feed_data(b"partial stdout")
        process.stderr.feed_data(b"partial stderr")
        process.exit_without_stream_eof()

        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            collect_process_output(process, log_handle),
            timeout=1.0,
        )

        log_payload = b"".join(log_handle.writes)
        self.assertEqual(stdout_bytes, b"partial stdout")
        self.assertEqual(stderr_bytes, b"partial stderr")
        self.assertIn(b"partial stdout", log_payload)
        self.assertIn(b"[stderr] partial stderr", log_payload)

    async def test_collect_process_output_reaps_process_when_log_writer_fails(
        self,
    ) -> None:
        process = _ProcessDouble()
        process.stdout.feed_data(b"line\n")

        with self.assertRaisesRegex(OSError, "disk full"):
            await asyncio.wait_for(
                collect_process_output(process, _FailingLogHandle()),
                timeout=1.0,
            )

        await asyncio.wait_for(process.wait(), timeout=1.0)
        self.assertEqual(process.returncode, -9)
        self.assertEqual(process.kill_calls, 1)

    async def test_collect_process_output_reaps_process_when_cancelled(self) -> None:
        process = _ProcessDouble()
        collector_task = asyncio.create_task(collect_process_output(process, None))
        await asyncio.sleep(0)

        collector_task.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(collector_task, timeout=1.0)

        await asyncio.wait_for(process.wait(), timeout=1.0)
        self.assertEqual(process.returncode, -15)
        self.assertEqual(process.terminate_calls, 1)
        self.assertEqual(process.kill_calls, 0)

    async def test_reap_failed_process_reports_process_already_exited_warning(
        self,
    ) -> None:
        process = _AlreadyExitedOnTerminateProcessDouble()
        diagnostics = []

        await reap_failed_process(process, diagnostic_sink=diagnostics.append)

        self.assertEqual(len(diagnostics), 1)
        self.assertEqual(
            diagnostics[0].operation,
            "process_already_exited_before_signal",
        )
        self.assertEqual(diagnostics[0].level, "warning")
        self.assertEqual(diagnostics[0].attributes["attempted_signal"], "terminate")
