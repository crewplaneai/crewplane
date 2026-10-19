from pathlib import Path
from typing import assert_type

from crewplane.adapters.invokers.cli_invoker import (
    CliCommand,
    CliInvocationRequest,
    CliProviderCapability,
    extract_strict_stdout,
    run_command_once,
    unconfirmed_process_cleanup,
)
from crewplane.architecture.contracts import CommandResult


def validate(request: CliInvocationRequest) -> None:
    if request.model is None:
        raise ValueError("model required")


def command(request: CliInvocationRequest, prompt: str) -> CliCommand:
    return CliCommand(list(request.config.cli_cmd), prompt.encode("utf-8"))


async def availability(request: CliInvocationRequest) -> None:
    result = await run_command_once(
        cmd=[*request.config.cli_cmd, "--version"],
        stdin_data=None,
        log_file=None,
        append_log=False,
        log_header=None,
        cwd=request.working_directory or Path.cwd(),
        invocation_context=None,
        idle_timeout_seconds=1.0,
    )
    assert_type(result, CommandResult)
    try:
        if result.returncode:
            raise ValueError("unavailable")
    finally:
        for path in (result.stdout_path, result.stderr_path):
            if path is not None:
                path.unlink(missing_ok=True)
    assert unconfirmed_process_cleanup(None) is None


CAPABILITY = CliProviderCapability(
    provider_kind="external",
    validate_request=validate,
    build_command=command,
    output_extractor=extract_strict_stdout,
    check_availability=availability,
)
