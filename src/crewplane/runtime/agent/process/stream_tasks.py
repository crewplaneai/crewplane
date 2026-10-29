from __future__ import annotations

import asyncio
from asyncio import sleep as asyncio_sleep
from time import monotonic
from typing import Any

from crewplane.architecture.contracts import InvocationDiagnosticSink

from .diagnostics import (
    PROCESS_PIPE_DRAIN_GRACE_SECONDS,
    emit_idle_timeout_diagnostic,
    emit_pipe_drain_timeout_diagnostic,
    emit_process_already_exited_diagnostic,
)
from .drain import ProcessDrainError, ProcessDrainEvidence, drain_async_process
from .session import wait_for_owned_task
from .signals import terminate_process_group, terminate_process_or_group

PROCESS_IDLE_POLL_INTERVAL_SECONDS = 1.0


class ProcessActivity:
    def __init__(self) -> None:
        self.last_output_at = monotonic()

    def mark_output(self) -> None:
        self.last_output_at = monotonic()

    def idle_seconds(self) -> float:
        return monotonic() - self.last_output_at


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
        _done_after_termination, pending = await asyncio.wait(
            tasks,
            timeout=PROCESS_PIPE_DRAIN_GRACE_SECONDS,
        )
    unresolved_pipes = bool(pending)
    for task in pending:
        task.cancel()

    results = await _bounded_task_results(tasks)
    if timed_out:
        emit_pipe_drain_timeout_diagnostic(
            diagnostic_sink,
            stdout_pending=stdout_pending,
            stderr_pending=stderr_pending,
        )

    if unresolved_pipes:
        raise ProcessDrainError(
            evidence=_unknown_process_evidence(),
            reason="Provider process pipes remained open after the finite drain deadline.",
        )

    for result in results:
        if isinstance(result, BaseException) and not isinstance(
            result,
            asyncio.CancelledError,
        ):
            raise result


async def drain_process_pipes(
    process: asyncio.subprocess.Process,
    diagnostic_sink: InvocationDiagnosticSink | None,
    process_group_id: int | None,
) -> None:
    stdout_task = asyncio.create_task(_discard_stream(process.stdout))
    stderr_task = asyncio.create_task(_discard_stream(process.stderr))
    try:
        await finish_stream_tasks_after_process_exit(
            stdout_task,
            stderr_task,
            diagnostic_sink,
            process_group_id,
        )
    finally:
        await cancel_pending_stream_tasks(stdout_task, stderr_task)


async def _discard_stream(reader: asyncio.StreamReader | None) -> None:
    if reader is None:
        return
    while await reader.read(65536):
        pass


async def cancel_pending_stream_tasks(*tasks: asyncio.Task[Any] | None) -> None:
    owned = tuple(task for task in tasks if task is not None)
    for task in owned:
        if not task.done():
            task.cancel()
    if owned:
        await _bounded_task_results(owned)


async def _bounded_task_results(
    tasks: tuple[asyncio.Task[Any], ...],
) -> tuple[object, ...]:
    done, pending = await asyncio.wait(
        tasks,
        timeout=PROCESS_PIPE_DRAIN_GRACE_SECONDS,
    )
    for task in pending:
        task.cancel()
    if pending:
        done_after_cancel, still_pending = await asyncio.wait(
            pending,
            timeout=PROCESS_PIPE_DRAIN_GRACE_SECONDS,
        )
        done.update(done_after_cancel)
        if still_pending:
            for task in done:
                _task_result(task)
            raise ProcessDrainError(
                evidence=_unknown_process_evidence(),
                reason="Provider process pipes remained open after the finite drain deadline.",
            )
    return tuple(_task_result(task) for task in tasks)


def _task_result(task: asyncio.Task[Any]) -> object:
    if task.cancelled():
        return asyncio.CancelledError()
    error = task.exception()
    return error if error is not None else task.result()


def _unknown_process_evidence() -> ProcessDrainEvidence:
    return ProcessDrainEvidence(
        pid=-1,
        process_group_id=None,
        leader_stopped=False,
        process_group_stopped=False,
    )


async def wait_for_process_or_idle_timeout(
    process_exit_task: asyncio.Task[int],
    idle_task: asyncio.Task[None] | None,
    readers: tuple[asyncio.Task[None], ...],
) -> None:
    wait_tasks: set[asyncio.Task[Any]] = {process_exit_task, *readers}
    if idle_task is not None:
        wait_tasks.add(idle_task)
    while wait_tasks:
        done, _ = await asyncio.wait(wait_tasks, return_when=asyncio.FIRST_COMPLETED)
        outcomes = [_task_result(task) for task in done]
        errors = [result for result in outcomes if isinstance(result, BaseException)]
        if errors:
            for error in errors[1:]:
                errors[0].add_note(f"Concurrent stream failure: {error!r}")
            raise errors[0]
        if process_exit_task in done:
            return
        wait_tasks.difference_update(done)


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


def format_timeout_seconds(timeout_seconds: float) -> str:
    return f"{timeout_seconds:g}s"


async def drain_after_stream_failure(
    process: asyncio.subprocess.Process,
    process_group_id: int | None,
    failure: BaseException,
) -> None:
    drain = asyncio.create_task(drain_async_process(process, process_group_id))
    await wait_for_owned_task(drain)
    try:
        drain.result()
    except BaseException as cleanup_error:
        if isinstance(failure, asyncio.CancelledError):
            failure.add_note(f"Process cleanup failed: {cleanup_error}")
            raise failure from cleanup_error
        raise cleanup_error from failure
