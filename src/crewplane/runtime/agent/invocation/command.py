from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable
from pathlib import Path
from typing import BinaryIO

from crewplane.architecture.contracts import (
    ChildProcessEnvironment,
    CommandResult,
    CommandRunner,
    InvocationContext,
    InvocationPlan,
)

from ..process.runner import (
    build_retry_log_header,
    close_log_handle,
    collect_process_output,
    reap_failed_process,
    write_stdin,
)
from ..process.stream_capture import ProcessOutputCapture
from ..workspace_environment import record_workspace_child_environment_applied
from .state import InvocationCommandRuntime
from .telemetry import emit_invocation_diagnostic


def open_log_handle(
    log_file: Path | None,
    append: bool,
    header_bytes: bytes | None = None,
) -> BinaryIO | None:
    if log_file is None:
        return None
    log_file.parent.mkdir(parents=True, exist_ok=True)
    mode = "ab" if append else "wb"
    handle = log_file.open(mode)
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
    log_handle: BinaryIO | None = None
    process: asyncio.subprocess.Process | None = None
    process_group_id: int | None = None
    output_capture: ProcessOutputCapture | None = None
    diagnostic_sink = (
        invocation_context.diagnostics if invocation_context is not None else None
    )
    try:
        process_kwargs = {"start_new_session": True} if os.name == "posix" else {}
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE if stdin_data else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=_child_process_env(child_environment),
            **process_kwargs,
        )
        record_workspace_child_environment_applied(
            invocation_context,
            child_environment,
        )
        # start_new_session=True makes the child a session leader on POSIX; read
        # the actual process group after spawn so cleanup does not assume pid==pgid.
        process_group_id = os.getpgid(process.pid) if os.name == "posix" else None
        log_handle = open_log_handle(
            log_file,
            append=append_log,
            header_bytes=log_header,
        )
        await write_stdin(process, stdin_data)
        output_capture = await collect_process_output(
            process,
            log_handle,
            diagnostic_sink,
            process_group_id,
            idle_timeout_seconds,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"CLI executable not found: {cmd[0]}") from exc
    except asyncio.CancelledError:
        if process is not None:
            await reap_failed_process(process, process_group_id, diagnostic_sink)
        if output_capture is not None:
            output_capture.cleanup()
        raise
    except Exception as exc:
        if process is not None:
            await reap_failed_process(process, process_group_id, diagnostic_sink)
        if isinstance(exc, RuntimeError):
            if output_capture is not None:
                output_capture.cleanup()
            raise
        if output_capture is not None:
            output_capture.cleanup()
        raise RuntimeError(f"Execution error: {exc}") from exc
    finally:
        close_log_handle(log_handle)

    if process.returncode is None:
        raise RuntimeError("Provider process finished without a return code.")
    return CommandResult(
        returncode=process.returncode,
        stdout_text=output_capture.stdout_tail.decode(errors="replace"),
        stderr_text=output_capture.stderr_tail.decode(errors="replace"),
        stdout_path=output_capture.stdout.path,
        stderr_path=output_capture.stderr.path,
    )


def build_invocation_runtime(plan: InvocationPlan) -> InvocationCommandRuntime:
    return InvocationCommandRuntime(
        failure_profile=plan.failure_profile,
        structured_output_mode=plan.structured_output_mode,
        output_extraction_mode=plan.output_extraction_mode,
        quota_parser=plan.quota_parser,
        usage_parser=plan.usage_parser,
        structured_output_file=plan.structured_output_file,
        cmd=plan.cmd,
        stdin_data=plan.stdin_data,
        log_header=plan.log_header,
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
    attempt_result = command_runner(
        cmd=runtime.cmd,
        stdin_data=runtime.stdin_data,
        log_file=log_file,
        append_log=attempt > 0,
        log_header=_retry_log_header(runtime, attempt),
        cwd=cwd,
        invocation_context=invocation_context,
        idle_timeout_seconds=idle_timeout_seconds,
        child_environment=child_environment,
    )
    return await _await_invocation_attempt(
        attempt_result=attempt_result,
        timeout_seconds=timeout_seconds,
        invocation_context=invocation_context,
        attempt=attempt,
    )


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
