from __future__ import annotations

import asyncio
from asyncio import sleep as asyncio_sleep
from time import monotonic
from typing import Any, BinaryIO

from orchestrator_cli.architecture.contracts import InvocationDiagnosticSink

from .diagnostics import (
    PROCESS_PIPE_DRAIN_GRACE_SECONDS,
    emit_idle_timeout_diagnostic,
    emit_pipe_drain_timeout_diagnostic,
    emit_process_already_exited_diagnostic,
)
from .log_rendering import render_log_text_segments
from .signals import (
    kill_process_or_group,
    reap_failed_process,
    terminate_process_group,
    terminate_process_or_group,
    wait_for_process_exit,
)
from .stream_capture import CapturedStream, ProcessOutputCapture, ProcessStreamCapture

STDERR_PREFIX = b"[stderr] "
PROCESS_IDLE_POLL_INTERVAL_SECONDS = 1.0
LOG_QUEUE_MAX_ITEMS = 64


class ProcessActivity:
    def __init__(self) -> None:
        self.last_output_at = monotonic()

    def mark_output(self) -> None:
        self.last_output_at = monotonic()

    def idle_seconds(self) -> float:
        return monotonic() - self.last_output_at


def close_log_handle(log_handle: BinaryIO | None) -> None:
    if log_handle is None:
        return
    try:
        log_handle.close()
    except Exception:
        return


async def write_stdin(
    process: asyncio.subprocess.Process,
    stdin_data: bytes | None,
) -> None:
    if stdin_data is None or process.stdin is None:
        return
    process.stdin.write(stdin_data)
    await process.stdin.drain()
    process.stdin.close()


async def collect_process_output(
    process: asyncio.subprocess.Process,
    log_handle: BinaryIO | None,
    diagnostic_sink: InvocationDiagnosticSink | None = None,
    process_group_id: int | None = None,
    idle_timeout_seconds: float | None = None,
) -> ProcessOutputCapture:
    if process.stdout is None or process.stderr is None:
        raise RuntimeError("Failed to capture process streams.")

    stdout_capture = CapturedStream()
    stderr_capture = CapturedStream()
    output_capture: ProcessOutputCapture | None = None
    log_queue: asyncio.Queue[bytes | None] | None = None
    writer_status: asyncio.Future[Exception | None] | None = None
    if log_handle is not None:
        log_queue = asyncio.Queue(maxsize=LOG_QUEUE_MAX_ITEMS)
        writer_status = asyncio.get_running_loop().create_future()

    try:
        try:
            async with asyncio.TaskGroup() as task_group:
                task_group.create_task(
                    capture_process_streams(
                        process,
                        log_queue,
                        stdout_capture,
                        stderr_capture,
                        diagnostic_sink,
                        process_group_id,
                        idle_timeout_seconds,
                    )
                )
                if log_handle is not None and log_queue is not None:
                    task_group.create_task(
                        drain_log_queue(log_handle, log_queue, writer_status)
                    )
                    task_group.create_task(
                        watch_log_writer_status(
                            process,
                            writer_status,
                            diagnostic_sink,
                            process_group_id,
                        )
                    )
        except asyncio.CancelledError:
            await reap_failed_process(process, process_group_id, diagnostic_sink)
            raise
        except Exception as exc:
            if process.returncode is None:
                try:
                    kill_process_or_group(process, process_group_id)
                except ProcessLookupError:
                    emit_process_already_exited_diagnostic(diagnostic_sink, "kill")
                await wait_for_process_exit(process)
            error = unwrap_task_group_error(exc)
            if error is not exc:
                raise error from exc
            raise

        output_capture = ProcessOutputCapture(
            stdout=ProcessStreamCapture(
                path=stdout_capture.path,
                tail_bytes=stdout_capture.tail_bytes,
            ),
            stderr=ProcessStreamCapture(
                path=stderr_capture.path,
                tail_bytes=stderr_capture.tail_bytes,
            ),
        )
        return output_capture
    finally:
        if output_capture is None:
            stdout_capture.cleanup()
            stderr_capture.cleanup()
        else:
            stdout_capture.close()
            stderr_capture.close()


async def capture_process_streams(
    process: asyncio.subprocess.Process,
    log_queue: asyncio.Queue[bytes | None] | None,
    stdout_capture: CapturedStream,
    stderr_capture: CapturedStream,
    diagnostic_sink: InvocationDiagnosticSink | None = None,
    process_group_id: int | None = None,
    idle_timeout_seconds: float | None = None,
) -> None:
    activity = ProcessActivity()
    stdout_task = asyncio.create_task(
        pipe_stream(process.stdout, log_queue, b"", stdout_capture, activity)
    )
    stderr_task = asyncio.create_task(
        pipe_stream(
            process.stderr,
            log_queue,
            STDERR_PREFIX,
            stderr_capture,
            activity,
        )
    )
    process_exit_task = asyncio.create_task(wait_for_process_exit(process))
    idle_task = build_idle_timeout_task(
        process,
        activity,
        idle_timeout_seconds,
        diagnostic_sink,
        process_group_id,
    )
    try:
        await wait_for_process_or_idle_timeout(process_exit_task, idle_task)
        await finish_stream_tasks_after_process_exit(
            stdout_task,
            stderr_task,
            diagnostic_sink,
            process_group_id,
        )
    finally:
        await cancel_pending_stream_tasks(
            stdout_task,
            stderr_task,
            process_exit_task,
            idle_task,
        )
        if log_queue is not None:
            await log_queue.put(None)


async def pipe_stream(
    reader: asyncio.StreamReader,
    log_queue: asyncio.Queue[bytes | None] | None,
    prefix: bytes,
    capture: CapturedStream,
    activity: ProcessActivity | None = None,
) -> None:
    line_open = False
    while True:
        chunk = await reader.read(1024)
        if not chunk:
            return
        if activity is not None:
            activity.mark_output()
        capture.write(chunk)
        if log_queue is None:
            continue
        payload, line_open = render_log_text_segments(
            chunk.decode(errors="replace"),
            prefix,
            line_open,
        )
        if payload:
            await log_queue.put(payload)


async def drain_log_queue(
    log_handle: BinaryIO,
    log_queue: asyncio.Queue[bytes | None],
    writer_status: asyncio.Future[Exception | None],
) -> None:
    try:
        while True:
            payload = await log_queue.get()
            if payload is None:
                if not writer_status.done():
                    writer_status.set_result(None)
                return
            await asyncio.to_thread(write_log_bytes, log_handle, payload)
    except Exception as exc:
        if not writer_status.done():
            writer_status.set_result(exc)
        return


async def watch_log_writer_status(
    process: asyncio.subprocess.Process,
    writer_status: asyncio.Future[Exception | None],
    diagnostic_sink: InvocationDiagnosticSink | None,
    process_group_id: int | None = None,
) -> None:
    writer_error = await writer_status
    if writer_error is None:
        return
    if process.returncode is None:
        try:
            kill_process_or_group(process, process_group_id)
        except ProcessLookupError:
            emit_process_already_exited_diagnostic(diagnostic_sink, "kill")
        await wait_for_process_exit(process)
    raise writer_error


async def watch_process_idle_timeout(
    process: asyncio.subprocess.Process,
    activity: ProcessActivity,
    idle_timeout_seconds: float,
    diagnostic_sink: InvocationDiagnosticSink | None,
    process_group_id: int | None,
) -> None:
    while process.returncode is None:
        idle_seconds = activity.idle_seconds()
        remaining_seconds = idle_timeout_seconds - idle_seconds
        if remaining_seconds <= 0:
            emit_idle_timeout_diagnostic(
                diagnostic_sink,
                idle_timeout_seconds=idle_timeout_seconds,
            )
            try:
                terminate_process_or_group(process, process_group_id)
            except ProcessLookupError:
                emit_process_already_exited_diagnostic(diagnostic_sink, "terminate")
            raise RuntimeError(
                "Provider invocation produced no output for "
                f"{format_timeout_seconds(idle_timeout_seconds)}."
            )
        await asyncio_sleep(min(PROCESS_IDLE_POLL_INTERVAL_SECONDS, remaining_seconds))


async def finish_stream_tasks_after_process_exit(
    stdout_task: asyncio.Task[None],
    stderr_task: asyncio.Task[None],
    diagnostic_sink: InvocationDiagnosticSink | None,
    process_group_id: int | None,
) -> None:
    tasks = (stdout_task, stderr_task)
    _done, pending = await asyncio.wait(
        tasks,
        timeout=PROCESS_PIPE_DRAIN_GRACE_SECONDS,
    )
    stdout_pending = stdout_task in pending
    stderr_pending = stderr_task in pending
    timed_out = bool(pending)
    if pending:
        await terminate_process_group(process_group_id)
        pending = {task for task in tasks if not task.done()}
    for task in pending:
        task.cancel()

    results = await asyncio.gather(*tasks, return_exceptions=True)
    if timed_out:
        emit_pipe_drain_timeout_diagnostic(
            diagnostic_sink,
            stdout_pending=stdout_pending,
            stderr_pending=stderr_pending,
        )

    for result in results:
        if isinstance(result, BaseException) and not isinstance(
            result,
            asyncio.CancelledError,
        ):
            raise result


async def cancel_pending_stream_tasks(*tasks: asyncio.Task[Any] | None) -> None:
    pending_tasks = [task for task in tasks if task is not None and not task.done()]
    for task in pending_tasks:
        task.cancel()
    if pending_tasks:
        await asyncio.gather(*pending_tasks, return_exceptions=True)


async def wait_for_process_or_idle_timeout(
    process_exit_task: asyncio.Task[int],
    idle_task: asyncio.Task[None] | None,
) -> None:
    wait_tasks: set[asyncio.Task[Any]] = {process_exit_task}
    if idle_task is not None:
        wait_tasks.add(idle_task)
    done, _ = await asyncio.wait(
        wait_tasks,
        return_when=asyncio.FIRST_COMPLETED,
    )
    if idle_task is not None and idle_task in done:
        await idle_task
    await process_exit_task


def build_idle_timeout_task(
    process: asyncio.subprocess.Process,
    activity: ProcessActivity,
    idle_timeout_seconds: float | None,
    diagnostic_sink: InvocationDiagnosticSink | None,
    process_group_id: int | None,
) -> asyncio.Task[None] | None:
    if idle_timeout_seconds is None:
        return None
    return asyncio.create_task(
        watch_process_idle_timeout(
            process,
            activity,
            idle_timeout_seconds,
            diagnostic_sink,
            process_group_id,
        )
    )


def write_log_bytes(log_handle: BinaryIO, payload: bytes) -> None:
    log_handle.write(payload)
    log_handle.flush()


def unwrap_task_group_error(error: Exception) -> Exception:
    current = error
    while isinstance(current, ExceptionGroup) and len(current.exceptions) == 1:
        next_error = current.exceptions[0]
        if not isinstance(next_error, Exception):
            break
        current = next_error
    return current


def format_timeout_seconds(timeout_seconds: float) -> str:
    return f"{timeout_seconds:g}s"
