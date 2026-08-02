import asyncio
import time
import tracemalloc
import unittest
from threading import Event
from unittest.mock import patch

from crewplane.runtime.agent.process.runner import (
    collect_process_output,
    reap_failed_process,
)
from crewplane.runtime.agent.process.stream_capture import (
    CapturedStream as RealCapturedStream,
)
from crewplane.runtime.agent.process.streams import (
    LOG_QUEUE_MAX_ITEMS,
    pipe_stream,
)


class _RecordingLogHandle:
    def __init__(self) -> None:
        self.writes: list[bytes] = []

    def write(self, payload: bytes) -> int:
        self.writes.append(payload)
        return len(payload)

    def flush(self) -> None:
        return


class _BlockingLogHandle:
    def __init__(self) -> None:
        self.write_started = Event()
        self.release_write = Event()
        self.writes: list[bytes] = []

    def write(self, payload: bytes) -> int:
        self.write_started.set()
        if not self.release_write.wait(timeout=2.0):
            raise TimeoutError("Test log write was not released.")
        self.writes.append(payload)
        return len(payload)

    def flush(self) -> None:
        if not self.release_write.wait(timeout=2.0):
            raise TimeoutError("Test log flush was not released.")


class _FailingLogHandle:
    def write(self, payload: bytes) -> int:  # noqa: ARG002 - Required by test double or callback signature.
        raise OSError("disk full")

    def flush(self) -> None:
        return None


class _SignallingStreamReader(asyncio.StreamReader):
    def __init__(self) -> None:
        super().__init__()
        self.read_started = asyncio.Event()

    async def read(self, n: int = -1) -> bytes:
        self.read_started.set()
        return await super().read(n)


class _ProcessDouble:
    def __init__(self) -> None:
        self.stdout = _SignallingStreamReader()
        self.stderr = _SignallingStreamReader()
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
    def test_captured_stream_keeps_full_file_and_bounded_tail(self) -> None:
        capture = RealCapturedStream(max_memory_bytes=4)
        try:
            capture.write(b"abcdef")
            capture.close()

            self.assertEqual(capture.tail_bytes, b"cdef")
            self.assertEqual(capture.path.read_bytes(), b"abcdef")
        finally:
            capture.cleanup()

    def test_captured_stream_preserves_exact_tail_across_chunk_boundaries(self) -> None:
        capture = RealCapturedStream(max_memory_bytes=7)
        try:
            for payload in (b"", b"ab", b"cdef", b"ghijklmnop", b"qr"):
                capture.write(payload)
            capture.close()

            self.assertEqual(capture.tail_bytes, b"lmnopqr")
            self.assertEqual(capture.path.read_bytes(), b"abcdefghijklmnopqr")
        finally:
            capture.cleanup()

    def test_captured_stream_supports_zero_length_tail(self) -> None:
        capture = RealCapturedStream(max_memory_bytes=0)
        try:
            capture.write(b"full output")
            capture.close()

            self.assertEqual(capture.tail_bytes, b"")
            self.assertEqual(capture.path.read_bytes(), b"full output")
        finally:
            capture.cleanup()

    def test_captured_stream_retains_100_mib_with_bounded_memory(self) -> None:
        capture = RealCapturedStream(max_memory_bytes=1024 * 1024)
        chunk = b"x" * 4096
        started = time.monotonic()
        tracemalloc.start()
        try:
            remaining_chunks = (100 * 1024 * 1024) // len(chunk)
            while remaining_chunks:
                capture.write(chunk)
                remaining_chunks -= 1
            _current, peak = tracemalloc.get_traced_memory()
            capture.close()

            self.assertEqual(capture.tail_bytes, b"x" * (1024 * 1024))
            self.assertEqual(capture.path.stat().st_size, 100 * 1024 * 1024)
            self.assertLess(peak, 8 * 1024 * 1024)
            self.assertLess(time.monotonic() - started, 20)
        finally:
            tracemalloc.stop()
            capture.cleanup()

    async def test_collect_process_output_keeps_event_loop_live_with_slow_log_sink(
        self,
    ) -> None:
        process = _ProcessDouble()
        process.stdout.feed_data(b"".join(f"out {i}\n".encode() for i in range(20)))
        process.stderr.feed_data(b"".join(f"err {i}\n".encode() for i in range(20)))
        process.complete()
        log_handle = _BlockingLogHandle()
        ticks = 0
        heartbeat_progressed = asyncio.Event()

        async def heartbeat() -> None:
            nonlocal ticks
            while not collector_task.done():
                ticks += 1
                if ticks >= 5:
                    heartbeat_progressed.set()
                await asyncio.sleep(0)

        collector_task = asyncio.create_task(
            collect_process_output(process, log_handle)
        )
        heartbeat_task = asyncio.create_task(heartbeat())
        try:
            self.assertTrue(await asyncio.to_thread(log_handle.write_started.wait, 1.0))
            await asyncio.wait_for(heartbeat_progressed.wait(), timeout=1.0)
        finally:
            log_handle.release_write.set()
            task_results = await asyncio.wait_for(
                asyncio.gather(
                    collector_task,
                    heartbeat_task,
                    return_exceptions=True,
                ),
                timeout=5.0,
            )
        collection_result = task_results[0]
        if isinstance(collection_result, BaseException):
            raise collection_result
        stdout_bytes, stderr_bytes = collection_result

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
        log_handle = _RecordingLogHandle()
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

    async def test_pipe_stream_logs_long_lines_in_bounded_chunks(self) -> None:
        reader = asyncio.StreamReader()
        reader.feed_data(b"x" * 10_000)
        reader.feed_eof()
        log_queue: asyncio.Queue[bytes | None] = asyncio.Queue(
            maxsize=LOG_QUEUE_MAX_ITEMS
        )
        capture = RealCapturedStream()
        try:
            await pipe_stream(reader, log_queue, b"[stderr] ", capture)
            payloads: list[bytes] = []
            while not log_queue.empty():
                payload = await log_queue.get()
                if payload is not None:
                    payloads.append(payload)

            self.assertGreater(len(payloads), 1)
            self.assertTrue(payloads[0].startswith(b"[stderr] "))
            self.assertFalse(
                any(payload.startswith(b"[stderr] ") for payload in payloads[1:])
            )
            self.assertTrue(
                all(len(payload) <= 1024 + len(b"[stderr] ") for payload in payloads)
            )
            self.assertEqual(
                b"".join(payload.removeprefix(b"[stderr] ") for payload in payloads),
                b"x" * 10_000,
            )
        finally:
            capture.cleanup()

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

    async def test_collect_process_output_removes_capture_files_when_collection_fails(
        self,
    ) -> None:
        created_paths = []

        class TrackingCapturedStream(RealCapturedStream):
            def __init__(self) -> None:
                super().__init__()
                created_paths.append(self.path)

        process = _ProcessDouble()
        process.stdout.feed_data(b"line\n")

        with (
            patch(
                "crewplane.runtime.agent.process.streams.CapturedStream",
                TrackingCapturedStream,
            ),
            self.assertRaisesRegex(OSError, "disk full"),
        ):
            await asyncio.wait_for(
                collect_process_output(process, _FailingLogHandle()),
                timeout=1.0,
            )

        self.assertEqual(process.returncode, -9)
        self.assertGreaterEqual(len(created_paths), 2)
        self.assertTrue(all(not path.exists() for path in created_paths))

    async def test_collect_process_output_reaps_process_when_cancelled(self) -> None:
        process = _ProcessDouble()
        collector_task = asyncio.create_task(collect_process_output(process, None))
        await asyncio.wait_for(process.stdout.read_started.wait(), timeout=1.0)

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
