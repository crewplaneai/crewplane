from __future__ import annotations

import os
import shutil
from dataclasses import replace
from pathlib import Path

from crewplane.adapters.invokers.cli_invoker import CliCommand, CliInvocationRequest
from crewplane.architecture.contracts import CommandResult

_VALUE_OPTIONS = frozenset({"--keepalive", "--format"})
_BOOL_OPTIONS = frozenset({"--verbose", "--insecure"})
_MANAGED_OPTIONS = frozenset({"--nowordwrap", "--hidethinking"})


def cleanup_temporary_captures(result: CommandResult) -> None:
    try:
        if result.stdout_path is not None:
            result.stdout_path.unlink(missing_ok=True)
    finally:
        if result.stderr_path is not None:
            result.stderr_path.unlink(missing_ok=True)


def validate_request(request: CliInvocationRequest) -> None:
    """Validate text-only stdin transport before constructing a command."""
    if request.model is None or not request.model.strip():
        raise ValueError("Ollama requires a nonblank effective model")
    if request.model.startswith("-"):
        raise ValueError("Ollama model cannot start with '-' (an option)")
    if request.requested_reasoning is not None:
        raise ValueError("Ollama does not support first-class reasoning")
    config = request.config
    if config.prompt_transport != "stdin" or config.prompt_transport_arg is not None:
        raise ValueError(
            "Ollama requires stdin prompt transport without prompt_transport_arg"
        )
    if len(config.cli_cmd) < 2 or config.cli_cmd[1] != "run":
        raise ValueError(
            "Ollama cli_cmd must start with the configured executable and 'run'"
        )
    _validate_options(config.cli_cmd[2:])
    _validate_options(config.extra_args)


def _validate_options(arguments: list[str]) -> None:
    tokens = iter(arguments)
    for token in tokens:
        name, separator, value = token.partition("=")
        if name in _MANAGED_OPTIONS:
            raise ValueError(f"Ollama option {name} conflicts with a managed flag")
        if name in _VALUE_OPTIONS:
            if not separator:
                value = next(tokens, "")
            if not value.strip() or value.startswith("--"):
                raise ValueError(f"Ollama option {name} requires a value")
        elif name in _BOOL_OPTIONS:
            if separator and value not in {"true", "false"}:
                raise ValueError(f"Ollama option {name} requires true or false")
        elif name == "--think":
            if separator and value not in {"true", "false", "high", "medium", "low"}:
                raise ValueError(
                    "Ollama --think requires true, false, high, medium, or low"
                )
        else:
            raise ValueError(
                f"Ollama rejects unsupported options or competing positional model/prompt arguments: {token!r}"
            )


def build_command(request: CliInvocationRequest, prompt: str) -> CliCommand:
    validate_request(request)
    request = resolve_request_executable(request)
    assert request.model is not None
    return CliCommand(
        [
            *request.config.cli_cmd[:2],
            request.model,
            "--nowordwrap",
            "--hidethinking",
            *request.config.cli_cmd[2:],
            *request.config.extra_args,
        ],
        prompt.encode("utf-8"),
    )


def resolve_request_executable(request: CliInvocationRequest) -> CliInvocationRequest:
    """Resolve symlinks to avoid Ollama's macOS app auto-launch path."""
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
