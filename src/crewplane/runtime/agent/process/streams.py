from __future__ import annotations

import asyncio
from typing import BinaryIO

from crewplane.architecture.contracts import InvocationDiagnosticSink

from .drain import drain_async_process
from .log_rendering import IncrementalLogRenderer
from .log_writer import (
    LOG_QUEUE_MAX_ITEMS,
    drain_log_queue,
    signal_log_queue_complete,
    watch_log_writer_status,
)
from .session import wait_for_owned_task
from .signals import wait_for_process_exit
from .stream_capture import (
    CapturedStream,
    ProcessOutputCapture,
    ProcessStreamCapture,
    close_captures,
)
from .stream_tasks import (
    ProcessActivity,
    build_idle_timeout_task,
    cancel_pending_stream_tasks,
    drain_after_stream_failure,
    finish_stream_tasks_after_process_exit,
    wait_for_process_or_idle_timeout,
)

STDERR_PREFIX = b"[stderr] "


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
    try:
        process.stdin.write(stdin_data)
        await process.stdin.drain()
    except (BrokenPipeError, ConnectionResetError):
        # Early child exits must still allow output and exit-status collection.
        pass
    finally:
        process.stdin.close()


async def write_stdin_and_collect_output(
    process: asyncio.subprocess.Process,
    stdin_data: bytes | None,
    log_handle: BinaryIO | None,
    diagnostic_sink: InvocationDiagnosticSink | None = None,
    process_group_id: int | None = None,
    idle_timeout_seconds: float | None = None,
) -> ProcessOutputCapture:
    output_task: asyncio.Task[ProcessOutputCapture] | None = None
    try:
        async with asyncio.TaskGroup() as task_group:
            task_group.create_task(write_stdin(process, stdin_data))
            output_task = task_group.create_task(
                collect_process_output(
                    process,
                    log_handle,
                    diagnostic_sink,
                    process_group_id,
                    idle_timeout_seconds,
                )
            )
    except BaseException as exc:
        if output_task is not None and output_task.done():
            if output_task.cancelled():
                try:
                    output_task.result()
                except asyncio.CancelledError as output_failure:
                    # TaskGroup ignores cancelled children, including their I/O errors.
                    if output_failure.__cause__ is not None or getattr(
                        output_failure, "__notes__", None
                    ):
                        exc.__cause__ = output_failure
            elif output_task.exception() is None:
                output_task.result().cleanup()
        if isinstance(exc, Exception):
            error = unwrap_task_group_error(exc)
            if error is not exc:
                raise error from exc
        raise
    return output_task.result()


async def collect_process_output(
    process: asyncio.subprocess.Process,
    log_handle: BinaryIO | None,
    diagnostic_sink: InvocationDiagnosticSink | None = None,
    process_group_id: int | None = None,
    idle_timeout_seconds: float | None = None,
) -> ProcessOutputCapture:
    captures: list[CapturedStream] = []
    writer_status: asyncio.Future[BaseException | None] | None = None
    try:
        if process.stdout is None or process.stderr is None:
            raise RuntimeError("Failed to capture process streams.")
        stdout_capture = CapturedStream()
        captures.append(stdout_capture)
        stderr_capture = CapturedStream()
        captures.append(stderr_capture)
        log_queue: asyncio.Queue[bytes | None] | None = None
        if log_handle is not None:
            log_queue = asyncio.Queue(maxsize=LOG_QUEUE_MAX_ITEMS)
            writer_status = asyncio.get_running_loop().create_future()
        try:
            async with asyncio.TaskGroup() as group:
                stream_task = group.create_task(
                    capture_process_streams(
                        process,
                        log_queue,
                        stdout_capture,
                        stderr_capture,
                        diagnostic_sink,
                        process_group_id,
                        idle_timeout_seconds,
                        writer_status,
                    )
                )
                owned_tasks = [stream_task]
                if (
                    log_handle is not None
                    and log_queue is not None
                    and writer_status is not None
                ):
                    owned_tasks.extend(
                        (
                            group.create_task(
                                drain_log_queue(log_handle, log_queue, writer_status)
                            ),
                            group.create_task(watch_log_writer_status(writer_status)),
                        )
                    )
                try:
                    await asyncio.wait(owned_tasks)
                except BaseException as exc:
                    for task in owned_tasks:
                        task.cancel()
                    # The stream task can finish before its retained log worker.
                    await drain_after_stream_failure(process, process_group_id, exc)
                    raise
        except BaseException as exc:
            _preserve_writer_failure(writer_status, exc)
            raise
        _preserve_writer_failure(writer_status)
        await drain_async_process(process, process_group_id)
        close_captures(captures)
        return ProcessOutputCapture(
            stdout=ProcessStreamCapture(stdout_capture.path, stdout_capture.tail_bytes),
            stderr=ProcessStreamCapture(stderr_capture.path, stderr_capture.tail_bytes),
        )
    except BaseException as exc:
        failure = unwrap_task_group_error(exc) if isinstance(exc, Exception) else exc
        try:
            await drain_after_stream_failure(process, process_group_id, failure)
        finally:
            for capture in captures:
                try:
                    capture.cleanup()
                except BaseException as cleanup_error:
                    failure.add_note(f"Capture cleanup failed: {cleanup_error!r}")
        if failure is not exc:
            raise failure from exc
        raise


def _preserve_writer_failure(
    status: asyncio.Future[BaseException | None] | None,
    failure: BaseException | None = None,
) -> None:
    if status is None or not status.done():
        return
    error = status.result()
    if error is None or isinstance(error, asyncio.CancelledError):
        return
    if failure is None:
        raise error
    failure.add_note(f"Log writer failed: {error!r}")
    if isinstance(failure, asyncio.CancelledError):
        failure.__cause__ = error


async def capture_process_streams(
    process: asyncio.subprocess.Process,
    log_queue: asyncio.Queue[bytes | None] | None,
    stdout_capture: CapturedStream,
    stderr_capture: CapturedStream,
    diagnostic_sink: InvocationDiagnosticSink | None = None,
    process_group_id: int | None = None,
    idle_timeout_seconds: float | None = None,
    writer_status: asyncio.Future[BaseException | None] | None = None,
) -> None:
    if process.stdout is None or process.stderr is None:
        raise RuntimeError("Failed to capture process streams.")
    activity = ProcessActivity()
    stdout_task = asyncio.create_task(
        pipe_stream(process.stdout, log_queue, b"", stdout_capture, activity)
    )
    stderr_task = asyncio.create_task(
        pipe_stream(process.stderr, log_queue, STDERR_PREFIX, stderr_capture, activity)
    )
    process_exit_task = asyncio.create_task(wait_for_process_exit(process))
    idle_task = build_idle_timeout_task(
        process, activity, idle_timeout_seconds, diagnostic_sink, process_group_id
    )
    failure: BaseException | None = None
    try:
        await wait_for_process_or_idle_timeout(
            process_exit_task, idle_task, (stdout_task, stderr_task)
        )
        await finish_stream_tasks_after_process_exit(
            stdout_task, stderr_task, diagnostic_sink, process_group_id
        )
    except BaseException as exc:
        failure = exc
        # Drain now: TaskGroup settlement may wait for a log write that cannot be cancelled.
        await drain_after_stream_failure(process, process_group_id, exc)
        raise
    finally:
        settlement = asyncio.create_task(
            cancel_pending_stream_tasks(
                stdout_task, stderr_task, process_exit_task, idle_task
            )
        )
        cancellation = await wait_for_owned_task(settlement)
        try:
            settlement.result()
        except BaseException as cleanup_error:
            raise cleanup_error from failure
        if cancellation is not None and failure is None:
            raise cancellation
        if failure is None and log_queue is not None and writer_status is not None:
            await signal_log_queue_complete(log_queue, writer_status)


async def pipe_stream(
    reader: asyncio.StreamReader,
    log_queue: asyncio.Queue[bytes | None] | None,
    prefix: bytes,
    capture: CapturedStream,
    activity: ProcessActivity | None = None,
) -> None:
    log_renderer = IncrementalLogRenderer(prefix) if log_queue is not None else None
    while True:
        chunk = await reader.read(1024)
        if not chunk:
            if log_queue is not None and log_renderer is not None:
                payload = log_renderer.finish()
                if payload:
                    await log_queue.put(payload)
            return
        if activity is not None:
            activity.mark_output()
        capture.write(chunk)
        if log_queue is None or log_renderer is None:
            continue
        payload = log_renderer.render(chunk)
        if payload:
            await log_queue.put(payload)


def unwrap_task_group_error(error: Exception) -> Exception:
    current = error
    while isinstance(current, ExceptionGroup) and len(current.exceptions) == 1:
        next_error = current.exceptions[0]
        if not isinstance(next_error, Exception):
            break
        current = next_error
    return current
