from __future__ import annotations

import os
import shutil
from pathlib import Path

from .capability import CliCommand, CliInvocationRequest


def build_standard_command(
    request: CliInvocationRequest,
    prompt: str,
    structured_args: tuple[str, ...] = (),
    reasoning_args: tuple[str, ...] = (),
    model_arg: str | None = "--model",
) -> CliCommand:
    config = request.config
    cmd = config.get_command()
    cmd[0] = resolved_cli_executable(cmd[0])
    if model_arg is not None and request.model is not None:
        cmd.extend([model_arg, request.model])
    cmd.extend(reasoning_args)
    cmd.extend(config.extra_args)
    cmd.extend(structured_args)
    if config.prompt_transport == "stdin":
        if config.prompt_transport_arg:
            cmd.append(config.prompt_transport_arg)
        return CliCommand(cmd, prompt.encode("utf-8"))
    if config.prompt_transport_arg is None:
        raise ValueError("prompt_transport_arg is required for argv prompt transport.")
    cmd.extend([config.prompt_transport_arg, prompt])
    return CliCommand(cmd, None)


def resolved_cli_executable(executable: str) -> str:
    """Return an executable path suitable for subprocess invocation.

    Bare executable names are resolved through `PATH` when available. Missing
    bare names are preserved so injected command runners and subprocess launch
    handle the execution boundary consistently. Absolute paths are validated
    directly. Relative path-like commands are preserved so subprocess can
    resolve them relative to the configured working directory.
    """
    executable_path = Path(executable)
    if executable_path.is_absolute():
        return _resolved_existing_executable(executable_path)
    if _contains_path_separator(executable):
        return executable
    resolved = shutil.which(executable)
    if resolved is None:
        return executable
    return _resolved_existing_executable(Path(resolved))


def _resolved_existing_executable(executable: Path) -> str:
    """Resolve an existing executable path and fail clearly if it is unusable."""
    resolved = executable.resolve(strict=True)
    if not resolved.is_file():
        raise FileNotFoundError(
            f"CLI executable '{executable.as_posix()}' is not a file."
        )
    if not os.access(resolved, os.X_OK):
        raise PermissionError(
            f"CLI executable '{resolved.as_posix()}' is not executable."
        )
    return resolved.as_posix()


def _contains_path_separator(value: str) -> bool:
    return "/" in value or "\\" in value
