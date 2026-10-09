from __future__ import annotations

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
    cmd[0] = request.command_strategy.prepare_executable(cmd[0], request)
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
