from __future__ import annotations

import asyncio
import os
import tempfile
from collections.abc import Awaitable
from pathlib import Path
from typing import BinaryIO

from orchestrator_cli.core.config import AgentConfig

from ..command_builder import ProviderKind, build_command, provider_kind
from ..process.runner import (
    build_log_header,
    build_retry_log_header,
    close_log_handle,
    collect_process_output,
    reap_failed_process,
    write_stdin,
)
from ..types import CommandResult, CommandRunner, InvocationContext
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
    invocation_context: InvocationContext | None,
    idle_timeout_seconds: float | None,
) -> CommandResult:
    log_handle: BinaryIO | None = None
    process: asyncio.subprocess.Process | None = None
    process_group_id: int | None = None
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
            **process_kwargs,
        )
        process_group_id = process.pid if os.name == "posix" else None
        log_handle = open_log_handle(
            log_file,
            append=append_log,
            header_bytes=log_header,
        )
        await write_stdin(process, stdin_data)
        stdout_bytes, stderr_bytes = await collect_process_output(
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
        raise
    except Exception as exc:
        if process is not None:
            await reap_failed_process(process, process_group_id, diagnostic_sink)
        if isinstance(exc, RuntimeError):
            raise
        raise RuntimeError(f"Execution error: {exc}") from exc
    finally:
        close_log_handle(log_handle)

    if process.returncode is None:
        raise RuntimeError("Provider process finished without a return code.")
    return CommandResult(
        returncode=process.returncode,
        stdout_text=stdout_bytes.decode(errors="replace"),
        stderr_text=stderr_bytes.decode(errors="replace"),
    )


def build_invocation_runtime(
    config: AgentConfig,
    model: str | None,
    prompt: str,
    output_file: Path,
) -> InvocationCommandRuntime:
    resolved_provider_kind = provider_kind(config.cli_cmd[0])
    structured_output_file = _structured_output_file(resolved_provider_kind)
    cmd = build_command(
        config,
        model,
        prompt,
        structured_output_file=structured_output_file,
    )
    return InvocationCommandRuntime(
        provider_kind=resolved_provider_kind,
        structured_output_file=structured_output_file,
        cmd=cmd,
        stdin_data=prompt.encode() if config.use_stdin else None,
        log_header=build_log_header(
            cli_executable=cmd[0],
            model=model,
            output_file=output_file,
        ),
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
    invocation_context: InvocationContext | None,
    timeout_seconds: float | None,
    idle_timeout_seconds: float | None,
) -> CommandResult:
    attempt_result = command_runner(
        cmd=runtime.cmd,
        stdin_data=runtime.stdin_data,
        log_file=log_file,
        append_log=attempt > 0,
        log_header=_retry_log_header(runtime, attempt),
        invocation_context=invocation_context,
        idle_timeout_seconds=idle_timeout_seconds,
    )
    return await _await_invocation_attempt(
        attempt_result=attempt_result,
        timeout_seconds=timeout_seconds,
        invocation_context=invocation_context,
        attempt=attempt,
    )


def _structured_output_file(
    provider_kind: ProviderKind,
) -> Path | None:
    if provider_kind != "codex":
        return None
    file_descriptor, temp_path = tempfile.mkstemp(
        prefix="orchestrator-codex-",
        suffix=".last-message.txt",
    )
    os.close(file_descriptor)
    Path(temp_path).unlink(missing_ok=True)
    return Path(temp_path)


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
        emit_invocation_diagnostic(
            invocation_context,
            level="error",
            message=f"Provider invocation timed out after {formatted_timeout}.",
            operation="invocation_timeout",
            attributes={
                "attempt": attempt + 1,
                "timeout_seconds": timeout_seconds,
            },
        )
        raise RuntimeError(
            f"Provider invocation timed out after {formatted_timeout}."
        ) from exc
