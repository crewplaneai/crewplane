from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Awaitable
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import BinaryIO, cast

from crewplane.architecture.contracts import (
    ChildProcessEnvironment,
    CommandResult,
    CommandRunner,
    InvocationContext,
    InvocationPlan,
    LogLevel,
)
from crewplane.core.platform import supports_posix_process_groups
from crewplane.runtime.workspace.state_evidence import (
    confirm_workspace_process_drain,
    record_unresolved_workspace_process_drain,
)

from ..process.drain import ProcessDrainError
from ..process.runner import (
    build_retry_log_header,
    reap_failed_process,
    write_stdin_and_collect_output,
)
from ..process.streams import drain_process_pipes, format_timeout_seconds
from ..process.windows_launch import WindowsLaunch
from ..workspace_environment import record_workspace_child_environment_applied
from .command_lifecycle import (
    CommandLifecycle,
    command_io_result,
    wait_for_command_io,
)
from .telemetry import emit_invocation_diagnostic


@dataclass(frozen=True)
class _CommandExecutionRequest:
    cmd: list[str]
    stdin_data: bytes | None
    log_file: Path | None
    append_log: bool
    log_header: bytes | None
    cwd: Path
    idle_timeout_seconds: float | None
    child_environment: ChildProcessEnvironment | None


def open_log_handle(
    log_file: Path | None,
    append: bool,
    header_bytes: bytes | None = None,
) -> BinaryIO | None:
    """Open a binary log, creating parents and flushing any header.

    The caller owns the returned handle and must close it. Return None when
    logging is disabled. Initialization failures close the handle and propagate
    the original exception, even if closing also fails.
    """
    if log_file is None:
        return None
    log_file.parent.mkdir(parents=True, exist_ok=True)
    mode = "ab" if append else "wb"
    handle = cast(BinaryIO, log_file.open(mode))
    try:
        if header_bytes:
            handle.write(header_bytes)
            handle.flush()
    except BaseException:
        with suppress(BaseException):
            handle.close()
        raise
    return handle


async def run_command_once(
    cmd: list[str],
    stdin_data: bytes | None,
    log_file: Path | None,
    append_log: bool,
    log_header: bytes | None,
    cwd: Path,
    invocation_context: InvocationContext | None,
    idle_timeout_seconds: float | None,
    child_environment: ChildProcessEnvironment | None = None,
) -> CommandResult:
    """Run one child process, drain it, close its log, and report its exit.

    Nonzero exits return normally. The caller must call cleanup_stream_files()
    on the result after consuming its persisted streams. Execution failures
    become RuntimeError; unconfirmed drain errors take precedence. Cancellation
    is re-raised after cleanup, with any drain failure attached as its cause.
    Log I/O, capture cleanup, and drain persistence finish before cancellation
    propagates. Concurrent I/O failures remain attached to cancellation.
    """
    lifecycle = CommandLifecycle(invocation_context)
    request = _CommandExecutionRequest(
        cmd=cmd,
        stdin_data=stdin_data,
        log_file=log_file,
        append_log=append_log,
        log_header=log_header,
        cwd=cwd,
        idle_timeout_seconds=idle_timeout_seconds,
        child_environment=child_environment,
    )
    try:
        await _execute_command(lifecycle, request)
    except FileNotFoundError as exc:
        drain_error = await _handle_failed_command(lifecycle, exc)
        if drain_error is not None:
            raise drain_error from exc
        if lifecycle.process is None:
            raise RuntimeError(f"CLI executable not found: {cmd[0]}") from exc
        raise RuntimeError(f"Execution error: {exc}") from exc
    except asyncio.CancelledError as exc:
        drain_error = await _handle_failed_command(lifecycle, exc)
        if drain_error is not None:
            exc.add_note(str(drain_error))
            exc.__cause__ = drain_error
        raise
    except Exception as exc:
        drain_error = await _handle_failed_command(lifecycle, exc)
        if drain_error is not None:
            raise drain_error from exc
        if isinstance(exc, RuntimeError):
            raise
        raise RuntimeError(f"Execution error: {exc}") from exc
    finally:
        await lifecycle.finalize(sys.exception())
    return lifecycle.build_result()


async def _execute_command(
    lifecycle: CommandLifecycle,
    request: _CommandExecutionRequest,
) -> None:
    start_new_session = supports_posix_process_groups()
    if sys.platform == "win32":
        lifecycle.windows_launch = WindowsLaunch()
        process = await lifecycle.windows_launch.start(
            request.cmd, request.cwd, _child_process_env(request.child_environment)
        )
    else:
        process = await asyncio.create_subprocess_exec(
            *request.cmd,
            stdin=asyncio.subprocess.PIPE
            if request.stdin_data
            else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=request.cwd,
            env=_child_process_env(request.child_environment),
            start_new_session=start_new_session,
        )
    lifecycle.process = process
    lifecycle.process_group_id = process.pid if start_new_session else None
    record_workspace_child_environment_applied(
        lifecycle.invocation_context,
        request.child_environment,
    )
    lifecycle.emit_started()
    await _open_command_log(lifecycle, request)
    if lifecycle.windows_launch is not None:
        lifecycle.windows_launch.release()
        lifecycle.output_capture = await lifecycle.windows_launch.collect(
            request.stdin_data,
            lifecycle.log_handle,
            lifecycle.diagnostic_sink,
            request.idle_timeout_seconds,
        )
    else:
        lifecycle.output_capture = await write_stdin_and_collect_output(
            process,
            request.stdin_data,
            lifecycle.log_handle,
            lifecycle.diagnostic_sink,
            lifecycle.process_group_id,
            request.idle_timeout_seconds,
        )
    lifecycle.cleanup_confirmed = True
    cancellation = await _persist_process_drain(lifecycle, None)
    if cancellation is not None:
        raise cancellation


async def _open_command_log(
    lifecycle: CommandLifecycle,
    request: _CommandExecutionRequest,
) -> None:
    task = asyncio.create_task(
        asyncio.to_thread(
            open_log_handle,
            request.log_file,
            append=request.append_log,
            header_bytes=request.log_header,
        )
    )
    cancellation = await wait_for_command_io(task)
    lifecycle.log_handle = command_io_result(task, cancellation)
    if cancellation is not None:
        raise cancellation


async def _persist_process_drain(
    lifecycle: CommandLifecycle,
    error: ProcessDrainError | None,
    cancellation: asyncio.CancelledError | None = None,
) -> asyncio.CancelledError | None:
    task = asyncio.create_task(
        asyncio.to_thread(
            _record_process_drain_outcome,
            lifecycle.invocation_context,
            lifecycle.process,
            lifecycle.process_group_id,
            error,
        )
    )
    cancellation = await wait_for_command_io(task, cancellation)
    command_io_result(task, cancellation)
    return cancellation


async def _handle_failed_command(
    lifecycle: CommandLifecycle,
    failure: BaseException,
) -> ProcessDrainError | None:
    cancellation = failure if isinstance(failure, asyncio.CancelledError) else None
    drain_error, cancellation = await _cleanup_failed_command(lifecycle, cancellation)
    if drain_error is None and isinstance(failure, ProcessDrainError):
        drain_error = failure
    cancellation = await _persist_process_drain(lifecycle, drain_error, cancellation)
    if cancellation is not None and cancellation is not failure:
        if drain_error is not None:
            cancellation.add_note(str(drain_error))
            cancellation.__cause__ = drain_error
        raise cancellation
    return drain_error


async def _cleanup_failed_command(
    lifecycle: CommandLifecycle,
    cancellation: asyncio.CancelledError | None,
) -> tuple[ProcessDrainError | None, asyncio.CancelledError | None]:
    task = asyncio.create_task(_drain_failed_command(lifecycle))
    cancellation = await wait_for_command_io(task, cancellation)
    drain_error = command_io_result(task, cancellation)
    cancellation = await lifecycle.cleanup_output(cancellation)
    if drain_error is None:
        lifecycle.cleanup_confirmed = True
    return drain_error, cancellation


async def _drain_failed_command(
    lifecycle: CommandLifecycle,
) -> ProcessDrainError | None:
    try:
        if lifecycle.windows_launch is not None:
            lifecycle.process = lifecycle.windows_launch.process
            await lifecycle.windows_launch.drain()
        if lifecycle.process is not None:
            await reap_failed_process(
                lifecycle.process,
                lifecycle.process_group_id,
                lifecycle.diagnostic_sink,
            )
            await drain_process_pipes(
                lifecycle.process,
                lifecycle.diagnostic_sink,
                lifecycle.process_group_id,
            )
    except ProcessDrainError as exc:
        return exc
    return None


def _record_process_drain_success(
    invocation_context: InvocationContext | None,
    process: asyncio.subprocess.Process,
    process_group_id: int | None,
) -> None:
    confirm_workspace_process_drain(
        _workspace_state_path(invocation_context),
        process.pid,
        process_group_id,
    )


def _record_process_drain_outcome(
    invocation_context: InvocationContext | None,
    process: asyncio.subprocess.Process | None,
    process_group_id: int | None,
    error: BaseException | None,
) -> None:
    if process is None:
        return
    if not isinstance(error, ProcessDrainError):
        _record_process_drain_success(
            invocation_context,
            process,
            process_group_id,
        )
        return
    record_unresolved_workspace_process_drain(
        _workspace_state_path(invocation_context), error
    )


def _workspace_state_path(
    invocation_context: InvocationContext | None,
) -> Path | None:
    if invocation_context is None or invocation_context.workspace is None:
        return None
    return invocation_context.workspace.workspace_state_path


def prepare_runtime_for_attempt(plan: InvocationPlan) -> None:
    """Delete the plan's stale structured output before starting an attempt.

    Missing or unconfigured output is harmless; other deletion errors propagate.
    """
    prepare_structured_output_file(plan.structured_output_file)


def prepare_structured_output_file(path: Path | None) -> None:
    """Unlink configured structured output, ignoring absence but no other error."""
    if path is None:
        return
    path.unlink(missing_ok=True)


async def run_invocation_attempt(
    plan: InvocationPlan,
    command_runner: CommandRunner,
    log_file: Path | None,
    attempt: int,
    cwd: Path,
    invocation_context: InvocationContext | None,
    timeout_seconds: float | None,
    idle_timeout_seconds: float | None,
    child_environment: ChildProcessEnvironment | None,
) -> CommandResult:
    """Run a zero-based attempt, using one-based receipts and retry log headers.

    Attempt zero uses the plan header and truncates the log; retries append.
    Return the runner's result unchanged, including its capture-file cleanup
    obligations. Wall-clock timeout cancels and awaits the runner, emits a
    diagnostic, and raises RuntimeError. Other errors and cancellation propagate.
    """
    attempt_context = _context_for_attempt(invocation_context, attempt)
    attempt_result = command_runner(
        cmd=plan.cmd,
        stdin_data=plan.stdin_data,
        log_file=log_file,
        append_log=attempt > 0,
        log_header=_retry_log_header(plan, attempt),
        cwd=cwd,
        invocation_context=attempt_context,
        idle_timeout_seconds=idle_timeout_seconds,
        child_environment=child_environment,
    )
    return await _await_invocation_attempt(
        attempt_result=attempt_result,
        timeout_seconds=timeout_seconds,
        invocation_context=attempt_context,
        attempt=attempt,
    )


def _context_for_attempt(
    invocation_context: InvocationContext | None,
    zero_based_attempt: int,
) -> InvocationContext | None:
    if invocation_context is None:
        return None
    return replace(invocation_context, attempt_num=zero_based_attempt + 1)


def _retry_log_header(plan: InvocationPlan, attempt: int) -> bytes:
    if attempt == 0:
        return plan.log_header
    return build_retry_log_header(attempt + 1)


async def _await_invocation_attempt(
    attempt_result: Awaitable[CommandResult],
    timeout_seconds: float | None,
    invocation_context: InvocationContext | None,
    attempt: int,
) -> CommandResult:
    if timeout_seconds is None:
        return await attempt_result
    try:
        return await asyncio.wait_for(attempt_result, timeout=timeout_seconds)
    except TimeoutError as exc:
        formatted_timeout = format_timeout_seconds(timeout_seconds)
        message = (
            "Configured invocation wall-clock timeout reached after "
            f"{formatted_timeout}."
        )
        emit_invocation_diagnostic(
            invocation_context,
            level=LogLevel.ERROR,
            message=message,
            operation="invocation_timeout",
            attributes={
                "attempt": attempt + 1,
                "timeout_seconds": timeout_seconds,
                "timeout_scope": "wall_clock",
            },
        )
        raise RuntimeError(message) from exc


def _child_process_env(
    child_environment: ChildProcessEnvironment | None,
) -> dict[str, str] | None:
    if child_environment is None:
        return None
    env = dict(os.environ)
    for key in child_environment.unset:
        env.pop(key, None)
    env.update(child_environment.set)
    return env
