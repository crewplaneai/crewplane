from __future__ import annotations

import math
import os
import shutil
from dataclasses import replace
from pathlib import Path

from crewplane.adapters.invokers.cli_invoker import CliCommand, CliInvocationRequest
from crewplane.architecture.contracts import CommandResult


def cleanup_temporary_captures(result: CommandResult) -> None:
    try:
        if result.stdout_path is not None:
            result.stdout_path.unlink(missing_ok=True)
    finally:
        if result.stderr_path is not None:
            result.stderr_path.unlink(missing_ok=True)


def validate_request(request: CliInvocationRequest) -> None:
    """Validate the managed agent contract without probing the executable or server."""
    if request.model is None or not request.model.strip():
        raise ValueError("Ollama requires a nonblank effective model")
    if request.model.startswith("-") or "\0" in request.model:
        raise ValueError("Ollama model cannot start with '-' or contain NUL")
    if request.requested_reasoning is not None:
        raise ValueError("Ollama does not support first-class reasoning")
    config = request.config
    guidance = "Regenerate or deliberately reconfigure the Ollama profile for crewplane-ollama-agent; legacy text-only profiles are not migrated."
    if config.prompt_transport != "stdin" or config.prompt_transport_arg is not None:
        raise ValueError(
            "Ollama requires stdin prompt transport without prompt_transport_arg. "
            + guidance
        )
    if (
        len(config.cli_cmd) != 1
        or not config.cli_cmd[0].strip()
        or "\0" in config.cli_cmd[0]
    ):
        raise ValueError(
            "Ollama cli_cmd must contain exactly one executable, without command tails or flags. "
            + guidance
        )
    if config.extra_args:
        raise ValueError(
            "Ollama requires empty extra_args; managed flags cannot be overridden. "
            + guidance
        )
    timeout = config.invocation_timeout_seconds
    if timeout is None or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError(
            "Ollama requires a finite positive invocation_timeout_seconds. " + guidance
        )


def build_command(request: CliInvocationRequest, prompt: str) -> CliCommand:
    validate_request(request)
    request = resolve_request_executable(request)
    assert request.model is not None
    return CliCommand(
        [request.config.cli_cmd[0], "--model", request.model],
        prompt.encode("utf-8"),
    )


def resolve_request_executable(request: CliInvocationRequest) -> CliInvocationRequest:
    """Resolve the configured executable against the invocation cwd and environment."""
    cwd = request.working_directory or Path.cwd()
    executable = request.config.cli_cmd[0]
    path = Path(executable)
    if not path.is_absolute() and "/" not in executable and "\\" not in executable:
        search_path = os.pathsep.join(
            str(cwd / entry)
            for entry in request.environment.get("PATH", os.defpath).split(os.pathsep)
        )
        found = shutil.which(executable, path=search_path)
        if found is None:
            raise FileNotFoundError(f"executable {executable!r} not found in PATH")
        path = Path(found)
    resolved = (cwd / path).resolve(strict=True)
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise OSError(
            f"executable {executable!r} must resolve to an executable regular file"
        )
    return replace(
        request,
        config=request.config.model_copy(
            update={"cli_cmd": [str(resolved), *request.config.cli_cmd[1:]]}
        ),
    )
