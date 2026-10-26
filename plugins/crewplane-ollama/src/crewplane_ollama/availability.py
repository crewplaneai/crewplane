from __future__ import annotations

import asyncio
import os
from pathlib import Path

from crewplane.adapters.invokers.cli_invoker import (
    CliCommand,
    CliInvocationRequest,
    extract_strict_stdout,
    run_command_once,
    unconfirmed_process_cleanup,
)
from crewplane.architecture.contracts import ChildProcessEnvironment, CommandResult

from .command import (
    cleanup_temporary_captures,
    resolve_request_executable,
    validate_request,
)
from .settings import load_settings


async def check_availability(request: CliInvocationRequest) -> None:
    """Probe one model under one deadline, retaining subprocess cleanup evidence."""
    validate_request(request)
    try:
        resolved = resolve_request_executable(request)
    except OSError as exc:
        raise ValueError(
            f"Ollama provider '{request.config.provider_kind}', model {request.model!r}, executable resolution failed: {str(exc)[:1000]}"
        ) from exc
    assert resolved.model is not None
    settings = load_settings(request.working_directory or Path.cwd())
    timeout_seconds = settings.readiness_timeout_seconds
    deadline = asyncio.timeout(timeout_seconds)
    try:
        async with deadline:
            await _probe_command(
                resolved,
                CliCommand(
                    [resolved.config.cli_cmd[0], "--check", "--model", resolved.model],
                    None,
                ),
            )
    except (RuntimeError, TimeoutError) as exc:
        if unconfirmed_process_cleanup(exc) is not None:
            raise
        if isinstance(exc, TimeoutError) and not deadline.expired():
            raise
        detail = (
            f"deadline exceeded (readiness_timeout_seconds={timeout_seconds:g}s)"
            if isinstance(exc, TimeoutError)
            else str(exc)
        )
        raise ValueError(
            f"Ollama provider '{request.config.provider_kind}', model {request.model!r}, readiness failed: {detail[:1000]}"
        ) from exc


async def _probe_command(request: CliInvocationRequest, command: CliCommand) -> None:
    result = await run_command_once(
        cmd=command.cmd,
        stdin_data=command.stdin_data,
        log_file=None,
        append_log=False,
        log_header=None,
        cwd=request.working_directory or Path.cwd(),
        invocation_context=None,
        idle_timeout_seconds=None,
        child_environment=ChildProcessEnvironment(
            set=request.environment,
            unset=tuple(os.environ.keys() - request.environment.keys()),
        ),
    )
    try:
        if result.returncode != 0:
            raise RuntimeError(
                f"exit {result.returncode}: {_diagnostic_detail(result)}"
            )
        if extract_strict_stdout(result, None).output_extraction_status != "success":
            raise RuntimeError(
                f"no nonblank stdout answer: {_diagnostic_detail(result)}"
            )
    finally:
        cleanup_temporary_captures(result)


def _diagnostic_detail(result: CommandResult) -> str:
    # Inline tails are bounded by the shared runner; do not read whole captures.
    return (result.stderr_text or result.stdout_text)[-1000:]
