from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Awaitable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import BinaryIO, cast

from crewplane.architecture.contracts import (
    ChildProcessEnvironment,
    CommandResult,
    CommandRunner,
    InvocationContext,
    InvocationDiagnosticSink,
    InvocationPlan,
    InvocationProcessEvent,
)
from crewplane.core.platform import supports_posix_process_groups
from crewplane.runtime.workspace import mutator_fence as workspace_mutator_fence
from crewplane.runtime.workspace import state as workspace_state

from ..process.drain import ProcessDrainError
from ..process.runner import (
    build_retry_log_header,
    close_log_handle,
    reap_failed_process,
    write_stdin_and_collect_output,
)
from ..process.stream_capture import ProcessOutputCapture
from ..workspace_environment import record_workspace_child_environment_applied
from .state import InvocationCommandRuntime
from .telemetry import emit_invocation_diagnostic


@dataclass
class _CommandLifecycle:
    invocation_context: InvocationContext | None
    process: asyncio.subprocess.Process | None = None
    process_group_id: int | None = None
    output_capture: ProcessOutputCapture | None = None
    log_handle: BinaryIO | None = None

    @property
    def diagnostic_sink(self) -> InvocationDiagnosticSink | None:
        if self.invocation_context is None:
            return None
        return self.invocation_context.diagnostics


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
    if log_file is None:
        return None
    log_file.parent.mkdir(parents=True, exist_ok=True)
    mode = "ab" if append else "wb"
    handle = cast(BinaryIO, log_file.open(mode))
    if header_bytes:
        handle.write(header_bytes)
        handle.flush()
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
    lifecycle = _CommandLifecycle(invocation_context)
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
        raise
    except Exception as exc:
        drain_error = await _handle_failed_command(lifecycle, exc)
        if drain_error is not None:
            raise drain_error from exc
        if isinstance(exc, RuntimeError):
            raise
        raise RuntimeError(f"Execution error: {exc}") from exc
    finally:
        active_exception = sys.exception()
        close_log_handle(lifecycle.log_handle)
        try:
            _emit_process_exit(
                invocation_context,
                lifecycle.process,
                lifecycle.process_group_id,
            )
        except Exception as exc:
            if active_exception is None:
                if lifecycle.output_capture is not None:
                    lifecycle.output_capture.cleanup()
                raise
            active_exception.add_note(f"Provider process exit reporting failed: {exc}")

    process = cast(asyncio.subprocess.Process, lifecycle.process)
    output_capture = cast(ProcessOutputCapture, lifecycle.output_capture)
    if process.returncode is None:
        raise RuntimeError("Provider process finished without a return code.")
    return CommandResult(
        returncode=process.returncode,
        stdout_text=output_capture.stdout_tail.decode(errors="replace"),
        stderr_text=output_capture.stderr_tail.decode(errors="replace"),
        stdout_path=output_capture.stdout.path,
        stderr_path=output_capture.stderr.path,
    )


async def _execute_command(
    lifecycle: _CommandLifecycle,
    request: _CommandExecutionRequest,
) -> None:
    start_new_session = supports_posix_process_groups()
    process = await asyncio.create_subprocess_exec(
        *request.cmd,
        stdin=(
            asyncio.subprocess.PIPE
            if request.stdin_data
            else asyncio.subprocess.DEVNULL
        ),
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
    _emit_process_started(
        lifecycle.invocation_context,
        process.pid,
        lifecycle.process_group_id,
    )
    lifecycle.log_handle = open_log_handle(
        request.log_file,
        append=request.append_log,
        header_bytes=request.log_header,
    )
    lifecycle.output_capture = await write_stdin_and_collect_output(
        process,
        request.stdin_data,
        lifecycle.log_handle,
        lifecycle.diagnostic_sink,
        lifecycle.process_group_id,
        request.idle_timeout_seconds,
    )
    _record_process_drain_success(
        lifecycle.invocation_context,
        process,
        lifecycle.process_group_id,
    )


async def _handle_failed_command(
    lifecycle: _CommandLifecycle,
    failure: BaseException,
) -> ProcessDrainError | None:
    drain_error = await _cleanup_failed_command(lifecycle)
    if drain_error is None and isinstance(failure, ProcessDrainError):
        drain_error = failure
    _record_process_drain_outcome(
        lifecycle.invocation_context,
        lifecycle.process,
        lifecycle.process_group_id,
        drain_error,
    )
    return drain_error


async def _cleanup_failed_command(
    lifecycle: _CommandLifecycle,
) -> ProcessDrainError | None:
    if lifecycle.process is not None:
        try:
            await reap_failed_process(
                lifecycle.process,
                lifecycle.process_group_id,
                lifecycle.diagnostic_sink,
            )
        except ProcessDrainError as exc:
            if lifecycle.output_capture is not None:
                lifecycle.output_capture.cleanup()
            return exc
    if lifecycle.output_capture is not None:
        lifecycle.output_capture.cleanup()
    return None


def _record_process_drain_success(
    invocation_context: InvocationContext | None,
    process: asyncio.subprocess.Process,
    process_group_id: int | None,
) -> None:
    state_path = _workspace_state_path(invocation_context)
    if state_path is None:
        return
    workspace_state.record_workspace_process_drain(
        state_path,
        "confirmed",
        process.pid,
        process_group_id,
    )
    workspace_mutator_fence.release_workspace_mutator(state_path)


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
    _record_unresolved_process_drain(invocation_context, error)


def _record_unresolved_process_drain(
    invocation_context: InvocationContext | None,
    error: ProcessDrainError,
) -> None:
    state_path = _workspace_state_path(invocation_context)
    if state_path is None:
        return
    workspace_mutator_fence.fence_workspace_mutator(state_path)
    try:
        workspace_state.record_workspace_process_drain(
            state_path,
            "unresolved",
            error.evidence.pid,
            error.evidence.process_group_id,
            str(error),
        )
    except Exception as persistence_error:
        error.add_note(
            f"Workspace process-drain evidence persistence failed: {persistence_error}"
        )


def _workspace_state_path(
    invocation_context: InvocationContext | None,
) -> Path | None:
    if invocation_context is None or invocation_context.workspace is None:
        return None
    return invocation_context.workspace.workspace_state_path


def build_invocation_runtime(plan: InvocationPlan) -> InvocationCommandRuntime:
    return InvocationCommandRuntime(
        failure_profile=plan.failure_profile,
        output_extractor=plan.output_extractor,
        usage_decoder=plan.usage_decoder,
        quota_parser=plan.quota_parser,
        structured_output_file=plan.structured_output_file,
        cmd=plan.cmd,
        stdin_data=plan.stdin_data,
        log_header=plan.log_header,
        one_shot_failure_retry=plan.one_shot_failure_retry,
    )


def prepare_runtime_for_attempt(runtime: InvocationCommandRuntime) -> None:
    prepare_structured_output_file(runtime.structured_output_file)


def prepare_structured_output_file(path: Path | None) -> None:
    if path is None:
        return
    path.unlink(missing_ok=True)


def cleanup_structured_output_file(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink()
    except FileNotFoundError:
        return


async def run_invocation_attempt(
    runtime: InvocationCommandRuntime,
    command_runner: CommandRunner,
    log_file: Path | None,
    attempt: int,
    cwd: Path,
    invocation_context: InvocationContext | None,
    timeout_seconds: float | None,
    idle_timeout_seconds: float | None,
    child_environment: ChildProcessEnvironment | None,
) -> CommandResult:
    attempt_context = _context_for_attempt(invocation_context, attempt)
    attempt_result = command_runner(
        cmd=runtime.cmd,
        stdin_data=runtime.stdin_data,
        log_file=log_file,
        append_log=attempt > 0,
        log_header=_retry_log_header(runtime, attempt),
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


def _emit_process_exit(
    invocation_context: InvocationContext | None,
    process: asyncio.subprocess.Process | None,
    process_group_id: int | None,
) -> None:
    if invocation_context is None or process is None or process.returncode is None:
        return
    _emit_process_event(
        invocation_context,
        InvocationProcessEvent(
            attempt=invocation_context.attempt_num,
            pid=process.pid,
            process_group_id=process_group_id,
            status="exited",
            returncode=process.returncode,
        ),
    )


def _emit_process_started(
    invocation_context: InvocationContext | None,
    pid: int,
    process_group_id: int | None,
) -> None:
    if invocation_context is None:
        return
    _emit_process_event(
        invocation_context,
        InvocationProcessEvent(
            attempt=invocation_context.attempt_num,
            pid=pid,
            process_group_id=process_group_id,
            status="started",
        ),
    )


def _emit_process_event(
    invocation_context: InvocationContext,
    event: InvocationProcessEvent,
) -> None:
    if invocation_context.process_event_sink is None:
        return
    try:
        invocation_context.process_event_sink(event)
    except Exception as exc:
        raise RuntimeError(
            f"Provider process {event.status} reporting failed: {exc}"
        ) from exc


def _retry_log_header(runtime: InvocationCommandRuntime, attempt: int) -> bytes:
    if attempt == 0:
        return runtime.log_header
    return build_retry_log_header(attempt + 1)


def _format_timeout_seconds(timeout_seconds: float) -> str:
    return f"{timeout_seconds:g}s"


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
        formatted_timeout = _format_timeout_seconds(timeout_seconds)
        message = (
            "Configured invocation wall-clock timeout reached after "
            f"{formatted_timeout}."
        )
        emit_invocation_diagnostic(
            invocation_context,
            level="error",
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
