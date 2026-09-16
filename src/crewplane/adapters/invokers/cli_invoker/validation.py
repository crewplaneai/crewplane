from __future__ import annotations

from .capability import CliInvocationRequest
from .env_command import EnvCommandContext, parse_env_command_context


def reject_unsupported_reasoning(request: CliInvocationRequest) -> None:
    if request.requested_reasoning is not None:
        raise ValueError(
            "First-class reasoning requires the built-in CLI invoker with "
            "provider_kind 'codex' or 'claude'."
        )


def reasoning_command_context(
    request: CliInvocationRequest,
    tracked_environment_name: str = "",
) -> EnvCommandContext:
    context = parse_env_command_context(
        request.config.cli_cmd,
        request.environment.get(tracked_environment_name),
        tracked_environment_name,
    )
    if context.command_working_directory_option is not None:
        raise ValueError(
            f"{context.command_working_directory_option} cannot be combined "
            "with a workflow reasoning request."
        )
    if "--" in context.command_arguments:
        raise ValueError(
            "A first-class reasoning request cannot be appended after the "
            "cli_cmd option terminator."
        )
    return context
