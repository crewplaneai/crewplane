from __future__ import annotations

from functools import partial

from crewplane.architecture.contracts import ProviderKind

from ..capability import CliInvocationRequest, CliProviderCapability
from ..commands import build_standard_command
from ..env_command import parse_env_command_context
from ..streaming import extract_strict_stdout
from ..validation import reject_unsupported_reasoning

_PERMISSION_ENV = "DSH_PERMISSION_MODE"
_PERMISSION_MODE = "danger-full-access"


def validate_deepseek_request(request: CliInvocationRequest) -> None:
    reject_unsupported_reasoning(request)
    if request.model is not None:
        raise ValueError(
            "DeepSeek requires native model configuration; omit Crewplane model values."
        )
    config = request.config
    if config.prompt_transport != "argv" or config.prompt_transport_arg != "--":
        raise ValueError(
            'DeepSeek requires argv transport with prompt_transport_arg: "--".'
        )
    context = parse_env_command_context(
        config.cli_cmd, request.environment.get(_PERMISSION_ENV), _PERMISSION_ENV
    )
    if context.command_executable is None:
        raise ValueError("DeepSeek requires an executable after the env wrapper.")
    if context.tracked_environment_value != _PERMISSION_MODE:
        raise ValueError(
            "DeepSeek requires effective DSH_PERMISSION_MODE=danger-full-access."
        )
    _validate_launcher_arguments((*context.command_arguments, *config.extra_args))


def _validate_launcher_arguments(arguments: tuple[str, ...]) -> None:
    tokens = iter(enumerate(arguments, start=1))
    has_headless_profile = False
    for position, token in tokens:
        option, separator, inline_value = token.partition("=")
        if option not in {"--profile", "--from-default-profile", "--patch"}:
            raise ValueError(
                f"DeepSeek launcher argument at position {position} "
                "conflicts with managed headless mode."
            )
        value = inline_value if separator else next(tokens, (0, None))[1]
        if not value or value == "--":
            raise ValueError(
                f"DeepSeek option {option!r} requires a value before the task separators."
            )
        if option in {"--profile", "--from-default-profile"} and value != "headless":
            raise ValueError(f"DeepSeek option {option!r} must select headless.")
        has_headless_profile |= option == "--profile"
    if not has_headless_profile:
        raise ValueError("DeepSeek requires --profile headless.")


DEEPSEEK = CliProviderCapability(
    provider_kind=ProviderKind.DEEPSEEK,
    validate_request=validate_deepseek_request,
    # The launcher consumes the first '--'; the headless parser needs the second.
    build_command=partial(
        build_standard_command, structured_args=("--",), model_arg=None
    ),
    output_extractor=extract_strict_stdout,
)
